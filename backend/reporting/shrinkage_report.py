"""Reports, Shrinkage (store operations PRD ST-INV-4; overall PRD R-INV-008, R-INV-009).

Pieces and cost lost to shrinkage, by store, brand, category or month, as a
share of sales - read from the reporting copies alone (``reporting.shrinkage_
facts`` and the sales copy, ``reporting.sales_facts``).

Formulas (version ``FORMULA_VERSION``; change the version if one changes):

* **Pieces lost** - pieces on approved shrinkage adjustments and on approved
  write-offs typed as shrinkage, dated by the day of approval. A reversed
  document is not counted.
* **Pieces sold** - pieces sold less pieces given back (the sales report's own
  figure), in the same group and period.
* **Pieces lost %** = pieces lost / pieces sold.
* **Sales before GST** - what customers paid, after discount, less GST, net of
  returns.
* **Cost lost** (only for viewers ``sees_cost`` allows) - the recorded layer
  cost of the pieces lost, as the document valued them; lines with no recorded
  cost are left out and said so.
* **Cost lost % of sales** = cost lost / sales before GST (only for the same
  viewers: with sales on the page, it would give cost away).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.db.models import Count, F, Q, QuerySet, Sum
from django.db.models.functions import TruncMonth

from reporting.base import (
    Column,
    Missing,
    ReportScope,
    envelope,
    freshness,
    note_scope,
    percent,
    sees_cost,
    strip_cost,
)
from reporting.models import SalesLineFact, ShrinkageLineFact
from reporting.sales_facts import KEY as SALES_KEY
from reporting.shrinkage_facts import KEY as FRESHNESS_KEY
from reporting.shrinkage_facts import shrinkage_reasons

REPORT = "shrinkage"
TITLE = "Shrinkage report"
FORMULA_VERSION = "shrinkage-1"
FEATURE_KEY = "shrinkage-report"

GROUPINGS: dict[str, str] = {
    "store": "Store",
    "brand": "Brand",
    "category": "Category",
    "month": "Month",
}
_FIELD = {"store": "store_id", "brand": "brand", "category": "category", "month": "month"}

BASIS = [
    "Pieces lost are the pieces on shrinkage adjustments and on write-offs typed as "
    "shrinkage that were approved, dated by the day of approval, India time. A "
    "document no longer in force (reversed or cancelled) is not counted.",
    "At a store still on the old stock system, an approved count correction counts "
    "where its reason for that piece is shrinkage.",
    "An adjust-down (a counting or recording error) is not shrinkage, and nor is a "
    "write-off for any other reason, such as damage.",
    "Pieces sold = sold less given back, from the sales report's own copy of the bills. "
    "Pieces lost % = pieces lost / pieces sold, in the same group and period.",
    "Sales before GST = what customers paid after discount, less GST, net of returns.",
    "Brand and category are matched by name, as the till names them on a bill. For "
    "the new stock system they are today's names, so after a brand is renamed its "
    "earlier sales may sit under the old name.",
]
COUNT_DIFFERENCES = (
    "A count's own difference at a store on the new stock system is not included: "
    "it is not recorded anywhere yet. Shortfalls found at a count show here once "
    "they are recorded as a shrinkage adjustment."
)
COST_BASIS = (
    "Cost lost is the recorded layer cost of the pieces lost, as the document valued "
    "them. Cost lost % of sales = cost lost / sales before GST."
)


def _shrink_facts(scope: ReportScope) -> QuerySet[ShrinkageLineFact]:
    return ShrinkageLineFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    ).annotate(month=TruncMonth("day"))


def _sales_facts(scope: ReportScope) -> QuerySet[SalesLineFact]:
    return SalesLineFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    ).annotate(month=TruncMonth("day"))


_SHRINK_SUMS: dict[str, Any] = {
    "sum_lost": Sum("pieces"),
    "sum_cost": Sum("cost_paise"),
    "sum_uncosted": Sum("pieces", filter=Q(cost_paise__isnull=True)),
    "sum_documents": Count("document_id", distinct=True),
}
_SALES_SUMS: dict[str, Any] = {
    "sum_sold_x100": Sum("pieces_x100"),
    "sum_sales": Sum(F("value_paise") - F("gst_paise")),
}


def _measures(shrink: dict[str, Any], sales: dict[str, Any]) -> dict[str, Any]:
    lost = int(shrink.get("sum_lost") or 0)
    sold_x100 = int(sales.get("sum_sold_x100") or 0)
    sales_paise = int(sales.get("sum_sales") or 0)
    # Cost over the lines whose cost is known; nothing lost is nought lost.
    cost_paise = int(shrink.get("sum_cost") or 0)
    return {
        "documents": int(shrink.get("sum_documents") or 0),
        "pieces_lost": lost,
        "pieces_sold": sold_x100 / 100,
        "pieces_pct": percent(lost * 100, sold_x100) if sold_x100 > 0 else None,
        "sales_paise": sales_paise,
        "cost_paise": cost_paise,
        "cost_share_pct": percent(cost_paise, sales_paise) if sales_paise > 0 else None,
        "uncosted_pieces": int(shrink.get("sum_uncosted") or 0),
    }


def _key_text(key: Any) -> str:
    if isinstance(key, date):
        return key.strftime("%Y-%m")
    return "" if key is None else str(key)


def _months(date_from: date, date_to: date) -> list[date]:
    out: list[date] = []
    cursor = date_from.replace(day=1)
    while cursor <= date_to:
        out.append(cursor)
        cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
    return out


def build(scope: ReportScope, group_by: str) -> dict[str, Any]:
    """The shrinkage report for ``scope``, grouped by ``group_by``, as the viewer may see it."""
    show_cost = sees_cost(scope.user, scope.stores)
    missing = Missing()
    as_of = freshness(FRESHNESS_KEY, missing)
    _sales_freshness(missing)
    note_scope(scope, TITLE, missing)
    missing.add("COUNT_DIFFERENCES", COUNT_DIFFERENCES)

    field = _FIELD[group_by]
    shrink = {
        _key_text(row[field]): row
        for row in _shrink_facts(scope).values(field).annotate(**_SHRINK_SUMS).order_by()
    }
    sales = {
        _key_text(row[field]): row
        for row in _sales_facts(scope).values(field).annotate(**_SALES_SUMS).order_by()
    }
    keys: list[str] = list(dict.fromkeys([*shrink, *sales]))
    if group_by == "store":
        keys = [str(store.pk) for store in sorted(scope.stores, key=lambda s: s.code)]
    elif group_by == "month":
        keys = [_key_text(month) for month in _months(scope.date_from, scope.date_to)]
    stores = {str(store.pk): store for store in scope.stores}
    rows = [
        {
            "key": key,
            "label": _label(group_by, key, stores),
            **_measures(shrink.get(key, {}), sales.get(key, {})),
        }
        for key in keys
    ]
    if group_by in ("brand", "category"):
        rows.sort(key=lambda r: (-r["pieces_lost"], -r["pieces_sold"], r["label"]))

    total = {
        "key": "total",
        "label": "Total",
        **_measures(
            _shrink_facts(scope).aggregate(**_SHRINK_SUMS),
            _sales_facts(scope).aggregate(**_SALES_SUMS),
        ),
    }
    if show_cost and total["uncosted_pieces"]:
        missing.add(
            "UNCOSTED",
            f"{total['uncosted_pieces']} piece(s) lost have no recorded cost; cost lost "
            "leaves them out.",
        )
    for row in [*rows, total]:
        row.pop("uncosted_pieces", None)
    if not show_cost:
        rows = [strip_cost(row) for row in rows]
        total = strip_cost(total)

    return envelope(
        report=REPORT,
        title=TITLE,
        formula_version=FORMULA_VERSION,
        scope=scope,
        as_of=as_of,
        basis=[*BASIS, _reasons_basis(), *([COST_BASIS] if show_cost else [])],
        missing=missing,
        shows_cost=show_cost,
        extra={
            "group_by": group_by,
            "groupings": [{"key": k, "label": v} for k, v in GROUPINGS.items()],
            "columns": [column_json(column) for column in columns(group_by, show_cost)],
            "rows": rows,
            "total": total,
        },
    )


def _reasons_basis() -> str:
    reasons = ", ".join(sorted(shrinkage_reasons())) or "none"
    return f"A write-off is typed as shrinkage when its reason code is one of: {reasons}."


def _sales_freshness(missing: Missing) -> None:
    """Say when the sales copy the shares are worked out against is absent or late."""
    sales = Missing()
    freshness(SALES_KEY, sales)
    for item in sales.items:
        missing.add(f"SALES_{item['code']}", f"Sales, for the shares: {item['text']}")


def _label(group_by: str, key: str, stores: dict[str, Any]) -> str:
    if group_by == "store":
        store = stores.get(key)
        return f"{store.name} ({store.code})" if store else key
    if group_by == "month":
        return date.fromisoformat(f"{key}-01").strftime("%b %Y")
    return key or f"(no {group_by})"


def columns(group_by: str, show_cost: bool) -> list[Column]:
    """The table's and the spreadsheet's columns, as this viewer may see them."""
    out = [
        Column("label", GROUPINGS[group_by], "text"),
        Column("pieces_lost", "Pieces lost"),
        Column("pieces_sold", "Pieces sold"),
        Column("pieces_pct", "Pieces lost % of sold", "rate"),
        Column("sales_paise", "Sales before GST (Rs)", "money"),
    ]
    if show_cost:
        out += [
            Column("cost_paise", "Cost lost (Rs)", "money"),
            Column("cost_share_pct", "Cost lost % of sales", "rate"),
        ]
    return out


def column_json(column: Column) -> dict[str, str]:
    return {"key": column.key, "label": column.label, "kind": column.kind}
