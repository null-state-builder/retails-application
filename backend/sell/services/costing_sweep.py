"""Paying off what a bill could not cost on the day (#186, grill Q5).

A piece can leave the shop before the books have ever heard of it. The tag is in
the customer's hand, the money is real, and the paperwork is three days behind -
so the bill is taken, the money side posts, and the *cost* side waits in
`DeferredCosting` rather than being guessed at or written as a nought (Rule 5,
Rule 8). This module is the other half of that bargain: when what was missing
arrives, it posts what was owed.

Three different things can be missing, and they arrive from two different
directions:

* **the price** - released by the PT that prices the cohort. Nothing has moved
  for this line at all: no cost event and no stock leg either, because a piece
  that was never inwarded cannot come off a shelf it was never on. Both post here.
* **the brand's commercial model**, and **the supplier a brand-owned piece is
  settled with** - released by *master data*, not by an inward. The piece was on
  the shelf and did leave it, so its stock leg is already written and only the
  value is missing. Re-posting the movement would take the piece out twice.

Which of the two a row is in is not read off its `reason`, because a reason
moves: a line waiting for its price can be priced by a PT and *still* not be
placeable, at which point it is waiting on the masters instead. What never moves
is the line's own `unit_cost_paise` - accept posted the stock leg for exactly the
lines that had one (`_priced_rows`), and this module writes that column only at
the moment it posts. So "has this piece moved yet" has one answer, on the row
itself, and it is the same answer the day after the bill and the year after it.

Everything posts under the **bill**, not under whatever released it: the cost is
the cost of that sale, and a COGS leg on the PT's voucher would say the arrival
incurred it. What makes the late voucher legible is `against_voucher` - it names
the bill, so a leg dated three days after the document it belongs to can be told
apart from one posted with it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.documents import DocStatus
from sell.models import ContinuityFlag, DeferredCosting, SaleLine, SellPolicy
from sell.services.movements import post_stock_move
from sell.services.postings import (
    CostedLine,
    CostPlan,
    plan_from_original,
    post_cost_event,
    resolve_cost_plan,
)
from sell.services.resolve import resolve_piece


def _waiting() -> Any:
    """The queue rows a sweep may act on, oldest first.

    Oldest first is load-bearing rather than tidy. An exchange leg whose own sale
    line is also waiting reads its cost off that line (`plan_from_original`, Rule
    3), so the sale has to be released before the piece coming back against it -
    and on one bill the sale line is always the earlier row.

    A cancelled bill's rows are not the sweep's to release (#220). Cancelling
    closes them as it goes, and this is the standing guard behind that: releasing
    one would post a cost event and take a piece off a shelf for a bill the books
    already say never happened, days after anybody was looking.
    """
    from masters.goods_models import SiteGuard

    goods_sites = SiteGuard.objects.filter(stock_contract=SiteGuard.StockContract.GOODS_V1).values(
        "site_id"
    )
    return (
        DeferredCosting.objects.filter(status=DeferredCosting.Status.WAITING)
        .exclude(sale_line__sale__docstatus=DocStatus.CANCELLED)
        .exclude(store_id__in=goods_sites)
        .order_by("id")
    )


@dataclass(frozen=True)
class _Replan:
    """What the books can say about a waiting line *now*."""

    plan: CostPlan
    unit_cost_paise: int
    #: The seven merchandising dims, for a line that had none to snapshot at
    #: billing. Empty when the line already describes itself.
    dims: dict[str, str]
    season: str


def sweep_deferred(barcode: str, season: str = "") -> int:
    """Release every line waiting on the price of this cohort. Returns how many.

    Called wherever a PT prices a cohort (`sell.signals`), which is once per
    valued row of the file - so it is written to be cheap on the overwhelmingly
    common answer, which is that nothing is waiting for this barcode at all.

    A line billed with no season on it is waiting for *any* price for its
    barcode, so it is swept too - and it is then priced from **this** cohort, the
    one the caller named, rather than from whichever of the barcode's cohorts a
    ranking heuristic would have picked. Those are not the same answer where a
    style was re-ordered into two seasons, and the difference would put the cost
    legs on one season and the stock leg on another.
    """
    rows = _waiting().filter(barcode=barcode)
    if season:
        rows = rows.filter(season__in=("", season))
    return _release(rows, season)


def sweep_brand(brand: str) -> int:
    """Release the lines waiting on master data about this brand. Returns how many.

    The other door into the queue, and the one no inward will ever open: a line
    whose brand the masters had never heard of, or whose brand-owned stock had no
    supplier of record, is waiting for somebody to type something into `masters`.
    Until that happens it sits in the queue deliberately - a guess about whose
    stock it was is the one outcome worse than a wait.
    """
    if not brand:
        return 0
    return _release(_waiting().filter(sale_line__brand=brand), "")


def _release(rows: Any, season: str = "") -> int:
    """Post what can now be posted, and re-label what still cannot.

    One transaction and one lock: the sweep runs from a signal, so two PTs
    posting the same barcode at once is an ordinary Tuesday, and without the lock
    both would read the same `waiting` row and post the cost twice. The trial
    balance would still be nought, which is exactly why the lock is here and not
    left to a later check to notice. `of="self"` keeps it to the queue rows -
    locking the joined bills and stores as well would put a PT post in the way of
    a counter that is still selling.

    Nothing is caught. This runs inside whatever transaction released it, so a
    failure here takes the PT down with it - which is the right way round: every
    reachable failure is a defect (the legs balance by construction, the cost is
    non-nought by the check below, the actor is the bill's own), and a PT that
    posted while quietly failing to cost a sale is a hole nothing downstream
    would find. A line the books simply still cannot place is not a failure and
    does not reach any of that: it stays in the queue and says why.
    """
    posted = 0
    with transaction.atomic():
        locked = rows.select_for_update(of=("self",)).select_related("sale_line__sale", "store")
        for row in locked:
            if _release_one(row, season):
                posted += 1
    return posted


def _release_one(row: DeferredCosting, season: str = "") -> bool:
    """One waiting line: post it, or record what it is still waiting for."""
    line = row.sale_line
    replanned = _replan(row, line, season)
    _describe_piece(line, replanned)
    if not replanned.plan.postable:
        # Not a failure and not a no-op: a line that was waiting for its price and
        # is now waiting for its brand has learnt something, and the queue should
        # say which of the three waits it is in.
        if row.reason != replanned.plan.deferral:
            row.reason = replanned.plan.deferral
            row.save(update_fields=["reason", "updated_at"])
        return False

    sale = line.sale
    # Whose posting this is. Not the person whose edit released it - they added a
    # brand or sent a PT, and did not sell anything - but the bill's own author,
    # because this is the bill's own cost event arriving late. It has to be a
    # named someone: the posting floor refuses an anonymous payable to a brand
    # outright ("nobody anonymous owes a brand money", `core.posting`), and a
    # signal has no request behind it to borrow one from.
    posted_by = sale.created_by
    # The piece never moved if it was never priced - accept wrote the stock leg
    # for exactly the lines that carried a cost. Read before the write below,
    # because the write is what destroys the evidence.
    moves_stock = int(line.unit_cost_paise or 0) <= 0
    _record_cost(line, replanned)
    if moves_stock:
        post_stock_move(sale, row.store, line, posted_by)
    # Marking the row posted is a claim that a voucher exists, so the claim is
    # checked rather than assumed. Unreachable today - a postable plan means a
    # cost above nought and a quantity of at least one - and left in because the
    # row is the *only* record that the cost was ever dealt with: if this ever
    # returned false, the queue would go quiet on a bill nothing had costed.
    if not post_cost_event(
        sale,
        [CostedLine(row=line, plan=replanned.plan)],
        posted_by,
        against_voucher=sale.doc_number or "",
    ):
        raise ValueError(
            f"{sale.doc_number} line {line.line_no}: the books could place this piece but "
            "wrote no cost event for it."
        )
    row.status = DeferredCosting.Status.POSTED
    row.posted_doc_number = sale.doc_number or ""
    row.save(update_fields=["status", "posted_doc_number", "updated_at"])
    _close_aged_flag(sale)
    return True


def _replan(row: DeferredCosting, line: SaleLine, season: str = "") -> _Replan:
    """Ask the books again what this line costs and which book it comes out of.

    A cost already on the line is never re-derived. It was frozen at billing or by
    an earlier pass of this sweep, the stock leg was written against it, and a
    cohort that has since been re-priced by a second PT must not silently restate
    what a posted movement was worth (Rule 3). Only what is genuinely still
    unknown is asked for.

    Which cohort, in order of who knows best: the season the *bill* carried, then
    the season the **caller just priced**, then the scan ladder. A re-ordered
    style sits in two seasons at two costs, and the ladder answers "what is the
    counter selling" - which is not the question a line that has already been sold
    is asking. Getting it wrong would price the cost legs off one season while the
    stock leg written by the same call carries the other.

    The middle rung is defence, not a fix for a live bug: a row is released by the
    first cohort that prices it, so by the time a barcode has two, no line is
    waiting for either. It is here because that is a property of `_release_one`'s
    ordering rather than of anything stated, and re-deriving an answer the caller
    already handed us is how it would stop being true.
    """
    if line.original_line is not None:
        # A piece coming back unwinds its own sale's posting, so it waits for the
        # same thing that line waits for and posts at that line's frozen cost.
        original = line.original_line
        return _Replan(
            plan=plan_from_original(original),
            unit_cost_paise=int(original.unit_cost_paise or 0),
            dims={},
            season=line.season,
        )
    piece = resolve_piece(row.store, row.barcode, row.season or line.season or season)
    unit_cost = int(line.unit_cost_paise or 0) or piece.unit_cost_paise
    dims = dict(piece.dims)
    season = dims.pop("season", "") or line.season
    return _Replan(
        plan=resolve_cost_plan(
            brand=line.brand or dims.get("brand", ""),
            barcode=row.barcode,
            season=season,
            unit_cost_paise=unit_cost,
        ),
        unit_cost_paise=unit_cost,
        dims=dims,
        season=season,
    )


def _describe_piece(line: SaleLine, replanned: _Replan) -> None:
    """Write onto the line what the books have just learnt the piece *is*.

    Rule 3 says a document snapshots its masters, and this is not an exception to
    it: a sold-before-inward line had *nothing* to snapshot - no brand, no design,
    no colour - and this is the first description that has ever existed. What is
    already on the line is left exactly as it is; only blanks are filled, so a
    later re-price of the cohort can never restate what a bill said it sold.

    Called on every pass, including one that cannot post, because the masters-side
    door looks a waiting line up *by its brand* - a line that stayed nameless
    would be one no brand edit could ever reach.

    The **season** is deliberately not written here (see `_pin_season`). It is the
    one dim that says *which cohort* the line is, and a line still waiting has not
    been placed in one yet.
    """
    fields: list[str] = []
    for field, value in replanned.dims.items():
        if value and not getattr(line, field, ""):
            setattr(line, field, value)
            fields.append(field)
    if fields:
        line.save(update_fields=[*fields, "updated_at"])


def _record_cost(line: SaleLine, replanned: _Replan) -> None:
    """Freeze what the piece cost, which book it came out of, and which cohort it
    was, at the moment the cost event posts.

    The cost matters beyond this posting. An exchange against this line reads
    `unit_cost_paise` and `cost_book` back off it to unwind what the sale posted
    (`plan_from_original`), so a line that stayed at nought after its cost was
    known would send the piece back into the queue a second time. It is also the
    record of whether the stock leg has been written - see the module docstring -
    which is why nothing writes it before the posting it describes.

    The season lands here rather than with the rest of the description for the
    same reason. It names the cohort, and the cohort is what the cost came out of:
    a season written while the line was still waiting would be the first PT's
    answer to a question the *releasing* PT ends up answering differently, and the
    cost legs and the stock leg would then disagree about which season sold.
    """
    fields = ["unit_cost_paise", "cost_book", "cost_vendor", "costing_status", "updated_at"]
    line.unit_cost_paise = replanned.unit_cost_paise
    line.cost_book = replanned.plan.book
    line.cost_vendor = replanned.plan.vendor
    line.costing_status = SaleLine.CostingStatus.POSTED
    if not line.season and replanned.season:
        line.season = replanned.season
        fields.append("season")
    line.save(update_fields=fields)


def _close_aged_flag(sale: Any) -> None:
    """A bill whose last waiting line has posted is not waiting on anything.

    Scoped to the bill and to `aged_uncosted` alone: the other five flag kinds are
    about what happened at the counter, and nothing this module does answers any
    of them.
    """
    if DeferredCosting.objects.filter(
        sale_line__sale=sale, status=DeferredCosting.Status.WAITING
    ).exists():
        return
    ContinuityFlag.objects.filter(
        sale=sale,
        kind=ContinuityFlag.Kind.AGED_UNCOSTED,
        status=ContinuityFlag.Status.OPEN,
    ).update(status=ContinuityFlag.Status.RESOLVED, resolved_at=timezone.now())


# --- ageing ----------------------------------------------------------------


def age_deferred(today: date | None = None, store: Any = None) -> int:
    """Flag every bill whose waiting lines have waited too long. Returns how many
    bills were newly flagged.

    The queue drains itself when the paperwork lands, so a row that is *still*
    there after the dial's number of days is telling you the paperwork is not
    coming - a PT nobody sent, a brand nobody added, a supplier nobody named. That
    is a person's job, and this is what puts it in front of one: the flag lands on
    the store's exception list, where the daily check reads it (#188).

    One flag per bill rather than per line, because "this bill has been waiting
    since Tuesday" is the sentence somebody acts on. It runs every night, so two
    things follow. A bill already carrying an open flag is not flagged again - a
    second copy of yesterday's finding is noise the person clearing the list has
    to read past. And a bill the store has deliberately *ignored* is left ignored:
    re-raising it every night would make "ignore" mean nothing, and the row is
    still on the Dashboard's count either way.

    The details are rewritten on every run rather than frozen at first sight. A
    bill whose three waiting lines become one has not stopped waiting, but the two
    that posted are no longer anybody's job, and a flag still naming them sends
    somebody after work that is done.

    Called by the nightly `sell_daily_check` (#188) as one of its four steps. It
    had a schedule of its own until that existed; `today` is passed in so a run
    about Tuesday ages Tuesday's queue rather than tonight's, and `store` so a
    run about one shop does not report every other shop's unpriced lines.
    """
    day = today or timezone.localdate()
    cutoff = day - timedelta(days=SellPolicy.current().uncosted_aging_days)
    raised = 0
    aged = _waiting().filter(created_at__date__lte=cutoff).select_related("sale_line", "store")
    if store is not None:
        aged = aged.filter(store=store)
    for sale_id, rows in _by_bill(aged).items():
        flag = ContinuityFlag.objects.filter(
            sale_id=sale_id, kind=ContinuityFlag.Kind.AGED_UNCOSTED
        ).exclude(status=ContinuityFlag.Status.RESOLVED)
        details = {
            "lines": sorted(row.sale_line.line_no for row in rows),
            "reasons": sorted({row.reason for row in rows}),
            "waiting_since": min(row.created_at for row in rows).date().isoformat(),
        }
        if flag.exists():
            flag.update(details=details, updated_at=timezone.now())
            continue
        ContinuityFlag.objects.create(
            sale_id=sale_id,
            store=rows[0].store,
            kind=ContinuityFlag.Kind.AGED_UNCOSTED,
            details=details,
        )
        raised += 1
    return raised


def _by_bill(rows: Any) -> dict[int, list[DeferredCosting]]:
    """Waiting rows grouped by the bill they are about, which is what a flag is
    about - a store person chases a bill, not a queue row."""
    grouped: dict[int, list[DeferredCosting]] = defaultdict(list)
    for row in rows:
        grouped[row.sale_line.sale_id].append(row)
    return grouped
