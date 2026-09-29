"""Setup > Consent Wording (store operations ticket 15, §6 ST-CMP-6).

Under ``/api/goods-v1/masters/``:

``GET  consent-wording``           every version, newest first (``setup: view``)
``POST consent-wording/versions``  save a new version (Admin only)

The wording is the three questions the customer answers on the customer
display: send my bill, are you under 18, send me offers. A save never changes an
old version: it adds the next number, which is current from that moment, and
every answer the counter takes afterwards records it. An answer already given
keeps the version it was given under.

Only Admin saves: ``setup: manage`` and a role code in
``accounts.role_lists.CONSENT_WORDING_EDITOR_ROLES`` (``it_admin``), with a scope
covering every store, because the wording is company-wide. Saving is new work,
so it needs the ``customer-consent`` switch on at one or more stores in scope;
reading never does. Each save is one command, audited with the version and the
wording before and after.
"""

from __future__ import annotations

import uuid
from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, check_revision, parse_meta
from accounts.goods_models import HumanIdentity
from accounts.permissions import user_can
from accounts.role_lists import CONSENT_WORDING_EDITOR_ROLES
from accounts.sections import CAP_MANAGE, CAP_VIEW
from core.commands import CommandResult, CommandRun, LockRank
from core.refusals import Refusal, issue
from masters.consent_wording import (
    VERSION_ONE,
    current_wording,
    parse_text,
    wording_of,
)
from masters.consent_wording_models import ConsentWording
from masters.scoping import actionable_store_ids, actionable_stores
from masters.store_feature_registry import CUSTOMER_CONSENT
from masters.store_features import feature, switch_states

SAVE_ACTION = "masters.consent_wording.save"


def may_change_consent_wording(user: Any) -> bool:
    """Admin only, with a company-wide scope (or break-glass)."""
    if getattr(user, "is_superuser", False):
        return True
    code = getattr(getattr(user, "role", None), "code", "")
    return (
        user_can(user, "setup", CAP_MANAGE)
        and code in CONSENT_WORDING_EDITOR_ROLES
        and actionable_store_ids(user) is None
    )


class ConsentWordingVersionSerializer(serializers.Serializer[Any]):
    version = serializers.IntegerField()
    bill = serializers.CharField()
    age = serializers.CharField()
    offers = serializers.CharField()
    first = serializers.BooleanField(help_text="Version 1: the built-in first wording (B13).")
    saved_at = serializers.DateTimeField(allow_null=True)
    saved_by = serializers.CharField(allow_blank=True)
    note = serializers.CharField(allow_blank=True)


class ConsentWordingStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    consent_on = serializers.BooleanField()


class ConsentWordingSerializer(serializers.Serializer[Any]):
    versions = ConsentWordingVersionSerializer(many=True)
    current_version = serializers.IntegerField()
    stores = ConsentWordingStoreSerializer(many=True)
    can_change = serializers.BooleanField()


class ConsentWordingRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField(
        help_text="The current version number the form was built from."
    )
    bill = serializers.CharField()
    age = serializers.CharField()
    offers = serializers.CharField()
    note = serializers.CharField()


def _names(rows: list[ConsentWording]) -> dict[Any, str]:
    ids = {row.actor_id for row in rows if row.actor_id}
    return dict(HumanIdentity.objects.filter(pk__in=ids).values_list("pk", "display_name"))


def _saved_json(row: ConsentWording, names: dict[Any, str]) -> dict[str, Any]:
    return {
        **wording_of(row).as_json(),
        "first": False,
        "saved_at": row.created_at,
        "saved_by": names.get(row.actor_id, "") if row.actor_id else "",
        "note": row.note,
    }


def _versions_json(tenant_id: Any) -> list[dict[str, Any]]:
    rows = list(ConsentWording.objects.filter(tenant_id=tenant_id).order_by("-version"))
    names = _names(rows)
    first = {
        **VERSION_ONE.as_json(),
        "first": True,
        "saved_at": None,
        "saved_by": "",
        "note": "The first wording.",
    }
    return [*(_saved_json(row, names) for row in rows), first]


class GoodsConsentWordingView(GoodsAPIView):
    @extend_schema(responses=ConsentWordingSerializer)
    def get(self, request: Request) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        if not user_can(request.user, "setup", CAP_VIEW):
            raise Refusal("ACTION_DENIED", "You do not have access to Setup.")
        stores = list(actionable_stores(request.user))
        states = switch_states(stores, [feature(CUSTOMER_CONSENT)])
        body = {
            "versions": _versions_json(access.tenant_id),
            "current_version": current_wording(access.tenant_id).version,
            "stores": [
                {
                    "id": store.pk,
                    "code": store.code,
                    "name": store.name,
                    "consent_on": state.enabled,
                }
                for store, state in zip(stores, states, strict=True)
            ],
            "can_change": may_change_consent_wording(request.user),
        }
        return Response(ConsentWordingSerializer(body).data)


def _note(value: Any) -> str:
    note = " ".join(str(value or "").split())
    if not note or len(note) > 240:
        raise Refusal(
            "INVALID_REQUEST",
            "Say in a few words why the wording is changing.",
            issues=[issue("REQUIRED", "note is 1-240 characters", field="note")],
        )
    return note


class GoodsConsentWordingVersionCreateView(GoodsAPIView):
    @extend_schema(
        request=ConsentWordingRequestSerializer, responses=ConsentWordingVersionSerializer
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        if not may_change_consent_wording(request.user):
            raise Refusal("ACTION_DENIED", "Only Admin can change the consent wording.")
        stores = list(actionable_stores(request.user))
        if not any(state.enabled for state in switch_states(stores, [feature(CUSTOMER_CONSENT)])):
            raise Refusal(
                "FEATURE_OFF",
                "Customer consent is switched off at every store. "
                "Admin can switch it on in Setup, Feature Switches.",
                status=403,
            )
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"bill", "age", "offers", "note"},
            required=["bill", "age", "offers", "note"],
        )
        texts = {key: parse_text(body[key], key) for key in ("bill", "age", "offers")}
        note = _note(body["note"])
        new_id = uuid.uuid4()

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, ["consent-wording"])
            before = current_wording(run.tenant_id)
            check_revision(meta.expected_revision, before.version)
            row = ConsentWording.objects.create(
                id=new_id,
                tenant_id=run.tenant_id,
                version=before.version + 1,
                bill_text=texts["bill"],
                age_text=texts["age"],
                offers_text=texts["offers"],
                note=note,
                actor_id=run.principal.human_id,
            )
            run.audit_before = before.as_json()
            run.audit_after = {**wording_of(row).as_json(), "note": note}
            return CommandResult(
                resource_type="consent_wording",
                resource_id=str(row.pk),
                revision=row.version,
                status_code=201,
            )

        result = self.run_command(
            request,
            access=access,
            action=SAVE_ACTION,
            meta=meta,
            business_input={**texts, "note": note},
            handler=handler,
            resource_ids=[str(new_id)],
            subject_key=f"consent_wording:{new_id}",
        )
        saved = ConsentWording.objects.get(pk=str(result.resource_id))
        return Response(
            ConsentWordingVersionSerializer(_saved_json(saved, _names([saved]))).data,
            status=result.status_code,
        )
