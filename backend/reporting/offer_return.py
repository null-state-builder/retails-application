"""Return on each offer: what it earned and what it cost (ticket 31, ST-OFR-1, §16).

Once an offer has run, it is judged on **the goods it covers**: the pieces of its
brand and item scope sold at its stores, whether or not a given bill took the
offer. The same goods are read in a **baseline period** the viewer states (by
default the same number of days just before the offer started), so the two
compare like for like: sales, pieces, discount given, the part this offer gave,
gross margin and the brand-funded part, and the change per day. Overall PRD
R-PRC-009: a figure with a missing input (no cost, no split) is left out and
said, never invented.

**Never slows a bill.** It reads only reporting copies: the sold lines of ticket
30 (``report_offer_sim_line_fact``, which also records each offer's part of a
line's discount) and the funding split of ticket 25
(``report_discount_funding_fact``). It takes no lock and writes nothing but the
audit entry of a spreadsheet export.

**Scope on the server.** Only the offer's stores in the viewer's scope where the
switch is on. Gross margin and the brand-funded part only for a viewer
``sees_cost`` allows (B54): store roles never receive them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.utils import timezone

from core.refusals import Refusal
from masters.models import Store
from masters.store_feature_registry import OFFER_RETURN
from masters.store_features import feature
from offers.models import Offer
from offers.resolution import CartLine, covers, normalise
from ptmapper.goods_workbook import Amount, workbook_bytes
from reporting.base import (
    Missing,
    ReportScope,
    freshness,
    paise_ratio,
    percent,
    ratio,
    record_export,
    sees_cost,
    viewer_stores,
)
from reporting.funding_facts import KEY as FUNDING_KEY
from reporting.models import DiscountFundingFact, OfferSimLineFact
from reporting.offer_sim_facts import KEY as FACTS_KEY
from reporting.offer_simulation import SimStores, sim_stores

FEATURE_KEY = OFFER_RETURN
FORMULA_VERSION = "offer-return/1"
REPORT = "offer-return"
TITLE = "Return on offer"

#: An offer has run once it went live, and keeps its return after it ends.
RAN = frozenset({Offer.Status.LIVE, Offer.Status.ENDED})

BASIS = [
    "An offer's return is read from the goods it covers: the pieces of its brand and item "
    "scope sold at its stores, whether or not a given bill took the offer. The same goods are "
    "read in the baseline, so the two compare like for like.",
    "While the offer ran = from its start to its end, or to today (today as far as the copy "
    "has its bills). Baseline = the period you choose; until you choose, the same number of "
    "days just before the offer started.",
    "Only pieces sold are read; returns and exchanges are not taken off. Sales = what "
    "customers paid, with GST. Discount given = every discount on those pieces, from any offer "
    "or keyed in; 'this offer gave' is its own part of it.",
    "Per-day figures divide by the days in each period, so periods of different lengths "
    "compare. Change = the offer's period against the baseline: per-day figures as a "
    "percentage, percentages as points.",
    "Customers, stock and season differ between two periods; the change is not all the "
    "offer's doing.",
]
COST_BASIS = [
    "Gross margin = sales without GST less the cost frozen on the line at billing, over pieces "
    "whose cost is known.",
    "Brand-funded part = the brand's share of the discount on those pieces, as the funding "
    "split recorded it when each bill reached head office. A part whose split is unknown, or "
    "that was never split, is left out and said.",
]


# -- the two periods ---------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    date_from: date
    date_to: date
    #: Did the viewer state it? Only the baseline can be left to the default.
    stated: bool = True

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "days": self.days,
            "stated": self.stated,
        }


def offer_period(starts_on: date, ends_on: date | None, today: date) -> Period:
    """From the offer's start to its end, or to today while it still runs."""
    end = today if ends_on is None else min(ends_on, today)
    return Period("offer", "While the offer ran", starts_on, max(end, starts_on))


def _longest() -> int:
    return int(settings.KDPS_REPORT_MAX_DAYS)


def _latest_days(period: Period) -> tuple[Period, bool]:
    """A run longer than a report may cover keeps its latest days, and says so."""
    longest = _longest()
    if period.days <= longest:
        return period, False
    return replace(period, date_from=period.date_to - timedelta(days=longest - 1)), True


def _day(params: Mapping[str, Any], key: str) -> date | None:
    raw = str(params.get(key) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise Refusal("INVALID_REQUEST", f"{key} must be a date, YYYY-MM-DD.", status=400) from None


def baseline_period(
    params: Mapping[str, Any], ran: Period, today: date, before: date | None = None
) -> Period:
    """The period the viewer states, or as many days as ``ran`` just before the
    offer started (``before``, its first day; by default ``ran``'s own)."""
    start, end = _day(params, "baseline_from"), _day(params, "baseline_to")
    if start is None and end is None:
        end = (before or ran.date_from) - timedelta(days=1)
        return Period("baseline", "Baseline", end - timedelta(days=ran.days - 1), end, False)
    if start is None or end is None:
        raise Refusal("INVALID_REQUEST", "Give both the baseline's first and last day.", status=400)
    if end < start:
        raise Refusal("INVALID_REQUEST", "The baseline ends before it starts.", status=400)
    if end > today:
        raise Refusal("INVALID_REQUEST", "The baseline cannot end after today.", status=400)
    period = Period("baseline", "Baseline", start, end)
    if period.days > _longest():
        raise Refusal(
            "PERIOD_TOO_LONG",
            f"A baseline covers at most {_longest()} days. Choose a shorter one.",
            status=400,
        )
    return period


# -- the goods and their figures ------------------------------------------------------


@dataclass
class Tally:
    """One period's sums over the goods the offer covers, all in whole paise."""

    bills: set[int] = field(default_factory=set)
    lines: set[int] = field(default_factory=set)
    pieces: int = 0
    mrp_paise: int = 0
    sales_paise: int = 0
    discount_paise: int = 0
    offer_pieces: int = 0
    offer_discount_paise: int = 0
    unread_lines: int = 0
    costed_value_paise: int = 0
    cost_paise: int = 0
    uncosted_pieces: int = 0
    # The funding split of these lines' discount (ticket 25), sold lines only.
    split_paise: int = 0
    brand_paise: int = 0
    unknown_paise: int = 0


#: Line ids asked of the funding copy at a time.
FUNDING_CHUNK = 5000

LINE_FIELDS = (
    "bill_id",
    "line_id",
    "brand",
    "item",
    "design",
    "size",
    "color",
    "barcode",
    "season",
    "qty",
    "mrp_paise",
    "disc_paise",
    "net_paise",
    "gst_paise",
    "cost_paise",
    "no_discount",
    "offer_parts",
    "offer_parts_unread",
)


def _lines(offer: Offer, store_ids: list[int], period: Period) -> Any:
    rows = OfferSimLineFact.objects.filter(
        store_id__in=store_ids, day__range=(period.date_from, period.date_to)
    )
    if offer.brand is not None:
        # A brand offer covers only its own brand's pieces, and gives only to them.
        rows = rows.filter(brand_key=normalise(offer.brand.name))
    return rows


def _check_size(offer: Offer, store_ids: list[int], periods: list[Period]) -> None:
    lines = sum(_lines(offer, store_ids, period).count() for period in periods)
    most = int(settings.KDPS_OFFER_RETURN_MAX_LINES)
    if lines > most:
        raise Refusal(
            "PERIOD_TOO_LARGE",
            f"The offer's run and the baseline hold {lines:,} sold lines at these stores, more "
            f"than the {most:,} one reading goes through. Choose a shorter baseline, or ask "
            "Admin to raise the limit.",
            status=400,
        )


def tally(offer: Offer, store_ids: list[int], period: Period) -> Tally:
    """The goods the offer covers, sold at these stores in ``period``."""
    # The rule, open on every day: the question is which goods it is about, not
    # whether it was running on the day.
    rule = replace(offer.as_rule(), starts_on=date.min, ends_on=None)
    mine = str(offer.pk)
    found = Tally()
    rows = _lines(offer, store_ids, period).order_by().values(*LINE_FIELDS)
    for row in rows.iterator(chunk_size=5000):
        parts = row["offer_parts"] or {}
        piece = CartLine(
            line_no=0,
            brand=row["brand"],
            item=row["item"],
            design=row["design"],
            size=row["size"],
            color=row["color"],
            barcode=row["barcode"],
            season=row["season"],
            qty=row["qty"],
            mrp_paise=int(row["mrp_paise"]),
            no_discount=row["no_discount"],
        )
        # A line the offer gave to is always its own, whatever its scope says now.
        if mine not in parts and not covers(rule, piece, date.min):
            continue
        _add(found, row, parts.get(mine))
    _add_funding(found, store_ids, period)
    return found


def _add(found: Tally, row: dict[str, Any], given: Any) -> None:
    qty = int(row["qty"])
    value = int(row["net_paise"])
    found.bills.add(int(row["bill_id"]))
    found.lines.add(int(row["line_id"]))
    found.pieces += qty
    found.mrp_paise += int(row["mrp_paise"]) * qty
    found.sales_paise += value
    found.discount_paise += int(row["disc_paise"])
    if row["offer_parts_unread"]:
        found.unread_lines += 1
    if given is not None:
        found.offer_pieces += qty
        found.offer_discount_paise += int(given)
    if row["cost_paise"] is None:
        found.uncosted_pieces += qty
        return
    found.costed_value_paise += value - int(row["gst_paise"])
    found.cost_paise += int(row["cost_paise"])


def _add_funding(found: Tally, store_ids: list[int], period: Period) -> None:
    """The recorded split of these lines' discount. Returns are not taken off,
    matching the sold lines they are compared with."""
    if not found.lines:
        return
    base = DiscountFundingFact.objects.filter(
        store_id__in=store_ids, day__range=(period.date_from, period.date_to), sign=1
    )
    wanted = sorted(found.lines)
    rows = (
        row
        for start in range(0, len(wanted), FUNDING_CHUNK)
        for row in base.filter(line_id__in=wanted[start : start + FUNDING_CHUNK]).values_list(
            "discount_paise", "brand_paise"
        )
    )
    for discount, brand in rows:
        found.split_paise += int(discount)
        if brand is None:
            found.unknown_paise += int(discount)
        else:
            found.brand_paise += int(brand)


def _margin(found: Tally) -> int | None:
    costed = found.pieces - found.uncosted_pieces
    return found.costed_value_paise - found.cost_paise if costed else None


def row(period: Period, found: Tally, show_cost: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        **period.as_dict(),
        "bills": len(found.bills),
        "pieces": found.pieces,
        "mrp_paise": found.mrp_paise,
        "sales_paise": found.sales_paise,
        "discount_paise": found.discount_paise,
        "discount_pct": percent(found.discount_paise, found.mrp_paise),
        "offer_pieces": found.offer_pieces,
        "offer_discount_paise": found.offer_discount_paise,
        "sales_per_day_paise": paise_ratio(found.sales_paise, period.days),
        "pieces_per_day": ratio(found.pieces, period.days),
    }
    if show_cost:
        margin = _margin(found)
        out.update(
            {
                # Nothing recorded against a discount is unknown, not nought.
                "brand_funded_paise": (
                    None if found.discount_paise and not found.split_paise else found.brand_paise
                ),
                "margin_paise": margin,
                "margin_pct": (
                    None if margin is None else percent(margin, found.costed_value_paise)
                ),
                "margin_per_day_paise": (
                    None if margin is None else paise_ratio(margin, period.days)
                ),
                "uncosted_pieces": found.uncosted_pieces,
            }
        )
    return out


#: Every figure a period row carries, and those only a cost viewer is sent.
FIGURE_KEYS = (
    "bills",
    "pieces",
    "mrp_paise",
    "sales_paise",
    "discount_paise",
    "discount_pct",
    "offer_pieces",
    "offer_discount_paise",
    "sales_per_day_paise",
    "pieces_per_day",
)
COST_KEYS = (
    "brand_funded_paise",
    "margin_paise",
    "margin_pct",
    "margin_per_day_paise",
    "uncosted_pieces",
)


def unknown_row(period: Period, show_cost: bool) -> dict[str, Any]:
    """A period whose figures cannot be read yet: every one None."""
    keys = FIGURE_KEYS + COST_KEYS if show_cost else FIGURE_KEYS
    return {**period.as_dict(), **dict.fromkeys(keys)}


def change_keys(show_cost: bool) -> tuple[str, ...]:
    keys = ("sales_per_day_paise", "pieces_per_day", "discount_pct")
    return keys + ("margin_per_day_paise", "margin_pct") if show_cost else keys


def _per_day_change(now: int | None, days: int, then: int | None, then_days: int) -> float | None:
    """How much the daily rate moved, as a percentage of the baseline's; None when
    either is unknown or the baseline's is nought."""
    if now is None or then is None or not then:
        return None
    rate = Decimal(now) / days
    base = Decimal(then) / then_days
    exact = (rate - base) * 100 / abs(base)
    return float(exact.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _points(now: float | None, then: float | None) -> float | None:
    if now is None or then is None:
        return None
    exact = Decimal(str(now)) - Decimal(str(then))
    return float(exact.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def change(
    ran: Period, now: Tally, base: Period, then: Tally, rows: list[dict[str, Any]], show_cost: bool
) -> dict[str, float | None]:
    out = {
        "sales_per_day_paise": _per_day_change(
            now.sales_paise, ran.days, then.sales_paise, base.days
        ),
        "pieces_per_day": _per_day_change(now.pieces, ran.days, then.pieces, base.days),
        "discount_pct": _points(rows[0]["discount_pct"], rows[1]["discount_pct"]),
    }
    if show_cost:
        out["margin_per_day_paise"] = _per_day_change(
            _margin(now), ran.days, _margin(then), base.days
        )
        out["margin_pct"] = _points(rows[0]["margin_pct"], rows[1]["margin_pct"])
    return out


# -- what is missing ----------------------------------------------------------------------


def _note_stores(missing: Missing, found: SimStores) -> None:
    if found.switched_off:
        names = ", ".join(f"{s.name} ({s.code})" for s in found.switched_off)
        missing.add(
            "SWITCHED_OFF", f"Not included: {names}, where return on each offer is switched off."
        )
    if found.outside:
        missing.add(
            "OUTSIDE_SCOPE",
            f"{found.outside} of this offer's stores are outside your scope and not shown.",
        )


def _note_period(missing: Missing, period: Period, found: Tally, show_cost: bool) -> None:
    tag = period.key.upper()
    name = period.label if period.key == "baseline" else "While the offer ran"
    if not found.pieces:
        missing.add(
            f"NO_BILLS_{tag}",
            f"{name}: no sold pieces of these goods are in the copy for "
            f"{period.date_from.isoformat()} to {period.date_to.isoformat()}.",
        )
    if found.unread_lines:
        missing.add(
            f"PARTS_UNREAD_{tag}",
            f"{name}: {found.unread_lines} line(s) have an offer record that does not add up to "
            "their discount; the offer the bill names is given the whole of it.",
        )
    if not show_cost:
        return
    if found.uncosted_pieces:
        missing.add(
            f"UNCOSTED_{tag}",
            f"{name}: {found.uncosted_pieces} piece(s) have no cost yet; gross margin leaves "
            "them out.",
        )
    if found.unknown_paise:
        missing.add(
            f"FUNDING_UNKNOWN_{tag}",
            f"{name}: Rs {found.unknown_paise / 100:,.2f} of discount has a split that is "
            "unknown (no approved brand terms, or no share in them); the brand-funded part "
            "leaves it out.",
        )
    not_split = found.discount_paise - found.split_paise
    if not_split > 0:
        missing.add(
            f"FUNDING_NOT_SPLIT_{tag}",
            f"{name}: Rs {not_split / 100:,.2f} of discount was never split between the brand "
            "and KDPS (the discount funding split was off, or the bill came before it); the "
            "brand-funded part leaves it out.",
        )


def _note_copies(missing: Missing, show_cost: bool) -> Any:
    as_of = freshness(FACTS_KEY, missing)
    if show_cost:
        funding = Missing()
        freshness(FUNDING_KEY, funding)
        for item in funding.items:
            missing.add(f"FUNDING_{item['code']}", f"Brand-funded part: {item['text']}")
    return as_of


# -- the read ------------------------------------------------------------------------------


def _refuse(offer: Offer, found: SimStores, today: date) -> None:
    # ``approved_by``: an offer stopped before it was ever approved (a draft
    # ended) never ran, whatever its dates say.
    if offer.status not in RAN or offer.approved_by_id is None or offer.starts_on > today:
        raise Refusal(
            "NOT_RUN",
            "An offer shows its return once it has run; this one has not started yet.",
            status=409,
        )
    if not found.stores and not found.switched_off:
        raise Refusal(
            "SCOPE_DENIED",
            "None of this offer's stores is in your scope, so there are no bills to read.",
            status=403,
        )
    if not found.stores:
        raise Refusal(
            "FEATURE_OFF",
            f"{feature(FEATURE_KEY).name} is switched off at every store of this offer you "
            "can see. Admin can switch it on in Setup, Feature Switches.",
            status=403,
        )


def _store(store: Store) -> dict[str, Any]:
    return {"id": store.pk, "code": store.code, "name": store.name}


def read(user: Any, offer: Offer, params: Mapping[str, Any]) -> dict[str, Any]:
    """The offer's return over the viewer's own stores, against the baseline asked for."""
    today = timezone.localdate()
    found = sim_stores(user, offer, FEATURE_KEY)
    _refuse(offer, found, today)
    whole = offer_period(offer.starts_on, offer.ends_on, today)
    ran, cut = _latest_days(whole)
    base = baseline_period(params, ran, today, before=offer.starts_on)
    store_ids = [store.pk for store in found.stores]
    _check_size(offer, store_ids, [ran, base])

    show_cost = sees_cost(user, found.stores)
    missing = Missing()
    as_of = _note_copies(missing, show_cost)
    if as_of is None:
        # Nothing is in the copy yet (or it is being rebuilt whole): no figure
        # is shown rather than noughts that look real.
        rows = [unknown_row(ran, show_cost), unknown_row(base, show_cost)]
        moved = dict.fromkeys(change_keys(show_cost))
    else:
        now, then = tally(offer, store_ids, ran), tally(offer, store_ids, base)
        rows = [row(ran, now, show_cost), row(base, then, show_cost)]
        moved = change(ran, now, base, then, rows, show_cost)
        _note_period(missing, ran, now, show_cost)
        _note_period(missing, base, then, show_cost)
    if cut:
        missing.add(
            "OFFER_PERIOD_CUT",
            f"The offer has run longer than a report covers; its latest {ran.days} days are shown.",
        )
    if base.date_from <= whole.date_to and whole.date_from <= base.date_to:
        missing.add(
            "BASELINE_OVERLAPS",
            "The baseline overlaps the days the offer ran, so it is not a clean comparison.",
        )
    _note_stores(missing, found)
    return {
        "report": REPORT,
        "title": TITLE,
        "formula_version": FORMULA_VERSION,
        "offer": offer.pk,
        "offer_name": offer.name,
        "as_of": as_of,
        "stores": [_store(store) for store in found.stores],
        "basis": [*BASIS, *COST_BASIS] if show_cost else list(BASIS),
        "missing": missing.items,
        "shows_cost": show_cost,
        "periods": rows,
        "change": moved,
    }


# -- the spreadsheet (§17: every report exports to .xlsx) ------------------------------------

#: The rows of the sheet as the offer page draws them: key, label, kind, cost-only.
SHEET_ROWS: tuple[tuple[str, str, str, bool], ...] = (
    ("bills", "Bills", "number", False),
    ("pieces", "Pieces sold", "number", False),
    ("sales_paise", "Sales (what customers paid, with GST)", "money", False),
    ("discount_paise", "Discount given", "money", False),
    ("discount_pct", "Discount, % of MRP", "number", False),
    ("offer_discount_paise", "Of which this offer gave", "money", False),
    ("offer_pieces", "Pieces this offer discounted", "number", False),
    ("brand_funded_paise", "Brand-funded part", "money", True),
    ("margin_paise", "Gross margin", "money", True),
    ("margin_pct", "Gross margin, %", "number", True),
    ("sales_per_day_paise", "Sales per day", "money", False),
    ("pieces_per_day", "Pieces per day", "number", False),
    ("margin_per_day_paise", "Gross margin per day", "money", True),
)


def export(user: Any, offer: Offer, params: Mapping[str, Any], *, access: Any = None) -> bytes:
    """The return as one sheet, built from ``read`` so it carries exactly what this
    viewer may see. The export is recorded in the audit log, as every report's is."""
    body = read(user, offer, params)
    ran, base = body["periods"]
    stamp = body["as_of"]
    head: list[list[Any]] = [
        [f"{body['title']}: {offer.name}"],
        ["Stores", ", ".join(f"{s['name']} ({s['code']})" for s in body["stores"])],
        ["While the offer ran", ran["date_from"], ran["date_to"]],
        ["Baseline", base["date_from"], base["date_to"]],
        [
            "As of",
            timezone.localtime(stamp).strftime("%Y-%m-%d %H:%M") if stamp else "Not built yet",
        ],
        ["Formula version", body["formula_version"]],
    ]
    head += [["Basis", line] for line in body["basis"]]
    head += [["Missing data", item["text"]] for item in body["missing"]] or [
        ["Missing data", "None"]
    ]
    table: list[list[Any]] = [[TITLE, ran["label"], base["label"], "Change"]]
    for key, label, kind, cost in SHEET_ROWS:
        if cost and not body["shows_cost"]:
            continue
        moved = body["change"].get(key)
        table.append([label, _cell(ran.get(key), kind), _cell(base.get(key), kind), moved])
    content = workbook_bytes(TITLE, [*head, [], *table])
    shown = {s["id"] for s in body["stores"]}
    stores = [store for store in viewer_stores(user) if store.pk in shown]
    record_export(
        user, access=access,
        report=REPORT,
        scope=ReportScope(
            user=user,
            options=stores,
            stores=stores,
            date_from=date.fromisoformat(min(ran["date_from"], base["date_from"])),
            date_to=date.fromisoformat(max(ran["date_to"], base["date_to"])),
        ),
        detail={
            "offer": offer.pk,
            "offer_from": ran["date_from"],
            "offer_to": ran["date_to"],
            "baseline_from": base["date_from"],
            "baseline_to": base["date_to"],
        },
        contains_cost=body["shows_cost"],
    )
    return content


def _cell(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind != "money":
        return value
    # ``Amount`` holds exact non-negative paise; a loss is written as exact rupees.
    paise = int(value)
    return Amount(paise) if paise >= 0 else Decimal(paise) / 100
