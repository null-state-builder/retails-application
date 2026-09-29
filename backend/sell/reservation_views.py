"""`/api/sell/reservations` - customer reservations (store operations ticket 20, ST-ORD-1).

Online only: every call is a request to head office, and nothing is queued on a
device. Anyone who works the counter (``sell: operate``) at the store, and only
at a store they may act at (``actionable_stores``). Making a reservation needs
the store's ``customer-reservation`` switch on; reading, collecting,
cancelling and refunding a reservation already made never do (ticket 01's rule:
refuse only new work). The rules are ``sell.services.reservations``.
"""

from __future__ import annotations

from datetime import date, timedelta
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
from masters.models import Store
from masters.scoping import actionable_stores
from masters.store_feature_registry import CUSTOMER_RESERVATION
from masters.store_features import is_feature_on
from sell.permissions import CanRunTill
from sell.reservation_models import CustomerReservation
from sell.services import reservations as rules
from sell.services.customers import normalise_mobile

# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class ReservationLineWriteSerializer(serializers.Serializer[dict[str, Any]]):
    barcode = serializers.CharField(max_length=64)
    season = serializers.CharField(max_length=24, required=False, allow_blank=True, default="")
    qty = serializers.IntegerField(min_value=1, max_value=99)


class ReservationAdvanceWriteSerializer(serializers.Serializer[dict[str, Any]]):
    amount_paise = serializers.IntegerField(min_value=1)
    mode = serializers.ChoiceField(choices=rules.TENDER_MODES)
    reference = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")


class ReservationWriteSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField(help_text="The screen's own id; a retry reuses it.")
    store = serializers.CharField(max_length=16)
    customer_name = serializers.CharField(max_length=120)
    customer_mobile = serializers.CharField(max_length=20)
    lines = serializers.ListField(child=ReservationLineWriteSerializer(), allow_empty=False)
    advance = ReservationAdvanceWriteSerializer(required=False, allow_null=True)


class ReservationStoreWriteSerializer(serializers.Serializer[dict[str, Any]]):
    store = serializers.CharField(max_length=16)
    command_id = serializers.UUIDField(
        required=False,
        help_text="The screen's own id for this attempt; a retry of the same tap reuses it.",
    )


class ReservationCancelWriteSerializer(ReservationStoreWriteSerializer):
    by = serializers.ChoiceField(
        choices=["customer", "store"],
        help_text="customer: the advance follows the reservation's policy; store: refunded.",
    )


class ReservationItemSerializer(serializers.Serializer[dict[str, Any]]):
    barcode = serializers.CharField()
    season = serializers.CharField()
    design = serializers.CharField()
    brand = serializers.CharField()
    item = serializers.CharField()
    size = serializers.CharField()
    color = serializers.CharField()
    hsn = serializers.CharField(allow_blank=True)
    mrp_paise = serializers.IntegerField(allow_null=True)
    no_discount = serializers.BooleanField()
    season_unknown_historical = serializers.BooleanField()


class ReservationPieceSerializer(serializers.Serializer[dict[str, Any]]):
    line_no = serializers.IntegerField()
    barcode = serializers.CharField()
    season = serializers.CharField()
    qty = serializers.IntegerField()
    item = ReservationItemSerializer()


class ReceiptVoucherSerializer(serializers.Serializer[dict[str, Any]]):
    number = serializers.CharField()
    issued_on = serializers.DateField()
    amount_paise = serializers.IntegerField()
    mode = serializers.CharField()
    reference = serializers.CharField(allow_blank=True)
    store_name = serializers.CharField()
    store_gstin = serializers.CharField()


class ReservationSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField()
    ref = serializers.CharField()
    store = serializers.CharField()
    status = serializers.ChoiceField(choices=CustomerReservation.Status.choices)
    customer_name = serializers.CharField()
    customer_mobile = serializers.CharField()
    reserved_on = serializers.DateField()
    collect_by = serializers.DateField()
    days = serializers.IntegerField()
    days_left = serializers.IntegerField()
    sale_period = serializers.BooleanField()
    advance_policy = serializers.ChoiceField(choices=CustomerReservation.Policy.choices)
    terms = serializers.CharField()
    pieces = ReservationPieceSerializer(many=True)
    voucher = ReceiptVoucherSerializer(allow_null=True)
    advance_balance_paise = serializers.IntegerField()
    advance_outcome = serializers.ChoiceField(
        choices=["none", "held", "used", "refunded", "forfeited", "refund_due"]
    )
    close_reason = serializers.CharField(allow_blank=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    sale_doc_number = serializers.CharField(allow_null=True)


class ReservationListSerializer(serializers.Serializer[dict[str, Any]]):
    store = serializers.CharField()
    switched_on = serializers.BooleanField(
        help_text="Whether a new reservation can be made at this store now."
    )
    reservations = ReservationSerializer(many=True)


class ReservationTermsSerializer(serializers.Serializer[dict[str, Any]]):
    """What a reservation made now would be: its length, date and terms."""

    store = serializers.CharField()
    switched_on = serializers.BooleanField()
    days = serializers.IntegerField()
    sale_period = serializers.BooleanField()
    collect_by = serializers.DateField()
    advance_policy = serializers.ChoiceField(choices=CustomerReservation.Policy.choices)
    terms_with_advance = serializers.CharField()
    terms_without_advance = serializers.CharField()


def reservation_body(reservation: CustomerReservation, today: date) -> dict[str, Any]:
    voucher = getattr(reservation, "voucher", None)
    store = reservation.store
    return {
        "id": reservation.pk,
        "ref": reservation.ref,
        "store": store.code,
        "status": reservation.status,
        "customer_name": reservation.customer_name,
        "customer_mobile": reservation.customer_mobile,
        "reserved_on": reservation.reserved_on,
        "collect_by": reservation.collect_by,
        "days": reservation.days,
        "days_left": (reservation.collect_by - today).days,
        "sale_period": reservation.sale_period,
        "advance_policy": reservation.advance_policy,
        "terms": reservation.terms,
        "pieces": [
            {
                "line_no": piece.line_no,
                "barcode": piece.barcode,
                "season": piece.season,
                "qty": piece.qty,
                "item": piece.item,
            }
            for piece in reservation.pieces.all()
        ],
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
        "advance_balance_paise": rules.balance_paise(reservation),
        "advance_outcome": rules.advance_outcome(reservation),
        "close_reason": reservation.close_reason,
        "closed_at": reservation.closed_at,
        "sale_doc_number": reservation.sale.doc_number if reservation.sale is not None else None,
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


def _rows(store: Store) -> QuerySet[CustomerReservation]:
    return (
        CustomerReservation.objects.filter(store=store)
        .select_related("store", "store__gstin", "sale", "voucher")
        .prefetch_related("pieces", "advance_movements")
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class ReservationListView(APIView):
    """`GET` - this store's reservations: open ones first, then those ended in the
    last 30 days or still owing a refund. `q` finds a reference, voucher number or
    mobile. `POST` - make one (the switch must be on)."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        operation_id="sell_reservations_list",
        parameters=[
            OpenApiParameter("store", str, required=True),
            OpenApiParameter("q", str, required=False),
        ],
        responses=ReservationListSerializer,
    )
    def get(self, request: Request) -> Response:
        store = _store(request.user, request.query_params.get("store", ""))
        today = timezone.localdate()
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
            rows = rows.filter(
                Q(status=CustomerReservation.Status.ACTIVE) | Q(closed_at__gte=since)
            )
        found = list(rows)
        owing = [
            r for r in _rows(store).exclude(pk__in=[r.pk for r in found]) if rules.refund_due(r)
        ]
        ordered = sorted(
            [*found, *owing],
            key=lambda r: (r.status != CustomerReservation.Status.ACTIVE, r.collect_by, r.ref),
        )
        body = {
            "store": store.code,
            "switched_on": is_feature_on(store, CUSTOMER_RESERVATION),
            "reservations": [reservation_body(r, today) for r in ordered],
        }
        return Response(ReservationListSerializer(body).data)

    @extend_schema(
        request=ReservationWriteSerializer,
        responses={200: ReservationSerializer, 201: ReservationSerializer},
    )
    def post(self, request: Request) -> Response:
        form = ReservationWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        data = dict(form.validated_data)
        store = _store(request.user, data["store"])
        made = rules.create(store, request.user, data)
        reservation = _rows(store).get(pk=made.reservation.pk)
        return Response(
            ReservationSerializer(reservation_body(reservation, timezone.localdate())).data,
            status=status.HTTP_201_CREATED if made.created else status.HTTP_200_OK,
        )


class ReservationTermsView(APIView):
    """`GET` - what a reservation made here now would be: length, date, terms."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        parameters=[OpenApiParameter("store", str, required=True)],
        responses=ReservationTermsSerializer,
    )
    def get(self, request: Request) -> Response:
        store = _store(request.user, request.query_params.get("store", ""))
        today = timezone.localdate()
        length = rules.length_for(store, today)
        policy = rules.current_policy()
        body = {
            "store": store.code,
            "switched_on": is_feature_on(store, CUSTOMER_RESERVATION),
            "days": length.days,
            "sale_period": length.sale_period,
            "collect_by": length.collect_by,
            "advance_policy": policy,
            "terms_with_advance": rules.terms_for(policy, length.collect_by, 1),
            "terms_without_advance": rules.terms_for(policy, length.collect_by, 0),
        }
        return Response(ReservationTermsSerializer(body).data)


class ReservationDetailView(APIView):
    """`GET` - one reservation, with its pieces as the counter bills them."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        operation_id="sell_reservations_detail",
        parameters=[OpenApiParameter("store", str, required=True)],
        responses=ReservationSerializer,
    )
    def get(self, request: Request, pk: str) -> Response:
        store = _store(request.user, request.query_params.get("store", ""))
        reservation = _rows(store).filter(pk=pk).first()
        if reservation is None:
            raise Refusal("NOT_FOUND", "That reservation was not found at this store.", status=404)
        return Response(
            ReservationSerializer(reservation_body(reservation, timezone.localdate())).data
        )


class ReservationCancelView(APIView):
    """`POST` - cancel an active reservation: pieces back, advance by policy."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=ReservationCancelWriteSerializer, responses=ReservationSerializer)
    def post(self, request: Request, pk: str) -> Response:
        form = ReservationCancelWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        store = _store(request.user, form.validated_data["store"])
        rules.cancel(
            store,
            request.user,
            pk,
            by_customer=form.validated_data["by"] == "customer",
            command_id=form.validated_data.get("command_id"),
        )
        reservation = _rows(store).get(pk=pk)
        return Response(
            ReservationSerializer(reservation_body(reservation, timezone.localdate())).data
        )


class ReservationRefundView(APIView):
    """`POST` - record paying back what an ended reservation still holds."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(request=ReservationStoreWriteSerializer, responses=ReservationSerializer)
    def post(self, request: Request, pk: str) -> Response:
        form = ReservationStoreWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        store = _store(request.user, form.validated_data["store"])
        rules.refund(store, request.user, pk, command_id=form.validated_data.get("command_id"))
        reservation = _rows(store).get(pk=pk)
        return Response(
            ReservationSerializer(reservation_body(reservation, timezone.localdate())).data
        )
