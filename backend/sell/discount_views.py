"""What the chain gave away, and what it forgot to give (D11 §5, §8).

One read, gated on ``offers_price: view`` and living in ``sell``, because the
data is *bills* — and the rulebook is not allowed to import the counter (the
import contract's one-way street: `sell` reads `offers`, never the reverse). The
gate is the one the Discounts screen publishes; the module is where the facts are.

Every persona from the Owner to the cashier's manager asks the same four
questions of a month:

1. how much came off the ticket, and under which rule;
2. how much of it was **keyed in** at a counter rather than priced by a rule —
   the discount a person decided, with the person named;
3. which brands it landed on;
4. **what was owed and never given** — the honest half of the month-end
   conversation (R4). The nightly continuity check already flags every bill where
   the till charged something other than the rulebook; this reads those flags back
   as money, on the true bill, on the true date. Nothing is restated, nothing is
   moved between months.

Scope is the caller's own: a store manager reads their shop, head office reads
the network. That is the store-scope rule everything else in this system obeys,
not a filter this screen invented.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db.models import Count, DecimalField, F, Sum
from django.db.models.functions import Cast
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require_section
from accounts.sections import CAP_VIEW
from masters.scoping import active_store_ids
from sell.models import ContinuityFlag, SaleLine

CanReadDiscounts = require_section("offers_price", CAP_VIEW)

#: How many rows of detail a screen shows before it stops being readable.
TOP = 25


def _window(request: Request) -> tuple[date, date]:
    """From/to, defaulting to the last thirty days.

    Thirty rather than the calendar month, deliberately: on the 2nd of a month a
    month-to-date screen shows two days of trading and reads like a broken
    report. The presets on the screen ask the calendar questions.
    """
    today = timezone.localdate()
    try:
        start = date.fromisoformat((request.query_params.get("from") or "").strip())
    except ValueError:
        start = today - timedelta(days=29)
    try:
        end = date.fromisoformat((request.query_params.get("to") or "").strip())
    except ValueError:
        end = today
    if end < start:
        start, end = end, start
    # A year of bills is a report, not a screen: cap the window rather than let
    # one URL walk the whole sale table.
    return max(start, end - timedelta(days=400)), end


class DiscountReportView(APIView):
    permission_classes = [IsAuthenticated, CanReadDiscounts]

    def get(self, request: Request) -> Response:
        start, end = _window(request)
        lines = SaleLine.objects.filter(
            direction=SaleLine.Direction.SALE,
            sale__billed_at__date__gte=start,
            sale__billed_at__date__lte=end,
        )
        flags = ContinuityFlag.objects.filter(
            kind=ContinuityFlag.Kind.OFFER_MISMATCH,
            sale__billed_at__date__gte=start,
            sale__billed_at__date__lte=end,
        )
        store_ids = active_store_ids(request.user)
        if store_ids is not None:
            lines = lines.filter(sale__store_id__in=store_ids)
            flags = flags.filter(sale__store_id__in=store_ids)
        code = (request.query_params.get("store") or "").strip().upper()
        if code:
            lines = lines.filter(sale__store__code=code)
            flags = flags.filter(sale__store__code=code)

        gross = Sum(Cast(F("mrp_paise") * F("qty"), DecimalField(max_digits=18, decimal_places=0)))
        totals = lines.aggregate(
            gross_paise=gross,
            disc_paise=Sum("disc_paise"),
            net_paise=Sum("net_paise"),
            lines=Count("id"),
            bills=Count("sale_id", distinct=True),
        )
        return Response(
            {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "totals": {
                    "gross_paise": int(totals["gross_paise"] or 0),
                    "disc_paise": int(totals["disc_paise"] or 0),
                    "net_paise": int(totals["net_paise"] or 0),
                    "lines": totals["lines"] or 0,
                    "bills": totals["bills"] or 0,
                    "discount_pct": _pct(totals["disc_paise"], totals["gross_paise"]),
                },
                "by_brand": self._by_brand(lines),
                "by_offer": self._by_offer(lines),
                "keyed_in": self._keyed_in(lines),
                "not_applied": self._not_applied(flags),
            }
        )

    def _by_brand(self, lines: Any) -> list[dict[str, Any]]:
        rows = (
            lines.filter(disc_paise__gt=0)
            .values("brand")
            .annotate(disc_paise=Sum("disc_paise"), net_paise=Sum("net_paise"), lines=Count("id"))
            .order_by("-disc_paise")[:TOP]
        )
        return [
            {
                "brand": row["brand"] or "(unbranded)",
                "disc_paise": int(row["disc_paise"] or 0),
                "net_paise": int(row["net_paise"] or 0),
                "lines": row["lines"],
            }
            for row in rows
        ]

    def _by_offer(self, lines: Any) -> list[dict[str, Any]]:
        """What each rule actually cost, with who funds it named.

        Funder rides along because it is the one thing the month-end
        conversation turns on (R3): a brand's scheme on the brand's own stock is
        a different conversation from KDPS's own markdown.
        """
        rows = (
            lines.filter(offer__isnull=False)
            .values("offer_id", "offer__name", "offer__funder", "offer__layer")
            .annotate(disc_paise=Sum("disc_paise"), lines=Count("id"))
            .order_by("-disc_paise")[:TOP]
        )
        return [
            {
                "offer_id": row["offer_id"],
                "name": row["offer__name"],
                "funder": row["offer__funder"],
                "layer": row["offer__layer"],
                "disc_paise": int(row["disc_paise"] or 0),
                "lines": row["lines"],
            }
            for row in rows
        ]

    def _keyed_in(self, lines: Any) -> dict[str, Any]:
        """Discount a person decided: no rule cited, money off anyway.

        This is the counter's own latitude being used, which is legitimate and is
        also the one number a manager should see every week. Named, not counted:
        "₹40,000 of manual discount" is a shrug; "₹40,000, and ₹31,000 of it on
        one salesperson's bills" is a conversation.
        """
        manual = lines.filter(disc_paise__gt=0, offer__isnull=True)
        by_person = (
            manual.values("sale__store__code", "salesperson_name")
            .annotate(disc_paise=Sum("disc_paise"), lines=Count("id"))
            .order_by("-disc_paise")[:TOP]
        )
        return {
            "disc_paise": int(manual.aggregate(total=Sum("disc_paise"))["total"] or 0),
            "lines": manual.count(),
            "by_person": [
                {
                    "store_code": row["sale__store__code"],
                    "name": row["salesperson_name"] or "(not named)",
                    "disc_paise": int(row["disc_paise"] or 0),
                    "lines": row["lines"],
                }
                for row in by_person
            ],
        }

    def _not_applied(self, flags: Any) -> dict[str, Any]:
        """Eligible discount the counter never gave — R4's honest engine.

        Read off the nightly flags rather than recomputed here: the check that
        raised them priced the bill against the rulebook *as of the day it
        printed*, which is the only comparison that means anything, and doing it
        again in a report would be a second opinion that can disagree.
        """
        rows: list[dict[str, Any]] = []
        shortfall = 0
        pending = 0
        for flag in flags.select_related("sale", "store").order_by("-sale__billed_at")[:200]:
            missed = sum(
                max(int(line.get("rulebook_paise") or 0) - int(line.get("charged_paise") or 0), 0)
                for line in (flag.details or {}).get("lines") or []
            )
            if missed <= 0:
                continue
            shortfall += missed
            if flag.status == ContinuityFlag.Status.OPEN:
                pending += 1
            if len(rows) < TOP:
                rows.append(
                    {
                        "flag_id": flag.id,
                        "bill_number": getattr(flag.sale, "doc_number", ""),
                        "billed_on": flag.sale.billed_at.date().isoformat() if flag.sale else "",
                        "store_code": getattr(flag.store, "code", ""),
                        "missed_paise": missed,
                        "status": flag.status,
                    }
                )
        return {"missed_paise": shortfall, "open_flags": pending, "rows": rows}


def _pct(part: Any, whole: Any) -> str | None:
    part, whole = int(part or 0), int(whole or 0)
    if whole <= 0:
        return None
    return f"{part * 100 / whole:.1f}"
