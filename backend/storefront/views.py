"""The store-facing read-only aggregator (D10 §2).

No models, no writes. See `storefront/dashboard.py` for what the Home payload is
made of, and `storefront/day.py` for the day arithmetic the Dashboard and the
Money section's cash summary share.
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require_section
from accounts.sections import CAP_VIEW
from core.dates import parse_day
from core.refusals import refusal_body
from storefront.cash_summary import build as build_cash_summary
from storefront.dashboard import build, resolve_store


class DashboardView(APIView):
    """`GET /api/store/dashboard` - one store's Home.

    Gated on `home: view`, the section every role holds, because the dashboard is
    a window onto work the person can already reach: each card is a count of rows
    some other gate has already decided they may see, and the two blocks that are
    not - the money tiles and the manager row - carry their own answer (the tiles
    are nought until the POS lands; the manager row is `sell >= approve`).

    Refuses with `SCOPE_DENIED` and nothing else. A store with no transfers, no
    approvals and no target is not an error - it is a quiet morning, and it
    renders as noughts.
    """

    permission_classes = [IsAuthenticated, require_section("home", CAP_VIEW)]

    def get(self, request: Request) -> Response:
        pick = resolve_store(request.user, (request.query_params.get("store") or "").strip())
        if pick.store is None:
            # Three refusals wear one code, so the sentence has to tell them
            # apart. It comes back from the resolver rather than being worked out
            # again here: only the branch that ruled a store out knows which of
            # the three happened.
            return Response(refusal_body("SCOPE_DENIED", pick.refusal), status=403)
        return Response(build(request.user, pick.store))


class CashSummaryView(APIView):
    """`GET /api/store/cash-summary` - one store's day, by tender.

    Gated on `money: view` per the contract, which is a rung the store itself
    holds ("Expenses only (create)" is `money: operate` on the ratified sheet), so
    a counter can read what its own day came to. Head office reads it through the
    same door, narrowed by the top-bar switcher exactly as the Dashboard is.

    Read-only, and there is deliberately no writer: agreeing a day - counting the
    drawer, locking the date - is store open/close (I3), and a screen that let
    somebody confirm a day before that flow exists would be a confirmation
    nothing downstream honours.
    """

    permission_classes = [IsAuthenticated, require_section("money", CAP_VIEW)]

    def get(self, request: Request) -> Response:
        asked = (request.query_params.get("date") or "").strip()
        day = parse_day(asked) if asked else timezone.localdate()
        if day is None:
            return Response(
                refusal_body("VALIDATION", f"'{asked}' is not a date (use 2026-07-31)."), status=400
            )
        pick = resolve_store(request.user, (request.query_params.get("store") or "").strip())
        if pick.store is None:
            return Response(refusal_body("SCOPE_DENIED", pick.refusal), status=403)
        return Response(build_cash_summary(pick.store, day))
