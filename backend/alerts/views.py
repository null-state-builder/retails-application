"""Alerts API - the one feed Home's Alerts surface and the bell read (#77, #226).

    GET  /api/alerts          everything open, scoped to *me*
    GET  /api/alerts/history  everything resolved in a window, same scope
    GET  /api/alerts/seen     where my read cursor stands
    POST /api/alerts/seen     move it to now

No decide endpoint: an alert is not approved or rejected, it is just read -
the job that raised it is also the job that resolves it once it stops being
true (``alerts.checks``). "Read", though, is a fact somebody has to record, and
that is the whole of ``AlertSeen``: the bell's unread badge counts open alerts
newer than the caller's stamp, so without one there is nothing to count from.

All four sit behind the same gate, ``home: view`` - history and the cursor
interpret the very feed they guard, and a second, looser door onto the same rows
is how a store person ends up reading another store's history.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.actions import SECTION_ACTIONS
from accounts.permissions import require_section
from accounts.principal import access_for_user, resolve_access
from accounts.sections import CAP_MANAGE, CAP_VIEW
from core.dates import bad_since, parse_day
from masters.scoping import scope_by_store_or_brand

from .models import Alert, AlertKind, AlertSeen, AlertStatus
from .serializers import AlertReadSerializer, AlertSeenSerializer
from .scope_contract import WHOLE_SITE_KINDS

#: How far History looks back when the caller names no window. It matches the
#: popup's own default range, which the client computes for itself - the two are
#: the same number by agreement, not by import, since the client also has to
#: label the button "7 days".
DEFAULT_HISTORY_DAYS = 7

#: Kinds only some sections read, beyond the store or brand scope: the section
#: and rung a reader needs. A brand claim to check (store operations ticket 26)
#: is money work for Accounts and the Owner, never a store role or Admin.
KIND_NEEDS: dict[str, tuple[str, str]] = {
    AlertKind.BRAND_CLAIM_FLAGGED: ("money", CAP_MANAGE),
    # Ticket 24: which stock is on sale or return is a brand term; store roles
    # never read terms (ticket 23), so the alert is Owner's and Accounts'.
    AlertKind.SOR_AGEING: ("money", CAP_MANAGE),
}

# These writers deliberately snapshot a whole-site condition with no brand.
# Branded stock/document alerts with missing identity remain unresolved.
KIND_FIELDS: dict[str, frozenset[str]] = {
    AlertKind.CASH_VARIANCE: frozenset({"financial"}),
    AlertKind.BRAND_CLAIM_FLAGGED: frozenset({"financial"}),
    AlertKind.SOR_AGEING: frozenset({"financial"}),
    AlertKind.RESERVATION_EXPIRY: frozenset({"customer"}),
    # Existing snapshots can contain either customer or staff return history.
    # Their single shape cannot establish a narrower field classification.
    AlertKind.NO_BILL_RETURN_CAP: frozenset({"customer", "employee_private"}),
}


def scoped_alerts(qs: Any, user: Any) -> Any:
    return scope_by_store_or_brand(
        qs, user, section="home", minimum="view",
        brandless=Q(kind__in=WHOLE_SITE_KINDS, brand=""),
    )


def readable_kinds(qs: Any, user: Any) -> Any:
    """Keep financial alerts only within one qualifying assignment's scope.

    Their title and object ID can reveal business terms, so a Money section on
    one assignment must never combine with a Home/site grant on another.
    """
    allowed = Q(pk__in=[])
    for row in access_for_user(user).section_grants("home", CAP_VIEW):
        cell = Q()
        if not row.all_sites:
            cell &= Q(store_id__in=row.site_ids)
        if not row.all_brands:
            cell &= Q(_access_brand_id__in=row.brand_ids)
        for kind, fields in KIND_FIELDS.items():
            need = KIND_NEEDS.get(kind)
            if fields <= row.fields and (
                need is None or SECTION_ACTIONS[need] in row.actions
            ):
                allowed |= Q(kind=kind) & cell
    return qs.filter(~Q(kind__in=KIND_FIELDS) | allowed)


class AlertInboxView(generics.ListAPIView[Alert]):
    """Open alerts, scoped exactly like the approvals inbox (ADR-0003): a
    store-scoped user sees their own store's, a brand-scoped user sees their
    own brands', HO sees the network. Gated on the Home section (#77) — the
    same section Approvals lives in."""

    permission_classes = [IsAuthenticated, require_section("home", CAP_VIEW)]
    serializer_class = AlertReadSerializer
    pagination_class = None

    def get_queryset(self) -> Any:
        qs = Alert.objects.filter(status=AlertStatus.OPEN).select_related("store")
        return readable_kinds(scoped_alerts(qs, self.request.user), self.request.user)


class AlertHistoryView(APIView):
    """Resolved alerts, newest first, within a window (#226).

    The other half of the same lifecycle the inbox shows, through the *same*
    scope call - history that answered a wider question than the live feed would
    let a store person read another store's problems a day late, which is the
    same leak with a delay on it.

    An ``APIView`` rather than a ``ListAPIView`` because a bad ``?since=`` is a
    refusal with a code, and a generic list has nowhere to say so.
    """

    permission_classes = [IsAuthenticated, require_section("home", CAP_VIEW)]

    @extend_schema(responses=AlertReadSerializer(many=True))
    def get(self, request: Request) -> Response:
        asked = (request.query_params.get("since") or "").strip()
        refusal = bad_since(asked)
        if refusal:
            return Response(refusal, status=status.HTTP_400_BAD_REQUEST)
        since = parse_day(asked) if asked else None
        if since is None:
            since = timezone.localdate() - timedelta(days=DEFAULT_HISTORY_DAYS)
        rows = Alert.objects.filter(
            status=AlertStatus.RESOLVED, resolved_at__date__gte=since
        ).select_related("store")
        rows = readable_kinds(scoped_alerts(rows, request.user), request.user)
        rows = rows.order_by("-resolved_at")
        return Response(AlertReadSerializer(rows, many=True).data)


class AlertSeenView(APIView):
    """The caller's read cursor: GET where it stands, POST to move it to now.

    The stamp is the *server's* clock, never a time sent up: a browser whose
    clock runs fast would otherwise mark alerts read before they were raised,
    and the badge would sit at zero through a real problem.
    """

    permission_classes = [IsAuthenticated, require_section("home", CAP_VIEW)]

    @extend_schema(responses=AlertSeenSerializer)
    def get(self, request: Request) -> Response:
        # IsAuthenticated above guarantees a real user, never AnonymousUser.
        user = cast(User, request.user)
        row = AlertSeen.objects.filter(user=user).first()
        return Response({"seen_at": row.seen_at if row else None})

    @extend_schema(request=None, responses=AlertSeenSerializer)
    def post(self, request: Request) -> Response:
        # Idempotent by design - the tab is opened a dozen times a day, and each
        # opening simply moves the one row forward.
        # IsAuthenticated above guarantees a real user, never AnonymousUser.
        user = cast(User, request.user)
        access = resolve_access(request)
        # The cursor belongs to this person, rather than a site or brand. Keep
        # the same Home requirement live through the write and its commit.
        with access.guard_legacy_write(lambda current: bool(current.section_grants("home", CAP_VIEW))):
            row, _ = AlertSeen.objects.update_or_create(user=user, defaults={"seen_at": timezone.now()})
        return Response({"seen_at": row.seen_at})
