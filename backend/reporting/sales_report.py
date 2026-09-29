"""Reports, Sales (store operations PRD ST-RPT-1; overall PRD R-AN-006 group A/B).

Bills, pieces, value, average selling price, average bill value, units per bill,
discount % and target achievement - by day, store, brand, category, salesperson
and tender - read from the reporting copy (``reporting.sales_facts``) alone.

Formulas (version ``FORMULA_VERSION``; change the version if one changes):

* **Bills** - accepted, numbered, not cancelled bills with a sold piece in the
  group. A bill counts once in a group; one bill can count in several groups
  (two brands, two salespeople), so the groups' bills can add to more than the
  total.
* **Pieces** - pieces sold less pieces given back.
* **Value** - what the customer paid for each piece after discount, GST
  included, less what was given back. Bill rounding is not in line value.
* **ASP** = value / pieces. **ABV** = value / bills. **UPT** = pieces / bills.
* **Discount %** = discount / MRP value of the pieces, both net of returns.
* **Target achievement** = value / the store's monthly targets for the months
  in the period (Money, Store Targets). Targets exist per store and month only.
* **By salesperson** uses split shares (ticket 08): each person's share of a
  line's pieces and money; no share rows means the line's own salesperson.
* **By tender** is money taken by each way of paying, bill rounding included.
* **Cost / margin** (only for viewers ``sees_cost`` allows): cost frozen on the
  line at billing; margin = value less GST less cost, over lines with a cost.

Conversion is not shown: it needs footfall, which is not recorded (OQ-15).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db.models import Count, F, Max, Q, QuerySet, Sum
from django.utils import timezone

from masters.models import StoreTarget
from reporting.base import (
    Column,
    Missing,
    ReportScope,
    envelope,
    freshness,
    note_scope,
    paise_ratio,
    percent,
    ratio,
    sees_cost,
    sees_targets,
    strip_cost,
)
from reporting.models import SalesLineFact, SalesTenderFact
from reporting.sales_facts import KEY as FRESHNESS_KEY

REPORT = "sales"
TITLE = "Sales report"
FORMULA_VERSION = "sales-1"
FEATURE_KEY = "sales-report"

GROUPINGS: dict[str, str] = {
    "day": "Day",
    "store": "Store",
    "brand": "Brand",
    "category": "Category",
    "salesperson": "Salesperson",
    "tender": "Tender",
}
_FACT_KEY = {
    "day": "day",
    "store": "store_id",
    "brand": "brand",
    "category": "category",
    "salesperson": "salesperson_key",
}
TENDER_LABELS = {
    "cash": "Cash",
    "card": "Card",
    "upi": "UPI",
    "credit_note": "Credit note",
    # Ticket 11: a bank instant discount taken as a payment (never cash).
    "bank_offer": "Bank offer",
    # Ticket 19: a gift voucher spent on the bill (paid for when it was sold).
    "gift_voucher": "Gift voucher",
    SalesTenderFact.GIVEN_BACK: "Given back on an old return",
}

BASIS = [
    "Bills the server has accepted and not cancelled, dated by the bill's time at the "
    "till, India time. Pieces given back come off on the day they were given back, so "
    "returns net out.",
    "Value is what the customer paid for each piece after discount, GST included. Bill "
    "rounding (at most 50 paise a bill) is not in line value; tender totals include it.",
    "Pieces = sold less given back. Average selling price = value / pieces. Average bill "
    "value = value / bills. Units per bill = pieces / bills. Discount % = discount / MRP "
    "value.",
    "A bill counts once in each group it has a sold piece in, so the groups' bills can "
    "add to more than the total.",
    "By salesperson: a line shared between two people is credited by each person's "
    "percentage (split sale); a piece given back comes off whoever sold it.",
    "By tender: money taken by each way of paying; a bill paid two ways counts under both. "
    "A bank offer is a bank instant discount recorded as a payment, owed by the bank.",
    "Target achievement = value / the store's monthly targets (Money, Store Targets) for "
    "the months in the period; a month still running is measured against its whole "
    "target.",
]
COST_BASIS = (
    "Cost is the cost frozen on each line at billing. Margin = value less GST less cost, "
    "over the lines whose cost is known."
)
CONVERSION = "Conversion is not shown: it needs footfall counts, which are not recorded (OQ-15)."


@dataclass(frozen=True)
class Period:
    """The months a period covers, and whether it is whole months (or up to today)."""

    months: list[date]
    whole: bool


def period_months(date_from: date, date_to: date, today: date) -> Period:
    months: list[date] = []
    cursor = date_from.replace(day=1)
    while cursor <= date_to:
        months.append(cursor)
        cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
    last_day = calendar.monthrange(date_to.year, date_to.month)[1]
    whole = date_from.day == 1 and (date_to.day == last_day or date_to == today)
    return Period(months, whole)


def _line_facts(scope: ReportScope) -> QuerySet[SalesLineFact]:
    return SalesLineFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    )


def _tender_facts(scope: ReportScope) -> QuerySet[SalesTenderFact]:
    return SalesTenderFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    )


#: Named ``sum_*`` so no aggregate shadows the column it sums.
_LINE_SUMS: dict[str, Any] = {
    "sum_bills": Count("bill_id", distinct=True),
    "sum_pieces_x100": Sum("pieces_x100"),
    "sum_gross": Sum("gross_paise"),
    "sum_disc": Sum("disc_paise"),
    "sum_value": Sum("value_paise"),
    "sum_cost": Sum("cost_paise"),
    "sum_costed_net": Sum(F("value_paise") - F("gst_paise"), filter=Q(cost_paise__isnull=False)),
    # One row per line: an unsplit line (position 0) or a split line's first share.
    "sum_uncosted_lines": Count("id", filter=Q(cost_paise__isnull=True, share_position__lte=1)),
    "sum_uncosted_value": Sum("value_paise", filter=Q(cost_paise__isnull=True)),
}


def _measures(sums: dict[str, Any]) -> dict[str, Any]:
    """The report's measures from one group's sums. Money in paise; ratios rounded."""
    bills = int(sums.get("sum_bills") or 0)
    pieces_x100 = int(sums.get("sum_pieces_x100") or 0)
    value = int(sums.get("sum_value") or 0)
    gross = int(sums.get("sum_gross") or 0)
    disc = int(sums.get("sum_disc") or 0)
    costed_net = sums.get("sum_costed_net")
    cost = sums.get("sum_cost")
    margin = None if costed_net is None or cost is None else int(costed_net) - int(cost)
    return {
        "bills": bills,
        "pieces": pieces_x100 / 100,
        "value_paise": value,
        "gross_paise": gross,
        "disc_paise": disc,
        "asp_paise": paise_ratio(value * 100, pieces_x100),
        "abv_paise": paise_ratio(value, bills),
        "upt": ratio(pieces_x100, bills * 100),
        "discount_pct": percent(disc, gross),
        "cost_paise": None if cost is None else int(cost),
        "margin_paise": margin,
        "margin_pct": None if margin is None else percent(margin, int(costed_net or 0)),
        "uncosted_lines": int(sums.get("sum_uncosted_lines") or 0),
        "uncosted_value_paise": int(sums.get("sum_uncosted_value") or 0),
    }


def _tender_measures(bills: int, value: int) -> dict[str, Any]:
    return {
        "bills": bills,
        "pieces": None,
        "value_paise": value,
        "gross_paise": None,
        "disc_paise": None,
        "asp_paise": None,
        "abv_paise": paise_ratio(value, bills),
        "upt": None,
        "discount_pct": None,
        "cost_paise": None,
        "margin_paise": None,
        "margin_pct": None,
        "uncosted_lines": 0,
        "uncosted_value_paise": 0,
    }


def _label(group_by: str, key: Any, row: dict[str, Any], stores: dict[int, Any]) -> str:
    if group_by == "day":
        return str(key.isoformat())
    if group_by == "store":
        store = stores.get(key)
        return f"{store.name} ({store.code})" if store else str(key)
    if group_by == "salesperson":
        if not key:
            return "No salesperson recorded"
        name = row.get("sp_name") or "Unnamed"
        code = row.get("sp_code")
        label = f"{name} ({code})" if code else name
        return f"{label}, before the staff list" if str(key).startswith("old:") else label
    if group_by == "tender":
        return TENDER_LABELS.get(key, str(key))
    if not key:
        return f"(no {group_by})"
    return str(key)


def _key_text(key: Any) -> str:
    return key.isoformat() if isinstance(key, date) else str(key)


def build(scope: ReportScope, group_by: str) -> dict[str, Any]:
    """The sales report for ``scope``, grouped by ``group_by``, as the viewer may see it."""
    user = scope.user
    show_cost = sees_cost(user)
    show_target = sees_targets(user)
    missing = Missing()
    as_of = freshness(FRESHNESS_KEY, missing)
    note_scope(scope, TITLE, missing)
    stores = {store.pk: store for store in scope.stores}

    if group_by == "tender":
        tenders = _tender_facts(scope)
        rows = [
            {
                "key": row["mode"],
                "label": _label("tender", row["mode"], row, stores),
                **_tender_measures(int(row["bills"] or 0), int(row["value"] or 0)),
            }
            for row in tenders.values("mode")
            .annotate(bills=Count("bill_id", distinct=True), value=Sum("amount_paise"))
            .order_by("-value", "mode")
        ]
    else:
        field = _FACT_KEY[group_by]
        grouped = _line_facts(scope).values(field)
        if group_by == "salesperson":
            grouped = grouped.annotate(
                sp_name=Max("salesperson_name"), sp_code=Max("salesperson_code")
            )
        found = list(grouped.annotate(**_LINE_SUMS))
        rows = [
            {
                "key": _key_text(row[field]),
                "label": _label(group_by, row[field], row, stores),
                **_measures(row),
            }
            for row in found
        ]
        if group_by in ("day", "store"):
            order = {s.pk: s.code for s in scope.stores}
            rows.sort(key=lambda r: r["key"] if group_by == "day" else order.get(int(r["key"]), ""))
        else:
            rows.sort(key=lambda r: (-r["value_paise"], r["label"]))
        if group_by == "salesperson" and any(not row["key"] for row in rows):
            missing.add(
                "NO_SALESPERSON",
                "Some pieces have no salesperson recorded; they are shown as their own row.",
            )

    total_sums = _line_facts(scope).aggregate(**_LINE_SUMS)
    total = {"key": "total", "label": "Total", **_measures(total_sums)}
    if group_by == "tender":
        tender_total = _tender_facts(scope).aggregate(
            bills=Count("bill_id", distinct=True), value=Sum("amount_paise")
        )
        total = {
            "key": "total",
            "label": "Total",
            **_tender_measures(int(tender_total["bills"] or 0), int(tender_total["value"] or 0)),
        }

    # Achievement is always net line value against the target, whichever grouping
    # is on screen: the tender total also carries bill rounding.
    achieved = int(total_sums.get("sum_value") or 0)
    _targets(scope, group_by, rows, total, achieved, show_target, missing)
    missing.add("CONVERSION", CONVERSION)
    if show_cost and int(total_sums.get("sum_uncosted_lines") or 0):
        missing.add(
            "UNCOSTED",
            f"{int(total_sums['sum_uncosted_lines'])} line(s) worth "
            f"Rs {int(total_sums['sum_uncosted_value'] or 0) / 100:,.2f} have no cost yet "
            "(sold before their goods were priced); cost and margin leave them out.",
        )

    for row in [*rows, total]:
        row.pop("uncosted_lines", None)
        row.pop("uncosted_value_paise", None)
    if not show_cost:
        rows = [strip_cost(row) for row in rows]
        total = strip_cost(total)

    return envelope(
        report=REPORT,
        title=TITLE,
        formula_version=FORMULA_VERSION,
        scope=scope,
        as_of=as_of,
        basis=[*BASIS, *([COST_BASIS] if show_cost else [])],
        missing=missing,
        shows_cost=show_cost,
        extra={
            "group_by": group_by,
            "groupings": [{"key": k, "label": v} for k, v in GROUPINGS.items()],
            "shows_target": show_target,
            "rows": rows,
            "total": total,
        },
    )


def _targets(
    scope: ReportScope,
    group_by: str,
    rows: list[dict[str, Any]],
    total: dict[str, Any],
    achieved: int,
    show_target: bool,
    missing: Missing,
) -> None:
    """Fill target and achievement where a target exists; say where one does not.

    Never invents a target: no row for a store and month means no target, and a
    grouping targets are not set by (brand, category, salesperson, day, tender)
    has none.
    """
    for row in [*rows, total]:
        row["target_paise"] = None
        row["target_pct"] = None
    if not show_target:
        for row in [*rows, total]:
            row.pop("target_paise")
            row.pop("target_pct")
        missing.add(
            "TARGETS_HIDDEN",
            "Target achievement is not shown to you: store targets are Money data your "
            "role does not read.",
        )
        return
    period = period_months(scope.date_from, scope.date_to, timezone.localdate())
    if not period.whole:
        missing.add(
            "TARGETS_PARTIAL_MONTH",
            "Targets are set per store per month, so achievement is shown only for whole "
            "months (or a month up to today). Choose a period that starts on the 1st.",
        )
        return
    set_for = {
        (row["store_id"], row["month"]): int(row["target_paise"])
        for row in StoreTarget.objects.filter(
            store_id__in=scope.store_ids, month__in=period.months
        ).values("store_id", "month", "target_paise")
    }
    per_store: dict[int, int | None] = {}
    for store in scope.stores:
        wanted = [set_for.get((store.pk, month)) for month in period.months]
        if any(value is None for value in wanted):
            gaps = [
                month.strftime("%b %Y")
                for month, value in zip(period.months, wanted, strict=True)
                if value is None
            ]
            missing.add(
                f"NO_TARGET_{store.code}",
                f"No target is set for {store.name} ({store.code}) for {', '.join(gaps)}.",
            )
            per_store[store.pk] = None
        else:
            per_store[store.pk] = sum(int(v or 0) for v in wanted)
    if group_by == "store":
        for row in rows:
            target = per_store.get(int(row["key"]))
            row["target_paise"] = target
            row["target_pct"] = percent(row["value_paise"], target) if target else None
    else:
        missing.add(
            "TARGETS_BY_STORE_ONLY",
            f"There are no targets by {GROUPINGS[group_by].lower()}: targets are set per "
            "store per month, so achievement is shown for the total only.",
        )
    if per_store and all(value is not None for value in per_store.values()):
        target_total = sum(int(v or 0) for v in per_store.values())
        total["target_paise"] = target_total
        total["target_pct"] = percent(achieved, target_total) if target_total else None


def columns(body: dict[str, Any]) -> list[Column]:
    """The spreadsheet's columns, as this viewer may see them."""
    grouping = GROUPINGS[body["group_by"]]
    out = [
        Column("label", grouping, "text"),
        Column("bills", "Bills"),
        Column("pieces", "Pieces"),
        Column("value_paise", "Value (Rs)", "money"),
        Column("asp_paise", "Average selling price (Rs)", "money"),
        Column("abv_paise", "Average bill value (Rs)", "money"),
        Column("upt", "Units per bill"),
        Column("gross_paise", "MRP value (Rs)", "money"),
        Column("disc_paise", "Discount (Rs)", "money"),
        Column("discount_pct", "Discount %"),
    ]
    if body["shows_target"]:
        out += [
            Column("target_paise", "Target (Rs)", "money"),
            Column("target_pct", "Target achievement %"),
        ]
    if body["shows_cost"]:
        out += [
            Column("cost_paise", "Cost (Rs)", "money"),
            Column("margin_paise", "Margin (Rs)", "money"),
            Column("margin_pct", "Margin %"),
        ]
    return out
