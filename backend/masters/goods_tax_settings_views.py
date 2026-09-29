"""Setup > Tax Settings (store operations PRD §6, ticket 03).

Under ``/api/goods-v1/masters/``:

``GET  tax-settings``           every version, newest first, and which stores in
                                the caller's scope are taxed by them
                                (``setup: view`` - Accounts reads it)
``POST tax-settings/versions``  save a new version (Admin only)

Only Admin saves: ``setup: manage`` *and* a role code in
``accounts.role_lists.TAX_SETTING_EDITOR_ROLES`` (``it_admin``), with a scope
covering every store, because a version applies company-wide wherever the switch
is on. Saving is new work, so it needs the ``tax-settings`` switch on at one or
more stores in scope (ticket 01's rule); reading never does.

A save never changes an old version. It adds the next number with the date it
applies from, which must be today or later (India): the overall PRD (§8.4) asks
for an approval and impact analysis before backdating, and none is built, so a
past date is refused. It must also be on or after the newest version's date, so
the order of versions and the order of their dates never disagree. Each save is
one command, so its ``AuditEvent`` records who, when, and the version before and
after, with ``version`` and ``applies_from`` for the Audit Log's version chip.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, check_revision, parse_meta
from accounts.goods_models import HumanIdentity
from accounts.permissions import user_can
from accounts.role_lists import TAX_SETTING_EDITOR_ROLES
from accounts.sections import CAP_MANAGE, CAP_VIEW
from core.commands import CommandResult, CommandRun, LockRank
from core.refusals import Refusal, issue
from masters.scoping import actionable_store_ids, actionable_stores
from masters.store_feature_registry import StoreFeature
from masters.store_features import feature, real_stores, switch_states
from masters.tax_setting_models import TaxSettingVersion
from masters.tax_settings import (
    FEATURE_KEY,
    LEGACY_VERSION,
    as_saved,
    default_options,
    in_force,
    latest_version_number,
    legacy_rules,
    options_of,
    parse_options,
    parse_rate,
    parse_rules,
    rate_text,
)

SAVE_ACTION = "masters.tax_setting.save"


def may_change_tax_settings(user: Any) -> bool:
    """Admin only, with a company-wide scope (or break-glass)."""
    if getattr(user, "is_superuser", False):
        return True
    code = getattr(getattr(user, "role", None), "code", "")
    return (
        user_can(user, "setup", CAP_MANAGE)
        and code in TAX_SETTING_EDITOR_ROLES
        and actionable_store_ids(user) is None
    )


class TaxRuleSerializer(serializers.Serializer[Any]):
    kind = serializers.CharField(
        help_text="price_line: the lower rate at or under the price line; flat_rate: the "
        "rate schedule, one rate for the HSN whatever the price (ticket 12)."
    )
    hsn_prefix = serializers.CharField(allow_blank=True)
    name = serializers.CharField()
    threshold_paise = serializers.IntegerField()
    rate_below = serializers.CharField()
    rate_above = serializers.CharField()
    #: A flat_rate rule's one rate (its price line is 0 and both rates equal it).
    rate = serializers.CharField(required=False)
    #: Version 1 only: each slab row carries its own date.
    effective_from = serializers.DateField(required=False)


class TaxRuleRequestSerializer(serializers.Serializer[Any]):
    """A rule as Admin submits it: a price line, or a flat rate (``rate`` only)."""

    kind = serializers.ChoiceField(choices=["price_line", "flat_rate"])
    hsn_prefix = serializers.CharField(allow_blank=True)
    name = serializers.CharField()
    threshold_paise = serializers.IntegerField(required=False)
    rate_below = serializers.CharField(required=False)
    rate_above = serializers.CharField(required=False)
    rate = serializers.CharField(required=False)


class TaxOptionsSerializer(serializers.Serializer[Any]):
    """A version's non-rate choices (tickets 11, 13, 14). Every key is always present."""

    round_total_paise = serializers.IntegerField(
        help_text="The invoice total is rounded to this many paise: 100 is the nearest "
        "rupee (the baseline), 1 is no rounding."
    )
    cross_gstin_returns = serializers.BooleanField(
        required=False,
        help_text="Ticket 13: may a store take back a bill another GSTIN issued? Off is the "
        "baseline (CA to confirm): such a return is refused, naming where it can go.",
    )
    annual_return_filed = serializers.DictField(
        child=serializers.DictField(child=serializers.CharField()),
        required=False,
        help_text='Ticket 13: {gstin: {"25-26": "YYYY-MM-DD"}} - the day each annual return '
        "was filed, as Accounts recorded it. A credit note reduces tax only up to 30 "
        "November after the year, or this date when it is earlier.",
    )
    gift_with_purchase_is_gift = serializers.BooleanField(
        required=False,
        help_text="Ticket 14: is a gift with purchase gift stock, whose input tax credit is "
        "reversed, rather than part of the sale price? Yes is the baseline (CA to confirm). "
        "A piece given free with no gift offer is gift stock either way.",
    )


class TaxVersionSerializer(serializers.Serializer[Any]):
    version = serializers.IntegerField()
    applies_from = serializers.DateField(allow_null=True)
    legacy = serializers.BooleanField(help_text="Version 1: the tax slab table.")
    saved_at = serializers.DateTimeField(allow_null=True)
    saved_by = serializers.CharField(allow_blank=True)
    note = serializers.CharField(allow_blank=True)
    rules = TaxRuleSerializer(many=True)
    unmatched_rate = serializers.CharField(allow_null=True)
    options = TaxOptionsSerializer()


class TaxStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    real = serializers.BooleanField()
    rules_on = serializers.BooleanField()
    locked_reason = serializers.CharField(allow_null=True)
    version_today = serializers.IntegerField()


class TaxSettingsSerializer(serializers.Serializer[Any]):
    versions = TaxVersionSerializer(many=True)
    latest_version = serializers.IntegerField()
    today = serializers.DateField()
    gate = serializers.CharField(allow_null=True)
    stores = TaxStoreSerializer(many=True)
    can_change = serializers.BooleanField()


class TaxVersionRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField(
        help_text="The newest version number the form was built from."
    )
    applies_from = serializers.DateField()
    rules = TaxRuleRequestSerializer(many=True)
    unmatched_rate = serializers.CharField()
    note = serializers.CharField()
    options = TaxOptionsSerializer(required=False)


def _tax_feature() -> StoreFeature:
    return feature(FEATURE_KEY)


def _saved_json(row: TaxSettingVersion, names: dict[Any, str]) -> dict[str, Any]:
    return {
        "version": row.version,
        "applies_from": row.applies_from,
        "legacy": False,
        "saved_at": row.created_at,
        "saved_by": names.get(row.actor_id, "") if row.actor_id else "",
        "note": row.note,
        "rules": row.rules,
        "unmatched_rate": rate_text(row.unmatched_rate),
        "options": options_of(row.options),
    }


def _names(rows: list[TaxSettingVersion]) -> dict[Any, str]:
    ids = {row.actor_id for row in rows if row.actor_id}
    return dict(HumanIdentity.objects.filter(pk__in=ids).values_list("pk", "display_name"))


def _versions_json(tenant_id: Any) -> list[dict[str, Any]]:
    rows = list(TaxSettingVersion.objects.filter(tenant_id=tenant_id).order_by("-version"))
    names = _names(rows)
    legacy = {
        "version": LEGACY_VERSION,
        "applies_from": None,
        "legacy": True,
        "saved_at": None,
        "saved_by": "",
        "note": "The tax slab table every bill used before versioned settings.",
        "rules": legacy_rules(),
        "unmatched_rate": None,
        "options": default_options(),
    }
    return [*(_saved_json(row, names) for row in rows), legacy]


class GoodsTaxSettingsView(GoodsAPIView):
    @extend_schema(responses=TaxSettingsSerializer)
    def get(self, request: Request) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        if not user_can(request.user, "setup", CAP_VIEW):
            raise Refusal("ACTION_DENIED", "You do not have access to Setup.")
        stores = list(actionable_stores(request.user))
        real = real_stores(stores)
        today = timezone.localdate()
        versions = [
            as_saved(r) for r in TaxSettingVersion.objects.filter(tenant_id=access.tenant_id)
        ]
        current = in_force(versions, today)
        states = switch_states(stores, [_tax_feature()])
        body = {
            "versions": _versions_json(access.tenant_id),
            "latest_version": latest_version_number(access.tenant_id),
            "today": today,
            "gate": _tax_feature().gate,
            "stores": [
                {
                    "id": store.pk,
                    "code": store.code,
                    "name": store.name,
                    "real": real[store.pk],
                    "rules_on": state.enabled,
                    "locked_reason": state.locked_reason,
                    "version_today": (
                        current.version if state.enabled and current else LEGACY_VERSION
                    ),
                }
                for store, state in zip(stores, states, strict=True)
            ],
            "can_change": may_change_tax_settings(request.user),
        }
        return Response(TaxSettingsSerializer(body).data)


def _applies_from(value: Any, today: date, newest: date | None) -> date:
    try:
        applies = date.fromisoformat(str(value))
    except ValueError:
        raise Refusal(
            "INVALID_REQUEST",
            "The date it applies from must be a date.",
            issues=[issue("INVALID", "applies_from must be YYYY-MM-DD", field="applies_from")],
        ) from None
    if applies < today:
        raise Refusal(
            "INVALID_REQUEST",
            "A new version cannot apply from a past date. Pick today or a later date. "
            "Backdating needs an approval and an impact check first, which is not built yet.",
            issues=[issue("BACKDATED", "applies_from is before today", field="applies_from")],
        )
    if newest is not None and applies < newest:
        raise Refusal(
            "INVALID_REQUEST",
            f"A new version must apply from {newest.isoformat()} or later, the date the "
            "newest version applies from.",
            issues=[
                issue(
                    "OUT_OF_ORDER",
                    "applies_from is before the newest version's",
                    field="applies_from",
                )
            ],
        )
    return applies


def _note(value: Any) -> str:
    note = str(value or "").strip()
    if not note or len(note) > 240:
        raise Refusal(
            "INVALID_REQUEST",
            "Say in a few words why this version is being saved.",
            issues=[issue("REQUIRED", "note is 1-240 characters", field="note")],
        )
    return note


class GoodsTaxSettingVersionCreateView(GoodsAPIView):
    @extend_schema(request=TaxVersionRequestSerializer, responses=TaxVersionSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        if not may_change_tax_settings(request.user):
            raise Refusal("ACTION_DENIED", "Only Admin can change the tax settings.")
        stores = list(actionable_stores(request.user))
        if not any(state.enabled for state in switch_states(stores, [_tax_feature()])):
            raise Refusal(
                "FEATURE_OFF",
                "Versioned tax settings are switched off at every store. "
                "Admin can switch them on in Setup, Feature Switches.",
                status=403,
            )
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"applies_from", "rules", "unmatched_rate", "note", "options"},
            required=["applies_from", "rules", "unmatched_rate", "note"],
        )
        rules = parse_rules(body["rules"])
        unmatched = parse_rate(body["unmatched_rate"], "unmatched_rate")
        note = _note(body["note"])
        options = parse_options(body.get("options"))
        new_id = uuid.uuid4()

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, ["tax-settings"])
            newest = (
                TaxSettingVersion.objects.filter(tenant_id=run.tenant_id)
                .order_by("-version")
                .first()
            )
            current = newest.version if newest is not None else LEGACY_VERSION
            check_revision(meta.expected_revision, current)
            # Today by the command's own clock (the database's), in India.
            today = timezone.localdate(run.now)
            applies = _applies_from(
                body["applies_from"], today, newest.applies_from if newest else None
            )
            run.audit_before = (
                {
                    "version": current,
                    "applies_from": newest.applies_from.isoformat(),
                    "rules": newest.rules,
                    "unmatched_rate": rate_text(newest.unmatched_rate),
                    "options": options_of(newest.options),
                }
                if newest is not None
                else {
                    "version": current,
                    "applies_from": None,
                    "rules": legacy_rules(),
                    "options": default_options(),
                }
            )
            row = TaxSettingVersion.objects.create(
                id=new_id,
                tenant_id=run.tenant_id,
                version=current + 1,
                applies_from=applies,
                rules=[rule.as_json() for rule in rules],
                unmatched_rate=unmatched,
                options=options,
                note=note,
                actor_id=run.principal.human_id,
            )
            run.audit_after = {
                "version": row.version,
                "applies_from": applies.isoformat(),
                "rules": row.rules,
                "unmatched_rate": rate_text(unmatched),
                "options": options,
                "note": note,
            }
            return CommandResult(
                resource_type="tax_setting_version",
                resource_id=str(row.pk),
                revision=row.version,
                status_code=201,
            )

        result = self.run_command(
            request,
            access=access,
            action=SAVE_ACTION,
            meta=meta,
            business_input={
                "applies_from": str(body["applies_from"]),
                "rules": [rule.as_json() for rule in rules],
                "unmatched_rate": rate_text(unmatched),
                "options": options,
                "note": note,
            },
            handler=handler,
            resource_ids=[str(new_id)],
            subject_key=f"tax_setting:{new_id}",
        )
        saved = TaxSettingVersion.objects.get(pk=str(result.resource_id))
        return Response(
            TaxVersionSerializer(_saved_json(saved, _names([saved]))).data,
            status=result.status_code,
        )
