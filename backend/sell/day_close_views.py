"""Closing the store's business day at the counter (R-POS-011 / R-FIN-015).

See `sell.services.day_close` for the rules. All three routes are the cash
count's: a till login for its one store (`CanRunTill` and the scoped counter
assignment), and the store is always the login's own - nothing here takes a
store from the request. The manager who confirms the close is named in the
request and proved by their own PIN, checked again on the server.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.principal import AccessContext
from core.refusals import first_message, refusal_body
from masters.models import Store
from sell.cash_views import _counter_access
from sell.day_close_serializers import (
    DayCloseStateSerializer,
    DayCloseWriteSerializer,
    DaySummaryCheckWriteSerializer,
    state_body,
)
from sell.permissions import CanRunTill
from sell.services.day_close import close_day, day_state, record_summary_check
from sell.views import till_store

#: Names of people on the page, which a login without personal fields does not get.
_NAMES = frozenset({
    "counted_by_name", "approved_by_name", "summary_checked_by_name",
    "closed_by_name", "confirmed_by_name",
})


def _project_people(access: AccessContext, store: Store, body: Any) -> Any:
    """Operational figures do not grant private staff names (as the cash count)."""
    if access.covers_all_actions({"section.sell.operate"}, [(store.pk, None)], {"personal"}):
        return body

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ("" if key in _NAMES else redact(child)) for key, child in value.items()}
        if isinstance(value, list):
            return [redact(child) for child in value]
        return value

    return redact(body)


def _command_access(request: Request, store: Store) -> AccessContext:
    """The counter's own assignment, recorded so the command replays it at commit."""
    access = _counter_access(request, store)
    access.require_all_actions({"section.sell.operate"}, site_id=store.pk)
    return access


def _answer(access: AccessContext, store: Store, created: bool | None = None) -> Response:
    body = _project_people(access, store, DayCloseStateSerializer(state_body(day_state(store))).data)
    access.revalidate_delivery()
    code = status.HTTP_200_OK if not created else status.HTTP_201_CREATED
    return Response(body, status=code)


class DayCloseView(APIView):
    """`GET /api/sell/day-close` - today's business day, step by step."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(responses=DayCloseStateSerializer)
    def get(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        return _answer(_counter_access(request, store), store)


class DaySummaryCheckView(APIView):
    """`POST /api/sell/day-close/summary-check` - the Day summary was read."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        request=DaySummaryCheckWriteSerializer,
        responses={200: DayCloseStateSerializer, 201: DayCloseStateSerializer},
    )
    def post(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        form = DaySummaryCheckWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        access = _command_access(request, store)
        saved = record_summary_check(
            store, request.user, dict(form.validated_data), access.principal()
        )
        return _answer(access, store, saved.created)


class DayCloseCloseView(APIView):
    """`POST /api/sell/day-close/close` - a manager closes the day with their PIN.
    201 the first time; 200 when the day was already closed."""

    permission_classes = [IsAuthenticated, CanRunTill]

    @extend_schema(
        request=DayCloseWriteSerializer,
        responses={200: DayCloseStateSerializer, 201: DayCloseStateSerializer},
    )
    def post(self, request: Request) -> Response:
        store, refusal = till_store(request)
        if store is None:
            return refusal
        form = DayCloseWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        access = _command_access(request, store)
        saved = close_day(store, request.user, dict(form.validated_data), access.principal())
        return _answer(access, store, saved.created)
