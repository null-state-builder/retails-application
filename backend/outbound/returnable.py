"""What a brand will actually take back, and how much room is left to send it.

Two questions, one module, because the screen asks them together (#75): *which
pieces may go back to this brand* (the **returnable pool**) and *how much of the
negotiated allowance this return would use* (the **cap**). Both are answered
here, server-side, from the books — never from the payload — so a scan that is
not in the pool is refused by the same rule the screen drew, and the cap the
approver sees is the cap the posting engine enforced.

The pool has exactly two sources, and they are different in kind:

* **quarantine** — confirmed-damaged pieces sitting in ``QuarantineStock``. They
  are in the bucket *because* a warehouse or HO person confirmed the damage
  (#138): a store's unconfirmed flag leaves the piece on the shelf and out of
  this bucket, so "confirmed only" is a property of where the pool reads from
  rather than a filter anyone has to remember. A defect is a defect whatever the
  calendar says, so these carry no window.
* **season_end** — unsold free-to-sell stock going back at season end. This one
  *is* window-bound: past the deadline the brand will not take it, so an
  out-of-window piece is not offered.

**Outright stock never appears** (#75). KDPS bought it outright; nothing goes
back. The exclusion is at the source — an Outright brand has no pool at all,
rather than a pool that happens to come out empty — so no later filter can
accidentally let one through.

Two things the design corpus left open, decided here and marked so:

* **The window's anchor date.** The corpus asks "from receipt? from season end?"
  and never answers, and the season calendar carries no dates to count from. So
  the clock starts at the piece's *first arrival at this location* — the only
  date the books actually hold. Earliest rather than latest inward on purpose:
  restocking a barcode must not silently hand the older pieces a fresh window.
* **The cap's period.** The corpus calls it a per-season budget. Seasons have no
  dates yet, so it is folded to one number per brand across every season, which
  is the same arithmetic — the percentage is one per brand, so the sum of the
  season budgets *is* the percentage of the summed delivered value. What is lost
  is only the ability to stop one season borrowing another's room.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import F, Min, Sum
from django.utils import timezone

from masters.models import Brand
from masters.scoping import scope_by_entitlement_or_brand, scope_by_store_and_brand
from outbound.costing import book_unit_costs
from outbound.models import ReturnSource
from stockledger.models import (
    MERCH_DIM_FIELDS,
    QuarantineStock,
    StockLedgerEntry,
    StockOnHand,
    merch_dims,
)

#: How stock genuinely *arrives* somewhere — the legs the window clock may start
#: from. Deliberately not every positive leg: a quarantine_in or a transit_in is
#: the same pieces changing state, and a stocktake surplus is the books catching
#: up with a shelf, so none of them is an arrival the brand would recognise.
ARRIVAL_KINDS = (StockLedgerEntry.Kind.PT_INWARD, StockLedgerEntry.Kind.TRANSFER_IN)

#: What KDPS took delivery of — the allowance's basis. Net of reversals, so a PT
#: posted twice and undone once does not inflate the room to send stock back.
DELIVERED_KINDS = (StockLedgerEntry.Kind.PT_INWARD, StockLedgerEntry.Kind.PT_REVERSAL)

#: How full the allowance may get before the screen starts warning. Not in the
#: corpus; picked so the warning arrives with room to act rather than at the
#: moment it is too late, and kept here as one named number to retune.
CAP_WARN_AT = Decimal("0.80")


class NotReturnable(Exception):
    """A scanned piece is not in this brand's returnable pool."""


@dataclass(frozen=True)
class PoolRow:
    """One returnable holding — a (store × barcode × source) with its book value.

    ``qty`` is what the books hold *now*, which is the cap on what a scan may
    take: the pool is re-read on every request, so a piece sold or transferred
    since the screen loaded is simply no longer there to scan.
    """

    source: str
    store_id: int
    store_code: str
    store_name: str
    sku_code: str
    dims: dict[str, str]
    qty: int
    unit_cost_paise: int
    arrived_on: date | None
    window_date: date | None
    days_left: int | None
    #: Past its deadline. Kept in the answer rather than dropped from it, so the
    #: screen can say *why* a holding is not offered — the window anchors on the
    #: earliest arrival, so a barcode re-delivered this season can expire on a
    #: date from last season, and a row that simply vanished would look like
    #: missing stock instead of an expired window.
    expired: bool = False

    @property
    def value_paise(self) -> int:
        return self.qty * self.unit_cost_paise

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.store_id, self.sku_code, self.source)

    def as_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "store": self.store_id,
            "store_code": self.store_code,
            "store_name": self.store_name,
            "sku_code": self.sku_code,
            **self.dims,
            "qty": self.qty,
            "unit_cost_paise": self.unit_cost_paise,
            "value_paise": self.value_paise,
            "arrived_on": self.arrived_on,
            "window_date": self.window_date,
            "days_left": self.days_left,
            "expired": self.expired,
        }


@dataclass(frozen=True)
class CapReading:
    """The Correction allowance, as it stands before this return.

    ``applies`` is False for the three models that have no cap — and then every
    number below it is zero, because a percentage of anything would be an
    invention. The screen shows the block only when it applies.
    """

    applies: bool
    percent: Decimal
    delivered_paise: int
    allowance_paise: int
    used_paise: int

    @property
    def remaining_paise(self) -> int:
        """Room left. Never negative on screen — an allowance already overdrawn
        has no room, and how far past it went is ``used`` against ``allowance``."""
        return max(0, self.allowance_paise - self.used_paise)

    def exceeded_by(self, this_return_paise: int) -> int:
        """By how much this return would overdraw the allowance. 0 if it fits."""
        if not self.applies:
            return 0
        return max(0, self.used_paise + this_return_paise - self.allowance_paise)

    def warns_at(self, this_return_paise: int) -> bool:
        """Is the allowance close enough to full to say so? Only meaningful while
        it still fits — once it is crossed the answer is the flag, not a warning."""
        if not self.applies or self.allowance_paise <= 0:
            return False
        if self.exceeded_by(this_return_paise):
            return False
        used = Decimal(self.used_paise + this_return_paise)
        return used >= CAP_WARN_AT * Decimal(self.allowance_paise)

    @property
    def warn_at_paise(self) -> int:
        """The used-value at which the screen starts warning.

        Sent to the client so the threshold stays one number in one place
        (Rule 12): a screen that recomputed ``CAP_WARN_AT`` itself would be a
        second copy, and retuning the constant here would leave the on-screen
        warning where it was.
        """
        if not self.applies:
            return 0
        return int(CAP_WARN_AT * Decimal(self.allowance_paise))

    def as_json(self, this_return_paise: int = 0) -> dict[str, Any]:
        return {
            "applies": self.applies,
            "percent": str(self.percent),
            "delivered_paise": self.delivered_paise,
            "allowance_paise": self.allowance_paise,
            "used_paise": self.used_paise,
            "remaining_paise": self.remaining_paise,
            "warn_at_paise": self.warn_at_paise,
            "this_return_paise": this_return_paise,
            "exceeded_by_paise": self.exceeded_by(this_return_paise),
            "warn": self.warns_at(this_return_paise),
        }


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


def _brand_ledger_total(brand: Brand, kinds: tuple[str, ...]) -> int:
    """Signed value of one family of legs for this brand, network-wide.

    Ledger rows carry the brand as the name printed on the PT rather than a key,
    so the match is case-insensitive against the master name — the same rule
    ``masters.scoping.scope_by_brand`` uses on stock.
    """
    total = StockLedgerEntry.objects.filter(brand__iexact=brand.name, kind__in=kinds).aggregate(
        total=Sum("amount")
    )["total"]
    return int(total or 0)


def _returned_value(brand: Brand) -> int:
    """What has already gone back to this brand, at the cost frozen on the lines.

    Read off the posted **return documents** rather than off ledger kinds. A
    defect claim drains the quarantine bucket and a season-end sweep drains the
    shelf, so counting kinds means remembering to add every future one — and the
    day somebody adds a "release from quarantine" that writes the same leg, the
    allowance quietly starts counting it as a return. "Which returns posted" is
    the question, so ask it directly.
    """
    from core.documents import DocStatus
    from outbound.models import ReturnToVendorLine

    total = ReturnToVendorLine.objects.filter(
        rtv__brand=brand, rtv__docstatus=DocStatus.SUBMITTED
    ).aggregate(total=Sum(F("qty") * F("unit_cost_paise")))["total"]
    return int(total or 0)


def cap_for(brand: Brand) -> CapReading:
    """This brand's return allowance and how much of it is already spent.

    Delivered value is what came in (PT inwards, net of reversals); used is what
    the posted returns took back out. Both are derived, never typed — the same
    rule as every other number in the system.
    """
    if not brand.cap_applies:
        return CapReading(False, Decimal(0), 0, 0, 0)
    percent = Decimal(brand.return_cap_percent or 0)
    delivered = _brand_ledger_total(brand, DELIVERED_KINDS)
    allowance = int(Decimal(max(0, delivered)) * percent / Decimal(100))
    return CapReading(True, percent, delivered, allowance, max(0, _returned_value(brand)))


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


def _arrival_dates(brand: Brand, store_ids: list[int] | None) -> dict[tuple[int, str], date]:
    """First arrival per (store × barcode) — the window clock's start.

    One grouped query rather than one per row: a brand at season end is thousands
    of barcodes, and the screen wants a countdown on every one of them.
    """
    qs = StockLedgerEntry.objects.filter(brand__iexact=brand.name, kind__in=ARRIVAL_KINDS)
    if store_ids is not None:
        qs = qs.filter(store_id__in=store_ids)
    grouped = qs.values("store_id", "sku_code").annotate(first_in=Min("created_at"))
    return {
        (row["store_id"], row["sku_code"]): timezone.localtime(row["first_in"]).date()
        for row in grouped
        if row["first_in"] is not None
    }


def _window(
    arrived_on: date | None, window_days: int, today: date
) -> tuple[date | None, int | None]:
    """(deadline, days left) for a piece that arrived on ``arrived_on``.

    Both are None when the brand has no negotiated window or the books cannot say
    when the piece arrived — "unknown", which the screen must show as unknown
    rather than as a comfortable number of days.
    """
    if window_days <= 0 or arrived_on is None:
        return None, None
    deadline = arrived_on + timedelta(days=window_days)
    return deadline, (deadline - today).days


def _priced(store_id: int, holdings: list[Any]) -> dict[str, int]:
    """What the books say each of these barcodes costs at this location.

    Batched through ``outbound.costing`` so a pool of hundreds of pieces stays
    two queries — and, more to the point, so the number the screen totals against
    the allowance is the *same* number the posting engine will freeze onto the
    line. Two readings of "what is this worth" is how a cap comes to be measured
    against postings worth something else (#103).
    """
    return book_unit_costs(store_id, {row.sku_code: row.season or "" for row in holdings})


def _quarantine_rows(qs: Any, arrivals: dict[tuple[int, str], date]) -> Iterator[PoolRow]:
    """Confirmed-damaged pieces. No window: a defect does not expire, and the
    negotiated 60–120 days is the *commercial* clock on unsold stock."""
    holdings = list(qs.order_by("store__code", "sku_code"))
    by_store: dict[int, dict[str, int]] = {}
    for store_id in {row.store_id for row in holdings}:
        by_store[store_id] = _priced(store_id, [r for r in holdings if r.store_id == store_id])
    for row in holdings:
        # Priced from the **bucket**, not from today's books. The value moved
        # into quarantine when the damage was confirmed, and that is the value
        # the return has to take back out again: re-deriving it here would drain
        # a quantity at one price and leave the difference stranded in a bucket
        # that is then deleted at zero — the same reason ``build_gap_closure_lines``
        # carries the in-transit cost forward rather than re-reading it. The
        # books are the fallback only where the bucket cannot say (a legacy row
        # carrying quantity but no value). Never the payload.
        unit_cost = (row.value_paise // row.qty if row.qty else 0) or by_store[row.store_id].get(
            row.sku_code, 0
        )
        arrived_on = arrivals.get((row.store_id, row.sku_code))
        yield PoolRow(
            source=ReturnSource.QUARANTINE,
            store_id=row.store_id,
            store_code=row.store.code,
            store_name=row.store.name,
            sku_code=row.sku_code,
            dims=merch_dims(row),
            qty=row.qty,
            unit_cost_paise=unit_cost,
            arrived_on=arrived_on,
            window_date=None,
            days_left=None,
        )


def _season_end_rows(
    brand: Brand, qs: Any, arrivals: dict[tuple[int, str], date], today: date
) -> Iterator[PoolRow]:
    """Unsold stock going back at season end — offered only inside the window.

    A brand with no negotiated window (``return_window_days`` is 0) still shows
    its stock, with the countdown blank: hiding the pool because nobody has typed
    a number would look like "this brand takes nothing back", which is a
    different and wrong answer. The screen says the window is not set.

    Past the deadline the holding is still yielded, marked ``expired``. It is not
    offered — ``pool_for`` drops it and a scan of it is refused — but it is *said*,
    because the window anchors on the earliest arrival and a barcode delivered
    twice can expire on the older date. A row that quietly disappeared would read
    as stock gone missing rather than as a window that closed.
    """
    holdings = list(qs.order_by("store__code", "sku_code"))
    by_store: dict[int, dict[str, int]] = {}
    for store_id in {row.store_id for row in holdings}:
        by_store[store_id] = _priced(store_id, [r for r in holdings if r.store_id == store_id])
    for row in holdings:
        unit_cost = by_store[row.store_id].get(row.sku_code) or 0
        arrived_on = arrivals.get((row.store_id, row.sku_code))
        window_date, days_left = _window(arrived_on, brand.return_window_days, today)
        yield PoolRow(
            source=ReturnSource.SEASON_END,
            store_id=row.store_id,
            store_code=row.store.code,
            store_name=row.store.name,
            sku_code=row.sku_code,
            dims=merch_dims(row),
            qty=row.net_qty,
            unit_cost_paise=unit_cost,
            arrived_on=arrived_on,
            window_date=window_date,
            days_left=days_left,
            expired=days_left is not None and days_left < 0,
        )


def _pool(
    brand: Brand,
    store_id: int | None,
    gate: Callable[[Any, str], Any],
    today: date | None = None,
) -> list[PoolRow]:
    """Both sources, windowed and priced — the shared body every reader of the
    pool goes through. ``gate`` is the only thing that differs between a
    request scoped to a signed-in user and a system job with none. ``today``
    defaults to the real date; a caller that needs the window measured against
    a fixed date (the alerts job, so it can be tested deterministically) may
    pass one."""
    if not brand.takes_returns:
        return []

    today = today or timezone.localdate()
    quarantine = gate(
        QuarantineStock.objects.filter(qty__gt=0, brand__iexact=brand.name).select_related("store"),
        "store_id",
    )
    season_end = gate(
        StockOnHand.objects.filter(net_qty__gt=0, brand__iexact=brand.name).select_related("store"),
        "store_id",
    )
    if store_id is not None:
        quarantine = quarantine.filter(store_id=store_id)
        season_end = season_end.filter(store_id=store_id)

    visible = [store_id] if store_id is not None else None
    arrivals = _arrival_dates(brand, visible)
    return [
        *_quarantine_rows(quarantine, arrivals),
        *_season_end_rows(brand, season_end, arrivals, today),
    ]


def returnable_pool(
    user: Any, brand: Brand, store_id: int | None = None, *, acting: bool = False
) -> list[PoolRow]:
    """Everything this brand will take back, at the locations ``user`` may see.

    Scoped through ``masters.scoping``, and *which* gate is the point. Reading
    the pool goes through ``scope_by_store_and_brand``, so the top-bar switcher
    narrows it like every other list. Building a return from it passes
    ``acting=True`` and goes through ``scope_by_entitlement_or_brand`` instead,
    because the acting gates ignore the switcher on purpose (ADR-0003): what a
    warehouse user happens to have on screen must not empty the pool underneath
    a scan and make the beep blame the stock for a header.

    Either way a brand manager's boundary is brands rather than stores, so they
    see their own brands' pool across the network and nobody else's.

    ``store_id`` narrows further to the one location a return is being built at —
    a return document leaves from a single store, so the scan validation behind
    it is always asked about one.
    """
    gate = scope_by_entitlement_or_brand if acting else scope_by_store_and_brand
    return _pool(brand, store_id, lambda qs, field: gate(qs, user, field))


def returnable_pool_all(
    brand: Brand, store_id: int | None = None, *, today: date | None = None
) -> list[PoolRow]:
    """Every returnable holding for this brand, network-wide — the same two
    sources and the same window math as ``returnable_pool``, unscoped.

    For a system reader with no signed-in caller to gate by — today, the alerts
    job (#77), which has to ask "which windows are closing anywhere" rather than
    "which windows can this person see". Never used to answer a request: a
    request always has a user, and must always go through ``returnable_pool``.
    """
    return _pool(brand, store_id, lambda qs, field: qs, today)


# ---------------------------------------------------------------------------
# Turning scans into return lines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allocation:
    """One scanned barcode's worth of one pool row — a return line, priced."""

    row: PoolRow
    qty: int

    @property
    def value_paise(self) -> int:
        return self.qty * self.row.unit_cost_paise

    def as_line_kwargs(self) -> dict[str, Any]:
        """The return line, whole. Dims, quantity, source and cost all come off
        the pool row the server built — nothing on a return line is ever read
        from the payload (#103)."""
        return {
            "sku_code": self.row.sku_code,
            **{field: self.row.dims.get(field, "") for field in MERCH_DIM_FIELDS},
            "qty": self.qty,
            "source": self.row.source,
            "unit_cost_paise": self.row.unit_cost_paise,
        }


#: Which pool source each kind of return draws from. A defect claim and a
#: season-end sweep are two different documents to the brand and two different
#: buckets in the ledger, so the return type is what narrows the pool before a
#: single barcode is matched — never a guess made afterwards from what turned up.
SOURCE_FOR_RETURN_TYPE = {
    "defective": ReturnSource.QUARANTINE,
    "seasonal": ReturnSource.SEASON_END,
}


def pool_for(rows: list[PoolRow], return_type: str, store_id: int) -> list[PoolRow]:
    """The slice of the pool one return may be built from — its store, its source.

    Expired holdings are shown but never offered, so they are dropped here rather
    than in the reader: one place decides what may be scanned.
    """
    source = SOURCE_FOR_RETURN_TYPE.get(return_type)
    if source is None:
        raise NotReturnable(f"'{return_type}' is not a kind of return.")
    return [
        row for row in rows if row.store_id == store_id and row.source == source and not row.expired
    ]


def allocate(scans: dict[str, int], rows: list[PoolRow]) -> list[Allocation]:
    """Turn scanned barcodes into priced return lines, against the pool.

    ``rows`` must already be one store's slice of one source — ``pool_for`` — so
    each barcode has at most one holding and there is nothing to choose between.

    A barcode the pool does not hold, or more of one than it holds, is refused
    outright rather than flagged. This is Rule 5's dangerous exception, the same
    one the transfer scanner takes: a return line the brand never agreed to take
    is not an approximation to correct later, it is a carton that comes back.
    """
    holdings = {row.sku_code: row for row in rows}
    if len(holdings) != len(rows):
        raise ValueError("Allocate against one store's slice of one pool source.")

    allocations: list[Allocation] = []
    for barcode, wanted in sorted(scans.items()):
        if wanted < 1:
            raise NotReturnable(f"{barcode}: a return line needs at least one piece.")
        row = holdings.get(barcode)
        if row is None:
            raise NotReturnable(
                f"{barcode} is not in this brand's returnable pool at this location — "
                "only confirmed-damaged pieces and in-window season-end stock go back."
            )
        if wanted > row.qty:
            raise NotReturnable(f"{barcode}: the pool holds {row.qty}, this return asks {wanted}.")
        if row.unit_cost_paise <= 0:
            # 0 out of ``book_unit_costs`` means "the books cannot price this",
            # never "free" (#103). Letting it through would understate the
            # allowance the approver is shown *and* build a document the posting
            # engine refuses, with no amend path out of it. Refused here, where
            # the message can say what to fix.
            raise NotReturnable(
                f"{barcode}: the books carry no cost for this piece, so it cannot be "
                "valued for a return. Price the cohort first."
            )
        allocations.append(Allocation(row, wanted))
    return allocations
