"""The nightly reconciliation - the pilot's go/no-go gate (#188, B3, D5 Q10).

Every bill is checked once as it arrives, at the counter's own pace, and that is
not the same thing as checking the *day*. Four questions can only be asked once
the day is over, and this is where they are asked:

  · **Did every bill arrive?** The till numbers its own bills, so a gap is normal
    for a few minutes and a problem by end of day. Accept flags the gap it jumped;
    what this adds is the standing fact - these numbers are *still* missing - and
    the other half of it, closing a flag whose bills have since turned up.
  · **Was the customer charged what the rulebook says?** The reprice runs again
    over the day's bills. It is not a second copy of the accept-time check: it is
    the same functions (`sell.services.recompute`), asked again because a bill
    that synced before its offer did, or before a slab was corrected, was checked
    against a book that has since been fixed.
  · **Is anything still unpriced?** The costing queue drains itself when the
    paperwork lands, so what is still in it after the dial's days is paperwork
    that is not coming (#186, folded in here from its own schedule).
  · **Who took the returns?** Counted per seller, because that is the
    best-documented theft channel at a counter (grill Q7). Counting is not
    accusing: it raises a row on a list.

**Everything here is idempotent, and none of it writes money.** It runs every
night, and a person's morning list must not grow a second copy of yesterday's
finding - so a bill already carrying an open flag of a kind is not flagged again,
a bill the store *ignored* stays ignored, and a standing flag's details are
rewritten rather than duplicated. Nothing in this module posts, reverses or
touches a document (A7); the whole output is exception rows.

The green-gate demo is the last consequence: **a clean day raises nothing.** That
is the criterion the pilot is judged on, and it is why every finding here has to
be a real disagreement rather than a thing the design merely makes possible.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from core.documents import DocStatus
from masters.models import Store
from sell.models import ContinuityFlag, Sale, SaleLine, SellPolicy
from sell.services.costing_sweep import age_deferred
from sell.services.recompute import gst_offenders, offer_offenders, rulebook_for, savings_for
from sell.services.register import register_state
from sell.services.tax_rulebook import StoreTaxBooks

#: Flags this check raises about a store rather than about a bill. Both carry the
#: day they are about (`ContinuityFlag.DAY_KEY`), because a check that runs at
#: two in the morning would otherwise file last night's finding under today.
STORE_LEVEL_KINDS = (ContinuityFlag.Kind.NUMBER_HOLE, ContinuityFlag.Kind.EMPLOYEE_RETURNS)

#: How many missing bill numbers a flag's details will name. The count beside the
#: list is the true total: a till that died at bill 5,000 leaves five thousand
#: holes, and a row that showed two hundred without saying so would tell somebody
#: they had finished when they had not. Same reasoning, same number, as the
#: register's own `HOLE_LIMIT`.
HOLES_NAMED = 200


@dataclass
class Report:
    """What one run found, per store and in total - what the command prints."""

    day: date
    stores: int = 0
    #: Flag kind -> how many *new* rows this run raised.
    raised: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: Standing flags whose finding is unchanged and whose details were rewritten.
    refreshed: int = 0
    #: Flags this run closed because what they were about has since been fixed.
    resolved: int = 0
    #: Returns taken, by seller, for the day - the count grill Q7 asks for,
    #: reported whether or not anybody crossed the dial.
    returns_by_seller: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def total_raised(self) -> int:
        return sum(self.raised.values())

    def lines(self) -> list[str]:
        """The run, as a person would read it out."""
        out = [f"{self.day}: {self.stores} store(s) checked."]
        if self.total_raised:
            for kind, count in sorted(self.raised.items()):
                out.append(f"  raised {count} × {kind}")
        else:
            out.append("  nothing new to flag.")
        if self.refreshed:
            out.append(f"  refreshed {self.refreshed} standing flag(s)")
        if self.resolved:
            out.append(f"  closed {self.resolved} flag(s) whose cause has gone")
        for seller, count in sorted(self.returns_by_seller.items(), key=lambda r: -r[1]):
            out.append(f"  returns · {seller}: {count}")
        return out


def run(day: date | None = None, store_code: str = "") -> Report:
    """Check `day` (default yesterday) at every active store, or at one.

    **Yesterday, not today**, because the questions are about a day that is over:
    "these numbers never arrived" is not a finding at four in the afternoon. The
    cron runs in the small hours, so the default is the day it has just ended.
    """
    day = day or timezone.localdate() - timedelta(days=1)
    report = Report(day=day)
    stores = Store.objects.filter(is_active=True).order_by("code")
    if store_code:
        stores = stores.filter(code__iexact=store_code)
    for store in stores:
        report.stores += 1
        _check_store(store, day, report)
    # The costing queue is chain-wide and its own dial decides what is late, so it
    # is swept once rather than per store - and `day` rides in so a run about
    # Tuesday ages Tuesday's queue rather than tonight's.
    # Store-scoped when the caller named one, so `--store DEO` does not report
    # every other shop's unpriced lines under a single-store run.
    aged = age_deferred(day, store=stores.first() if store_code else None)
    if aged:
        report.raised[ContinuityFlag.Kind.AGED_UNCOSTED] += aged
    return report


def _check_store(store: Store, day: date, report: Report) -> None:
    with transaction.atomic():
        _reconcile_holes(store, day, report)
        _recheck_bills(store, day, report)
        _check_returns_by_seller(store, day, report)


# --- did every bill arrive? -------------------------------------------------


def _reconcile_holes(store: Store, day: date, report: Report) -> None:
    """The holes that survived the day, and the ones that did not.

    Two flags say different things about the same gap and both are wanted. The
    accept pipeline's is a *moment*: "when bill 40 landed, 37 to 39 were missing".
    This one is the *standing fact*: "at the end of Tuesday, 37 to 39 had still
    not arrived" - which is the sentence somebody acts on, by going to look for
    three printed receipts.

    So the moment's flags are closed here once their gap has filled, and the
    standing one is raised, refreshed or closed to match what is missing now.
    That is what makes a clean day clean: a till that was simply behind at six
    o'clock and caught up by midnight leaves nothing on anybody's list.

    **There is at most one standing row per store, and its day never moves.** A
    hole is not a fact about a day the way a seller's return count is - it is a
    fact about a shop that goes on being true - so raising a fresh row every
    night would put the same three missing bills on a person's list seven times
    in a week. The `day` it carries is the day the gap first survived, which is
    what the Money section files it under, and a run for an older date must not
    drag it backwards out of the day somebody has been looking at it on.

    **An ignored row does not silence a new hole.** `ignored` is the store having
    looked at *these* missing numbers and said they need nothing - a till that
    was reinstalled, a pad of test bills. When the missing set later changes, that
    is a finding nobody has seen, so it gets a row of its own rather than being
    written quietly into the one somebody already dismissed.

    Only the current financial year, because that is what the register knows - a
    hole in last year's series is a question for last year's books, and the year
    end is where it would be asked.
    """
    state = register_state(store)
    missing = set(state.holes)
    live = _open(store, ContinuityFlag.Kind.NUMBER_HOLE)

    # The hole *list* is capped (`register.HOLE_LIMIT`) and the count is not, so a
    # store with more than two hundred missing bills has a list that cannot be
    # asked "is 4,900 still missing". Nothing is closed on a partial answer: a
    # flag left standing costs somebody a second look, and one closed wrongly
    # costs a printed bill nobody ever goes to find.
    if state.hole_count == len(state.holes):
        for flag in live.filter(sale__isnull=False, status=ContinuityFlag.Status.OPEN):
            # `count` numbers starting at `from_seq`, as accept wrote them. A gap
            # nothing is missing out of any more is a gap somebody has filled.
            start = int(flag.details.get("from_seq") or 0)
            span = range(start, start + int(flag.details.get("count") or 0))
            if start and not missing.intersection(span):
                _resolve(flag, report)

    standing = live.filter(sale__isnull=True).order_by("-created_at").first()
    if not state.hole_count:
        # Every open standing row goes, whatever day it was raised on: nothing is
        # missing any more, so there is nothing left for any of them to be about.
        # An ignored one is left alone - it is already off every count, and
        # "resolved" would claim somebody dealt with it.
        for flag in live.filter(sale__isnull=True, status=ContinuityFlag.Status.OPEN):
            _resolve(flag, report)
        return
    named = state.holes[:HOLES_NAMED]
    details: dict[str, Any] = {
        "fy": state.fy,
        "count": state.hole_count,
        "missing": named,
        "last_accepted_seq": state.last_accepted_seq,
    }
    if standing is not None and standing.status == ContinuityFlag.Status.IGNORED:
        # Dismissed. Only a *different* set of missing numbers is news; the same
        # ones are the thing the store already looked at.
        if named == (standing.details or {}).get("missing"):
            return
        standing = None
    if standing is not None:
        # The day it first survived, kept - see the docstring.
        details[ContinuityFlag.DAY_KEY] = (standing.details or {}).get(
            ContinuityFlag.DAY_KEY, day.isoformat()
        )
    else:
        details[ContinuityFlag.DAY_KEY] = day.isoformat()
    _raise_or_refresh(store, ContinuityFlag.Kind.NUMBER_HOLE, details, report, standing)


# --- was the customer charged what the rulebook says? -----------------------


def _recheck_bills(store: Store, day: date, report: Report) -> None:
    """Reprice the day's bills against the rulebook and the dated slab.

    Not a second copy of the accept-time check - the same two functions, asked
    again. What changes between the two askings is the *book*, not the bill: an
    offer authored after a counter had already synced its afternoon, a slab head
    office corrected on Wednesday for a rate that changed on Monday. The bill is
    immutable, so the only thing that can move a finding is the book it is read
    against, and this is where that re-reading happens.

    A bill already carrying a live flag of the kind is left alone. Two rows saying
    the same thing about one bill is not twice the information, it is a list
    somebody stops reading.
    """
    bills = (
        Sale.objects.filter(store=store, billed_at__date=day, doc_number__isnull=False)
        .exclude(docstatus=DocStatus.CANCELLED)  # the books say this bill did not happen
        .prefetch_related("lines", "flags")
    )
    # The rulebook is one query for the whole store-day rather than one per bill:
    # this walks every bill a shop rang up, and a busy Saturday is a thousand.
    rules = rulebook_for(store.code, day)
    # Each bill's tax is judged by the version it recorded (ticket 03), so a
    # switch flipped or a version saved after the bill never re-judges it. A
    # version this server does not know falls back to the one in force; the
    # accept pipeline already flagged that bill `tax_version_mismatch`.
    tax_books = StoreTaxBooks(store)
    for sale in bills:
        rows = list(sale.lines.all())
        _flag_bill(
            sale,
            ContinuityFlag.Kind.GST_MISMATCH,
            {
                "lines": gst_offenders(
                    rows,
                    tax_books.recorded(sale.tax_setting_version, sale.billed_at)
                    or tax_books.at(sale.billed_at),
                )
            },
            report,
        )
        _flag_bill(
            sale,
            ContinuityFlag.Kind.OFFER_MISMATCH,
            {
                "lines": offer_offenders(
                    rows,
                    # Ticket 11: read the bill the way it says it was priced.
                    savings_for(
                        store.code, day, rows, rules, after_discount=sale.gst_after_discount
                    ),
                )
            },
            report,
        )


def _flag_bill(
    sale: Sale, kind: ContinuityFlag.Kind, details: dict[str, Any], report: Report
) -> None:
    """Raise `kind` on this bill, unless there is nothing to say or somebody has
    already been told.

    "Already been told" **includes a flag somebody has resolved**, and that is
    what makes a re-run of the same day silent. A bill cannot change - the kernel
    corrects by reversal, and there is no writer for a posted sale anywhere (A7) -
    so a finding somebody has answered is answered for good. Treating `resolved`
    as "say it again" would mean a cron retry after a partial night handed the
    store back work it had already cleared.

    It is deliberately narrow the other way: only a flag of this kind that is
    about *lines* silences this. The B2B corner raises `gst_mismatch` for a
    different reason entirely - the split the till printed disagreeing with the
    one the server derives (#187) - and it carries no `lines`. Treating the two as
    one would let a printed-split disagreement hide a bill whose tax is simply not
    the dated slab's, which is the finding this check exists for.
    """
    if not details["lines"]:
        return
    if any(flag.kind == kind and "lines" in (flag.details or {}) for flag in sale.flags.all()):
        return
    ContinuityFlag.objects.create(kind=kind, store=sale.store, sale=sale, details=details)
    report.raised[kind] += 1


# --- who took the returns? --------------------------------------------------


def _check_returns_by_seller(store: Store, day: date, report: Report) -> None:
    """Count the day's returns per seller, and name anybody past the dial.

    **Whose return is it?** The seller who served the customer on the bill the
    piece came back on - the bill's default seller, the first seller named on its
    sold lines - not the one credited with the original sale. The control grill Q7
    cites is refund fraud, which is about the person *processing* the return, and
    the seller who sold the piece a fortnight ago has nothing to do with it. Where
    a bill names nobody at all (every line on it is a return leg, so no line
    carries a salesperson), the original sale's seller is the only name there is
    and it is used.

    A seller is a person: the staff record on the line, or the one an old
    salesperson row was matched to (ticket 07), so somebody's returns before and
    after the move count as one. A line with no staff record behind it yet is
    counted by its frozen code. The code and name shown are the ones frozen on
    the first line counted.

    The count is reported whichever side of the dial it falls, because "counted
    per employee in the daily check" is the ask; the flag is only what happens
    when a count is large enough to be worth somebody's morning.
    """
    rows = list(
        SaleLine.objects.filter(
            sale__store=store,
            sale__billed_at__date=day,
            sale__doc_number__isnull=False,
            direction=SaleLine.Direction.RETURN,
        )
        .exclude(sale__docstatus=DocStatus.CANCELLED)
        .select_related("original_line__salesperson_match")
    )
    served_by = _bill_sellers({row.sale_id for row in rows})
    per_seller: dict[str, int] = defaultdict(int)
    shown: dict[str, tuple[str, str]] = {}
    for row in rows:
        seller = served_by.get(row.sale_id)
        if seller is None and row.original_line is not None:
            seller = _seller_of(row.original_line)
        if seller is not None:
            key, code, name = seller
            shown.setdefault(key, (code, name))
            per_seller[key] += int(row.qty)
    for key, count in per_seller.items():
        report.returns_by_seller[f"{store.code}/{shown[key][0]}"] += count

    limit = SellPolicy.current().return_review_count
    over: list[dict[str, Any]] = sorted(
        (
            {"salesman": shown[key][0], "name": shown[key][1], "returns": count}
            for key, count in per_seller.items()
            if count > limit
        ),
        key=lambda row: -int(row["returns"]),
    )
    # Keyed to *this day*, unlike the hole flag: "Ali took back six on Tuesday"
    # is a fact about Tuesday and stays true whatever happens on Wednesday.
    standing = _for_day(store, ContinuityFlag.Kind.EMPLOYEE_RETURNS, day).first()
    if not over:
        # Nobody crossed the dial. A row from an earlier run of the same day was
        # about a count that has since changed - a bill cancelled, the dial
        # widened - so it is closed rather than left saying something untrue.
        if standing is not None and standing.status == ContinuityFlag.Status.OPEN:
            _resolve(standing, report)
        return
    if standing is not None and standing.status != ContinuityFlag.Status.OPEN:
        # Somebody has already answered this day's count. A re-run must not hand
        # it back to them - see `_flag_bill` for the same rule about a bill.
        return
    details: dict[str, Any] = {
        ContinuityFlag.DAY_KEY: day.isoformat(),
        "threshold": limit,
        "sellers": over,
    }
    _raise_or_refresh(store, ContinuityFlag.Kind.EMPLOYEE_RETURNS, details, report, standing)


Seller = tuple[str, str, str]


def _seller_of(line: SaleLine) -> Seller | None:
    """(who, code, name) for a line: the person where one is known, else the code."""
    if not (line.salesperson_code or line.salesperson_name):
        return None
    staff_id = line.salesperson_id or (
        line.salesperson_match.staff_id if line.salesperson_match is not None else None
    )
    key = f"staff:{staff_id}" if staff_id else f"code:{line.salesperson_code}"
    return key, line.salesperson_code, line.salesperson_name


def _bill_sellers(sale_ids: set[int]) -> dict[int, Seller]:
    """Each bill's default seller: the first sold line that names one.

    Until ticket 07 the bill carried this as a column, written from exactly this
    rule; the column went with the old salesperson table and the rule stays.
    """
    found: dict[int, Seller] = {}
    lines = (
        SaleLine.objects.filter(sale_id__in=sale_ids, direction=SaleLine.Direction.SALE)
        .exclude(salesperson_code="", salesperson_name="")
        .select_related("salesperson_match")
        .order_by("sale_id", "id")
    )
    for line in lines:
        if line.sale_id in found:
            continue
        seller = _seller_of(line)
        if seller is not None:
            found[line.sale_id] = seller
    return found


# --- the two ways a store-level flag moves ----------------------------------


def _open(store: Store, kind: ContinuityFlag.Kind) -> QuerySet[ContinuityFlag]:
    """This store's flags of one kind that nobody has answered `resolved`.

    `ignored` is in here: the store looked and said this one is fine, and
    re-raising it every night would make the third state mean nothing. What each
    caller then does with an ignored row is its own decision, because the two
    findings differ - see `_reconcile_holes`.
    """
    return ContinuityFlag.objects.filter(store=store, kind=kind).exclude(
        status=ContinuityFlag.Status.RESOLVED
    )


def _for_day(store: Store, kind: ContinuityFlag.Kind, day: date) -> QuerySet[ContinuityFlag]:
    """This store's flags of one kind about `day`, whatever their status.

    Every status, because a re-run of a day must be able to see that somebody has
    already answered it - a `resolved` row this could not see would be a finding
    handed straight back to the person who cleared it.
    """
    return ContinuityFlag.objects.filter(
        store=store, kind=kind, **{f"details__{ContinuityFlag.DAY_KEY}": day.isoformat()}
    ).order_by("-created_at")


def _raise_or_refresh(
    store: Store,
    kind: ContinuityFlag.Kind,
    details: dict[str, Any],
    report: Report,
    standing: ContinuityFlag | None,
) -> None:
    """Write the finding, or update the one that is already saying it.

    Details are rewritten rather than frozen at first sight, for the reason the
    ageing sweep gives: a gap of five that has become a gap of two has not gone
    away, but a flag still naming the three that arrived sends somebody after
    work that is done.
    """
    if standing is not None:
        standing.details = details
        standing.save(update_fields=["details", "updated_at"])
        report.refreshed += 1
        return
    ContinuityFlag.objects.create(kind=kind, store=store, details=details)
    report.raised[kind] += 1


def _resolve(flag: ContinuityFlag, report: Report) -> None:
    """Close a flag whose cause has gone, without a name against it.

    `resolved_by` stays empty on purpose: nobody did this, the thing simply
    stopped being true - the late bills arrived - and putting the nightly job's
    name in a field that means "the person who dealt with it" would make an
    audit trail lie. It is the same close the costing sweep does when a PT lands.
    """
    flag.status = ContinuityFlag.Status.RESOLVED
    flag.resolved_at = timezone.now()
    flag.save(update_fields=["status", "resolved_at", "updated_at"])
    report.resolved += 1
