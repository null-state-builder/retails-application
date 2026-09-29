"""`/api/sell/gift-vouchers` - gift vouchers (store operations ticket 19, ST-POS-4).

Online only: every call is a request to head office, and nothing is queued on a
device. Anyone who works the counter (``sell: operate``) at the store, and only
at a store they may act at (``actionable_stores``). Selling a voucher and looking
one up to take it need the store's ``gift-vouchers`` switch on; the list never
does (ticket 01's rule: refuse only new work). The rules are
``sell.services.gift_vouchers``.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db.models import QuerySet
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
from masters.store_feature_registry import GIFT_VOUCHERS
from masters.store_features import is_feature_on
from sell.gift_voucher_models import GiftVoucher, GiftVoucherMovement
from sell.permissions import CanRunTill
from sell.services import gift_vouchers as rules

#: What the list shows: every voucher, those still usable, or those expired.
SHOW = ("all", "active", "expired")
#: The most vouchers one list answers with, newest first.
LIMIT = 500

# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class GiftVoucherWriteSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField(help_text="The till's own id for this sale; a retry reuses it.")
    store = serializers.CharField(max_length=16)
    value_paise = serializers.IntegerField(min_value=1)
    mode = serializers.ChoiceField(choices=["cash", "card", "upi"])
    reference = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")


class GiftVoucherMovementSerializer(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=GiftVoucherMovement.Kind.choices)
    amount_paise = serializers.IntegerField()
    at = serializers.DateTimeField()
    store = serializers.CharField()
    sale_doc_number = serializers.CharField(allow_null=True)


class GiftVoucherSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    number = serializers.CharField()
    store = serializers.CharField()
    store_name = serializers.CharField()
    store_gstin = serializers.CharField()
    issued_on = serializers.DateField()
    valid_until = serializers.DateField(help_text="The last day it can be used.")
    value_paise = serializers.IntegerField()
    mode = serializers.CharField()
    reference = serializers.CharField(allow_blank=True)
    balance_paise = serializers.IntegerField()
    expired_paise = serializers.IntegerField(
        help_text="Written off when it expired unused, with no GST."
    )
    state = serializers.ChoiceField(choices=["active", "used_up", "expired"])
    movements = GiftVoucherMovementSerializer(many=True)


class GiftVoucherSoldSerializer(GiftVoucherSerializer):
    """The voucher as the till that sold it gets it back, once: with the check
    code to print on the slip. No list or look-up ever shows the code (B311)."""

    code = serializers.CharField()


class GiftVoucherListSerializer(serializers.Serializer[dict[str, Any]]):
    store = serializers.CharField()
    switched_on = serializers.BooleanField(
        help_text="Whether this store sells and takes gift vouchers now."
    )
    months = serializers.IntegerField(help_text="How many months a new voucher can be used for.")
    no_gst_note = serializers.CharField()
    #: What the vouchers listed as expired held when they expired, with no GST.
    expired_unused_paise = serializers.IntegerField()
    #: More vouchers matched than are listed (the newest ``LIMIT``).
    cut = serializers.BooleanField()
    gift_vouchers = GiftVoucherSerializer(many=True)


def voucher_body(voucher: GiftVoucher, today: Any = None) -> dict[str, Any]:
    day = today or timezone.localdate()
    store = voucher.store
    return {
        "id": voucher.pk,
        "number": voucher.number,
        "store": store.code,
        "store_name": store.name,
        "store_gstin": store.gstin.gstin,
        "issued_on": voucher.issued_on,
        "valid_until": voucher.valid_until,
        "value_paise": voucher.value_paise,
        "mode": voucher.mode,
        "reference": voucher.reference,
        "balance_paise": rules.balance_paise(voucher),
        "expired_paise": rules.expired_paise(voucher),
        "state": rules.state_on(voucher, day),
        "movements": [
            {
                "kind": row.kind,
                "amount_paise": row.amount_paise,
                "at": row.at,
                "store": row.store.code,
                "sale_doc_number": row.sale.doc_number if row.sale is not None else None,
            }
            for row in voucher.movements.all()
        ],
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


def _loaded() -> QuerySet[GiftVoucher]:
    return GiftVoucher.objects.select_related("store__gstin").prefetch_related(
        "movements__sale", "movements__store"
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class GiftVoucherListView(APIView):
    """`GET` - the vouchers this store sold, newest first: `show=active` those still
    usable, `show=expired` those past their last day (what they held expires with
    no GST); `q` finds a number. `POST` - sell one (the switch must be on)."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        parameters=[
            OpenApiParameter("store", str, required=True),
            OpenApiParameter("show", str, required=False, enum=list(SHOW)),
            OpenApiParameter("q", str, required=False),
        ],
        responses=GiftVoucherListSerializer,
    )
    def get(self, request: Request) -> Response:
        store = _store(request.user, request.query_params.get("store", ""))
        show = request.query_params.get("show") or "all"
        if show not in SHOW:
            raise Refusal("VALIDATION", "show is all, active or expired.", status=400)
        rows = rules.rows_for(store).select_related("store__gstin")
        q = rules.normalise_number(request.query_params.get("q") or "")
        if q:
            rows = rows.filter(number__icontains=q)
        today = timezone.localdate()
        bodies = [voucher_body(v, today) for v in rows]
        if show == "active":
            bodies = [b for b in bodies if b["state"] == rules.ACTIVE]
        elif show == "expired":
            bodies = [b for b in bodies if b["state"] == rules.EXPIRED]
        body = {
            "store": store.code,
            "switched_on": is_feature_on(store, GIFT_VOUCHERS),
            "months": int(settings.KDPS_GIFT_VOUCHER_MONTHS),
            "no_gst_note": rules.NO_GST_NOTE,
            "expired_unused_paise": sum(
                b["expired_paise"] + b["balance_paise"]
                for b in bodies
                if b["state"] == rules.EXPIRED
            ),
            "cut": len(bodies) > LIMIT,
            "gift_vouchers": bodies[:LIMIT],
        }
        return Response(GiftVoucherListSerializer(body).data)

    @extend_schema(
        request=GiftVoucherWriteSerializer,
        responses={200: GiftVoucherSoldSerializer, 201: GiftVoucherSoldSerializer},
    )
    def post(self, request: Request) -> Response:
        form = GiftVoucherWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        data = dict(form.validated_data)
        store = _store(request.user, data["store"])
        made = rules.issue(store, request.user, data)
        voucher = _loaded().get(pk=made.voucher.pk)
        return Response(
            GiftVoucherSoldSerializer({**voucher_body(voucher), "code": voucher.code}).data,
            status=status.HTTP_201_CREATED if made.created else status.HTTP_200_OK,
        )


class GiftVoucherLookupView(APIView):
    """`GET` - the voucher the till is about to take, judged for this store today:
    refused where the switch is off here, or the voucher is unknown, was sold under
    another GSTIN, has expired or has nothing left."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        parameters=[
            OpenApiParameter("store", str, required=True),
            OpenApiParameter("number", str, required=True),
            OpenApiParameter("code", str, required=True),
        ],
        responses=GiftVoucherSerializer,
    )
    def get(self, request: Request) -> Response:
        store = _store(request.user, request.query_params.get("store", ""))
        number = request.query_params.get("number") or ""
        if not number.strip():
            raise Refusal("VALIDATION", "Type the voucher's number.", status=400)
        found = rules.look_up(store, number, request.query_params.get("code") or "")
        return Response(GiftVoucherSerializer(voucher_body(_loaded().get(pk=found.pk))).data)
