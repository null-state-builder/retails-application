"""The price module: what a piece is ticketed at, and what it was ticketed at then.

Mounted under ``/api/offers/price-list`` and gated on ``offers_price``, because
that is the section the sidebar publishes for this screen and the server must
mirror the pair the sidebar published — one gate per screen, no second answer.
The *data* is master data and lives in ``masters``; the *authority* to read it
and to move it is Offers & Price's, which is why these views sit here.

Three questions, three endpoints:

* the list — current ticket, cost, margin, and whether the piece may be
  discounted at all, for a page of barcodes;
* one barcode — the same, plus its whole trail, which is the only way to explain
  a bill printed a month ago;
* the re-ticket — a named act with a reason, floored at landed cost. Below cost
  it is refused here with a message that says the cost, rather than by a
  database CHECK that says ``violates constraint``.
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

from accounts.principal import resolve_access
from accounts.permissions import require_section
from accounts.sections import CAP_OPERATE, CAP_VIEW
from core.refusals import Refusal
from masters.models import PriceChange, Sku
from masters.scoping import scope_by_store_and_brand
from stockledger.models import StockLedgerEntry, StockOnHand

#: `view` reads the price list, `operate` moves a ticket (D11 §7, the re-ticket
#: gate). `manage` stays what it has always been — configuration.
CanReadOrReprice = require_section("offers_price", CAP_VIEW, write_minimum=CAP_OPERATE)

#: One screenful. The price list is a search-and-answer screen, not an export:
#: nobody reads forty thousand barcodes, and the search box is how a person
#: actually finds the one they are asking about.
PAGE = 200

REFUSAL_RESPONSE = {
    "type": "object", "required": ["error", "code"],
    "properties": {"error": {"type": "string"}, "code": {"type": "string"}},
}
PRICE_ROW: dict[str, Any] = {
    "type": "object", "required": [
        "barcode", "design", "color", "size", "brand", "item", "hsn",
        "mrp_paise", "mrp_then_paise", "moved_since", "cost_paise",
        "season", "margin_pct", "no_discount",
    ],
    "properties": {
        **{name: {"type": "string"} for name in (
            "barcode", "design", "color", "size", "brand", "item", "hsn", "season"
        )},
        **{name: {"type": "integer", "nullable": True} for name in (
            "mrp_paise", "mrp_then_paise", "cost_paise"
        )},
        "moved_since": {"type": "boolean"},
        "margin_pct": {"type": "string", "nullable": True},
        "no_discount": {"type": "boolean"},
    },
}
PRICE_LIST_RESPONSE = {
    "type": "object", "required": ["as_of", "count", "truncated", "brands", "rows"],
    "properties": {
        "as_of": {"type": "string", "format": "date"},
        "count": {"type": "integer"}, "truncated": {"type": "boolean"},
        "brands": {"type": "array", "items": {"type": "string"}},
        "rows": {"type": "array", "items": PRICE_ROW},
    },
}
PRICE_DETAIL_RESPONSE = {
    **PRICE_ROW,
    "required": [*PRICE_ROW["required"], "cohorts", "history"],
    "properties": {
        **PRICE_ROW["properties"],
        "cohorts": {"type": "array", "items": {"type": "object", "required": [
            "season", "unit_cost_paise", "mrp_paise", "last_doc_number"
        ], "properties": {
            "season": {"type": "string"},
            "unit_cost_paise": {"type": "integer", "nullable": True},
            "mrp_paise": {"type": "integer", "nullable": True},
            "last_doc_number": {"type": "string", "nullable": True},
        }}},
        "history": {"type": "array", "items": {"type": "object", "required": [
            "id", "effective_from", "from_paise", "to_paise", "source", "source_label",
            "doc_number", "reason", "changed_by_name", "at",
        ], "properties": {
            "id": {"type": "integer"},
            "effective_from": {"type": "string", "format": "date"},
            "from_paise": {"type": "integer", "nullable": True},
            "to_paise": {"type": "integer"},
            **{name: {"type": "string"} for name in (
                "source", "source_label", "doc_number", "reason", "changed_by_name"
            )},
            "at": {"type": "string", "format": "date-time"},
        }}},
    },
}
REPRICE_REQUEST = {
    "type": "object", "required": ["reason"],
    "anyOf": [{"required": ["mrp_paise"]}, {"required": ["mrp"]}],
    "properties": {
        "reason": {"type": "string", "minLength": 3},
        "mrp_paise": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        "mrp": {"anyOf": [{"type": "number"}, {"type": "string"}]},
    },
}


def _as_of(request: Request) -> date:
    raw = (request.query_params.get("as_of") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return timezone.localdate()


def _sources(request: Request) -> list[Any]:
    """Only tenant-owned stock with proven identity establishes a price-book row."""
    query = scope_by_store_and_brand(
        StockOnHand.objects.all(), request.user, section="offers_price", minimum="view",
    )
    return list(query.order_by("sku_code", "store_id"))


def _price_body(request: Request, sku: Sku, sources: list[Any], day: date) -> dict[str, Any]:
    access = resolve_access(request)
    cells = [(row.store_id, row._access_brand_id) for row in sources]
    if len({brand for _, brand in cells}) != 1:
        raise Refusal("IDENTITY_CONFLICT", "This barcode has conflicting brand ownership.")
    fields = {field for field in ("cost", "margin", "personal") if access.covers_all_actions(
        {"section.offers_price.view"}, cells, {field},
    )}
    ledger = scope_by_store_and_brand(
        StockLedgerEntry.objects.filter(sku_code=sku.barcode, qty__gt=0), request.user,
        section="offers_price", minimum="view",
    ).filter(store_id__in=[row.store_id for row in sources]).order_by("-created_at", "-id")
    latest = ledger.first()
    cost = None
    if latest is not None and "cost" in fields:
        cost = {"unit_cost_paise": int(latest.amount or 0) // latest.qty, "season": latest.season}
    history = PriceChange.objects.filter(
        barcode=sku.barcode, changed_by__tenant_id=access.tenant_id,
    ).select_related("changed_by")
    then = history.filter(effective_from__lte=day).order_by("-effective_from", "-id").first()
    body = _row(sku, cost, then.to_paise if then is not None else None)
    # Shared SKU descriptions do not establish ownership or override its proven snapshot.
    for key in ("design", "color", "size", "brand", "item", "hsn", "season"):
        body[key] = getattr(sources[0], key)
    if "margin" not in fields:
        body["margin_pct"] = None
    body["cohorts"] = [{
        "season": row.season, "unit_cost_paise": int(row.amount or 0) // row.qty if "cost" in fields else None,
        "mrp_paise": None, "last_doc_number": row.doc_number,
    } for row in ledger[:200]]
    body["history"] = [{
        "id": row.pk, "effective_from": row.effective_from.isoformat(),
        "from_paise": row.from_paise, "to_paise": row.to_paise,
        "source": row.source, "source_label": row.get_source_display(),
        "doc_number": row.doc_number, "reason": row.reason if "cost" in fields else "",
        "changed_by_name": (getattr(row.changed_by, "full_name", "") or getattr(row.changed_by, "username", "")) if "personal" in fields else "Restricted person",
        "at": row.created_at.isoformat(),
    } for row in history[:200]]
    return body


def _margin_pct(mrp_paise: int | None, cost_paise: int | None) -> str | None:
    """Margin on the ticket, before GST — stated as a string so nobody rounds it
    twice. Blank where either side is unknown, which is honest and is not zero."""
    if not mrp_paise or not cost_paise or mrp_paise <= 0:
        return None
    return f"{(mrp_paise - cost_paise) * 100 / mrp_paise:.1f}"


def _row(sku: Sku, cost: dict[str, Any] | None, then: int | None) -> dict[str, Any]:
    cost_paise = int(cost["unit_cost_paise"]) if cost else None
    return {
        "barcode": sku.barcode,
        "design": sku.design,
        "color": sku.color,
        "size": sku.size,
        "brand": sku.brand,
        "item": sku.item,
        "hsn": sku.hsn,
        "mrp_paise": sku.mrp_paise,
        "mrp_then_paise": then if then is not None else sku.mrp_paise,
        "moved_since": then is not None and sku.mrp_paise is not None and then != sku.mrp_paise,
        "cost_paise": cost_paise,
        "season": cost["season"] if cost else "",
        "margin_pct": _margin_pct(sku.mrp_paise, cost_paise),
        "no_discount": sku.no_discount,
    }


class PriceListView(APIView):
    """The price list: search, and read what a ticket is — or was."""

    permission_classes = [IsAuthenticated, CanReadOrReprice]

    @extend_schema(
        operation_id="offers_price_list_list",
        parameters=[
            OpenApiParameter("q", str), OpenApiParameter("brand", str),
            OpenApiParameter("as_of", str, description="ISO calendar date"),
            OpenApiParameter("no_discount", bool),
        ],
        responses={200: PRICE_LIST_RESPONSE},
    )
    def get(self, request: Request) -> Response:
        day = _as_of(request)
        by_code: dict[str, list[Any]] = {}
        term = str(request.query_params.get("q") or "").strip().casefold()
        brand = str(request.query_params.get("brand") or "").strip().casefold()
        for row in _sources(request):
            if brand and row.brand.casefold() != brand:
                continue
            if term and not any(term in str(getattr(row, key)).casefold() for key in ("sku_code", "design", "item", "brand")):
                continue
            by_code.setdefault(row.sku_code, []).append(row)
        skus = Sku.objects.filter(barcode__in=by_code, is_active=True)
        if request.query_params.get("no_discount") == "true":
            skus = skus.filter(no_discount=True)
        page = list(skus.order_by("barcode")[:PAGE + 1])
        bodies = [_price_body(request, sku, by_code[sku.barcode], day) for sku in page[:PAGE]]
        for body in bodies:
            body.pop("cohorts")
            body.pop("history")
        return Response({"as_of": day.isoformat(), "count": len(bodies), "truncated": len(page) > PAGE,
                         "brands": sorted({row.brand for rows in by_code.values() for row in rows}), "rows": bodies})


class PriceDetailView(APIView):
    """One barcode, with the whole trail behind its ticket."""

    permission_classes = [IsAuthenticated, CanReadOrReprice]

    @extend_schema(
        operation_id="offers_price_list_detail",
        responses={200: PRICE_DETAIL_RESPONSE, 404: REFUSAL_RESPONSE},
    )
    def get(self, request: Request, barcode: str) -> Response:
        sources = [row for row in _sources(request) if row.sku_code == barcode]
        sku = Sku.objects.filter(barcode=barcode).first() if sources else None
        if sku is None:
            raise Refusal("NOT_FOUND", "That price-book row was not found.", status=404)
        return Response(_price_body(request, sku, sources, _as_of(request)))


class PriceRepriceView(APIView):
    """Move a ticket, on the record.

    Two refusals, and both are the point of the screen: a re-ticket without a
    reason is the thing nobody can explain next month, and a ticket below landed
    cost is a loss booked by typing.
    """

    permission_classes = [IsAuthenticated, CanReadOrReprice]

    @extend_schema(
        request={"application/json": REPRICE_REQUEST},
        responses={200: PRICE_DETAIL_RESPONSE, 400: REFUSAL_RESPONSE, 404: REFUSAL_RESPONSE},
    )
    def post(self, request: Request, barcode: str) -> Response:
        # This shared master has no tenant owner. Scope cannot make its mutation safe.
        raise Refusal("OWNERSHIP_UNRESOLVED", "Shared SKU repricing awaits SO-04 ownership and SO-06 price-book cutover.", status=409)


def _new_price_paise(data: dict[str, Any]) -> int:
    """Rupees or paise, whichever the caller sent; nothing else is guessed."""
    if data.get("mrp_paise") not in (None, ""):
        raw = data["mrp_paise"]
    elif data.get("mrp") not in (None, ""):
        raw = float(str(data["mrp"]).replace(",", "")) * 100
    else:
        raise ValueError("Say what the new ticket is.")
    try:
        paise = int(round(float(raw)))
    except (TypeError, ValueError):
        raise ValueError("The new ticket has to be a number.") from None
    if paise <= 0:
        raise ValueError("A ticket has to be above nought.")
    return paise
