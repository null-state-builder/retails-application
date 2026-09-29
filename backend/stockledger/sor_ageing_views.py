"""Brands > SOR Ageing (store operations ticket 24, ST-BRD-5).

``GET  /api/goods-v1/stock/sor-ageing[?site_id=]``        a site's SOR stock aged from the
                                                           brand's dispatch date
                                                           (``stockledger.sor_ageing``)
``POST /api/goods-v1/stock/sor-ageing/dispatch-date``      record a delivery's dispatch date
``POST /api/goods-v1/stock/sor-ageing/brand-invoice``      record the brand's invoice for it

Which stock is on sale or return is a brand term, and store roles never read terms
(ticket 23), so readers hold ``money: manage`` (Owner and Accounts, B54), for the
active sites in their own scope where the ``sor-ageing`` switch is on. A site
outside that is not found. The read carries no cost, margin or other figure.

Accounts records (``inbound.sor_ageing_editors``), only for a delivery whose SOR
pieces stand on that site's list and only while its switch is on. Each write is
one command whose ``AuditEvent`` records who, when, the site, and the value
before and after (``inbound.sor_records``).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    parse_int_id,
    parse_meta,
    parse_uuid,
)
from accounts.permissions import user_can
from accounts.role_lists import SOR_AGEING_EDITOR_ROLES
from accounts.sections import CAP_MANAGE
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal, issue
from inbound import sor_records
from inbound.goods_models import Arrival
from masters.models import Store
from masters.scoping import actionable_stores
from masters.store_feature_registry import SOR_AGEING
from masters.store_features import feature, switch_states
from stockledger.sor_ageing import GROUPS, months_setting, store_sor_ageing


def may_read(user: Any) -> bool:
    """Owner and Accounts: ``money: manage``. Store roles never read brand terms."""
    if getattr(user, "is_superuser", False):
        return True
    return user_can(user, "money", CAP_MANAGE)


def may_record(user: Any) -> bool:
    """Accounts (``money: manage`` narrowed to the declared editors)."""
    if getattr(user, "is_superuser", False):
        return True
    role = str(getattr(getattr(user, "role", None), "code", "") or "")
    return may_read(user) and role in SOR_AGEING_EDITOR_ROLES


def sor_sites(user: Any) -> list[Store]:
    """The active sites in this person's scope where the switch is on."""
    sites = list(actionable_stores(user).filter(is_active=True).order_by("code"))
    states = switch_states(sites, [feature(SOR_AGEING)])
    return [site for site, state in zip(sites, states, strict=True) if state.enabled]


def _site(user: Any, raw: Any) -> Store:
    wanted = parse_int_id(raw, "site_id")
    chosen = next((site for site in sor_sites(user) if site.pk == wanted), None)
    if chosen is None:
        raise Refusal("NOT_FOUND", "That site's SOR ageing is not available.", status=404)
    return chosen


def _require_reader(user: Any) -> None:
    if not may_read(user):
        raise Refusal("ACTION_DENIED", "Only the Owner and Accounts read SOR ageing.")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


class SorSiteSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class SorBrandInvoiceSerializer(serializers.Serializer[Any]):
    number = serializers.CharField()
    invoice_date = serializers.DateField()


class SorRowSerializer(serializers.Serializer[Any]):
    origin_id = serializers.CharField()
    arrival_id = serializers.CharField(
        allow_null=True, help_text="The delivery; null for opening stock, which came on none."
    )
    arrival_site = serializers.CharField(allow_blank=True)
    arrived_on = serializers.DateField(allow_null=True)
    vendor_invoice = serializers.CharField(allow_blank=True)
    sku_id = serializers.CharField()
    barcode = serializers.CharField(allow_blank=True)
    brand = serializers.CharField(allow_blank=True)
    item = serializers.CharField(allow_blank=True)
    design = serializers.CharField(allow_blank=True)
    size = serializers.CharField(allow_blank=True)
    colour = serializers.CharField(allow_blank=True)
    season_code = serializers.CharField(allow_blank=True)
    season_label = serializers.CharField(allow_blank=True)
    qty = serializers.IntegerField()
    dispatch_date = serializers.DateField(allow_null=True)
    brand_invoice = SorBrandInvoiceSerializer(allow_null=True)
    state = serializers.CharField(help_text=f"One of: {', '.join(GROUPS)}.")
    alert_on = serializers.DateField(allow_null=True, help_text="5 months from dispatch.")
    invoice_by = serializers.DateField(
        allow_null=True, help_text="6 months from dispatch: the brand must have invoiced by then."
    )
    days_left = serializers.IntegerField(
        allow_null=True, help_text="Days to invoice_by; 0 on the day, negative once past."
    )


class SorGroupSerializer(serializers.Serializer[Any]):
    group = serializers.CharField(help_text=f"One of: {', '.join(GROUPS)}.")
    qty = serializers.IntegerField()
    items = serializers.IntegerField()
    deliveries = serializers.IntegerField()


class SorUnknownModelSerializer(serializers.Serializer[Any]):
    brand = serializers.CharField(allow_blank=True)
    season_code = serializers.CharField(allow_blank=True)
    season_label = serializers.CharField(allow_blank=True)
    qty = serializers.IntegerField()
    items = serializers.IntegerField()


class SorAgeingSerializer(serializers.Serializer[Any]):
    stores = SorSiteSerializer(many=True)
    site_id = serializers.IntegerField(allow_null=True)
    today = serializers.DateField()
    alert_months = serializers.IntegerField()
    invoice_months = serializers.IntegerField()
    goods_records = serializers.BooleanField(
        help_text="False: this site's stock is not on the goods records, so none can be traced."
    )
    groups = SorGroupSerializer(many=True)
    rows = SorRowSerializer(many=True)
    unknown_models = SorUnknownModelSerializer(many=True)
    can_record = serializers.BooleanField()


class GoodsSorAgeingView(GoodsAPIView):
    """A site's SOR stock aged from the brand's dispatch date (``money: manage``)."""

    @extend_schema(
        parameters=[OpenApiParameter("site_id", int, required=False)],
        responses=SorAgeingSerializer,
    )
    def get(self, request: Request) -> Response:
        self.access(request)
        params = check_query(request, allowed=("site_id",))
        _require_reader(request.user)
        sites = sor_sites(request.user)
        chosen = _site(request.user, params["site_id"]) if params.get("site_id") else None
        if chosen is None and sites:
            chosen = sites[0]
        today = timezone.localdate()
        report = store_sor_ageing(chosen, today) if chosen is not None else None
        alert, invoice = months_setting()
        body = {
            "stores": [{"id": s.pk, "code": s.code, "name": s.name} for s in sites],
            "site_id": chosen.pk if chosen is not None else None,
            "today": today,
            "alert_months": alert,
            "invoice_months": invoice,
            "goods_records": report.goods_records if report else True,
            "groups": [total.as_json() for total in report.totals()] if report else [],
            "rows": [row.as_json() for row in report.rows] if report else [],
            "unknown_models": [u.as_json() for u in report.unknown_models] if report else [],
            "can_record": bool(sites) and may_record(request.user),
        }
        return Response(SorAgeingSerializer(body).data)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class SorDeliverySerializer(serializers.Serializer[Any]):
    arrival_id = serializers.CharField()
    brand_dispatch_date = serializers.DateField(allow_null=True)
    brand_invoice = SorBrandInvoiceSerializer(allow_null=True)


class SorDispatchDateRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    site_id = serializers.IntegerField()
    arrival_id = serializers.UUIDField()
    dispatch_date = serializers.DateField()
    reason = serializers.CharField(required=False, help_text="Required to change a recorded date.")


class SorBrandInvoiceRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    site_id = serializers.IntegerField()
    arrival_id = serializers.UUIDField()
    invoice_number = serializers.CharField()
    invoice_date = serializers.DateField()
    reason = serializers.CharField(
        required=False, help_text="Required to replace a recorded invoice."
    )


def _day(raw: Any, field: str) -> date:
    from django.utils.dateparse import parse_date

    parsed = parse_date(raw) if isinstance(raw, str) else None
    if parsed is None:
        raise Refusal(
            "INVALID_REQUEST",
            f"{field} must be a date (YYYY-MM-DD).",
            issues=[issue("INVALID", "not a date", field=field)],
        )
    return parsed


def _text(raw: Any, field: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise Refusal(
            "INVALID_REQUEST",
            f"{field} must be text.",
            issues=[issue("INVALID", "not text", field=field)],
        )
    return raw


def _not_on_list() -> Refusal:
    return Refusal("NOT_FOUND", "That delivery has no SOR pieces on this site's list.", status=404)


def _require_on_list(site: Store, arrival: Arrival) -> None:
    """Refuse unless the delivery's SOR pieces stand on this site's list."""
    if not any(row.arrival_id == arrival.pk for row in store_sor_ageing(site).rows):
        raise _not_on_list()


def _delivery_json(arrival: Arrival) -> dict[str, Any]:
    dispatch = sor_records.standing_dispatch(arrival.pk)
    invoice = sor_records.standing_invoice(arrival.pk)
    return {
        "arrival_id": str(arrival.pk),
        "brand_dispatch_date": dispatch.dispatch_date if dispatch else None,
        "brand_invoice": {"number": invoice.invoice_number, "invoice_date": invoice.invoice_date}
        if invoice
        else None,
    }


class _SorWriteView(GoodsAPIView):
    ACTION = ""
    FIELDS: frozenset[str] = frozenset()
    REQUIRED: tuple[str, ...] = ()

    def record(self, run: CommandRun, arrival: Arrival, body: dict[str, Any]) -> None:
        raise NotImplementedError

    def write(self, request: Request) -> Response:
        access = self.access(request)
        _require_reader(request.user)
        if not may_record(request.user):
            raise Refusal("ACTION_DENIED", "Only Accounts records a delivery's SOR details.")
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data, {"site_id", "arrival_id", *self.FIELDS}, required=self.REQUIRED
        )
        site_id = parse_int_id(body["site_id"], "site_id")
        arrival = Arrival.objects.filter(pk=parse_uuid(body["arrival_id"], "arrival_id")).first()
        if arrival is None:
            raise _not_on_list()

        def handler(run: CommandRun) -> CommandResult:
            # Checked inside the command: a save pressed again after its answer
            # was lost is answered from the first save, even if the pieces have
            # sold or the switch has gone off since (B169).
            site = _site(request.user, site_id)
            _require_on_list(site, arrival)
            self.record(run, arrival, body)
            return CommandResult(resource_type="arrival", resource_id=str(arrival.pk), revision=1)

        result = self.run_command(
            request,
            access=access,
            action=self.ACTION,
            meta=meta,
            business_input={**body, "site_id": site_id, "arrival_id": str(arrival.pk)},
            handler=handler,
            resource_ids=[f"arrival:{arrival.pk}"],
            subject_key=f"sor_delivery:{arrival.pk}",
            site_id=site_id,
        )
        # A settled delivery leaves its alert now, not at tomorrow's check.
        from alerts.checks import sync_sor_alerts

        sync_sor_alerts()
        return Response(
            SorDeliverySerializer(_delivery_json(arrival)).data, status=result.status_code
        )


class GoodsSorDispatchDateView(_SorWriteView):
    """Record the brand's dispatch date for a delivery received without one, or change it."""

    ACTION = sor_records.AUDIT_DISPATCH
    FIELDS = frozenset({"dispatch_date", "reason"})
    REQUIRED = ("site_id", "arrival_id", "dispatch_date")

    def record(self, run: CommandRun, arrival: Arrival, body: dict[str, Any]) -> None:
        sor_records.record_dispatch_date(
            run,
            arrival,
            _day(body["dispatch_date"], "dispatch_date"),
            _text(body.get("reason"), "reason"),
        )

    @extend_schema(request=SorDispatchDateRequestSerializer, responses=SorDeliverySerializer)
    def post(self, request: Request) -> Response:
        return self.write(request)


class GoodsSorBrandInvoiceView(_SorWriteView):
    """Record the brand's invoice that settles a delivery's SOR pieces, or replace it."""

    ACTION = sor_records.AUDIT_INVOICE
    FIELDS = frozenset({"invoice_number", "invoice_date", "reason"})
    REQUIRED = ("site_id", "arrival_id", "invoice_number", "invoice_date")

    def record(self, run: CommandRun, arrival: Arrival, body: dict[str, Any]) -> None:
        sor_records.record_brand_invoice(
            run,
            arrival,
            _text(body["invoice_number"], "invoice_number"),
            _day(body["invoice_date"], "invoice_date"),
            _text(body.get("reason"), "reason"),
        )

    @extend_schema(request=SorBrandInvoiceRequestSerializer, responses=SorDeliverySerializer)
    def post(self, request: Request) -> Response:
        return self.write(request)
