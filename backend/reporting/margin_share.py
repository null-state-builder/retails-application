"""Brands, Margin Share: the monthly statement per brand (store operations PRD ST-BRD-4).

For SOR and concession brands, how each month's sales divided between the brand
and KDPS, read from the reporting copy (``reporting.margin_facts``) alone. Each
sold line's split was worked out once, when its bill reached head office
(``sell.services.margin_share``); the statement only adds it up.

Two views of one month:

* **Every brand** - one row per brand: its model(s), margin(s), lines, sale value
  without GST, KDPS's share, the brand's share, and what was not split. Brands
  with any line not split (model or margin unknown) are also listed apart.
* **One brand** (``brand=<id>``) - that brand's statement: each line, with its
  day, store, bill, piece, model, margin and shares.

Formulas (version ``FORMULA_VERSION``): sale value = KDPS's share + the brand's
share + not split, always; a piece given back takes its part back, so returns net
out. Every figure is an **estimate** until OQ-50 is decided (R-FIN-023: an
incomplete formula is never presented as final).

Only Accounts and Owner read it (``money: manage``, the books' own gate, as the
discount funding report, B99): no store role, Admin or brand manager does.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, QuerySet, Sum
from django.utils import timezone

from core.refusals import Refusal
from masters.models import Brand
from masters.store_feature_registry import MARGIN_SHARE
from offers.resolution import normalise
from reporting.base import Column, Missing, ReportScope, envelope, freshness, note_scope
from reporting.margin_facts import KEY as FRESHNESS_KEY
from reporting.models import MarginShareFact
from sell.models import SaleLineMarginShare

REPORT = "margin_share"
TITLE = "Margin share statement (Estimate until OQ-50 is decided)"
SHEET = "Margin share"
FORMULA_VERSION = "margin-share-1"
FEATURE_KEY = MARGIN_SHARE

MODEL_LABELS = dict(SaleLineMarginShare.Model.choices)
UNKNOWN_MODEL = "Unknown"
REASONS = dict(SaleLineMarginShare.Unknown.choices)

BASIS = [
    "An estimate, not a final figure: how a sale divides between a brand and KDPS waits "
    "on OQ-50 (the profitability formula). Nothing here is posted to the books or owed; "
    "SOR payables wait on OQ-26.",
    "Sold lines of SOR and concession brands on bills the server has accepted and not "
    "cancelled, dated by the bill's own day, India time. A bill has a split only if the "
    "switch was on at its store when the bill reached head office.",
    "The amount split is what the line sold for after discount, without GST. KDPS keeps "
    "the brand's margin % from the terms in force on the bill date, rounded half up to the "
    "paisa; the brand's share is the rest.",
    "Outright and consignment brands are not split and are not on this statement.",
    "Not split: the brand's model or margin is not recorded (or the line's brand or season "
    "is not on the books), so nothing is assumed. Sale value = KDPS's share + brand's share "
    "+ not split.",
    "A piece given back takes its part back by pieces, so returns net out.",
    "Worked out once, when the bill reached head office (an offline bill when it synced), "
    "by the terms then approved for the bill's date. A later change to terms never "
    "changes it.",
]

_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def month_bounds(raw: str | None) -> tuple[str, date, date]:
    """``YYYY-MM`` (default: this month, India) as its label, first and last day."""
    text = (raw or "").strip() or timezone.localdate().strftime("%Y-%m")
    found = _MONTH.match(text)
    if not found or not 1 <= int(found.group(2)) <= 12:
        raise Refusal("INVALID_REQUEST", "month must be a month, YYYY-MM.")
    try:
        first = date(int(found.group(1)), int(found.group(2)), 1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    except (ValueError, OverflowError):
        # Year 0000, or a December too late to have a next month.
        raise Refusal("INVALID_REQUEST", "month must be a month, YYYY-MM.") from None
    return text, first, last


def brand_of(raw: str | None) -> Brand | None:
    """The brand a statement is for, by id; None for every brand."""
    text = (raw or "").strip()
    if not text:
        return None
    if not text.isdigit():
        raise Refusal("INVALID_REQUEST", "brand must be a brand's id.")
    brand = Brand.objects.filter(pk=int(text)).first()
    if brand is None:
        raise Refusal("NOT_FOUND", "No brand has that id.")
    return brand


def columns(brand: Brand | None) -> list[Column]:
    money = [
        Column("value_paise", "Sale value without GST (Rs)", "money"),
        Column("kdps_paise", "KDPS's share (Rs)", "money"),
        Column("brand_paise", "Brand's share (Rs)", "money"),
    ]
    if brand is None:
        return [
            Column("label", "Brand", "text"),
            Column("model", "Model", "text"),
            Column("margin", "KDPS margin", "text"),
            Column("lines", "Lines", "number"),
            *money,
            Column("unsplit_paise", "Not split (Rs)", "money"),
        ]
    return [
        Column("day", "Day", "text"),
        Column("store", "Store", "text"),
        Column("bill", "Bill", "text"),
        Column("barcode", "Piece", "text"),
        Column("season", "Season", "text"),
        Column("qty", "Pieces", "number"),
        Column("model", "Model", "text"),
        Column("margin", "KDPS margin", "text"),
        *money,
        Column("note", "Not split because", "text"),
    ]


def _percent(value: Decimal | None) -> str:
    return "" if value is None else f"{value:.2f}%"


def _facts(scope: ReportScope) -> QuerySet[MarginShareFact]:
    return MarginShareFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    )


def _sums() -> dict[str, Any]:
    # Named apart from the columns they add up, which an aggregate may not shadow.
    return {
        "n_lines": Count("line_id", filter=Q(sign=1), distinct=True),
        "sum_value": Sum("value_paise"),
        "sum_kdps": Sum("kdps_paise"),
        "sum_brand": Sum("brand_paise"),
        "sum_unsplit": Sum("value_paise", filter=~Q(unknown_reason="")),
    }


def _figures(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "lines": int(row.get("n_lines") or 0),
        "value_paise": int(row.get("sum_value") or 0),
        "kdps_paise": int(row.get("sum_kdps") or 0),
        "brand_paise": int(row.get("sum_brand") or 0),
        "unsplit_paise": int(row.get("sum_unsplit") or 0),
    }


def _key(ref: int | None, text: str, names: dict[int, str]) -> tuple[str, str]:
    """A brand in the brand list by its id, under its name there; a line whose brand
    is not one brand in the list by what it says."""
    if ref is not None and ref in names:
        return f"brand:{ref}", names[ref]
    return f"text:{normalise(text)}", text or "(no brand on the line)"


def _models(found: set[str]) -> str:
    known = sorted(MODEL_LABELS.get(model, model) for model in found if model)
    return ", ".join(known + ([UNKNOWN_MODEL] if "" in found else []))


def _by_brand(facts: QuerySet[MarginShareFact]) -> tuple[list[dict[str, Any]], list[Any]]:
    names = dict(
        Brand.objects.filter(
            pk__in=facts.exclude(brand_ref_id=None).values("brand_ref_id")
        ).values_list("pk", "name")
    )
    grouped: dict[str, dict[str, Any]] = {}
    models: dict[str, set[str]] = {}
    margins: dict[str, set[Decimal]] = {}
    for row in facts.values("brand_ref_id", "brand").annotate(**_sums()):
        key, label = _key(row["brand_ref_id"], row["brand"], names)
        figures = _figures(row)
        if key not in grouped:
            grouped[key] = {"key": key, "label": label, **figures}
            continue
        # Two spellings of one brand ("U.S. Polo", "US POLO") are one row.
        for name, value in figures.items():
            grouped[key][name] += value
    reasons: dict[str, set[str]] = {}
    for kind in facts.values(
        "brand_ref_id", "brand", "model", "margin_percent", "unknown_reason"
    ).distinct():
        key, _ = _key(kind["brand_ref_id"], kind["brand"], names)
        models.setdefault(key, set()).add(kind["model"])
        if kind["margin_percent"] is not None:
            margins.setdefault(key, set()).add(kind["margin_percent"])
        if kind["unknown_reason"]:
            reasons.setdefault(key, set()).add(kind["unknown_reason"])
    unsplit = _unsplit(facts, names)
    rows = []
    for key, row in grouped.items():
        row["model"] = _models(models.get(key, set()))
        row["margin"] = ", ".join(_percent(m) for m in sorted(margins.get(key, set())))
        rows.append(row)
    unknown = [
        {
            "key": key,
            "label": grouped[key]["label"],
            **unsplit[key],
            "reasons": [REASONS.get(r, r) for r in sorted(reasons[key])],
        }
        for key in sorted(reasons, key=lambda k: grouped[k]["label"])
    ]
    return rows, unknown


def _unsplit(facts: QuerySet[MarginShareFact], names: dict[int, str]) -> dict[str, dict[str, int]]:
    """Per brand, the lines not split and their value, returns netted."""
    out: dict[str, dict[str, int]] = {}
    for row in (
        facts.exclude(unknown_reason="")
        .values("brand_ref_id", "brand")
        .annotate(n_lines=Count("line_id", filter=Q(sign=1), distinct=True), v=Sum("value_paise"))
    ):
        key, _ = _key(row["brand_ref_id"], row["brand"], names)
        found = out.setdefault(key, {"lines": 0, "value_paise": 0})
        found["lines"] += int(row["n_lines"] or 0)
        found["value_paise"] += int(row["v"] or 0)
    return out


def _lines(facts: QuerySet[MarginShareFact], scope: ReportScope) -> list[dict[str, Any]]:
    stores = {store.pk: store.code for store in scope.stores}
    return [
        {
            "key": str(fact.line_id),
            "day": fact.day.isoformat(),
            "store": stores.get(fact.store_id, ""),
            "bill": fact.doc_number,
            "barcode": fact.barcode,
            "season": fact.season,
            "qty": fact.qty,
            "model": MODEL_LABELS.get(fact.model, UNKNOWN_MODEL),
            "margin": _percent(fact.margin_percent),
            "value_paise": int(fact.value_paise),
            "kdps_paise": None if fact.kdps_paise is None else int(fact.kdps_paise),
            "brand_paise": None if fact.brand_paise is None else int(fact.brand_paise),
            "note": REASONS.get(fact.unknown_reason, fact.unknown_reason),
        }
        for fact in facts.order_by("day", "bill_id", "line_id")
    ]


def _note_unknown(facts: QuerySet[MarginShareFact], missing: Missing) -> None:
    reasons = facts.exclude(unknown_reason="").values_list("unknown_reason", flat=True).distinct()
    for reason in sorted(reasons):
        text = REASONS.get(reason, reason)
        if reason in (SaleLineMarginShare.Unknown.MODEL, SaleLineMarginShare.Unknown.MARGIN):
            text += " (Brands, Terms lists every brand whose model is unknown)"
        missing.add(
            f"UNKNOWN_{reason.upper()}",
            f"{text}: those lines are listed, not split, never guessed.",
        )


def build(scope: ReportScope, month: str, brand: Brand | None) -> dict[str, Any]:
    """The statement for ``scope``'s month: every brand, or one brand's lines."""
    missing = Missing()
    as_of = freshness(FRESHNESS_KEY, missing)
    note_scope(scope, "Margin share statement", missing)
    facts = _facts(scope)
    unknown: list[Any] = []
    if brand is None:
        rows, unknown = _by_brand(facts)
        rows.sort(key=lambda row: (-row["value_paise"], row["label"]))
    else:
        facts = facts.filter(brand_ref_id=brand.pk)
        rows = _lines(facts, scope)
    _note_unknown(facts, missing)
    total = {"key": "total", "label": "Total", **_figures(facts.aggregate(**_sums()))}
    if brand is not None:
        total["day"] = "Total"
    return envelope(
        report=REPORT,
        title=TITLE,
        formula_version=FORMULA_VERSION,
        scope=scope,
        as_of=as_of,
        basis=BASIS,
        missing=missing,
        shows_cost=False,
        extra={
            "estimate": True,
            "month": month,
            "brand": None if brand is None else {"id": brand.pk, "name": brand.name},
            "columns": [{"key": c.key, "label": c.label, "kind": c.kind} for c in columns(brand)],
            "rows": rows,
            "total": total,
            "unknown_brands": unknown,
        },
    )
