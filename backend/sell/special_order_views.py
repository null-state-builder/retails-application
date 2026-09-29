"""`/api/sell/special-orders` - special orders (store operations ticket 21, ST-ORD-2).

Online only: every call is a request to head office, and nothing is queued on a
device. Anyone who works the counter (``sell: operate``) at the store, and only
at a store they may act at (``actionable_stores``). Taking a new order needs the
store's ``special-orders`` switch on; moving, cancelling and refunding an order
already taken never do (ticket 01's rule: refuse only new work). Raising a
transfer request also needs the person's own ``transfer.allocate`` at the store.
The rules are ``sell.services.special_orders``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.refusals import Refusal, first_message, refusal_body
from masters.models import Brand, Store
from masters.scoping import actionable_stores
from masters.store_feature_registry import SPECIAL_ORDERS
from masters.store_features import is_feature_on
from sell.permissions import CanRunTill
from sell.reservation_models import CustomerReservation
from sell.reservation_views import ReceiptVoucherSerializer, ReservationAdvanceWriteSerializer
from sell.services import special_orders as rules
from sell.services.customers import normalise_mobile
from sell.services.goods_stock import is_goods_site
from sell.special_order_models import SpecialOrder

# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class SpecialOrderWriteSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField(help_text="The screen's own id; a retry reuses it.")
    store = serializers.CharField(max_length=16)
    customer_name = serializers.CharField(max_length=120)
    customer_mobile = serializers.CharField(max_length=20)
    brand = serializers.IntegerField()
    style_code = serializers.CharField(max_length=80)
    size = serializers.CharField(max_length=24)
    colour = serializers.CharField(max_length=40)
    note = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    #: The same advance as a reservation's (ticket 20).
    advance = ReservationAdvanceWriteSerializer(required=False, allow_null=True)


class SpecialOrderStepWriteSerializer(serializers.Serializer[dict[str, Any]]):
    store = serializers.CharField(max_length=16)
    command_id = serializers.UUIDField(
        required=False,
        help_text="The screen's own id for this attempt; a retry of the same tap reuses it.",
    )


class SpecialOrderOrderWriteSerializer(SpecialOrderStepWriteSerializer):
    route = serializers.ChoiceField(choices=SpecialOrder.Route.choices)
    source_site = serializers.CharField(
        max_length=16,
        required=False,
        allow_blank=True,
        default="",
        help_text="Transfer: the code of the site that holds the item.",
    )
    barcode = serializers.CharField(
        max_length=64,
        required=False,
        allow_blank=True,
        default="",
        help_text="Transfer: the item's barcode.",
    )
    booking = serializers.CharField(
        max_length=128,
        required=False,
        allow_blank=True,
        default="",
        help_text="Booking: the confirmed booking's number.",
    )


class SpecialOrderArriveWriteSerializer(SpecialOrderStepWriteSerializer):
    barcode = serializers.CharField(max_length=64)


class SpecialOrderTellWriteSerializer(SpecialOrderStepWriteSerializer):
    told_how = serializers.ChoiceField(choices=SpecialOrder.Told.choices)


class SpecialOrderCancelWriteSerializer(SpecialOrderStepWriteSerializer):
    by = serializers.ChoiceField(
        choices=["customer", "store"],
        help_text="customer: the advance follows the policy; store: refunded.",
    )


class SpecialOrderSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField()
    ref = serializers.CharField()
    store = serializers.CharField()
    status = serializers.ChoiceField(choices=SpecialOrder.Status.choices)
    customer_name = serializers.CharField()
    customer_mobile = serializers.CharField()
    brand = serializers.IntegerField()
    brand_name = serializers.CharField()
    style_code = serializers.CharField()
    size = serializers.CharField()
    colour = serializers.CharField()
    note = serializers.CharField(allow_blank=True)
    asked_on = serializers.DateField()
    advance_policy = serializers.ChoiceField(choices=CustomerReservation.Policy.choices)
    terms = serializers.CharField()
    route = serializers.CharField(allow_blank=True)
    ordered_barcode = serializers.CharField(allow_blank=True)
    transfer_request_id = serializers.UUIDField(allow_null=True)
    transfer_source = serializers.CharField(allow_null=True)
    booking_number = serializers.CharField(allow_null=True)
    ordered_at = serializers.DateTimeField(allow_null=True)
    arrived_barcode = serializers.CharField(allow_blank=True)
    arrived_at = serializers.DateTimeField(allow_null=True)
    told_how = serializers.CharField(allow_blank=True)
    told_at = serializers.DateTimeField(allow_null=True)
    voucher = ReceiptVoucherSerializer(allow_null=True)
    advance_balance_paise = serializers.IntegerField()
    advance_outcome = serializers.ChoiceField(
        choices=["none", "held", "used", "refunded", "forfeited", "refund_due"]
    )
    close_reason = serializers.CharField(allow_blank=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    sale_doc_number = serializers.CharField(allow_null=True)


class SpecialOrderChoiceSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    name = serializers.CharField()


class SpecialOrderSourceSerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField()
    name = serializers.CharField()


class SpecialOrderListSerializer(serializers.Serializer[dict[str, Any]]):
    store = serializers.CharField()
    switched_on = serializers.BooleanField(
        help_text="Whether a new special order can be taken at this store now."
    )
    advance_policy = serializers.ChoiceField(choices=CustomerReservation.Policy.choices)
    terms_with_advance = serializers.CharField()
    terms_without_advance = serializers.CharField()
    brands = SpecialOrderChoiceSerializer(many=True)
    #: Sites a transfer request can ask; empty where this store takes none.
    sources = SpecialOrderSourceSerializer(many=True)
    special_orders = SpecialOrderSerializer(many=True)


def order_body(order: SpecialOrder) -> dict[str, Any]:
    voucher = getattr(order, "voucher", None)
    store = order.store
    request = order.transfer_request
    return {
        "id": order.pk,
        "ref": order.ref,
        "store": store.code,
        "status": order.status,
        "customer_name": order.customer_name,
        "customer_mobile": order.customer_mobile,
        "brand": order.brand_id,
        "brand_name": order.brand.name,
        "style_code": order.style,
        "size": order.size,
        "colour": order.colour,
        "note": order.note,
        "asked_on": order.asked_on,
        "advance_policy": order.advance_policy,
        "terms": order.terms,
        "route": order.route,
        "ordered_barcode": order.ordered_barcode,
        "transfer_request_id": request.pk if request is not None else None,
        "transfer_source": request.source_site.code if request is not None else None,
        "booking_number": rules.booking_number(order),
        "ordered_at": order.ordered_at,
        "arrived_barcode": order.arrived_barcode,
        "arrived_at": order.arrived_at,
        "told_how": order.told_how,
        "told_at": order.told_at,
        "voucher": None
        if voucher is None
        else {
            "number": voucher.number,
            "issued_on": voucher.issued_on,
            "amount_paise": voucher.amount_paise,
            "mode": voucher.mode,
            "reference": voucher.reference,
            "store_name": store.name,
            "store_gstin": store.gstin.gstin,
        },
        "advance_balance_paise": rules.balance_paise(order),
        "advance_outcome": rules.advance_outcome(order),
        "close_reason": order.close_reason,
        "closed_at": order.closed_at,
        "sale_doc_number": order.sale.doc_number if order.sale is not None else None,
    }


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _store(user: Any, code: str) -> Store:
    """The store named, if this person may act there; otherwise refused."""
    code = (code or "").strip().upper()
    if not code:
        raise Refusal("VALIDATION", "Name the store.", status=400)
    store: Store | None = actionable_stores(user).select_related("gstin").filter(code=code).first()
    if store is None:
        raise Refusal("SCOPE_DENIED", f"You cannot work at {code}.", status=403)
    return store


def _rows(store: Store) -> QuerySet[SpecialOrder]:
    return (
        SpecialOrder.objects.filter(store=store)
        .select_related(
            "store",
            "store__gstin",
            "brand",
            "sale",
            "voucher",
            "transfer_request__source_site",
            "booking__document",
        )
        .prefetch_related("advance_movements")
    )


def _sources(store: Store) -> list[dict[str, str]]:
    """Other goods sites of the same legal entity a transfer request could ask
    (a request across entities is refused)."""
    if not is_goods_site(store):
        return []
    rows = (
        Store.objects.filter(
            is_active=True,
            tenant_id=store.tenant_id,
            gstin__legal_entity_id=store.gstin.legal_entity_id,
        )
        .exclude(pk=store.pk)
        .order_by("code")
    )
    return [{"code": s.code, "name": s.name} for s in rows if is_goods_site(s)]


def _answer(store: Store, pk: Any) -> Response:
    return Response(SpecialOrderSerializer(order_body(_rows(store).get(pk=pk))).data)


def _form(serializer: type[serializers.Serializer[dict[str, Any]]], data: Any) -> dict[str, Any]:
    form = serializer(data=data)
    if not form.is_valid():
        raise Refusal("VALIDATION", first_message(form.errors), status=400)
    return dict(form.validated_data)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class SpecialOrderListView(APIView):
    """`GET` - this store's special orders: open ones first, then those ended in the
    last 30 days or still owing a refund, with the brands and sources the form
    offers. `q` finds a reference, voucher number or mobile. `POST` - take one
    (the switch must be on)."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        operation_id="sell_special_orders_list",
        parameters=[
            OpenApiParameter("store", str, required=True),
            OpenApiParameter("q", str, required=False),
        ],
        responses=SpecialOrderListSerializer,
    )
    def get(self, request: Request) -> Response:
        store = _store(request.user, request.query_params.get("store", ""))
        rows = _rows(store)
        q = (request.query_params.get("q") or "").strip()
        if q:
            mobile = normalise_mobile(q)
            match = Q(ref__iexact=q) | Q(voucher__number__iexact=q)
            if len(mobile) == 10:
                match |= Q(customer_mobile=mobile)
            rows = rows.filter(match)
        else:
            since = timezone.now() - timedelta(days=30)
            rows = rows.filter(Q(status__in=rules.OPEN) | Q(closed_at__gte=since))
        found = list(rows)
        owing = [
            o for o in _rows(store).exclude(pk__in=[o.pk for o in found]) if rules.refund_due(o)
        ]
        ordered = sorted([*found, *owing], key=lambda o: (not o.is_open, o.asked_on, o.created_at))
        policy = rules.current_policy()
        body = {
            "store": store.code,
            "switched_on": is_feature_on(store, SPECIAL_ORDERS),
            "advance_policy": policy,
            "terms_with_advance": rules.terms_for(policy, 1),
            "terms_without_advance": rules.terms_for(policy, 0),
            "brands": [
                {"id": b.pk, "name": b.name}
                for b in Brand.objects.filter(is_active=True).order_by("name")
            ],
            "sources": _sources(store),
            "special_orders": [order_body(o) for o in ordered],
        }
        return Response(SpecialOrderListSerializer(body).data)

    @extend_schema(
        request=SpecialOrderWriteSerializer,
        responses={200: SpecialOrderSerializer, 201: SpecialOrderSerializer},
    )
    def post(self, request: Request) -> Response:
        form = SpecialOrderWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        data = dict(form.validated_data)
        store = _store(request.user, data["store"])
        made = rules.create(store, request.user, data)
        order = _rows(store).get(pk=made.order.pk)
        return Response(
            SpecialOrderSerializer(order_body(order)).data,
            status=status.HTTP_201_CREATED if made.created else status.HTTP_200_OK,
        )


class SpecialOrderDetailView(APIView):
    """`GET` - one special order."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        operation_id="sell_special_orders_detail",
        parameters=[OpenApiParameter("store", str, required=True)],
        responses=SpecialOrderSerializer,
    )
    def get(self, request: Request, pk: str) -> Response:
        store = _store(request.user, request.query_params.get("store", ""))
        if not _rows(store).filter(pk=pk).exists():
            raise Refusal(
                "NOT_FOUND", "That special order was not found at this store.", status=404
            )
        return _answer(store, pk)


class SpecialOrderOrderView(APIView):
    """`POST` - asked to ordered: raise a transfer request, or name the booking line."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=SpecialOrderOrderWriteSerializer, responses=SpecialOrderSerializer)
    def post(self, request: Request, pk: str) -> Response:
        data = _form(SpecialOrderOrderWriteSerializer, request.data)
        store = _store(request.user, data["store"])
        if data["route"] == SpecialOrder.Route.TRANSFER:
            rules.order_by_transfer(
                store,
                request.user,
                pk,
                source_code=data["source_site"],
                barcode=data["barcode"],
                command_id=data.get("command_id"),
            )
        else:
            rules.order_by_booking(
                store, request.user, pk, number=data["booking"], command_id=data.get("command_id")
            )
        return _answer(store, pk)


class SpecialOrderArriveView(APIView):
    """`POST` - ordered to arrived: the piece is here; scan its tag."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=SpecialOrderArriveWriteSerializer, responses=SpecialOrderSerializer)
    def post(self, request: Request, pk: str) -> Response:
        data = _form(SpecialOrderArriveWriteSerializer, request.data)
        store = _store(request.user, data["store"])
        rules.arrive(
            store, request.user, pk, barcode=data["barcode"], command_id=data.get("command_id")
        )
        return _answer(store, pk)


class SpecialOrderTellView(APIView):
    """`POST` - arrived to customer told, and how."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=SpecialOrderTellWriteSerializer, responses=SpecialOrderSerializer)
    def post(self, request: Request, pk: str) -> Response:
        data = _form(SpecialOrderTellWriteSerializer, request.data)
        store = _store(request.user, data["store"])
        rules.tell(store, request.user, pk, how=data["told_how"], command_id=data.get("command_id"))
        return _answer(store, pk)


class SpecialOrderCancelView(APIView):
    """`POST` - cancel an open order: the advance by policy."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=SpecialOrderCancelWriteSerializer, responses=SpecialOrderSerializer)
    def post(self, request: Request, pk: str) -> Response:
        data = _form(SpecialOrderCancelWriteSerializer, request.data)
        store = _store(request.user, data["store"])
        rules.cancel(
            store,
            request.user,
            pk,
            by_customer=data["by"] == "customer",
            command_id=data.get("command_id"),
        )
        return _answer(store, pk)


class SpecialOrderRefundView(APIView):
    """`POST` - record paying back what an ended order still holds."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=SpecialOrderStepWriteSerializer, responses=SpecialOrderSerializer)
    def post(self, request: Request, pk: str) -> Response:
        data = _form(SpecialOrderStepWriteSerializer, request.data)
        store = _store(request.user, data["store"])
        rules.refund(store, request.user, pk, command_id=data.get("command_id"))
        return _answer(store, pk)
