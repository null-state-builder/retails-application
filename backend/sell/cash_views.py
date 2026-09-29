"""The day-close cash count at the till (ticket 41, ST-MNY-2). See
`sell.services.cash_count` for the rules.

All three routes are a till login for its one store (`CanRunTill`, the same
one-store rule as the dataset) at a store with the `cash-count` switch on. The
store is always the login's own; nothing here takes a store from the request.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.refusals import first_message, refusal_body
from masters.store_feature_registry import CASH_COUNT
from masters.store_features import is_feature_on, require_feature
from sell.permissions import CanRunTill
from sell.serializers import (
    CashCountReadSerializer,
    CashCountWriteSerializer,
    CashMovementReadSerializer,
    CashMovementWriteSerializer,
    CashPositionSerializer,
)
from sell.services.cash_count import cash_position, record_count, record_movement
from sell.views import till_store


class CashPositionView(APIView):
    """`GET /api/sell/cash-count` - the Z-report: the drawer since the previous
    count, the expected cash, and today's count if it is saved.

    Readable with the switch off (ticket 01: refuse only new work), so saved
    counts stay in view; `switched_on` tells the screen not to offer a count."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(responses=CashPositionSerializer)
    def get(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        on = is_feature_on(store, CASH_COUNT)
        return Response(CashPositionSerializer(cash_position(store, switched_on=on)).data)


class CashCountsView(APIView):
    """`POST /api/sell/cash-counts` - save today's count. 201 first, 200 a retry
    of the same id; refusals say why in a sentence."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        request=CashCountWriteSerializer,
        responses={200: CashCountReadSerializer, 201: CashCountReadSerializer},
    )
    def post(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        require_feature(store, CASH_COUNT)
        form = CashCountWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        saved = record_count(store, request.user, dict(form.validated_data))
        return Response(
            CashCountReadSerializer(saved.row).data,
            status=status.HTTP_201_CREATED if saved.created else status.HTTP_200_OK,
        )


class CashMovementsView(APIView):
    """`POST /api/sell/cash-movements` - a bank deposit or a handover, both sides named."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        request=CashMovementWriteSerializer,
        responses={200: CashMovementReadSerializer, 201: CashMovementReadSerializer},
    )
    def post(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        require_feature(store, CASH_COUNT)
        form = CashMovementWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        saved = record_movement(store, request.user, dict(form.validated_data))
        return Response(
            CashMovementReadSerializer(saved.row).data,
            status=status.HTTP_201_CREATED if saved.created else status.HTTP_200_OK,
        )
