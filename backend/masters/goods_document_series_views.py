"""Setup > Document Numbering (store operations ticket 04, ST-CMP-5).

Under ``/api/goods-v1/masters/``:

``GET  document-series``            the start date, every site's prefix, head
                                    office's prefix per GSTIN, the counters'
                                    number blocks and the cancelled numbers
                                    (``setup: view``)
``POST document-series/prefixes``   give a site, or head office for a GSTIN, its
                                    3-letter prefix, or change one not yet used
                                    (Admin only)
``POST document-series/setting``    the date the new format starts (a 1 April,
                                    not in the past, B8) and the till block size
                                    (Admin only)

Only Admin changes anything: ``setup: manage`` *and* a role code in
``accounts.role_lists.DOCUMENT_SERIES_EDITOR_ROLES`` (``it_admin``), with a scope
covering every store, because prefixes are unique across the company and the
start date is company-wide. Changing anything is new work, so it needs the
``document-series`` switch on at one or more sites in scope (ticket 01's rule);
reading never does. Every change is one command whose ``AuditEvent`` holds the
values before and after, under ``document_prefix:<id>`` or
``numbering_setting:<id>``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, check_revision, parse_meta
from accounts.permissions import user_can
from accounts.role_lists import DOCUMENT_SERIES_EDITOR_ROLES
from accounts.sections import CAP_MANAGE, CAP_VIEW
from core.commands import CommandResult, CommandRun, LockRank
from core.fiscal import financial_year
from core.refusals import Refusal, issue
from masters.document_series import (
    FEATURE_KEY,
    PREFIX_CODE,
    DocumentSeries,
    cancelled_numbers,
    new_series_issued,
    numbering_setting,
    prefix_in_use,
    render,
    start_date_problem,
)
from masters.document_series_models import DocumentPrefix, NumberingSetting
from masters.models import Gstin, Store
from masters.scoping import actionable_store_ids, actionable_stores
from masters.store_features import feature, switch_states

PREFIX_ACTION = "masters.document_prefix.set"
SETTING_ACTION = "masters.numbering_setting.save"
MAX_BLOCK_SIZE = 5000


def may_change_numbering(user: Any) -> bool:
    """Admin only, with a company-wide scope (or break-glass)."""
    if getattr(user, "is_superuser", False):
        return True
    code = getattr(getattr(user, "role", None), "code", "")
    return (
        user_can(user, "setup", CAP_MANAGE)
        and code in DOCUMENT_SERIES_EDITOR_ROLES
        and actionable_store_ids(user) is None
    )


class SeriesSerializer(serializers.Serializer[Any]):
    code = serializers.CharField()
    name = serializers.CharField()
    example = serializers.CharField()
    owner = serializers.CharField()


class SitePrefixSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    kind = serializers.CharField(help_text="store or warehouse")
    series_on = serializers.BooleanField()
    prefix = serializers.CharField(allow_blank=True)
    prefix_id = serializers.CharField(allow_null=True)
    revision = serializers.IntegerField()
    used = serializers.BooleanField()


class GstinPrefixSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    gstin = serializers.CharField()
    state_name = serializers.CharField()
    prefix = serializers.CharField(allow_blank=True)
    prefix_id = serializers.CharField(allow_null=True)
    revision = serializers.IntegerField()
    used = serializers.BooleanField()


class NumberingSettingSerializer(serializers.Serializer[Any]):
    new_format_from = serializers.DateField()
    till_block_size = serializers.IntegerField()
    revision = serializers.IntegerField()
    started = serializers.BooleanField()


class BlockSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    store = serializers.CharField()
    counter = serializers.CharField()
    month = serializers.CharField()
    first_number = serializers.CharField()
    last_number = serializers.CharField()


class CancelledSerializer(serializers.Serializer[Any]):
    prefix = serializers.CharField()
    series = serializers.CharField()
    month = serializers.CharField()
    count = serializers.IntegerField()
    first = serializers.CharField()
    last = serializers.CharField()


class DocumentSeriesSerializer(serializers.Serializer[Any]):
    today = serializers.DateField()
    setting = NumberingSettingSerializer()
    series = SeriesSerializer(many=True)
    sites = SitePrefixSerializer(many=True)
    head_office = GstinPrefixSerializer(many=True)
    blocks = BlockSerializer(many=True)
    cancelled = CancelledSerializer(many=True)
    can_change = serializers.BooleanField()


class PrefixRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField(
        required=False,
        help_text="The prefix's revision the form was built from; absent for a new one.",
    )
    site_id = serializers.IntegerField(required=False)
    gstin_id = serializers.IntegerField(required=False)
    code = serializers.CharField()


class PrefixResponseSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    code = serializers.CharField()
    revision = serializers.IntegerField()


class SettingRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField(required=False)
    new_format_from = serializers.DateField()
    till_block_size = serializers.IntegerField()


SERIES_OWNER = {
    DocumentSeries.TAX_INVOICE: "store",
    DocumentSeries.CREDIT_NOTE: "store",
    DocumentSeries.RECEIPT_VOUCHER: "store",
    DocumentSeries.GIFT_VOUCHER: "store",
    DocumentSeries.DELIVERY_CHALLAN: "sending site",
    DocumentSeries.DEBIT_NOTE: "head office, per GSTIN",
}


def _series_json(fy: str) -> list[dict[str, Any]]:
    return [
        {
            "code": series.value,
            "name": series.label,
            "example": render(series.value, "XXX", fy, 1),
            "owner": SERIES_OWNER[series],
        }
        for series in DocumentSeries
    ]


def _stores_in_scope(user: Any) -> list[Store]:
    return list(actionable_stores(user).select_related("gstin"))


def _require_switch(stores: list[Store]) -> None:
    if not any(state.enabled for state in switch_states(stores, [feature(FEATURE_KEY)])):
        raise Refusal(
            "FEATURE_OFF",
            "Store prefixes and document series are switched off at every site. "
            "Admin can switch them on in Setup, Feature Switches.",
            status=403,
        )


class GoodsDocumentSeriesView(GoodsAPIView):
    @extend_schema(responses=DocumentSeriesSerializer)
    def get(self, request: Request) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        if not user_can(request.user, "setup", CAP_VIEW):
            raise Refusal("ACTION_DENIED", "You do not have access to Setup.")
        from sell.services.invoice_numbers import blocks_in_use

        stores = _stores_in_scope(request.user)
        states = switch_states(stores, [feature(FEATURE_KEY)])
        prefixes = {
            (row.site_id, row.gstin_id): row
            for row in DocumentPrefix.objects.filter(tenant_id=access.tenant_id)
        }
        setting = numbering_setting(access.tenant_id)
        today = timezone.localdate()

        def prefix_fields(row: DocumentPrefix | None) -> dict[str, Any]:
            return {
                "prefix": row.code if row else "",
                "prefix_id": str(row.pk) if row else None,
                "revision": row.revision if row else 0,
                "used": prefix_in_use(row) if row else False,
            }

        gstin_ids = {store.gstin_id for store in stores}
        company_wide = actionable_store_ids(request.user) is None
        gstins = Gstin.objects.filter(is_active=True).order_by("state_name")
        if not company_wide:
            gstins = gstins.filter(pk__in=gstin_ids)
        body = {
            "today": today,
            "setting": {
                "new_format_from": setting.new_format_from,
                "till_block_size": setting.till_block_size,
                "revision": setting.revision,
                "started": setting.new_format_from <= today,
            },
            "series": _series_json(financial_year(setting.new_format_from)),
            "sites": [
                {
                    "id": store.pk,
                    "code": store.code,
                    "name": store.name,
                    "kind": store.store_type,
                    "series_on": state.enabled,
                    **prefix_fields(prefixes.get((store.pk, None))),
                }
                for store, state in zip(stores, states, strict=True)
            ],
            "head_office": [
                {
                    "id": row.pk,
                    "gstin": row.gstin,
                    "state_name": row.state_name,
                    **prefix_fields(prefixes.get((None, row.pk))),
                }
                for row in gstins
            ],
            "blocks": blocks_in_use([store.pk for store in stores]),
            "cancelled": cancelled_numbers(access.tenant_id) if company_wide else [],
            "can_change": may_change_numbering(request.user),
        }
        return Response(DocumentSeriesSerializer(body).data)


def _code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if not PREFIX_CODE.match(code):
        raise Refusal(
            "INVALID_REQUEST",
            "A prefix is exactly 3 letters, A to Z.",
            issues=[issue("INVALID", "code must be 3 letters A-Z", field="code")],
        )
    return code


@dataclass(frozen=True)
class _Owner:
    """The site, or head office for a GSTIN, a prefix is being set for."""

    site: Store | None
    gstin: Gstin | None
    name: str

    @property
    def key(self) -> Q:
        return Q(site=self.site) if self.site is not None else Q(gstin=self.gstin)


def _id(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Refusal(
            "INVALID_REQUEST",
            f"{name} must be a number.",
            issues=[issue("INVALID", f"{name} must be an integer", field=name)],
        )
    return value


def _owner(body: dict[str, Any]) -> _Owner:
    site_id, gstin_id = body.get("site_id"), body.get("gstin_id")
    if (site_id is None) == (gstin_id is None):
        raise Refusal(
            "INVALID_REQUEST",
            "Name either a site or a GSTIN for head office, not both.",
            issues=[issue("INVALID", "exactly one of site_id and gstin_id", field="site_id")],
        )
    if site_id is not None:
        site = Store.objects.filter(pk=_id(site_id, "site_id"), is_active=True).first()
        if site is None:
            raise Refusal("NOT_FOUND", "That site does not exist.", status=404)
        return _Owner(site=site, gstin=None, name=f"{site.code} · {site.name}")
    gstin = Gstin.objects.filter(pk=_id(gstin_id, "gstin_id"), is_active=True).first()
    if gstin is None:
        raise Refusal("NOT_FOUND", "That GSTIN does not exist.", status=404)
    return _Owner(site=None, gstin=gstin, name=f"head office ({gstin.gstin})")


def _check_change(row: DocumentPrefix | None, code: str, expected: int | None, owner: str) -> None:
    """A new prefix, or a change to one nobody has used yet."""
    if row is None:
        if expected is not None:
            check_revision(expected, 0)
        return
    if expected is None:
        raise Refusal(
            "REVISION_SUPERSEDED", f"{owner} already has prefix {row.code}. Reload and review it."
        )
    check_revision(expected, row.revision)
    if row.code == code:
        raise Refusal("INVALID_REQUEST", f"{owner} already has prefix {code}.")
    if prefix_in_use(row):
        raise Refusal(
            "PREFIX_IN_USE",
            f"Prefix {row.code} has already been used on a document, so it cannot change.",
            status=409,
        )


def _refuse_clash(tenant_id: Any, code: str, row: DocumentPrefix | None) -> None:
    clash = DocumentPrefix.objects.filter(tenant_id=tenant_id, code=code)
    if row is not None:
        clash = clash.exclude(pk=row.pk)
    taken = clash.first()
    if taken is not None:
        raise Refusal(
            "PREFIX_TAKEN",
            f"Prefix {code} already belongs to {_owner_of(taken)}. Each prefix is unique "
            "across the company.",
            status=409,
        )


def _owner_of(row: DocumentPrefix) -> str:
    if row.site is not None:
        return f"{row.site.code} · {row.site.name}"
    assert row.gstin is not None
    return f"head office ({row.gstin.gstin})"


class GoodsDocumentPrefixView(GoodsAPIView):
    @extend_schema(request=PrefixRequestSerializer, responses=PrefixResponseSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        if not may_change_numbering(request.user):
            raise Refusal("ACTION_DENIED", "Only Admin can set a document prefix.")
        _require_switch(_stores_in_scope(request.user))
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, {"site_id", "gstin_id", "code"}, required=["code"])
        code = _code(body["code"])
        owner = _owner(body)
        existing = DocumentPrefix.objects.filter(owner.key, tenant_id=access.tenant_id).first()
        prefix_id = existing.pk if existing is not None else uuid.uuid4()

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, ["document-prefixes"])
            row = (
                DocumentPrefix.objects.select_for_update()
                .filter(owner.key, tenant_id=run.tenant_id)
                .first()
            )
            _check_change(row, code, meta.expected_revision, owner.name)
            _refuse_clash(run.tenant_id, code, row)
            now = timezone.now()
            if row is None:
                run.audit_before = None
                row = DocumentPrefix.objects.create(
                    id=prefix_id,
                    tenant_id=run.tenant_id,
                    code=code,
                    site=owner.site,
                    gstin=owner.gstin,
                    updated_at=now,
                )
            else:
                run.audit_before = {"code": row.code, "owner": owner.name, "revision": row.revision}
                row.code = code
                row.revision += 1
                row.updated_at = now
                row.save(update_fields=["code", "revision", "updated_at"])
            run.audit_after = {"code": row.code, "owner": owner.name, "revision": row.revision}
            return CommandResult(
                resource_type="document_prefix",
                resource_id=str(row.pk),
                revision=row.revision,
                status_code=200,
            )

        result = self.run_command(
            request,
            access=access,
            action=PREFIX_ACTION,
            meta=meta,
            business_input={
                "site_id": body.get("site_id"),
                "gstin_id": body.get("gstin_id"),
                "code": code,
            },
            handler=handler,
            resource_ids=[str(prefix_id)],
            subject_key=f"document_prefix:{prefix_id}",
            site_id=owner.site.pk if owner.site is not None else None,
        )
        saved = DocumentPrefix.objects.get(pk=str(result.resource_id))
        return Response(
            PrefixResponseSerializer(
                {"id": str(saved.pk), "code": saved.code, "revision": saved.revision}
            ).data,
            status=result.status_code,
        )


class GoodsNumberingSettingView(GoodsAPIView):
    @extend_schema(request=SettingRequestSerializer, responses=NumberingSettingSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        if not may_change_numbering(request.user):
            raise Refusal(
                "ACTION_DENIED", "Only Admin can change when the new number format starts."
            )
        _require_switch(_stores_in_scope(request.user))
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"new_format_from", "till_block_size"},
            required=["new_format_from", "till_block_size"],
        )
        try:
            starts = date.fromisoformat(str(body["new_format_from"]))
        except ValueError:
            raise Refusal(
                "INVALID_REQUEST",
                "The start date must be a date.",
                issues=[
                    issue("INVALID", "new_format_from must be YYYY-MM-DD", field="new_format_from")
                ],
            ) from None
        size = body["till_block_size"]
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_BLOCK_SIZE:
            raise Refusal(
                "INVALID_REQUEST",
                f"A till's block holds between 1 and {MAX_BLOCK_SIZE:,} invoice numbers.",
                issues=[issue("INVALID", "till_block_size out of range", field="till_block_size")],
            )
        setting_id = uuid.uuid4()

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.DOCUMENT, ["numbering-setting"])
            row = (
                NumberingSetting.objects.select_for_update().filter(tenant_id=run.tenant_id).first()
            )
            current = numbering_setting(run.tenant_id)
            check_revision(meta.expected_revision, current.revision)
            today = timezone.localdate(run.now)
            if starts != current.new_format_from:
                if current.new_format_from <= today:
                    raise Refusal(
                        "FORMAT_STARTED",
                        f"The new number format started on {current.new_format_from.isoformat()}. "
                        "The start date cannot move once it has arrived.",
                        status=409,
                    )
                problem = start_date_problem(starts, today)
                if (
                    problem is None
                    and starts > current.new_format_from
                    and new_series_issued(run.tenant_id, financial_year(current.new_format_from))
                ):
                    problem = (
                        "Tills already hold invoice numbers in the new series from "
                        f"{current.new_format_from.isoformat()}, so the start cannot move later."
                    )
                if problem is not None:
                    raise Refusal(
                        "INVALID_REQUEST",
                        problem,
                        issues=[issue("NOT_FIRST_APRIL", problem, field="new_format_from")],
                    )
            run.audit_before = {
                "new_format_from": current.new_format_from.isoformat(),
                "till_block_size": current.till_block_size,
                "revision": current.revision,
            }
            now = timezone.now()
            if row is None:
                row = NumberingSetting.objects.create(
                    id=setting_id,
                    tenant_id=run.tenant_id,
                    new_format_from=starts,
                    till_block_size=size,
                    updated_at=now,
                )
            else:
                row.new_format_from = starts
                row.till_block_size = size
                row.revision += 1
                row.updated_at = now
                row.save(
                    update_fields=["new_format_from", "till_block_size", "revision", "updated_at"]
                )
            run.audit_after = {
                "new_format_from": starts.isoformat(),
                "till_block_size": size,
                "revision": row.revision,
            }
            return CommandResult(
                resource_type="numbering_setting",
                resource_id=str(row.pk),
                revision=row.revision,
                status_code=200,
            )

        existing = NumberingSetting.objects.filter(tenant_id=access.tenant_id).first()
        subject = existing.pk if existing is not None else setting_id
        self.run_command(
            request,
            access=access,
            action=SETTING_ACTION,
            meta=meta,
            business_input={"new_format_from": starts.isoformat(), "till_block_size": size},
            handler=handler,
            resource_ids=[str(subject)],
            subject_key=f"numbering_setting:{subject}",
        )
        saved = numbering_setting(access.tenant_id)
        return Response(
            NumberingSettingSerializer(
                {
                    "new_format_from": saved.new_format_from,
                    "till_block_size": saved.till_block_size,
                    "revision": saved.revision,
                    "started": saved.started,
                }
            ).data
        )
