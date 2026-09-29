"""Reports, Gift Stock: gift pieces and the input tax credit to reverse (ticket 14).

Store operations PRD ST-CMP-7 (baseline, CA to confirm); CGST Act s.17(5)(h).
Read from the reporting copy (``reporting.gift_facts``) alone. Each month
Accounts reverses the input tax credit on stock given away as gifts; this lists
the pieces and works out the credit, per GSTIN. **Prepared for Accounts, not a
filing:** nothing is posted or sent anywhere.

Two views, one at a time:

* ``gstin`` - per GSTIN: gift pieces, their cost and the credit to reverse.
* ``pieces`` - each gift piece, with its bill, receipt rate, cost and credit.

Cost and credit reach only a viewer ``sees_cost`` allows (``money: manage``,
baseline B54): anyone else who may read reports sees the pieces without them -
the columns are left out, not blanked.

Formulas (version ``FORMULA_VERSION``):

* **Credit to reverse** = for each receipt layer the piece came from, its
  receipt cost x the input tax rate on that layer's price-ticket line, half up
  to the paisa, frozen when the piece left stock (B11, B95).
* A piece whose credit is not known (no receipt layer, no rate, or received
  under another GSTIN) is listed, counted apart and left out of the credit
  total - never guessed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db.models import Count, Q, QuerySet, Sum

from masters.store_feature_registry import GIFT_STOCK_ITC
from reporting.base import Column, Missing, ReportScope, envelope, freshness, note_scope
from reporting.gift_facts import KEY as FRESHNESS_KEY
from reporting.models import GiftItcFact

REPORT = "gift_itc"
TITLE = "Gift stock: input tax credit to reverse"
SHEET = "Gift stock ITC"
FORMULA_VERSION = "gift-itc-1"
FEATURE_KEY = GIFT_STOCK_ITC

NOT_A_FILING = (
    "Prepared for Accounts to reverse input tax credit on gift stock. Nothing here is "
    "posted or filed; the CA checks it first. Treating a gift with purchase as a gift "
    "is a baseline the CA has still to confirm."
)

VIEWS: dict[str, str] = {
    "gstin": "By GSTIN",
    "pieces": "Each gift piece",
}

#: Keys only a viewer allowed to see cost receives.
VALUE_KEYS = ("cost_paise", "itc_paise")

SOURCE_LABELS = {
    "gift_offer": "Gift with purchase",
    "free": "Given free, no gift offer",
}
MISSING_LABELS = {
    "": "Known",
    "no_layer": "Missing: no receipt layer recorded",
    "no_rate": "Missing: no input tax rate on its receipt",
    "other_gstin": "Missing: received under another GSTIN",
}

BASIS = [
    NOT_A_FILING,
    "A gift piece is a piece sold on a bill the server has accepted and not cancelled, "
    "for which the customer paid nothing, and which was not part of a buy X get Y offer. "
    "It is tagged when it leaves stock, only at stores where this report is switched on "
    "then. A gift sold at a token price was paid for, so it is a sale, not gift stock.",
    "Dated by the day the piece left stock, India time, under the GSTIN its store had then.",
    "Credit to reverse = for each receipt layer the piece came from, its receipt cost times "
    "the input tax rate on that layer's price ticket, half up to the paisa, fixed when the "
    "piece left stock (CGST Act s.17(5)(h)).",
    "A piece whose credit is not known - no receipt layer recorded, no input tax rate on "
    "its receipt, or received under another GSTIN - is listed and counted apart, and left "
    "out of the credit total. It is never guessed.",
    "A gift piece given back later still counts here; its credit is for the CA to decide.",
]


def stores_with_tags() -> set[int]:
    """Stores holding gift tags stay readable with the switch off (B97): the
    credit on pieces already given away is still to be reversed."""
    return set(GiftItcFact.objects.values_list("store_id", flat=True).distinct())


def _facts(scope: ReportScope) -> QuerySet[GiftItcFact]:
    return GiftItcFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    )


def _money(value: Any) -> int | None:
    return None if value is None else int(value)


def _by_gstin(facts: QuerySet[GiftItcFact]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    unknown = ~Q(missing="")
    sums: dict[str, Any] = {
        "sum_pieces": Sum("pieces"),
        "sum_unknown": Sum("pieces", filter=unknown),
        "sum_returned": Sum("returned"),
        "sum_cost": Sum("cost_paise"),
        "sum_itc": Sum("itc_paise", filter=Q(missing="")),
        "sum_known": Sum("pieces", filter=Q(missing="")),
        "sum_bills": Count("doc_id", distinct=True),
    }

    def measures(found: dict[str, Any]) -> dict[str, Any]:
        return {
            "bills": int(found["sum_bills"] or 0),
            "pieces": int(found["sum_pieces"] or 0),
            "pieces_unknown": int(found["sum_unknown"] or 0),
            "returned": int(found["sum_returned"] or 0),
            "cost_paise": _money(found["sum_cost"]),
            # Nothing known is unknown, never nought.
            "itc_paise": int(found["sum_itc"] or 0) if found["sum_known"] else None,
        }

    rows = [
        {"key": found["gstin"], "gstin": found["gstin"] or "(no GSTIN)", **measures(found)}
        for found in facts.values("gstin").annotate(**sums).order_by("gstin")
    ]
    total = {"key": "total", "gstin": "Total", **measures(facts.aggregate(**sums))}
    return rows, total


def _gstin_columns() -> list[Column]:
    return [
        Column("gstin", "GSTIN", "text"),
        Column("bills", "Bills"),
        Column("pieces", "Gift pieces"),
        Column("pieces_unknown", "Pieces with credit not known"),
        Column("returned", "Given back since"),
        Column("cost_paise", "Receipt cost (Rs)", "money"),
        Column("itc_paise", "Input tax credit to reverse (Rs)", "money"),
    ]


def _pieces(
    facts: QuerySet[GiftItcFact], codes: dict[int, str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "key": f"gift:{fact['tag_id']}",
            "day": fact["day"],
            "store": codes.get(fact["store_id"], ""),
            "gstin": fact["gstin"] or "(no GSTIN)",
            "doc_number": fact["doc_number"],
            "barcode": fact["barcode"],
            "brand": fact["brand"],
            "item": fact["item"],
            "hsn": fact["hsn"],
            "pieces": int(fact["pieces"]),
            "source": SOURCE_LABELS.get(fact["source"], fact["source"]),
            "offer_name": fact["offer_name"],
            "input_tax_pct": fact["input_tax_pct"],
            "cost_paise": _money(fact["cost_paise"]),
            "itc_paise": _money(fact["itc_paise"]),
            "credit": MISSING_LABELS.get(fact["missing"], fact["missing"]),
            "returned": int(fact["returned"]),
        }
        for fact in facts.order_by("day", "doc_number", "line_id").values()
    ]
    total: dict[str, Any] = {
        "key": "total",
        "day": None,
        "store": "Total",
        "pieces": sum(row["pieces"] for row in rows),
        "returned": sum(row["returned"] for row in rows),
        "cost_paise": _known_sum(row["cost_paise"] for row in rows),
        "itc_paise": _known_sum(row["itc_paise"] for row in rows),
    }
    return rows, total


def _known_sum(values: Iterable[int | None]) -> int | None:
    """The known figures added up; None when none is known (never nought)."""
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _piece_columns() -> list[Column]:
    return [
        Column("day", "Date", "date"),
        Column("store", "Store", "text"),
        Column("gstin", "GSTIN", "text"),
        Column("doc_number", "Bill", "text"),
        Column("barcode", "Barcode", "text"),
        Column("brand", "Brand", "text"),
        Column("item", "Item", "text"),
        Column("hsn", "HSN", "text"),
        Column("pieces", "Pieces"),
        Column("source", "Given as", "text"),
        Column("offer_name", "Gift offer", "text"),
        Column("input_tax_pct", "Input tax %", "text"),
        Column("cost_paise", "Receipt cost (Rs)", "money"),
        Column("itc_paise", "Input tax credit to reverse (Rs)", "money"),
        Column("credit", "Credit", "text"),
        Column("returned", "Given back since"),
    ]


def columns(view: str, shows_cost: bool) -> list[Column]:
    found = _gstin_columns() if view == "gstin" else _piece_columns()
    return [c for c in found if shows_cost or c.key not in VALUE_KEYS]


def _without_values(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in VALUE_KEYS}


def build(scope: ReportScope, view: str, *, shows_cost: bool) -> dict[str, Any]:
    """The Gift Stock report for ``scope``, one view at a time."""
    missing = Missing()
    as_of = freshness(FRESHNESS_KEY, missing)
    note_scope(scope, "Gift stock report", missing)
    facts = _facts(scope)
    if view == "gstin":
        rows, total = _by_gstin(facts)
    else:
        rows, total = _pieces(facts, {store.pk: store.code for store in scope.stores})
    unknown = facts.exclude(missing="").aggregate(pieces=Sum("pieces"))["pieces"]
    if unknown:
        missing.add(
            "CREDIT_UNKNOWN",
            f"{int(unknown)} gift piece(s) have no known input tax credit (no receipt layer, "
            "no rate, or received under another GSTIN). They are listed but left out of the "
            "credit total.",
        )
    no_cost = facts.filter(cost_paise__isnull=True).aggregate(pieces=Sum("pieces"))["pieces"]
    if no_cost and shows_cost:
        missing.add(
            "COST_UNKNOWN",
            f"{int(no_cost)} gift piece(s) have no receipt cost recorded; their cost is blank.",
        )
    returned = facts.aggregate(pieces=Sum("returned"))["pieces"]
    if returned:
        missing.add(
            "GIVEN_BACK",
            f"{int(returned)} gift piece(s) were given back later. They still count here; "
            "whether their credit can be taken again is for the CA.",
        )
    if not shows_cost:
        missing.add(
            "VALUES_HIDDEN",
            "Cost and input tax credit are shown only to Accounts and Owner.",
        )
        rows = [_without_values(row) for row in rows]
        total = _without_values(total)
    return envelope(
        report=REPORT,
        title=TITLE,
        formula_version=FORMULA_VERSION,
        scope=scope,
        as_of=as_of,
        basis=BASIS,
        missing=missing,
        shows_cost=shows_cost,
        extra={
            "not_a_filing": NOT_A_FILING,
            "view": view,
            "views": [{"key": key, "label": label} for key, label in VIEWS.items()],
            "columns": [
                {"key": c.key, "label": c.label, "kind": c.kind} for c in columns(view, shows_cost)
            ],
            "rows": rows,
            "total": total,
        },
    )
