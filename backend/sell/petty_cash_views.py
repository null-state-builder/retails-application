"""A store's petty cash (store operations ticket 42, ST-MNY-3). See
`sell.services.petty_cash` for the rules.

Every route answers to `money: operate` or higher (the store's "Expenses only
(create)", Owner and Accounts), for a store the caller may act at. Setting the
float and custodian is `money: manage`. Reads work with the switch off (ticket
01: refuse only new work); every write needs the store's `petty-cash` switch on.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require_section
from accounts.sections import CAP_OPERATE
from core.refusals import Refusal, first_message, refusal_body
from masters.store_features import require_feature
from sell.petty_cash_models import PettyCashSpend
from sell.petty_cash_serializers import (
    PettyCashBillWriteSerializer,
    PettyCashFloatReadSerializer,
    PettyCashFloatWriteSerializer,
    PettyCashPositionSerializer,
    PettyCashSpendReadSerializer,
    PettyCashSpendWriteSerializer,
    PettyCashTopUpReadSerializer,
    PettyCashTopUpWriteSerializer,
)
from sell.services import petty_cash

CanWorkPettyCash = require_section("money", CAP_OPERATE)

SITE_PARAM = OpenApiParameter(
    "site_id",
    OpenApiTypes.INT,
    required=False,
    description="The store. May be left out by a login placed at one store.",
)


def _invalid(errors: Any) -> Response:
    return Response(refusal_body("VALIDATION", first_message(errors)), status=400)


def _saved(body: Any, created: bool) -> Response:
    return Response(body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


def _spend(request: Request, pk: int) -> PettyCashSpend:
    """A spend at a store the caller may act at; anything else is "not found"."""
    row = (
        PettyCashSpend.objects.filter(pk=pk)
        .select_related("store", "custodian", "recorded_by", "decided_by")
        .first()
    )
    if row is None:
        raise Refusal("NOT_FOUND", "That spend was not found.", status=404)
    petty_cash.store_for(request.user, row.store_id)
    return row


class PettyCashView(APIView):
    """`GET /api/sell/petty-cash?site_id=` - the store's box: float, custodian,
    balance, and the latest spends and top-ups."""

    permission_classes = [IsAuthenticated, CanWorkPettyCash]

    @extend_schema(parameters=[SITE_PARAM], responses=PettyCashPositionSerializer)
    def get(self, request: Request) -> Response:
        store = petty_cash.store_for(request.user, request.query_params.get("site_id"))
        return Response(PettyCashPositionSerializer(petty_cash.position(store, request.user)).data)


class PettyCashFloatView(APIView):
    """`POST /api/sell/petty-cash/float` - head office sets the float and custodian."""

    permission_classes = [IsAuthenticated, CanWorkPettyCash]

    @extend_schema(request=PettyCashFloatWriteSerializer, responses=PettyCashFloatReadSerializer)
    def post(self, request: Request) -> Response:
        if not petty_cash.may_set_float(request.user):
            raise Refusal(
                "ACTION_DENIED", "Only head office sets a store's petty cash float.", status=403
            )
        form = PettyCashFloatWriteSerializer(data=request.data)
        if not form.is_valid():
            return _invalid(form.errors)
        data = dict(form.validated_data)
        store = petty_cash.store_for(request.user, data["site_id"])
        require_feature(store, petty_cash.FEATURE_KEY)
        row = petty_cash.set_float(store, request.user, data)
        return Response(PettyCashFloatReadSerializer(row).data)


class PettyCashTopUpsView(APIView):
    """`POST /api/sell/petty-cash/top-ups` - cash into the box from the till or
    head office. 201 first, 200 a retry of the same id."""

    permission_classes = [IsAuthenticated, CanWorkPettyCash]

    @extend_schema(
        request=PettyCashTopUpWriteSerializer,
        responses={200: PettyCashTopUpReadSerializer, 201: PettyCashTopUpReadSerializer},
    )
    def post(self, request: Request) -> Response:
        form = PettyCashTopUpWriteSerializer(data=request.data)
        if not form.is_valid():
            return _invalid(form.errors)
        data = dict(form.validated_data)
        store = petty_cash.store_for(request.user, data.pop("site_id"))
        require_feature(store, petty_cash.FEATURE_KEY)
        saved = petty_cash.record_top_up(store, request.user, data)
        return _saved(PettyCashTopUpReadSerializer(saved.row).data, saved.created)


class PettyCashSpendsView(APIView):
    """`POST /api/sell/petty-cash/spends` - cash out of the box, with its bill photo.
    Over the limit it waits for the Owner; with no photo it opens an exception."""

    permission_classes = [IsAuthenticated, CanWorkPettyCash]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(
        request={"multipart/form-data": PettyCashSpendWriteSerializer},
        responses={200: PettyCashSpendReadSerializer, 201: PettyCashSpendReadSerializer},
    )
    def post(self, request: Request) -> Response:
        form = PettyCashSpendWriteSerializer(data=request.data)
        if not form.is_valid():
            return _invalid(form.errors)
        data = dict(form.validated_data)
        store = petty_cash.store_for(request.user, data.pop("site_id"))
        require_feature(store, petty_cash.FEATURE_KEY)
        bill = petty_cash.read_bill(data.pop("bill", None))
        saved = petty_cash.record_spend(store, request.user, data, bill)
        return _saved(PettyCashSpendReadSerializer(saved.row).data, saved.created)


class PettyCashSpendView(APIView):
    """`GET /api/sell/petty-cash/spends/<id>` - one spend, as the Owner opens it
    from the approvals inbox."""

    permission_classes = [IsAuthenticated, CanWorkPettyCash]

    @extend_schema(responses=PettyCashSpendReadSerializer)
    def get(self, request: Request, pk: int) -> Response:
        return Response(PettyCashSpendReadSerializer(_spend(request, pk)).data)


class PettyCashBillView(APIView):
    """`GET` the bill photo of a spend; `POST` one to a spend saved without it,
    which closes its exception."""

    permission_classes = [IsAuthenticated, CanWorkPettyCash]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        responses={
            (200, "*/*"): {
                "type": "string",
                "format": "binary",
                "description": "The bill photo as it was taken.",
            }
        }
    )
    def get(self, request: Request, pk: int) -> HttpResponse:
        row = _spend(request, pk)
        if not row.has_bill:
            raise Refusal("NOT_FOUND", "This spend has no bill photo yet.", status=404)
        response = HttpResponse(petty_cash.bill_bytes(row), content_type=row.bill_media_type)
        safe = row.bill_filename.replace('"', "")
        response["Content-Disposition"] = f'inline; filename="{safe}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response

    @extend_schema(
        request={"multipart/form-data": PettyCashBillWriteSerializer},
        responses={200: PettyCashSpendReadSerializer, 201: PettyCashSpendReadSerializer},
    )
    def post(self, request: Request, pk: int) -> Response:
        row = _spend(request, pk)
        require_feature(row.store, petty_cash.FEATURE_KEY)
        form = PettyCashBillWriteSerializer(data=request.data)
        if not form.is_valid():
            return _invalid(form.errors)
        bill = petty_cash.read_bill(form.validated_data["bill"])
        if bill is None:
            return _invalid({"bill": ["Attach the bill photo."]})
        saved = petty_cash.attach_bill(row.pk, row.store, request.user, bill)
        return _saved(PettyCashSpendReadSerializer(saved.row).data, saved.created)
