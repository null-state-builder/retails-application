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

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require_section
from accounts.sections import CAP_OPERATE, CAP_VIEW
from core.refusals import refusal_body
from masters.models import Cohort, PriceChange, Sku
from masters.price_history import record_price_change

#: `view` reads the price list, `operate` moves a ticket (D11 §7, the re-ticket
#: gate). `manage` stays what it has always been — configuration.
CanReadOrReprice = require_section("offers_price", CAP_VIEW, write_minimum=CAP_OPERATE)

#: One screenful. The price list is a search-and-answer screen, not an export:
#: nobody reads forty thousand barcodes, and the search box is how a person
#: actually finds the one they are asking about.
PAGE = 200


def _as_of(request: Request) -> date:
    raw = (request.query_params.get("as_of") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return timezone.localdate()


def _costs(barcodes: list[str]) -> dict[str, dict[str, Any]]:
    """Landed cost per barcode — the newest cohort wins.

    A barcode can carry several cohorts (one per season it came in under) and
    they can be costed differently. The ticket is one number, so the comparison
    has to be against one cost: the most recently inwarded one, which is the
    stock a shop is holding now.
    """
    out: dict[str, dict[str, Any]] = {}
    rows = (
        Cohort.objects.filter(barcode__in=barcodes)
        .order_by("barcode", "-id")
        .values("barcode", "season", "unit_cost_paise", "mrp_paise")
    )
    for row in rows:
        out.setdefault(row["barcode"], dict(row))
    return out


def _as_of_prices(barcodes: list[str], day: date) -> dict[str, int]:
    """The ticket each barcode carried on ``day``, where the trail knows."""
    out: dict[str, int] = {}
    rows = (
        PriceChange.objects.filter(barcode__in=barcodes, effective_from__lte=day)
        .order_by("barcode", "-effective_from", "-id")
        .values("barcode", "to_paise")
    )
    for row in rows:
        out.setdefault(row["barcode"], int(row["to_paise"]))
    return out


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

    def get(self, request: Request) -> Response:
        day = _as_of(request)
        rows = Sku.objects.filter(is_active=True)
        term = (request.query_params.get("q") or "").strip()
        if term:
            rows = rows.filter(
                Q(barcode__icontains=term)
                | Q(design__icontains=term)
                | Q(item__icontains=term)
                | Q(brand__icontains=term)
            )
        brand = (request.query_params.get("brand") or "").strip()
        if brand:
            rows = rows.filter(brand__iexact=brand)
        if request.query_params.get("no_discount") == "true":
            rows = rows.filter(no_discount=True)
        page = list(rows.order_by("brand", "design", "barcode")[:PAGE])
        barcodes = [sku.barcode for sku in page]
        costs = _costs(barcodes)
        then = _as_of_prices(barcodes, day) if day < timezone.localdate() else {}
        return Response(
            {
                "as_of": day.isoformat(),
                "count": len(page),
                "truncated": len(page) == PAGE,
                "brands": sorted(
                    {
                        name
                        for name in Sku.objects.filter(is_active=True)
                        .exclude(brand="")
                        .values_list("brand", flat=True)
                        .distinct()
                    }
                ),
                "rows": [_row(sku, costs.get(sku.barcode), then.get(sku.barcode)) for sku in page],
            }
        )


class PriceDetailView(APIView):
    """One barcode, with the whole trail behind its ticket."""

    permission_classes = [IsAuthenticated, CanReadOrReprice]

    def get(self, request: Request, barcode: str) -> Response:
        sku = Sku.objects.filter(barcode=barcode).first()
        if sku is None:
            return Response(refusal_body("NOT_FOUND", f"No piece with barcode {barcode}."), 404)
        costs = _costs([barcode])
        body = _row(sku, costs.get(barcode), None)
        body["cohorts"] = list(
            Cohort.objects.filter(barcode=barcode)
            .order_by("-id")
            .values("season", "unit_cost_paise", "mrp_paise", "last_doc_number")
        )
        body["history"] = [
            {
                "id": change.id,
                "effective_from": change.effective_from.isoformat(),
                "from_paise": change.from_paise,
                "to_paise": change.to_paise,
                "source": change.source,
                "source_label": change.get_source_display(),
                "doc_number": change.doc_number,
                "reason": change.reason,
                "changed_by_name": getattr(change.changed_by, "full_name", "")
                or getattr(change.changed_by, "username", ""),
                "at": change.created_at.isoformat(),
            }
            for change in PriceChange.objects.filter(barcode=barcode).select_related("changed_by")
        ]
        return Response(body)


class PriceRepriceView(APIView):
    """Move a ticket, on the record.

    Two refusals, and both are the point of the screen: a re-ticket without a
    reason is the thing nobody can explain next month, and a ticket below landed
    cost is a loss booked by typing.
    """

    permission_classes = [IsAuthenticated, CanReadOrReprice]

    @transaction.atomic
    def post(self, request: Request, barcode: str) -> Response:
        sku = Sku.objects.select_for_update().filter(barcode=barcode).first()
        if sku is None:
            return Response(refusal_body("NOT_FOUND", f"No piece with barcode {barcode}."), 404)

        reason = str(request.data.get("reason") or "").strip()
        if len(reason) < 3:
            return Response(
                refusal_body(
                    "VALIDATION",
                    "Say why this ticket is moving — the reason is part of the record.",
                ),
                status=400,
            )
        try:
            new_paise = _new_price_paise(request.data)
        except ValueError as exc:
            return Response(refusal_body("VALIDATION", str(exc)), status=400)

        cost = max(
            (
                int(value or 0)
                for value in Cohort.objects.filter(barcode=barcode).values_list(
                    "unit_cost_paise", flat=True
                )
            ),
            default=0,
        )
        if cost and new_paise < cost:
            return Response(
                refusal_body(
                    "VALIDATION",
                    f"₹{new_paise / 100:,.2f} is below what this piece cost to land "
                    f"(₹{cost / 100:,.2f}). A ticket under cost is a loss booked by typing.",
                ),
                status=400,
            )

        was = sku.mrp_paise
        if was == new_paise:
            return Response(
                refusal_body("VALIDATION", "That is the ticket it already carries."), status=400
            )
        sku.mrp_paise = new_paise
        sku.save(update_fields=["mrp_paise", "updated_at"])
        # The cohorts carry the ticket too — that is what the till's dataset and
        # the sale line read. Leaving them behind would price tomorrow's bill
        # under yesterday's ticket while the screen showed the new one.
        Cohort.objects.filter(barcode=barcode).update(mrp_paise=new_paise)
        record_price_change(
            barcode=barcode,
            sku=sku,
            from_paise=was,
            to_paise=new_paise,
            source=PriceChange.Source.REPRICE,
            reason=reason,
            user=request.user,
        )
        return PriceDetailView().get(request, barcode)


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
