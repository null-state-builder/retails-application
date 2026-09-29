"""Money > Payables (store operations PRD ST-MNY-4; ticket 28).

Under ``/api/goods-v1/finledger/``:

``GET  payables?store=&group=brand|vendor``  per outright brand or vendor: owed,
                                             paid, due and ageing; brands with an
                                             unknown model apart; the invoices and
                                             payments behind them
``GET  payables/options``                    what the record forms choose from
``POST payables/invoices``                   Accounts: record a vendor invoice
``POST payables/invoices/<id>/cancel``       Accounts: cancel one, with a reason
``POST payables/payments``                   Accounts: record a payment
``POST payables/payments/<id>/cancel``       Accounts: cancel one, with a reason

Reading needs ``money: manage`` (Owner and Accounts), for the stores in reach;
store roles never see payables. Writing is Accounts' alone. New work needs the
``brand-payables`` switch on at the store; reading never does. Every write is one
command, so its audit record carries the record before and after.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, parse_meta
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal, issue
from finledger import payables as pay
from finledger.payable_models import PayableInvoice, PayablePayment
from masters.models import Brand, Season
from masters.store_features import is_feature_on
from vendors.models import Vendor, VendorBrand

#: How many invoices (open ones first, oldest first) and payments (newest first)
#: the page lists. The figures always count every invoice.
LIST_LIMIT = 500

# -- the wire ------------------------------------------------------------------------


class PayablePartySerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class PayableStoreSerializer(PayablePartySerializer):
    switched_on = serializers.BooleanField()


class PayableBandsSerializer(serializers.Serializer[Any]):
    not_due = serializers.CharField(help_text="Unpaid, not yet due.")
    days_0_30 = serializers.CharField(help_text="Due up to 30 days ago (or today).")
    days_31_60 = serializers.CharField()
    days_61_90 = serializers.CharField()
    over_90 = serializers.CharField()
    due_unknown = serializers.CharField(
        help_text="Unpaid where the brand's terms leave payment days blank."
    )


class PayablePositionSerializer(serializers.Serializer[Any]):
    key = serializers.IntegerField(help_text="The brand's or vendor's id.")
    name = serializers.CharField()
    owed_paise = serializers.CharField(help_text="Invoices, including tax.")
    paid_paise = serializers.CharField()
    outstanding_paise = serializers.CharField(help_text="Owed less paid.")
    due_paise = serializers.CharField(help_text="Outstanding whose due date is today or past.")
    bands = PayableBandsSerializer()
    open_invoices = serializers.IntegerField()
    oldest_days_late = serializers.IntegerField(allow_null=True)


class PayableInvoiceSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    revision = serializers.IntegerField()
    store = PayablePartySerializer()
    vendor = PayablePartySerializer()
    brand = PayablePartySerializer()
    season_code = serializers.CharField()
    invoice_number = serializers.CharField()
    invoice_date = serializers.DateField()
    amount_paise = serializers.CharField(help_text="The invoice's total including tax.")
    model = serializers.CharField(help_text="outright, or unknown: no recorded model (D9).")
    payment_days = serializers.IntegerField(allow_null=True)
    due_date = serializers.DateField(allow_null=True, help_text="Null: payment days unknown.")
    paid_paise = serializers.CharField()
    outstanding_paise = serializers.CharField()
    band = serializers.ChoiceField(choices=list(pay.BANDS), allow_null=True)
    days_late = serializers.IntegerField(allow_null=True)
    note = serializers.CharField(allow_blank=True)
    status = serializers.ChoiceField(choices=PayableInvoice.Status.values)
    recorded_by = serializers.CharField(allow_blank=True)
    recorded_at = serializers.DateTimeField()
    cancel_reason = serializers.CharField(allow_blank=True)
    cancelled_by = serializers.CharField(allow_blank=True)
    allowed_actions = serializers.ListField(child=serializers.CharField())


class PayableAllocationSerializer(serializers.Serializer[Any]):
    invoice_id = serializers.IntegerField()
    invoice_number = serializers.CharField()
    brand_name = serializers.CharField()
    amount_paise = serializers.CharField()


class PayablePaymentSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    revision = serializers.IntegerField()
    store = PayablePartySerializer()
    vendor = PayablePartySerializer()
    paid_on = serializers.DateField()
    amount_paise = serializers.CharField()
    mode = serializers.ChoiceField(choices=PayablePayment.Mode.values)
    reference = serializers.CharField()
    note = serializers.CharField(allow_blank=True)
    status = serializers.ChoiceField(choices=PayablePayment.Status.values)
    recorded_by = serializers.CharField(allow_blank=True)
    recorded_at = serializers.DateTimeField()
    cancel_reason = serializers.CharField(allow_blank=True)
    allocations = PayableAllocationSerializer(many=True)
    allowed_actions = serializers.ListField(child=serializers.CharField())


class PayablesSummarySerializer(serializers.Serializer[Any]):
    as_of = serializers.DateField(help_text="Ageing is counted to this day (India).")
    group = serializers.CharField(help_text="brand or vendor: what each row is.")
    store_id = serializers.IntegerField(allow_null=True)
    can_edit = serializers.BooleanField(help_text="Accounts: records invoices and payments.")
    switched_on = serializers.BooleanField(help_text="On at one or more stores in reach.")
    stores = PayableStoreSerializer(many=True)
    rows = PayablePositionSerializer(many=True, help_text="Outright brands or vendors only.")
    totals = PayablePositionSerializer()
    unknown = PayablePositionSerializer(
        many=True,
        help_text="Per brand: invoices of brands with no recorded model, not in the totals.",
    )
    later = serializers.CharField(help_text="Why SOR and consignment are not here.")
    invoices = PayableInvoiceSerializer(many=True)
    payments = PayablePaymentSerializer(many=True)


class PayableVendorOptionSerializer(PayablePartySerializer):
    brand_ids = serializers.ListField(child=serializers.IntegerField())
    is_active = serializers.BooleanField(
        help_text="False: retired. Nothing new is invoiced; what is owed can still be paid."
    )


class PayableSeasonOptionSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class PayableOptionsSerializer(serializers.Serializer[Any]):
    stores = PayableStoreSerializer(many=True)
    vendors = PayableVendorOptionSerializer(many=True)
    brands = PayablePartySerializer(many=True)
    seasons = PayableSeasonOptionSerializer(many=True)
    modes = serializers.ListField(child=serializers.CharField())


class PayableInvoiceRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    store_id = serializers.IntegerField()
    vendor_id = serializers.IntegerField()
    brand_id = serializers.IntegerField()
    season_id = serializers.IntegerField()
    invoice_number = serializers.CharField()
    invoice_date = serializers.DateField()
    amount_paise = serializers.CharField(help_text="Total including tax, whole paise as text.")
    note = serializers.CharField(required=False, allow_blank=True)


class PayableAllocationRequestSerializer(serializers.Serializer[Any]):
    invoice_id = serializers.IntegerField()
    amount_paise = serializers.CharField()


class PayablePaymentRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    store_id = serializers.IntegerField()
    vendor_id = serializers.IntegerField()
    paid_on = serializers.DateField()
    amount_paise = serializers.CharField()
    mode = serializers.ChoiceField(choices=PayablePayment.Mode.values)
    reference = serializers.CharField()
    note = serializers.CharField(required=False, allow_blank=True)
    allocations = PayableAllocationRequestSerializer(many=True)


class PayableCancelRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    expected_revision = serializers.IntegerField()
    reason = serializers.CharField()


# -- building answers ------------------------------------------------------------------

LATER = (
    "Outright brands only. What is owed to SOR and consignment brands arises when "
    "their goods sell, and comes here later (P4), once OQ-26 is decided."
)


def _name(user: Any) -> str:
    if user is None:
        return ""
    return str(getattr(user, "full_name", "") or getattr(user, "username", "") or "")


def _party(row: Any) -> dict[str, Any]:
    return {"id": row.pk, "code": row.code, "name": row.name}


def position_json(row: pay.Position) -> dict[str, Any]:
    return {
        "key": row.key,
        "name": row.label,
        "owed_paise": str(row.owed_paise),
        "paid_paise": str(row.paid_paise),
        "outstanding_paise": str(row.outstanding_paise),
        "due_paise": str(row.due_paise),
        "bands": {name: str(value) for name, value in row.bands.items()},
        "open_invoices": row.open_invoices,
        "oldest_days_late": row.oldest_days_late,
    }


def invoice_json(
    invoice: PayableInvoice, *, paid: int, today: Any, editor: bool, switched_on: bool
) -> dict[str, Any]:
    is_open = invoice.status == PayableInvoice.Status.OPEN
    left = int(invoice.amount_paise) - paid if is_open else 0
    return {
        "id": invoice.pk,
        "revision": invoice.revision,
        "store": _party(invoice.store),
        "vendor": _party(invoice.vendor),
        "brand": _party(invoice.brand),
        "season_code": invoice.season.code,
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date,
        "amount_paise": str(invoice.amount_paise),
        "model": invoice.model,
        "payment_days": invoice.payment_days,
        "due_date": invoice.due_date,
        "paid_paise": str(paid),
        "outstanding_paise": str(left),
        "band": pay.band(invoice.due_date, today) if left > 0 else None,
        "days_late": pay.days_late(invoice.due_date, today) if left > 0 else None,
        "note": invoice.note,
        "status": invoice.status,
        "recorded_by": _name(invoice.recorded_by),
        "recorded_at": invoice.created_at,
        "cancel_reason": invoice.cancel_reason,
        "cancelled_by": _name(invoice.cancelled_by),
        "allowed_actions": ["cancel"] if editor and switched_on and is_open and not paid else [],
    }


def payment_json(
    payment: PayablePayment,
    *,
    invoices: dict[int, PayableInvoice],
    editor: bool,
    switched_on: bool,
) -> dict[str, Any]:
    recorded = payment.status == PayablePayment.Status.RECORDED
    allocations = []
    for row in payment.allocations.all():
        invoice = invoices.get(row.invoice_id)
        allocations.append(
            {
                "invoice_id": row.invoice_id,
                "invoice_number": invoice.invoice_number if invoice else "",
                "brand_name": invoice.brand.name if invoice else "",
                "amount_paise": str(row.amount_paise),
            }
        )
    return {
        "id": payment.pk,
        "revision": payment.revision,
        "store": _party(payment.store),
        "vendor": _party(payment.vendor),
        "paid_on": payment.paid_on,
        "amount_paise": str(payment.amount_paise),
        "mode": payment.mode,
        "reference": payment.reference,
        "note": payment.note,
        "status": payment.status,
        "recorded_by": _name(payment.recorded_by),
        "recorded_at": payment.created_at,
        "cancel_reason": payment.cancel_reason,
        "allocations": sorted(allocations, key=lambda a: a["invoice_id"]),
        "allowed_actions": ["cancel"] if editor and switched_on and recorded else [],
    }


def _require_reader(user: Any) -> None:
    if not pay.may_read(user):
        raise Refusal("ACTION_DENIED", "Payables are Accounts' and the Owner's work.")


def _require_editor(user: Any, site_id: int | None, brand_id: int | None = None) -> None:
    if not pay.may_edit(user, site_id, brand_id):
        raise Refusal(
            "ACTION_DENIED", "Accounts records vendor invoices and the payments made on them."
        )


def _store_filter(value: str, stores: list[Any]) -> int | None:
    if not value:
        return None
    try:
        sid = int(value)
    except ValueError:
        raise Refusal(
            "INVALID_REQUEST",
            "store is a store id.",
            issues=[issue("INVALID", "store is an integer id", field="store")],
        ) from None
    if sid not in {store.pk for store in stores}:
        raise Refusal("NOT_FOUND", "That store was not found.")
    return sid


def _invoice_answer(user: Any, tenant_id: Any, pk: int) -> dict[str, Any]:
    invoice = pay.readable_invoices(user, tenant_id).filter(pk=pk).first()
    if invoice is None:
        raise Refusal("NOT_FOUND", "That invoice was not found.")
    return invoice_json(
        invoice,
        paid=pay.paid_by_invoice([pk]).get(pk, 0),
        today=timezone.localdate(),
        editor=pay.may_edit(user, invoice.store_id, invoice.brand_id),
        switched_on=is_feature_on(invoice.store_id, pay.FEATURE_KEY),
    )


def _payment_answer(user: Any, tenant_id: Any, pk: int) -> dict[str, Any]:
    payment = pay.readable_payments(user, tenant_id).filter(pk=pk).first()
    if payment is None:
        raise Refusal("NOT_FOUND", "That payment was not found.")
    ids = [row.invoice_id for row in payment.allocations.all()]
    invoices = PayableInvoice.objects.select_related("brand").in_bulk(ids)
    return payment_json(
        payment,
        invoices=invoices,
        editor=pay.may_edit(user, payment.store_id),
        switched_on=is_feature_on(payment.store_id, pay.FEATURE_KEY),
    )


# -- the views ---------------------------------------------------------------------------


class GoodsPayablesView(GoodsAPIView):
    @extend_schema(
        parameters=[
            OpenApiParameter(
                "store", int, description="One store; every store in reach if left out."
            ),
            OpenApiParameter("group", str, description="brand (the default) or vendor."),
        ],
        responses=PayablesSummarySerializer,
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, allowed=("store", "group"))
        user = request.user
        _require_reader(user)
        group = params.get("group") or pay.BY_BRAND
        if group not in pay.GROUPS:
            raise Refusal(
                "INVALID_REQUEST",
                "Group by brand or by vendor.",
                issues=[issue("INVALID", "group is brand or vendor", field="group")],
            )
        stores = pay.readable_stores(user, access.tenant_id)
        store_id = _store_filter(params.get("store", ""), stores)
        switches = {store.pk: is_feature_on(store, pay.FEATURE_KEY) for store in stores}
        today = timezone.localdate()

        invoices = pay.readable_invoices(user, access.tenant_id)
        payments = pay.readable_payments(user, access.tenant_id)
        if store_id is not None:
            invoices = invoices.filter(store_id=store_id)
            payments = payments.filter(store_id=store_id)
        every = list(invoices)
        editor = pay.may_edit(user) or any(
            pay.may_edit(user, invoice.store_id, invoice.brand_id) for invoice in every
        )
        paid = pay.paid_by_invoice(inv.pk for inv in every)
        rows, totals, unknown = pay.summarise(pay.figures_of(every, paid), today, group=group)
        by_id = {inv.pk: inv for inv in every}
        shown = sorted(
            every,
            key=lambda inv: (inv.status != PayableInvoice.Status.OPEN, inv.invoice_date, inv.pk),
        )[:LIST_LIMIT]
        paid_rows = list(payments[:LIST_LIMIT])
        missing = {
            row.invoice_id
            for payment in paid_rows
            for row in payment.allocations.all()
            if row.invoice_id not in by_id
        }
        if missing:
            by_id.update(PayableInvoice.objects.select_related("brand").in_bulk(list(missing)))
        body = {
            "as_of": today,
            "group": group,
            "store_id": store_id,
            "can_edit": editor,
            "switched_on": any(switches.values()),
            "stores": [{**_party(store), "switched_on": switches[store.pk]} for store in stores],
            "rows": [position_json(row) for row in rows],
            "totals": position_json(totals),
            "unknown": [position_json(row) for row in unknown],
            "later": LATER,
            "invoices": [
                invoice_json(
                    inv,
                    paid=paid.get(inv.pk, 0),
                    today=today,
                    editor=pay.may_edit(user, inv.store_id, inv.brand_id),
                    switched_on=switches.get(inv.store_id, False),
                )
                for inv in shown
            ],
            "payments": [
                payment_json(
                    payment,
                    invoices=by_id,
                    editor=pay.may_edit(user, payment.store_id),
                    switched_on=switches.get(payment.store_id, False),
                )
                for payment in paid_rows
            ],
        }
        return Response(PayablesSummarySerializer(body).data)


class GoodsPayableOptionsView(GoodsAPIView):
    @extend_schema(responses=PayableOptionsSerializer)
    def get(self, request: Request) -> Response:
        access = self.access(request)
        check_query(request, allowed=())
        user = request.user
        _require_reader(user)
        stores = pay.readable_stores(user, access.tenant_id)
        links: dict[int, list[int]] = {}
        for vendor_id, brand_id in VendorBrand.objects.filter(
            vendor__tenant_id=access.tenant_id
        ).values_list("vendor_id", "brand_id"):
            links.setdefault(vendor_id, []).append(brand_id)
        vendors = Vendor.objects.filter(tenant_id=access.tenant_id).order_by("name")
        brands = Brand.objects.filter(tenant_id=access.tenant_id, is_active=True).order_by("name")
        seasons = Season.objects.filter(historical_unknown=False).order_by("-sort_order", "code")
        body = {
            "stores": [
                {**_party(store), "switched_on": is_feature_on(store, pay.FEATURE_KEY)}
                for store in stores
            ],
            "vendors": [
                {
                    **_party(vendor),
                    "brand_ids": sorted(links.get(vendor.pk, [])),
                    "is_active": vendor.is_active,
                }
                for vendor in vendors
            ],
            "brands": [_party(brand) for brand in brands],
            "seasons": [{"id": s.pk, "code": s.code, "name": s.name} for s in seasons],
            "modes": list(PayablePayment.Mode.values),
        }
        return Response(PayableOptionsSerializer(body).data)


class GoodsPayableInvoiceCreateView(GoodsAPIView):
    @extend_schema(request=PayableInvoiceRequestSerializer, responses=PayableInvoiceSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        user = request.user
        _require_reader(user)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {
                "store_id",
                "vendor_id",
                "brand_id",
                "season_id",
                "invoice_number",
                "invoice_date",
                "amount_paise",
                "note",
            },
            required=(
                "store_id",
                "vendor_id",
                "brand_id",
                "season_id",
                "invoice_number",
                "invoice_date",
                "amount_paise",
            ),
        )
        store_id = body.get("store_id") if isinstance(body.get("store_id"), int) else None
        brand_id = body.get("brand_id") if isinstance(body.get("brand_id"), int) else None
        _require_editor(user, store_id, brand_id)

        def handler(run: CommandRun) -> CommandResult:
            invoice = pay.record_invoice(
                run,
                user=user,
                tenant_id=access.tenant_id,
                body=body,
                today=timezone.localdate(run.now),
            )
            return CommandResult(
                resource_type="payable_invoice", resource_id=str(invoice.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action=pay.RECORD_INVOICE_ACTION,
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=store_id,
        )
        return Response(
            PayableInvoiceSerializer(
                _invoice_answer(user, access.tenant_id, int(result.resource_id or 0))
            ).data,
            status=201,
        )


class GoodsPayablePaymentCreateView(GoodsAPIView):
    @extend_schema(request=PayablePaymentRequestSerializer, responses=PayablePaymentSerializer)
    def post(self, request: Request) -> Response:
        access = self.access(request)
        user = request.user
        _require_reader(user)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {
                "store_id",
                "vendor_id",
                "paid_on",
                "amount_paise",
                "mode",
                "reference",
                "note",
                "allocations",
            },
            required=(
                "store_id",
                "vendor_id",
                "paid_on",
                "amount_paise",
                "mode",
                "reference",
                "allocations",
            ),
        )
        store_id = body.get("store_id") if isinstance(body.get("store_id"), int) else None
        _require_editor(user, store_id)

        def handler(run: CommandRun) -> CommandResult:
            payment = pay.record_payment(
                run,
                user=user,
                tenant_id=access.tenant_id,
                body=body,
                today=timezone.localdate(run.now),
            )
            return CommandResult(
                resource_type="payable_payment", resource_id=str(payment.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action=pay.RECORD_PAYMENT_ACTION,
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=store_id,
        )
        return Response(
            PayablePaymentSerializer(
                _payment_answer(user, access.tenant_id, int(result.resource_id or 0))
            ).data,
            status=201,
        )


class GoodsPayableInvoiceCancelView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_finledger_payables_invoices_cancel",
        request=PayableCancelRequestSerializer,
        responses=PayableInvoiceSerializer,
    )
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        user = request.user
        _require_reader(user)
        current = _invoice_answer(user, access.tenant_id, pk)
        _require_editor(user, current["store"]["id"], current["brand"]["id"])
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reason"}, required=("reason",))

        def handler(run: CommandRun) -> CommandResult:
            pay.cancel_invoice(
                run,
                pk,
                user=user,
                reason=body["reason"],
                expected_revision=meta.expected_revision,
            )
            return CommandResult(resource_type="payable_invoice", resource_id=str(pk))

        self.run_command(
            request,
            access=access,
            action=pay.CANCEL_INVOICE_ACTION,
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"payable_invoice:{pk}",
            site_id=current["store"]["id"],
        )
        return Response(PayableInvoiceSerializer(_invoice_answer(user, access.tenant_id, pk)).data)


class GoodsPayablePaymentCancelView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_finledger_payables_payments_cancel",
        request=PayableCancelRequestSerializer,
        responses=PayablePaymentSerializer,
    )
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        user = request.user
        _require_reader(user)
        current = _payment_answer(user, access.tenant_id, pk)
        _require_editor(user, current["store"]["id"])
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reason"}, required=("reason",))

        def handler(run: CommandRun) -> CommandResult:
            pay.cancel_payment(
                run,
                pk,
                user=user,
                reason=body["reason"],
                expected_revision=meta.expected_revision,
            )
            return CommandResult(resource_type="payable_payment", resource_id=str(pk))

        self.run_command(
            request,
            access=access,
            action=pay.CANCEL_PAYMENT_ACTION,
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"payable_payment:{pk}",
            site_id=current["store"]["id"],
        )
        return Response(PayablePaymentSerializer(_payment_answer(user, access.tenant_id, pk)).data)
