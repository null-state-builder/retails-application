"""The day-close cash count at the till (ticket 41, ST-MNY-2). See
`sell.services.cash_count` for the rules.

All three routes are a till login for its one store (`CanRunTill`, the same
one-store rule as the dataset) at a store with the `cash-count` switch on. The
store is always the login's own; nothing here takes a store from the request.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.principal import AccessContext, resolve_access
from core.refusals import Refusal, first_message, refusal_body
from masters.store_feature_registry import CASH_COUNT
from masters.store_features import is_feature_on, require_feature
from masters.models import Store
from sell.permissions import CanRunTill
from sell.serializers import (
    CashCountReadSerializer,
    CashCountWriteSerializer,
    CashMovementReadSerializer,
    CashMovementWriteSerializer,
    CashPositionSerializer,
)
from sell.services.cash_count import cash_position, record_count, record_movement
from sell.views import _can_accept_bill, till_store


def _project_people(access: AccessContext, store: Store, body: Any) -> Any:
    """Operational cash figures do not grant private staff/recipient names."""
    if access.covers_all_actions({"section.sell.operate"}, [(store.pk, None)], {"personal"}):
        return body
    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ("" if key in {"counted_by_name", "approved_by_name", "given_by_name", "received_by"}
                          else redact(child)) for key, child in value.items()}
        if isinstance(value, list):
            return [redact(child) for child in value]
        return value
    return redact(body)


def _counter_access(request: Request, store: Store) -> AccessContext:
    access = resolve_access(request)
    if not _can_accept_bill(access, store.code):
        raise Refusal("ACTION_DENIED", "This cash close requires the store's scoped counter assignment.")
    return access


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
        access = _counter_access(request, store)
        on = is_feature_on(store, CASH_COUNT)
        body = _project_people(access, store, CashPositionSerializer(cash_position(store, switched_on=on)).data)
        access.revalidate_delivery()
        return Response(body)


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
        access = _counter_access(request, store)
        with access.guard_legacy_write(lambda current: _can_accept_bill(current, store.code)):
            saved = record_count(store, request.user, dict(form.validated_data))
        body = _project_people(access, store, CashCountReadSerializer(saved.row).data)
        access.revalidate_delivery()
        return Response(
            body,
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
        access = _counter_access(request, store)
        with access.guard_legacy_write(lambda current: _can_accept_bill(current, store.code)):
            saved = record_movement(store, request.user, dict(form.validated_data))
        body = _project_people(access, store, CashMovementReadSerializer(saved.row).data)
        access.revalidate_delivery()
        return Response(
            body,
            status=status.HTTP_201_CREATED if saved.created else status.HTTP_200_OK,
        )
