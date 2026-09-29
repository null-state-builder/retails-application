"""Reports, Discount Funding (store operations PRD ST-OFR-2; overall PRD R-PRC-001).

Who paid for the discounts given: the brand's share and KDPS's share, per
offer, brand or store, read from the reporting copy (``reporting.funding_facts``)
alone. Each discounted line's split was worked out once, when its bill reached
head office (``sell.services.discount_funding``); this report only adds it up.

Formulas (version ``FORMULA_VERSION``):

* **Discount** = the discount the parts carry; **Brand's share** and **KDPS's
  share** add up the parts whose split is known; **Unknown** is the discount of
  the parts whose split is not (the brand's model or share is not recorded).
  Discount = brand's share + KDPS's share + unknown, always.
* A piece given back takes its part back, so returns net out.
* **Lines** counts the discounted sold lines (a line with two offers counts once
  for each offer, once for its brand and store).

Only Accounts and Owner read it (``money: manage``, the books' own gate): no
store role does (§17, "hide cost and margin from store roles").
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db.models import Count, Max, Q, QuerySet, Sum

from masters.models import Brand, Store
from masters.store_feature_registry import DISCOUNT_FUNDING_SPLIT
from offers.resolution import normalise
from reporting.base import Column, Missing, ReportScope, envelope, freshness, note_scope
from reporting.funding_facts import KEY as FRESHNESS_KEY
from reporting.models import DiscountFundingFact
from sell.models import SaleLineFunding

REPORT = "discount_funding"
TITLE = "Discount funding: brand's share and KDPS's share"
SHEET = "Discount funding"
FORMULA_VERSION = "funding-1"
FEATURE_KEY = DISCOUNT_FUNDING_SPLIT

GROUPINGS: dict[str, str] = {
    "offer": "By offer",
    "brand": "By brand",
    "store": "By store",
}

FUNDER_LABELS = {"brand": "Brand, by its terms", "kdps": "KDPS", "": "Not known"}
MANUAL_LABEL = "Not from an offer (manual discount)"
UNREAD_LABEL = "Offers not readable on the bill"

BASIS = [
    "Discounted lines of bills the server has accepted and not cancelled, dated by the "
    "bill's own day, India time. A bill has a split only if the switch was on at its store "
    "when the bill reached head office.",
    "Each line's discount is split by where it came from: the winning offer, any add-on "
    "stacked on it, and the rest no offer gave (a manual discount).",
    "A brand-funded offer is shared by the brand terms in force on the bill date: the brand "
    "pays its discount-funding %, rounded half up to the paisa, and KDPS pays the rest. A "
    "KDPS offer and a manual discount are KDPS's alone.",
    "Unknown: the brand's model or share is not recorded (or the bill cannot be read "
    "against its offers), so nothing is assumed. Discount = brand's share + KDPS's share "
    "+ unknown.",
    "A piece given back takes its part of the split back by pieces, so returns net out. "
    "A partly returned line's discount here can differ from the sales report's by a "
    "paisa of rounding; a line returned whole matches exactly.",
    "Worked out once, when the bill reached head office (an offline bill when it synced), "
    "by the terms then approved for the bill's date. A later change to terms or offers "
    "never changes it.",
    "The offers' share of a line is never taken as more than the server's own reading of "
    "the offers gives; a till claiming more leaves the line unknown.",
]

_MONEY = ("discount_paise", "brand_paise", "kdps_paise", "unknown_paise")


def columns(group_by: str) -> list[Column]:
    head = {
        "offer": [Column("label", "Offer", "text"), Column("funder", "Funded by", "text")],
        "brand": [Column("label", "Brand", "text")],
        "store": [Column("label", "Store", "text")],
    }[group_by]
    return [
        *head,
        Column("lines", "Lines", "number"),
        Column("discount_paise", "Discount (Rs)", "money"),
        Column("brand_paise", "Brand's share (Rs)", "money"),
        Column("kdps_paise", "KDPS's share (Rs)", "money"),
        Column("unknown_paise", "Unknown (Rs)", "money"),
    ]


def _facts(scope: ReportScope) -> QuerySet[DiscountFundingFact]:
    return DiscountFundingFact.objects.filter(
        store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
    )


def _sums() -> dict[str, Any]:
    # Named apart from the columns they add up, which an aggregate may not shadow.
    return {
        "n_lines": Count("line_id", filter=Q(sign=1), distinct=True),
        "sum_discount": Sum("discount_paise"),
        "sum_brand": Sum("brand_paise"),
        "sum_kdps": Sum("kdps_paise"),
        "sum_unknown": Sum("discount_paise", filter=~Q(unknown_reason="")),
    }


def _figures(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "lines": int(row.get("n_lines") or 0),
        **{key: int(row.get(f"sum_{key.removesuffix('_paise')}") or 0) for key in _MONEY},
    }


def _by_offer(facts: QuerySet[DiscountFundingFact]) -> list[dict[str, Any]]:
    # An offer by its id (under the last of its recorded names, alphabetically); a
    # part with no offer by what it says.
    listed = (
        facts.filter(offer_id__isnull=False)
        .values("offer_id", "funder")
        .annotate(name=Max("offer_name"), **_sums())
    )
    unlisted = (
        facts.filter(offer_id__isnull=True)
        .values("funder", "offer_name")
        .annotate(name=Max("offer_name"), **_sums())
    )
    rows = []
    for row in [*listed, *unlisted]:
        if row.get("offer_id") is not None:
            key, label = f"offer:{row['offer_id']}:{row['funder']}", row["name"]
        elif row["funder"] == "kdps" and not row["name"]:
            key, label = "manual", MANUAL_LABEL
        elif not row["name"]:
            # A whole line whose offers could not be read, with no offer named.
            key, label = "unread", UNREAD_LABEL
        else:
            key, label = f"unlisted:{row['name']}", row["name"] or "An offer not on the books"
        rows.append(
            {
                "key": key,
                "label": label,
                "funder": FUNDER_LABELS.get(row["funder"], row["funder"]),
                **_figures(row),
            }
        )
    return rows


def _by_brand(facts: QuerySet[DiscountFundingFact]) -> list[dict[str, Any]]:
    """One row per brand in the brand list, under its name there; a line whose brand
    is not one brand in the list is grouped by what it says."""
    names = dict(
        Brand.objects.filter(
            pk__in=facts.exclude(brand_ref_id=None).values("brand_ref_id")
        ).values_list("pk", "name")
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in facts.values("brand_ref_id", "brand").annotate(**_sums()):
        ref = row["brand_ref_id"]
        if ref is not None and ref in names:
            key, label = f"brand:{ref}", names[ref]
        else:
            key = f"text:{normalise(row['brand'])}"
            label = row["brand"] or "(no brand on the line)"
        figures = _figures(row)
        if key not in grouped:
            grouped[key] = {"key": key, "label": label, **figures}
            continue
        # Two spellings of one brand ("U.S. Polo", "US POLO") are one row.
        for name, value in figures.items():
            grouped[key][name] += value
    return list(grouped.values())


def _by_store(
    facts: QuerySet[DiscountFundingFact], stores: Iterable[Store]
) -> list[dict[str, Any]]:
    named = {store.pk: store for store in stores}
    rows = []
    for row in facts.values("store_id").annotate(**_sums()):
        store = named[row["store_id"]]
        rows.append({"key": store.code, "label": f"{store.name} ({store.code})", **_figures(row)})
    return rows


def _note_unknown(facts: QuerySet[DiscountFundingFact], missing: Missing) -> None:
    reasons = facts.exclude(unknown_reason="").values_list("unknown_reason", flat=True).distinct()
    labels = dict(SaleLineFunding.Unknown.choices)
    for reason in sorted(reasons):
        text = labels.get(reason, reason)
        if reason == SaleLineFunding.Unknown.MODEL.value:
            text += " (Brands, Terms lists every brand whose model is unknown)"
        missing.add(
            f"UNKNOWN_{reason.upper()}",
            f"{text}: that discount's split is shown as unknown, never guessed.",
        )


def build(scope: ReportScope, group_by: str) -> dict[str, Any]:
    """The funding report for ``scope``, grouped one way."""
    missing = Missing()
    as_of = freshness(FRESHNESS_KEY, missing)
    note_scope(scope, "Discount funding split", missing)
    facts = _facts(scope)
    if group_by == "offer":
        rows = _by_offer(facts)
    elif group_by == "brand":
        rows = _by_brand(facts)
    else:
        rows = _by_store(facts, scope.stores)
    rows.sort(key=lambda row: (-row["discount_paise"], row["label"]))
    _note_unknown(facts, missing)
    total = {"key": "total", "label": "Total", **_figures(facts.aggregate(**_sums()))}
    if group_by == "offer":
        total["funder"] = ""
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
            "group_by": group_by,
            "groupings": [{"key": key, "label": label} for key, label in GROUPINGS.items()],
            "columns": [
                {"key": c.key, "label": c.label, "kind": c.kind} for c in columns(group_by)
            ],
            "rows": rows,
            "total": total,
        },
    )
