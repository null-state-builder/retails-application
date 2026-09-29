"""The Running Offers read (OPS-10, PRD §11).

Mounted at `/api/offers/running` because Offers & Price is the screen asking, but
written here for the reason `sell/discount_views.py` gives: the rulebook is a leaf
below the counter and may not import it, and the pieces an offer reaches are the
counter's own working set. `config/urls.py` mounts the one path; the offers URL
conf stays as it was.

Two things this endpoint answers in one call, deliberately:

* **which sites the caller reaches**, so the screen can draw a picker without a
  second question and without a second scope rule to keep in step;
* **what is running at the chosen one**, with the pieces each rule covers.

Scope is the caller's own store scope - the same boundary every other read in
this system obeys. A site outside it is *not found*, never "forbidden": a refusal
that distinguishes the two tells somebody which store codes exist.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require_section
from accounts.sections import CAP_VIEW
from core.refusals import refusal_body
from masters.models import Store
from masters.scoping import scoped_stores
from sell.services.running_offers import running_offers

#: The same rung the Promotions list and the Discount pack read at: seeing what is
#: running at a shop you work in is the whole of `offers_price: view`.
CanReadOffers = require_section("offers_price", CAP_VIEW)

SITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "code": {"type": "string"},
        "name": {"type": "string"},
        "store_type": {"type": "string"},
    },
}

ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One piece on this site's working set that the offer reaches.",
    "properties": {
        "barcode": {"type": "string"},
        "description": {"type": "string"},
        "season": {"type": "string"},
        "mrp_paise": {"type": "integer"},
    },
}

OFFER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One running offer and the pieces it applies to on `day`.",
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "source": {"type": "string", "description": "`company`, or `brand:<code>`."},
        "brand": {"type": "string"},
        "layer": {"type": "string", "enum": ["brand", "storewide", "bank"]},
        "starts_on": {"type": "string", "format": "date"},
        "ends_on": {"type": "string", "format": "date", "nullable": True},
        "combinable": {"type": "boolean"},
        "priority": {"type": "integer"},
        "reward": {"type": "string"},
        "trigger": {"type": "string"},
        "items_count": {"type": "integer"},
        "items_truncated": {"type": "boolean"},
        "items": {"type": "array", "items": ITEM_SCHEMA},
    },
}

RUNNING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Running Offers for one site on one day (PRD §11).",
    "properties": {
        "site": {**SITE_SCHEMA, "nullable": True},
        "sites": {"type": "array", "items": SITE_SCHEMA},
        "day": {"type": "string", "format": "date"},
        "offers": {"type": "array", "items": OFFER_SCHEMA},
    },
}

REFUSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"type": "string"},
        "code": {"type": "string"},
    },
}


def _site_row(store: Store) -> dict[str, Any]:
    return {
        "id": store.pk,
        "code": store.code,
        "name": store.name,
        "store_type": store.store_type,
    }


class RunningOffersView(APIView):
    """What is running at one site today, and which of its pieces each rule reaches."""

    permission_classes = [IsAuthenticated, CanReadOffers]
    http_method_names = ["get", "head", "options"]

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "site",
                int,
                description="Which site to read. Defaults to the caller's first reachable one.",
            ),
            OpenApiParameter(
                "day",
                str,
                description="The day to judge the rules against (YYYY-MM-DD). Defaults to today.",
            ),
        ],
        responses={200: RUNNING_SCHEMA, 400: REFUSAL_SCHEMA, 404: REFUSAL_SCHEMA},
    )
    def get(self, request: Request) -> Response:
        sites = list(scoped_stores(request.user).order_by("code"))
        raw_day = (request.query_params.get("day") or "").strip()
        if raw_day:
            try:
                day = date.fromisoformat(raw_day)
            except ValueError:
                return Response(
                    refusal_body("VALIDATION", "day is a date, written 2026-09-22."), status=400
                )
        else:
            day = timezone.localdate()

        raw_site = (request.query_params.get("site") or "").strip()
        if raw_site:
            if not raw_site.isdigit():
                return Response(refusal_body("VALIDATION", "site is a site's id."), status=400)
            chosen = next((s for s in sites if s.pk == int(raw_site)), None)
            if chosen is None:
                # Out of scope reads as absent, the same way every other record
                # in this system does (ADR-0003).
                return Response(refusal_body("NOT_FOUND", "That site was not found."), status=404)
        else:
            # No site named: the one they work in, or the first of several. The
            # picker below is how they change it, and it is sent either way.
            chosen = sites[0] if sites else None

        return Response(
            {
                "site": _site_row(chosen) if chosen else None,
                "sites": [_site_row(store) for store in sites],
                "day": day.isoformat(),
                "offers": running_offers(chosen, day) if chosen else [],
            }
        )
