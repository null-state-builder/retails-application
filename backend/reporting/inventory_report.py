"""Reports, Inventory (store operations PRD ST-RPT-2; overall PRD R-AN-006, R-AN-012).

Sell-through, weeks of cover, GMROI and dead stock, by store, brand, category
or season - read from the reporting copies alone (``reporting.inventory_facts``
and the sales copy, ``reporting.sales_facts``). Brand terms (ticket 23) are
master data, read to know each brand's model for a season.

Formulas (version ``FORMULA_VERSION``; change the version if one changes):

* **Sold** - pieces sold less pieces given back, from the sales copy.
* **Received** - pieces put away good at the store from a receipt, opening stock
  or a transfer in. Nothing given away (transfers out) is taken off.
* **Sell-through %** = sold / received, over the chosen dates, or - with the
  season span - over everything of each known season up to the last day.
  Worked out at stores on the goods records only (the others keep no receipts).
* **On hand** - good, accepted pieces at the store at the end of the last day,
  from that day's stock snapshot (or the latest before it, said so).
* **Weeks of cover** = on hand / average weekly pieces sold over the 4 weeks
  ending on the last day (sold in those 28 days / 4).
* **Dead stock** - in-season pieces whose item has had no sale at the store for
  the ageing policy's days (ticket 33's rule, 90 to start).
* **Margin %** (cost viewers only) = (sales before GST - cost) / sales before GST,
  over the chosen dates, for lines whose brand has a known model for their
  season and whose cost is known.
* **GMROI** (cost viewers only) = gross margin / average stock at cost, over the
  chosen dates, **outright brands only**. Average stock at cost is the mean of
  the daily snapshots in the period. Not annualised. A brand with a SOR,
  consignment or concession model shows margin % instead; a brand with an
  unknown model shows neither.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import F, Max, Min, Q, Sum

from masters.brand_terms import approved_terms, in_force
from masters.brand_terms_models import CommercialModel
from masters.models import Brand, Season
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
from reporting.inventory_facts import KEY as FRESHNESS_KEY
from reporting.models import (
    InventoryReceiptFact,
    InventorySnapshot,
    InventoryStockFact,
    SalesLineFact,
)
from reporting.sales_facts import KEY as SALES_KEY

REPORT = "inventory"
TITLE = "Inventory report"
FORMULA_VERSION = "inventory-1"
FEATURE_KEY = "inventory-report"

GROUPINGS: dict[str, str] = {
    "store": "Store",
    "brand": "Brand",
    "category": "Category",
    "season": "Season",
}
SPANS: dict[str, str] = {
    "period": "The chosen dates",
    "season": "The season to date",
}
#: Weeks of cover reads the average weekly sales over this many weeks.
COVER_WEEKS = 4

#: Everything a store role must not receive: cost, margin, GMROI and the brand's
#: commercial model (brand terms are read by no store role, ticket 23's B83).
HIDDEN_FIELDS = (*COST_FIELDS, "stock_cost_paise", "dead_cost_paise", "gmroi", "model")

MODEL_LABELS = {choice.value: str(choice.label) for choice in CommercialModel}
OUTRIGHT = CommercialModel.OUTRIGHT.value
UNKNOWN = "unknown"

BASIS = [
    "Sold = pieces sold less pieces given back, from the sales report's own copy of the bills.",
    "Received = pieces put away good at the store from a receipt, opening stock or a "
    "transfer in. Pieces sent away are not taken off. A customer's return put back on "
    "the shelf is not received: the sale it cancels is already netted out of sold.",
    "Sell-through % = sold / received, at stores on the goods records only.",
    "On hand = good, accepted pieces at the store at the end of the last day, from the "
    "stock snapshot the server takes every day.",
    f"Weeks of cover = on hand / average weekly pieces sold over the {COVER_WEEKS} weeks "
    "ending on the last day. Only stores with a stock snapshot count.",
    "Dead stock = in-season pieces whose item has had no sale at the store for the "
    "ageing policy's days (the same rule the Stock Ageing page flags). Stock of an "
    "ended season is aged, not dead, and is not counted here.",
    "Brand and category are matched by name, as the till names them on a bill.",
]
PERIOD_BASIS = "Sold and received are counted over the chosen dates."
SEASON_BASIS = (
    "Season to date: sold and received count every piece of a known season up to the "
    "last day chosen, so each season's sell-through is its own. Sell-through at a store "
    "counts its sales only from the first piece it received on the goods records."
)
COST_BASIS = [
    "Margin % = (sales before GST - cost) / sales before GST, over the chosen dates, for "
    "lines whose brand has approved terms for their season (ticket 23) and whose cost "
    "is known.",
    "GMROI = gross margin / average stock at cost, over the chosen dates, for outright "
    "brands only. Average stock at cost is the mean of the daily stock snapshots in the "
    "period. It is for the period, not a year. SOR, consignment and concession brands "
    "show margin % instead; a brand with no approved terms shows neither.",
    "A brand's model for a season is the one in force on the last day chosen.",
]


# -- the brand terms -----------------------------------------------------------------


class Models:
    """Each (brand, season)'s commercial model on one day, read once per request."""

    def __init__(self, tenant_id: Any, day: date) -> None:
        self._brands: dict[str, list[int]] = defaultdict(list)
        for pk, name in Brand.objects.values_list("pk", "name"):
            self._brands[normalise(name)].append(pk)
        self._seasons = {code: pk for pk, code in Season.objects.values_list("pk", "code")}
        by_pair: dict[tuple[int, int], list[Any]] = defaultdict(list)
        for terms in approved_terms(tenant_id):
            by_pair[(terms.brand_id, terms.season_id)].append(terms)
        self._models: dict[tuple[int, int], str] = {
            pair: str(found.model)
            for pair, versions in by_pair.items()
            if (found := in_force(versions, day)) is not None
        }

    def of(self, brand: str, season_code: str) -> str:
        """The model's value, or ``UNKNOWN``: no one brand by that name, no such
        season, or no approved terms in force (D9: nothing is assumed)."""
        brands = self._brands.get(normalise(brand), []) if normalise(brand) else []
        season = self._seasons.get(season_code)
        if len(brands) != 1 or season is None:
            return UNKNOWN
        return self._models.get((brands[0], season), UNKNOWN)


class Seasons:
    """Season codes, and which a bill line's season text names."""

    def __init__(self) -> None:
        rows = list(Season.objects.values_list("code", "name", "historical_unknown"))
        self.names = {code: name for code, name, _ in rows}
        self.known = {code for code, _name, unknown in rows if not unknown}
        by_name: dict[str, list[str]] = defaultdict(list)
        for code, name, _ in rows:
            by_name[name].append(code)
        self._by_name = {name: codes[0] for name, codes in by_name.items() if len(codes) == 1}

    def code(self, text: str) -> str:
        """The one season ``text`` names, by code or by name; "" when none or two."""
        if text in self.names:
            return text
        return self._by_name.get(text, "")


# -- the sums ------------------------------------------------------------------------


@dataclass(frozen=True)
class Sale:
    """One group of sold lines, as a row adds it up (pieces x100, money in paise)."""

    sold: int
    #: Sold at a store on the goods records, counted from when its records began.
    goods_sold: int
    #: The last weeks' pieces, for weeks of cover.
    recent: int
    at_goods_store: bool
    model: str
    sales: int
    cost: int
    #: False where the store has no stock snapshot in the period: GMROI's average
    #: stock cannot include it, so neither may its margin.
    in_gmroi: bool


@dataclass
class Acc:
    """One row's sums, added up from the copies' pieces."""

    sold_x100: int = 0
    goods_sold_x100: int = 0
    received: int = 0
    recent_x100: int = 0
    on_hand: int = 0
    stocked: bool = False
    stock_cost: int = 0
    dead: int = 0
    dead_cost: int = 0
    sales_known: int = 0
    cost_known: int = 0
    outright_margin: int = 0
    outright_seen: bool = False
    avg_outright_cost: Decimal = Decimal(0)
    models: set[str] = field(default_factory=set)

    def add_sale(self, sale: Sale) -> None:
        self.sold_x100 += sale.sold
        if sale.at_goods_store:
            self.goods_sold_x100 += sale.goods_sold
            self.recent_x100 += sale.recent
        self.models.add(sale.model)
        if sale.model != UNKNOWN:
            self.sales_known += sale.sales
            self.cost_known += sale.cost
        if sale.model == OUTRIGHT and sale.in_gmroi:
            self.outright_margin += sale.sales - sale.cost
            self.outright_seen = True

    def row(self) -> dict[str, Any]:
        weekly = Decimal(self.recent_x100) / 100 / COVER_WEEKS
        margin = self.sales_known - self.cost_known
        return {
            "sold": self.sold_x100 / 100,
            "received": self.received if self.stocked else None,
            "sell_through_pct": (
                percent(self.goods_sold_x100, self.received * 100)
                if self.stocked and self.received > 0
                else None
            ),
            "on_hand": self.on_hand if self.stocked else None,
            "weekly_sales": ratio(weekly, 1, places=1) if self.stocked else None,
            "weeks_cover": ratio(self.on_hand, weekly, 1) if self.stocked and weekly > 0 else None,
            "dead_pieces": self.dead if self.stocked else None,
            "dead_pct": percent(self.dead, self.on_hand) if self.stocked and self.on_hand else None,
            "stock_cost_paise": self.stock_cost if self.stocked else None,
            "dead_cost_paise": self.dead_cost if self.stocked else None,
            "margin_paise": margin if self.sales_known or self.cost_known else None,
            "margin_pct": percent(margin, self.sales_known) if self.sales_known > 0 else None,
            "gmroi": (
                ratio(self.outright_margin, self.avg_outright_cost)
                if self.outright_seen and self.avg_outright_cost > 0
                else None
            ),
            "model": model_text(self.models),
        }


def model_text(models: set[str]) -> str:
    """The models a row mixes, in words, "Unknown" last."""
    if not models:
        return ""
    return ", ".join(
        "Unknown" if model == UNKNOWN else MODEL_LABELS.get(model, model)
        for model in sorted(models, key=lambda m: (m == UNKNOWN, m))
    )


@dataclass
class Missed:
    """Pieces left out of one figure or another, said once in the missing list."""

    uncosted_x100: int = 0
    unknown_model_x100: int = 0
    unknown_season_x100: int = 0
    unknown_season_received: int = 0
    #: Stores whose outright margin GMROI leaves out (no stock snapshot in the period).
    gmroi_left_out: set[int] = field(default_factory=set)


def build(scope: ReportScope, group_by: str, span: str = "period") -> dict[str, Any]:
    """The inventory report for ``scope``, grouped by ``group_by``, as the viewer may see it."""
    show_cost = sees_cost(scope.user, scope.stores)
    missing = Missing()
    as_of = freshness(FRESHNESS_KEY, missing)
    note_freshness(SALES_KEY, "Sales", missing)
    note_scope(scope, TITLE, missing)

    seasons = Seasons()
    models = Models(scope.stores[0].tenant_id, scope.date_to)
    stock_day, goods = stock_days(scope, missing)
    period_days = period_snapshots(scope, goods)
    accs: dict[str, Acc] = defaultdict(Acc)
    total = Acc()
    missed = Missed()

    def both(key: str) -> list[Acc]:
        return [accs[key], total]

    _add_sales(scope, group_by, span, seasons, models, (goods, set(period_days)), both, missed)
    _add_received(scope, group_by, span, seasons, goods, both, missed)
    _add_stock(scope, group_by, stock_day, models, both)
    _add_average_stock(scope, group_by, period_days, models, both)
    keys: list[str] = list(accs)
    if group_by == "store":
        keys = [str(store.pk) for store in sorted(scope.stores, key=lambda s: s.code)]
    # Stock figures are known for a row only where it covers a store on the goods
    # records; there, nothing on hand is a known nought.
    for key in keys:
        accs[key].stocked = int(key) in goods if group_by == "store" else bool(goods)
    total.stocked = bool(goods)
    stores = {str(store.pk): store for store in scope.stores}
    rows = [
        {"key": key, "label": _label(group_by, key, stores, seasons), **accs[key].row()}
        for key in keys
    ]
    if group_by != "store":
        rows.sort(key=lambda r: (-(r["sold"] or 0), -(r["on_hand"] or 0), r["label"]))
    total_row = {"key": "total", "label": "Total", **total.row(), "model": ""}

    _say_missed(missed, show_cost, span, missing)
    if show_cost and goods and not period_days:
        missing.add(
            "NO_AVERAGE_STOCK",
            "No stock snapshot was taken in the chosen dates, so average stock at cost, "
            "and so GMROI, cannot be worked out.",
        )
    elif show_cost and goods:
        say_snapshot_days(scope, period_days, missing)
    if group_by != "brand":
        for row in [*rows, total_row]:
            row.pop("model", None)
    total_row.pop("model", None)
    if not show_cost:
        rows = [_hide(row) for row in rows]
        total_row = _hide(total_row)

    return envelope(
        report=REPORT,
        title=TITLE,
        formula_version=FORMULA_VERSION,
        scope=scope,
        as_of=as_of,
        basis=[
            *BASIS,
            SEASON_BASIS if span == "season" else PERIOD_BASIS,
            *(COST_BASIS if show_cost else []),
        ],
        missing=missing,
        shows_cost=show_cost,
        extra={
            "group_by": group_by,
            "span": span,
            "groupings": [{"key": k, "label": v} for k, v in GROUPINGS.items()],
            "spans": [{"key": k, "label": v} for k, v in SPANS.items()],
            "columns": [column_json(c) for c in columns(group_by, show_cost)],
            "rows": rows,
            "total": total_row,
        },
    )


def _hide(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in HIDDEN_FIELDS}


# -- which stock days -----------------------------------------------------------------


#: The figures a store with no usable stock snapshot goes without, in plain words.
STOCK_FIGURES = "received, sell-through, on hand, cover and dead stock"


def stock_days(
    scope: ReportScope, missing: Missing, figures: str = STOCK_FIGURES
) -> tuple[dict[int, date], set[int]]:
    """Each store's latest stock snapshot on or before the last day, and the stores
    on the goods records by it. Stores without one are said, never guessed;
    ``figures`` names what they go without (ticket 45 shows other figures)."""
    latest = {
        row["store_id"]: row["day"]
        for row in InventorySnapshot.objects.filter(
            store_id__in=scope.store_ids, day__lte=scope.date_to
        )
        .values("store_id")
        .annotate(day=Max("day"))
        .order_by()
    }
    records = {
        (store_id, day): (goods, idle)
        for store_id, day, goods, idle in InventorySnapshot.objects.filter(
            store_id__in=scope.store_ids, day__lte=scope.date_to
        )
        .filter(day__in=set(latest.values()))
        .values_list("store_id", "day", "goods_records", "idle_days")
    }
    goods: set[int] = set()
    none, legacy, earlier = [], [], []
    for store in sorted(scope.stores, key=lambda s: s.code):
        day = latest.get(store.pk)
        if day is None:
            none.append(store)
        elif not records[(store.pk, day)][0]:
            legacy.append(store)
        else:
            goods.add(store.pk)
            if day < scope.date_to:
                earlier.append(f"{store.name} ({store.code}) on {day:%d %b %Y}")
    if none:
        missing.add(
            "NO_SNAPSHOT",
            f"No stock snapshot on or before {scope.date_to:%d %b %Y} for {_names(none)}, so "
            f"{figures} are not shown there.",
        )
    if legacy:
        missing.add(
            "NOT_GOODS",
            f"{_names(legacy)} is not on the goods records, so {figures} are not shown there.",
        )
    if earlier:
        missing.add(
            "EARLIER_SNAPSHOT",
            "On hand is from the last day stock was recorded: " + "; ".join(earlier) + ".",
        )
    if any(records[(pk, latest[pk])][1] is None for pk in goods):
        missing.add(
            "NO_AGEING_POLICY",
            "The ageing policy sets no days with no sale, so no stock is counted as dead.",
        )
    return {pk: latest[pk] for pk in goods}, goods


def period_snapshots(scope: ReportScope, goods: set[int]) -> dict[int, list[date]]:
    """Each goods store's snapshot days inside the chosen dates."""
    out: dict[int, list[date]] = defaultdict(list)
    for store_id, day in InventorySnapshot.objects.filter(
        store_id__in=sorted(goods),
        day__gte=scope.date_from,
        day__lte=scope.date_to,
        goods_records=True,
    ).values_list("store_id", "day"):
        out[store_id].append(day)
    return dict(out)


def say_snapshot_days(
    scope: ReportScope, period_days: dict[int, list[date]], missing: Missing
) -> None:
    wanted = (scope.date_to - scope.date_from).days + 1
    short = [
        f"{store.name} ({store.code}) {len(period_days.get(store.pk, []))} of {wanted}"
        for store in sorted(scope.stores, key=lambda s: s.code)
        if store.pk in period_days and len(period_days[store.pk]) < wanted
    ]
    if short:
        missing.add(
            "SNAPSHOT_DAYS",
            "Average stock at cost uses only the days a stock snapshot was taken: "
            + "; ".join(short)
            + " days.",
        )


# -- adding up ------------------------------------------------------------------------

_FIELD = {"store": "store_id", "brand": "brand", "category": "category", "season": "season"}


def _key(group_by: str, value: Any, seasons: Seasons | None = None) -> str:
    if group_by == "season" and seasons is not None:
        return seasons.code(str(value or ""))
    return "" if value is None else str(value)


def _add_sales(
    scope: ReportScope,
    group_by: str,
    span: str,
    seasons: Seasons,
    models: Models,
    stores: tuple[set[int], set[int]],
    both: Any,
    missed: Missed,
) -> None:
    """``stores``: those on the goods records, and those with a snapshot in the period."""
    goods, snapshotted = stores
    recent_from = scope.date_to - timedelta(days=7 * COVER_WEEKS - 1)
    in_period = Q(day__gte=scope.date_from)
    facts = SalesLineFact.objects.filter(store_id__in=scope.store_ids, day__lte=scope.date_to)
    if span == "period":
        facts = facts.filter(day__gte=min(scope.date_from, recent_from))
    costed = in_period & Q(cost_paise__isnull=False)
    sums = facts.values(*dict.fromkeys([_FIELD[group_by], "store_id", "brand", "season"])).annotate(
        sold=Sum("pieces_x100", filter=in_period),
        ever=Sum("pieces_x100"),
        since_records=Sum("pieces_x100", filter=_since_records(goods)),
        recent=Sum("pieces_x100", filter=Q(day__gte=recent_from)),
        uncosted=Sum("pieces_x100", filter=in_period & Q(cost_paise__isnull=True)),
        sales=Sum(F("value_paise") - F("gst_paise"), filter=costed),
        cost=Sum("cost_paise", filter=costed),
    )
    for row in sums.order_by():
        season = seasons.code(row["season"])
        sold, goods_sold = int(row["sold"] or 0), int(row["sold"] or 0)
        if span == "season":
            sold, goods_sold = int(row["ever"] or 0), int(row["since_records"] or 0)
            if season not in seasons.known:
                missed.unknown_season_x100 += sold
                sold = goods_sold = 0
        sale = Sale(
            sold=sold,
            goods_sold=goods_sold,
            recent=int(row["recent"] or 0),
            at_goods_store=row["store_id"] in goods,
            model=models.of(row["brand"], season),
            sales=int(row["sales"] or 0),
            cost=int(row["cost"] or 0),
            in_gmroi=row["store_id"] in snapshotted,
        )
        missed.uncosted_x100 += int(row["uncosted"] or 0)
        if row["sold"] and sale.model == UNKNOWN:
            missed.unknown_model_x100 += int(row["sold"] or 0)
        if row["sold"] and sale.model == OUTRIGHT and not sale.in_gmroi:
            missed.gmroi_left_out.add(row["store_id"])
        for acc in both(_key(group_by, row[_FIELD[group_by]], seasons)):
            acc.add_sale(sale)


def _since_records(goods: set[int]) -> Q:
    """Days at each goods store from its first piece received: the season span's sold
    is set against received, which a store has only from then."""
    first = (
        InventoryReceiptFact.objects.filter(store_id__in=sorted(goods))
        .values("store_id")
        .annotate(first=Min("day"))
        .order_by()
    )
    window = Q(pk__in=[])
    for row in first:
        window |= Q(store_id=row["store_id"], day__gte=row["first"])
    return window


def _add_received(
    scope: ReportScope,
    group_by: str,
    span: str,
    seasons: Seasons,
    goods: set[int],
    both: Any,
    missed: Missed,
) -> None:
    facts = InventoryReceiptFact.objects.filter(store_id__in=sorted(goods), day__lte=scope.date_to)
    if span == "period":
        facts = facts.filter(day__gte=scope.date_from)
    fields = dict.fromkeys([_FIELD[group_by], "season"])
    for row in facts.values(*fields).annotate(pieces=Sum("pieces")).order_by():
        pieces = int(row["pieces"] or 0)
        if span == "season" and row["season"] not in seasons.known:
            missed.unknown_season_received += pieces
            continue
        for acc in both(_key(group_by, row[_FIELD[group_by]])):
            acc.received += pieces


def _add_stock(
    scope: ReportScope, group_by: str, stock_day: dict[int, date], models: Models, both: Any
) -> None:
    if not stock_day:
        return
    which = Q()
    for store_id, day in stock_day.items():
        which |= Q(store_id=store_id, day=day)
    rows = (
        InventoryStockFact.objects.filter(which)
        .values(*dict.fromkeys([_FIELD[group_by], "brand", "season"]))
        .annotate(
            pieces=Sum("pieces"),
            cost=Sum("cost_paise"),
            dead=Sum("dead_pieces"),
            dead_cost=Sum("dead_cost_paise"),
        )
        .order_by()
    )
    for row in rows:
        model = models.of(row["brand"], row["season"])
        for acc in both(_key(group_by, row[_FIELD[group_by]])):
            acc.on_hand += int(row["pieces"] or 0)
            acc.stock_cost += int(row["cost"] or 0)
            acc.dead += int(row["dead"] or 0)
            acc.dead_cost += int(row["dead_cost"] or 0)
            acc.models.add(model)


def _add_average_stock(
    scope: ReportScope,
    group_by: str,
    period_days: dict[int, list[date]],
    models: Models,
    both: Any,
) -> None:
    """Outright stock at cost, averaged over each store's snapshot days in the period."""
    if not period_days:
        return
    which = Q()
    for store_id, days in period_days.items():
        which |= Q(store_id=store_id, day__in=days)
    fields = dict.fromkeys([_FIELD[group_by], "store_id", "brand", "season"])
    rows = InventoryStockFact.objects.filter(which).values(*fields).annotate(cost=Sum("cost_paise"))
    for row in rows.order_by():
        if models.of(row["brand"], row["season"]) != OUTRIGHT:
            continue
        average = Decimal(int(row["cost"] or 0)) / len(period_days[row["store_id"]])
        for acc in both(_key(group_by, row[_FIELD[group_by]])):
            acc.avg_outright_cost += average
            acc.outright_seen = True
            acc.models.add(OUTRIGHT)


def _say_missed(missed: Missed, show_cost: bool, span: str, missing: Missing) -> None:
    if span == "season" and missed.unknown_season_x100:
        missing.add(
            "UNKNOWN_SEASON_SOLD",
            f"{_pieces(missed.unknown_season_x100)} sold with no known season are left out "
            "of the season-to-date figures.",
        )
    if span == "season" and missed.unknown_season_received:
        missing.add(
            "UNKNOWN_SEASON_RECEIVED",
            f"{missed.unknown_season_received} piece(s) received with no known season are "
            "left out of the season-to-date figures.",
        )
    if not show_cost:
        return
    if missed.uncosted_x100:
        missing.add(
            "UNCOSTED",
            f"{_pieces(missed.uncosted_x100)} sold have no cost yet, so margin and GMROI "
            "leave them out.",
        )
    if missed.gmroi_left_out:
        missing.add(
            "GMROI_LEFT_OUT",
            f"{len(missed.gmroi_left_out)} store(s) had no stock snapshot in the chosen "
            "dates, so their outright sales are left out of GMROI (its average stock "
            "cannot include them).",
        )
    if missed.unknown_model_x100:
        missing.add(
            "UNKNOWN_MODEL",
            f"{_pieces(missed.unknown_model_x100)} sold are of a brand with no approved terms "
            "for their season (or a brand or season not in the lists), so margin % and GMROI "
            "leave them out. Brand terms are recorded under Brands.",
        )


def _pieces(x100: int) -> str:
    whole = x100 / 100
    return f"{whole:g} piece(s)"


def _names(stores: list[Any]) -> str:
    return ", ".join(f"{s.name} ({s.code})" for s in stores)


def _label(group_by: str, key: str, stores: dict[str, Any], seasons: Seasons) -> str:
    if group_by == "store":
        store = stores.get(key)
        return f"{store.name} ({store.code})" if store else key
    if group_by == "season":
        return f"{seasons.names[key]} ({key})" if key in seasons.names else "(no season)"
    return key or f"(no {group_by})"


def columns(group_by: str, show_cost: bool) -> list[Column]:
    """The table's and the spreadsheet's columns, as this viewer may see them."""
    out = [Column("label", GROUPINGS[group_by], "text")]
    if show_cost and group_by == "brand":
        out.append(Column("model", "Model", "text"))
    out += [
        Column("sold", "Sold"),
        Column("received", "Received"),
        Column("sell_through_pct", "Sell-through %", "rate"),
        Column("on_hand", "On hand"),
        Column("weekly_sales", f"Sold per week (last {COVER_WEEKS} weeks)"),
        Column("weeks_cover", "Weeks of cover"),
        Column("dead_pieces", "Dead stock"),
        Column("dead_pct", "Dead % of on hand", "rate"),
    ]
    if show_cost:
        out += [
            Column("stock_cost_paise", "On hand at cost (Rs)", "money"),
            Column("dead_cost_paise", "Dead stock at cost (Rs)", "money"),
            Column("margin_paise", "Gross margin (Rs)", "money"),
            Column("margin_pct", "Margin %", "rate"),
            Column("gmroi", "GMROI (outright)"),
        ]
    return out


def column_json(column: Column) -> dict[str, str]:
    return {"key": column.key, "label": column.label, "kind": column.kind}
