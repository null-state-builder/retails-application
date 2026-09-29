"""Reports, Brand Performance (store operations PRD ST-RPT-3; overall PRD R-FIN-023).

Per brand and store - or per brand over every store - how it sells, what it
earns and how old its stock is. Read from the reporting copies alone: the sales
copy (``reporting.sales_facts``), the inventory copy (``reporting.inventory_facts``,
ticket 43) and the margin share copy (``reporting.margin_facts``, ticket 27).
Brand terms (ticket 23) are master data, read to know each brand's model.

Formulas (version ``FORMULA_VERSION``; change the version if one changes):

* **Sold** and **sales** - pieces and value after discount with GST, net of
  pieces given back, over the chosen dates.
* **Discount %** = discount / MRP value, both net of returns (the sales report's).
* **Returns** - pieces and value given back; **returns %** = pieces given back /
  pieces sold before returns.
* **Sell-through %** = sold / received over the chosen dates, at stores on the
  goods records (ticket 43's rule).
* **Stock age** - from the latest stock snapshot on or before the last day: on
  hand, the average days a piece has been at the store, in-season dead stock
  (ticket 33's flag), pieces of an ended season, and **aged %** = (dead + ended
  season) / on hand.
* **Margin** (outright) = sales before GST - cost, for lines with a known cost.
* **Commission** (SOR and concession) = KDPS's share of each line, as the margin
  share statement recorded it when the bill reached head office.
* **Margin or commission %** = (margin + commission) / the sales before GST they
  were worked out on.
* **GMROI** = outright margin / average outright stock at cost over the chosen
  dates (ticket 43's rule), **outright brands only**.

Margin, commission, their % and GMROI wait on OQ-50 (R-FIN-023: the
profitability formula), so they are labelled estimates. Only a viewer
``sees_cost`` allows receives them, or a brand's model; a store role gets the
rest.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import F, Q, Sum

from masters.brand_terms_models import CommercialModel
from masters.models import Brand
from masters.store_feature_registry import BRAND_PERFORMANCE_REPORT
from offers.resolution import normalise
from reporting.base import (
    COST_FIELDS,
    Column,
    Missing,
    ReportScope,
    envelope,
    freshness,
    note_freshness,
    note_scope,
    percent,
    ratio,
    sees_cost,
)
from reporting.inventory_facts import KEY as INVENTORY_KEY
from reporting.inventory_report import (
    OUTRIGHT,
    UNKNOWN,
    Models,
    Seasons,
    model_text,
    period_snapshots,
    say_snapshot_days,
    stock_days,
)
from reporting.margin_facts import KEY as MARGIN_SHARE_KEY
from reporting.models import (
    InventoryReceiptFact,
    InventoryStockFact,
    MarginShareFact,
    SalesLineFact,
)
from reporting.sales_facts import KEY as SALES_KEY

REPORT = "brand_performance"
TITLE = "Brand performance report"
FORMULA_VERSION = "brand-performance-1"
FEATURE_KEY = BRAND_PERFORMANCE_REPORT

GROUPINGS: dict[str, str] = {
    "brand_store": "Brand and store",
    "brand": "Brand, every store",
}

#: Models whose sales the margin share statement splits into a commission.
SPLIT_MODELS = {CommercialModel.SOR.value, CommercialModel.CONCESSION.value}
CONSIGNMENT = CommercialModel.CONSIGNMENT.value

#: The figures that wait on OQ-50 (R-FIN-023), labelled estimates.
ESTIMATE_FIELDS = ("margin_paise", "commission_paise", "earned_pct", "gmroi")
#: Everything a store role must not receive: margin, commission, GMROI, cost and
#: the brand's model (brand terms are read by no store role, ticket 23's B83).
HIDDEN_FIELDS = (*COST_FIELDS, *ESTIMATE_FIELDS, "stock_cost_paise", "model")

ESTIMATE_NOTE = (
    "Margin, commission, their % and GMROI are estimates, not final figures: the "
    "profitability formula waits on OQ-50."
)
STOCK_FIGURES = "received, sell-through and stock age"

BASIS = [
    "Sold and sales = pieces and value after discount with GST, net of pieces given back, "
    "over the chosen dates, from the sales report's own copy of the bills.",
    "Discount % = discount / MRP value of the pieces, both net of returns, as the sales "
    "report works it out.",
    "Returns = pieces and value given back in the chosen dates. Returns % = pieces given "
    "back / pieces sold before returns.",
    "Sell-through % = sold / received over the chosen dates, at stores on the goods records "
    "only. Received = pieces put away good from a receipt, opening stock or a transfer in.",
    "Stock age is read from the stock snapshot the server takes every day, the last one on "
    "or before the last day chosen: on hand, the average days a piece has been at the store, "
    "dead stock (in-season pieces whose item has had no sale at the store for the ageing "
    "policy's days) and pieces of a season that has ended. Aged % = (dead + ended season) / "
    "on hand.",
    "Brands are matched by name, as the till names them on a bill: two spellings of one "
    "brand are one row.",
]
COST_BASIS = [
    ESTIMATE_NOTE,
    "Margin (outright brands) = sales before GST - cost, for lines whose cost is known.",
    "Commission (SOR and concession brands) = KDPS's share of each line, as the margin share "
    "statement worked it out from the terms in force when the bill reached head office, for "
    "brands whose model on the last day chosen is SOR or concession.",
    "Margin or commission % = (margin + commission) / the sales before GST they were worked "
    "out on.",
    "GMROI = outright margin / average outright stock at cost, both over the chosen dates, "
    "for outright brands only (the inventory report's rule). It is for the period, not a year.",
    "A brand's model for a season is the one approved and in force on the last day chosen. "
    "Consignment brands and brands with no approved terms show neither margin nor commission.",
]


# -- the sums ------------------------------------------------------------------------


@dataclass
class Acc:
    """One row's sums (pieces x100, money in paise)."""

    label: str = ""
    store_id: int | None = None
    sold_x100: int = 0
    #: Sold at a store on the goods records: what sell-through sets against received.
    goods_sold_x100: int = 0
    out_x100: int = 0
    back_x100: int = 0
    sales: int = 0
    back_value: int = 0
    gross: int = 0
    disc: int = 0
    received: int = 0
    stocked: bool = False
    on_hand: int = 0
    stock_cost: int = 0
    dead: int = 0
    ended: int = 0
    piece_days: int = 0
    #: A stock row of this row's snapshot recorded no age (taken before ticket 45).
    age_unknown: bool = False
    outright_sales: int = 0
    outright_cost: int = 0
    outright_seen: bool = False
    gmroi_margin: int = 0
    avg_outright_cost: Decimal = Decimal(0)
    gmroi_seen: bool = False
    commission: int = 0
    split_value: int = 0
    commission_seen: bool = False
    models: set[str] = field(default_factory=set)

    def row(self) -> dict[str, Any]:
        margin = self.outright_sales - self.outright_cost if self.outright_seen else None
        earned_on = (self.outright_sales if self.outright_seen else 0) + self.split_value
        earned = (margin or 0) + (self.commission if self.commission_seen else 0)
        stock_age = self.stocked and not self.age_unknown
        return {
            "sold": self.sold_x100 / 100,
            "sales_paise": self.sales,
            "disc_paise": self.disc,
            "discount_pct": percent(self.disc, self.gross),
            "returned": self.back_x100 / 100,
            "returns_paise": self.back_value,
            "returns_pct": percent(self.back_x100, self.out_x100),
            "received": self.received if self.stocked else None,
            "sell_through_pct": (
                percent(self.goods_sold_x100, self.received * 100)
                if self.stocked and self.received > 0
                else None
            ),
            "on_hand": self.on_hand if self.stocked else None,
            "avg_age_days": (
                int(ratio(self.piece_days, self.on_hand, places=0) or 0)
                if stock_age and self.on_hand
                else None
            ),
            "dead_pieces": self.dead if self.stocked else None,
            "ended_pieces": self.ended if stock_age else None,
            "aged_pct": (
                percent(self.dead + self.ended, self.on_hand)
                if stock_age and self.on_hand
                else None
            ),
            "stock_cost_paise": self.stock_cost if self.stocked else None,
            "margin_paise": margin,
            "commission_paise": self.commission if self.commission_seen else None,
            "earned_pct": (
                percent(earned, earned_on)
                if (self.outright_seen or self.commission_seen) and earned_on > 0
                else None
            ),
            "gmroi": (
                ratio(self.gmroi_margin, self.avg_outright_cost)
                if self.gmroi_seen and self.avg_outright_cost > 0
                else None
            ),
            "model": model_text(self.models),
        }


@dataclass
class Missed:
    """Sales left out of one figure or another, said once in the missing list."""

    uncosted_x100: int = 0
    unknown_model_x100: int = 0
    consignment_x100: int = 0
    #: Stores whose outright margin GMROI leaves out (no stock snapshot in the period).
    gmroi_left_out: set[int] = field(default_factory=set)
    #: Sales before GST of SOR and concession brands, and what the margin share
    #: copy holds for them, per (store, brand, season): the gap has no split recorded.
    split_sales: dict[tuple[int, str, str], int] = field(default_factory=lambda: defaultdict(int))
    split_recorded: dict[tuple[int, str, str], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    unsplit_paise: int = 0
    age_unknown: bool = False


class Rows:
    """The report's rows, keyed by brand (and store), and their total."""

    def __init__(self, group_by: str, labels: dict[str, str]) -> None:
        self.group_by = group_by
        self._labels = labels
        self.accs: dict[str, Acc] = {}
        self.total = Acc(label="Total")

    def of(self, store_id: int, brand: str) -> list[Acc]:
        """The accumulators a figure for ``brand`` at ``store_id`` adds to."""
        name = normalise(brand)
        key = f"{store_id}:{name}" if self.group_by == "brand_store" else name
        acc = self.accs.get(key)
        if acc is None:
            label = self._labels.get(name) or (brand.strip() if name else "(no brand)")
            acc = self.accs[key] = Acc(
                label=label, store_id=store_id if self.group_by == "brand_store" else None
            )
        return [acc, self.total]


def _brand_labels() -> dict[str, str]:
    """A brand in the brand list, by the name two spellings of it share; a name two
    brands share names neither."""
    found: dict[str, list[str]] = defaultdict(list)
    for name in Brand.objects.values_list("name", flat=True):
        found[normalise(name)].append(name)
    return {key: names[0] for key, names in found.items() if len(names) == 1}


def build(scope: ReportScope, group_by: str) -> dict[str, Any]:
    """The brand performance report for ``scope``, as the viewer may see it."""
    show_cost = sees_cost(scope.user)
    missing = Missing()
    as_of = freshness(SALES_KEY, missing)
    note_freshness(INVENTORY_KEY, "Stock", missing)
    if show_cost:
        note_freshness(MARGIN_SHARE_KEY, "Commission", missing)
    note_scope(scope, TITLE, missing)

    models = Models(scope.stores[0].tenant_id, scope.date_to)
    stock_day, goods = stock_days(scope, missing, STOCK_FIGURES)
    period_days = period_snapshots(scope, goods)
    rows = Rows(group_by, _brand_labels())
    missed = Missed()

    seasons = Seasons()
    _add_sales(scope, seasons, models, (goods, set(period_days)), rows, missed)
    if show_cost:
        _add_commission(scope, seasons, models, rows, missed)
    _add_received(scope, goods, rows)
    _add_stock(stock_day, models, rows, missed)
    _add_average_stock(period_days, models, rows)
    for acc in rows.accs.values():
        acc.stocked = acc.store_id in goods if group_by == "brand_store" else bool(goods)
    rows.total.stocked = bool(goods)

    stores = {store.pk: store for store in scope.stores}
    out = [{"key": key, **_names(acc, stores), **acc.row()} for key, acc in rows.accs.items()]
    if group_by == "brand_store":
        out.sort(key=lambda r: (r["label"].upper(), r["store"]))
    else:
        out.sort(key=lambda r: (-r["sales_paise"], r["label"].upper()))
    total = {"key": "total", "label": "Total", **rows.total.row(), "model": ""}
    if group_by == "brand_store":
        total["store"] = ""

    _say_missed(missed, show_cost, missing)
    if missed.age_unknown:
        missing.add(
            "AGE_NOT_RECORDED",
            "Some stock was counted in a snapshot taken before stock age was recorded, so its "
            "average age, ended-season pieces and aged % are not shown.",
        )
    if show_cost and goods and not period_days:
        missing.add(
            "NO_AVERAGE_STOCK",
            "No stock snapshot was taken in the chosen dates, so average stock at cost, "
            "and so GMROI, cannot be worked out.",
        )
    elif show_cost and goods:
        say_snapshot_days(scope, period_days, missing)
    if not show_cost:
        out = [_hide(row) for row in out]
        total = _hide(total)
    shown = columns(group_by, show_cost)

    return envelope(
        report=REPORT,
        title=TITLE,
        formula_version=FORMULA_VERSION,
        scope=scope,
        as_of=as_of,
        basis=[*BASIS, *(COST_BASIS if show_cost else [])],
        missing=missing,
        shows_cost=show_cost,
        extra={
            "group_by": group_by,
            "groupings": [{"key": k, "label": v} for k, v in GROUPINGS.items()],
            "estimate_fields": [c.key for c in shown if c.key in ESTIMATE_FIELDS],
            "estimate_note": ESTIMATE_NOTE if show_cost else None,
            "columns": [column_json(c) for c in shown],
            "rows": out,
            "total": total,
        },
    )


def _names(acc: Acc, stores: dict[int, Any]) -> dict[str, str]:
    if acc.store_id is None:
        return {"label": acc.label}
    store = stores.get(acc.store_id)
    return {"label": acc.label, "store": store.code if store else str(acc.store_id)}


def _hide(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in HIDDEN_FIELDS}


# -- adding up ------------------------------------------------------------------------


def _add_sales(
    scope: ReportScope,
    seasons: Seasons,
    models: Models,
    stores: tuple[set[int], set[int]],
    rows: Rows,
    missed: Missed,
) -> None:
    """``stores``: those on the goods records (sell-through), and those with a stock
    snapshot in the period (whose outright margin GMROI's average stock covers)."""
    goods, snapshotted = stores
    costed = Q(cost_paise__isnull=False)
    back = Q(pieces_x100__lt=0)
    net = F("value_paise") - F("gst_paise")
    sums = (
        SalesLineFact.objects.filter(
            store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
        )
        .values("store_id", "brand", "season")
        .annotate(
            sold=Sum("pieces_x100"),
            out=Sum("pieces_x100", filter=Q(pieces_x100__gt=0)),
            back=Sum("pieces_x100", filter=back),
            sales=Sum("value_paise"),
            back_value=Sum("value_paise", filter=back),
            gross=Sum("gross_paise"),
            disc=Sum("disc_paise"),
            net=Sum(net),
            net_costed=Sum(net, filter=costed),
            cost=Sum("cost_paise", filter=costed),
            uncosted=Sum("pieces_x100", filter=Q(cost_paise__isnull=True)),
        )
        .order_by()
    )
    for row in sums:
        store_id, sold = row["store_id"], int(row["sold"] or 0)
        model = models.of(row["brand"], seasons.code(row["season"]))
        net_costed, cost = int(row["net_costed"] or 0), int(row["cost"] or 0)
        _note_model(missed, model, row, gmroi_left_out=store_id not in snapshotted and sold != 0)
        for acc in rows.of(store_id, row["brand"]):
            acc.sold_x100 += sold
            if store_id in goods:
                acc.goods_sold_x100 += sold
            acc.out_x100 += int(row["out"] or 0)
            acc.back_x100 -= int(row["back"] or 0)
            acc.sales += int(row["sales"] or 0)
            acc.back_value -= int(row["back_value"] or 0)
            acc.gross += int(row["gross"] or 0)
            acc.disc += int(row["disc"] or 0)
            acc.models.add(model)
            if model != OUTRIGHT:
                continue
            acc.outright_sales += net_costed
            acc.outright_cost += cost
            acc.outright_seen = acc.outright_seen or bool(net_costed or cost)
            if store_id in snapshotted:
                acc.gmroi_margin += net_costed - cost


def _note_model(missed: Missed, model: str, row: dict[str, Any], *, gmroi_left_out: bool) -> None:
    """What one group of sold lines leaves out of margin, commission or GMROI."""
    sold = int(row["sold"] or 0)
    if model == UNKNOWN:
        missed.unknown_model_x100 += sold
    elif model == CONSIGNMENT:
        missed.consignment_x100 += sold
    elif model in SPLIT_MODELS:
        missed.split_sales[_split_key(row)] += int(row["net"] or 0)
    elif model == OUTRIGHT:
        missed.uncosted_x100 += int(row["uncosted"] or 0)
        if gmroi_left_out:
            missed.gmroi_left_out.add(row["store_id"])


def _split_key(row: dict[str, Any]) -> tuple[int, str, str]:
    """A store, brand and season: where sales of a split brand meet their splits."""
    return (row["store_id"], normalise(row["brand"]), row["season"])


def _add_commission(
    scope: ReportScope, seasons: Seasons, models: Models, rows: Rows, missed: Missed
) -> None:
    """KDPS's share of each split line, as the margin share copy holds it - only
    where the brand's model for the season is SOR or concession, so the lines are
    never also counted as margin, nor a commission shown for an unknown model."""
    split = Q(unknown_reason="")
    sums = (
        MarginShareFact.objects.filter(
            store_id__in=scope.store_ids, day__gte=scope.date_from, day__lte=scope.date_to
        )
        .values("store_id", "brand", "season")
        .annotate(
            kdps=Sum("kdps_paise", filter=split),
            split=Sum("value_paise", filter=split),
            unsplit=Sum("value_paise", filter=~split),
            recorded=Sum("value_paise"),
        )
        .order_by()
    )
    for row in sums:
        if models.of(row["brand"], seasons.code(row["season"])) not in SPLIT_MODELS:
            continue
        missed.split_recorded[_split_key(row)] += int(row["recorded"] or 0)
        missed.unsplit_paise += int(row["unsplit"] or 0)
        has_split = row["split"] is not None
        for acc in rows.of(row["store_id"], row["brand"]):
            acc.commission += int(row["kdps"] or 0)
            acc.split_value += int(row["split"] or 0)
            acc.commission_seen = acc.commission_seen or has_split


def _add_received(scope: ReportScope, goods: set[int], rows: Rows) -> None:
    sums = (
        InventoryReceiptFact.objects.filter(
            store_id__in=sorted(goods), day__gte=scope.date_from, day__lte=scope.date_to
        )
        .values("store_id", "brand")
        .annotate(pieces=Sum("pieces"))
        .order_by()
    )
    for row in sums:
        for acc in rows.of(row["store_id"], row["brand"]):
            acc.received += int(row["pieces"] or 0)


def _add_stock(stock_day: dict[int, date], models: Models, rows: Rows, missed: Missed) -> None:
    if not stock_day:
        return
    which = Q()
    for store_id, day in stock_day.items():
        which |= Q(store_id=store_id, day=day)
    facts = InventoryStockFact.objects.filter(which).values_list(
        "store_id",
        "brand",
        "season",
        "pieces",
        "cost_paise",
        "dead_pieces",
        "ended_pieces",
        "piece_days",
    )
    for store_id, brand, season, pieces, cost, dead, ended, piece_days in facts:
        model = models.of(brand, season)
        unknown = ended is None or piece_days is None
        missed.age_unknown = missed.age_unknown or (unknown and pieces > 0)
        for acc in rows.of(store_id, brand):
            acc.on_hand += int(pieces)
            acc.stock_cost += int(cost)
            acc.dead += int(dead)
            acc.ended += int(ended or 0)
            acc.piece_days += int(piece_days or 0)
            acc.models.add(model)
            acc.age_unknown = acc.age_unknown or (unknown and pieces > 0)


def _add_average_stock(period_days: dict[int, list[date]], models: Models, rows: Rows) -> None:
    """Outright stock at cost, averaged over each store's snapshot days in the period."""
    if not period_days:
        return
    which = Q()
    for store_id, days in period_days.items():
        which |= Q(store_id=store_id, day__in=days)
    sums = (
        InventoryStockFact.objects.filter(which)
        .values("store_id", "brand", "season")
        .annotate(cost=Sum("cost_paise"))
        .order_by()
    )
    for row in sums:
        if models.of(row["brand"], row["season"]) != OUTRIGHT:
            continue
        average = Decimal(int(row["cost"] or 0)) / len(period_days[row["store_id"]])
        for acc in rows.of(row["store_id"], row["brand"]):
            acc.avg_outright_cost += average
            acc.gmroi_seen = True
            acc.models.add(OUTRIGHT)


def _say_missed(missed: Missed, show_cost: bool, missing: Missing) -> None:
    if not show_cost:
        return
    if missed.unknown_model_x100:
        missing.add(
            "UNKNOWN_MODEL",
            f"{_pieces(missed.unknown_model_x100)} sold are of a brand with no approved terms "
            "for their season (or a brand or season not in the lists), so neither margin nor "
            "commission is worked out for them. Brand terms are recorded under Brands.",
        )
    if missed.consignment_x100:
        missing.add(
            "CONSIGNMENT",
            f"{_pieces(missed.consignment_x100)} sold are of consignment brands: no commission "
            "is recorded for them, so neither margin nor commission is shown.",
        )
    if missed.uncosted_x100:
        missing.add(
            "UNCOSTED",
            f"{_pieces(missed.uncosted_x100)} of outright brands sold have no cost yet, so "
            "margin and GMROI leave them out.",
        )
    if missed.gmroi_left_out:
        missing.add(
            "GMROI_LEFT_OUT",
            f"{len(missed.gmroi_left_out)} store(s) had no stock snapshot in the chosen dates, "
            "so their outright sales are left out of GMROI (its average stock cannot include "
            "them).",
        )
    no_split = sum(
        max(0, value - missed.split_recorded.get(pair, 0))
        for pair, value in missed.split_sales.items()
    )
    if no_split:
        missing.add(
            "NO_SPLIT",
            f"Rs {_rupees(no_split)} of SOR and concession sales (before GST) have no split "
            "recorded - the margin share switch was off at the store when the bill reached "
            "head office - so commission leaves them out.",
        )
    if missed.unsplit_paise:
        missing.add(
            "UNSPLIT",
            f"Rs {_rupees(missed.unsplit_paise)} of SOR and concession sales could not be split "
            "(the brand's model or margin is not recorded), so commission leaves them out. The "
            "margin share statement lists them.",
        )


def _pieces(x100: int) -> str:
    return f"{x100 / 100:g} piece(s)"


def _rupees(paise: int) -> str:
    return f"{Decimal(paise) / 100:,.2f}"


def columns(group_by: str, show_cost: bool) -> list[Column]:
    """The table's and the spreadsheet's columns, as this viewer may see them."""
    out = [Column("label", "Brand", "text")]
    if group_by == "brand_store":
        out.append(Column("store", "Store", "text"))
    if show_cost:
        out.append(Column("model", "Model", "text"))
    out += [
        Column("sold", "Sold"),
        Column("sales_paise", "Sales (Rs)", "money"),
        Column("discount_pct", "Discount %", "rate"),
        Column("returned", "Given back"),
        Column("returns_paise", "Given back (Rs)", "money"),
        Column("returns_pct", "Returns %", "rate"),
        Column("received", "Received"),
        Column("sell_through_pct", "Sell-through %", "rate"),
        Column("on_hand", "On hand"),
        Column("avg_age_days", "Average days in store"),
        Column("dead_pieces", "Dead stock"),
        Column("ended_pieces", "Ended season"),
        Column("aged_pct", "Aged % of on hand", "rate"),
    ]
    if show_cost:
        out += [
            Column("stock_cost_paise", "On hand at cost (Rs)", "money"),
            Column("margin_paise", "Margin, outright (Rs) (estimate)", "money"),
            Column("commission_paise", "Commission, SOR and concession (Rs) (estimate)", "money"),
            Column("earned_pct", "Margin or commission % (estimate)", "rate"),
            Column("gmroi", "GMROI, outright only (estimate)"),
        ]
    return out


def column_json(column: Column) -> dict[str, str]:
    return {"key": column.key, "label": column.label, "kind": column.kind}
