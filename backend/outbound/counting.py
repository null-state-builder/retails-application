"""Blind stock counting — sessions, the merged variance, and its correction (#76).

Four ideas, in the order they happen:

**Blind.** A counter scans; nothing on the screen or in the API says how many
there should be. That is enforced here rather than in the PWA: the book quantity
is not *taken* until the session is submitted, so an open session has no number
to leak however the client is driven.

**Merged.** Several counters run parallel sessions over different parts of a
location. The book is one number per piece, not one per counter, so the variance
is computed over the stocktake — every submitted session pooled — and never per
session.

**Server-side.** The variance (book vs counted, in pieces *and* in value) is
computed here, from the ledger's own on-hand projection and the books' unit cost.
Nothing about the difference is typed or arithmetic'd by a client, which is what
this slice took away from the old "book vs counted" typing screen.

**Never a blind overwrite.** The book is snapshotted at submit. If a piece moves
between the count and the correction — sold, transferred, adjusted by someone
else — the live book no longer matches the snapshot, and that line is held back
until a person says to apply it anyway.

Value is shown to whoever counted, unmasked: they are counting their own
location's stock and can already read the PT that carries the rate, so hiding
the number stops them sizing their own problem and protects nothing.

**Recounted, above tolerance (#78).** A difference worth more than the policy's
tolerance is not one person's to post. It is held back until a *second* person
has counted that piece again and said why it is off, and the correction then
goes to a named approver chosen by what it is worth — the Store Manager inside
the band, the Operations Head above it. Two of those refusals are floor rules
and answer to no configuration: nobody recounts a piece they counted, and nobody
approves a document they made.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from approvals.models import CLEARED_STATUSES, ApprovalPolicy
from approvals.services import display_name
from outbound.costing import OutboundPostingError, book_unit_cost
from outbound.maker_checker import KINDS, request_document_approval
from outbound.models import (
    AdjustmentReason,
    CountScope,
    CountSession,
    CountSessionLine,
    CountStatus,
    Recount,
    StockAdjustment,
    StockAdjustmentLine,
    Stocktake,
)
from outbound.posting import post_adjustment, resolve_line_identity
from stockledger.models import MERCH_DIM_FIELDS, StockOnHand, merch_dims

if TYPE_CHECKING:
    from accounts.models import User


class CountError(Exception):
    """A counting action the state of the count does not allow."""


class SameCounterError(Exception):
    """Somebody tried to recount a piece they counted themselves.

    A rights failure rather than a bad request — the action is well-formed and
    this person may never take it, however the tolerance and the bands are
    retuned. Kept as its own type so the API answers 403 and no caller can
    mistake it for the ordinary "that is not how a count works" refusal.
    """


class MovedMidCountError(Exception):
    """Stock moved between the count and the correction on at least one line.

    Carries the offending lines so the caller can ask about them by name — the
    whole point is that the person deciding sees *which* piece moved and by how
    much, not a blanket "something changed, try again".
    """

    def __init__(self, lines: list[VarianceLine]) -> None:
        self.lines = lines
        super().__init__(
            f"{len(lines)} line{'' if len(lines) == 1 else 's'} moved since the count was "
            "submitted. Confirm each one before it is applied."
        )


class RecountRequiredError(Exception):
    """At least one line is worth too much to correct on one person's count.

    Shaped like ``MovedMidCountError`` on purpose: both are "this correction is
    not refused forever, it is refused until a named person does a named thing",
    and both name the pieces so the screen can ask about those and nothing else.
    """

    def __init__(self, lines: list[VarianceLine]) -> None:
        self.lines = lines
        super().__init__(
            f"{len(lines)} line{'' if len(lines) == 1 else 's'} "
            f"{'is' if len(lines) == 1 else 'are'} worth more than the tolerance. Each needs a "
            "recount by somebody who did not count it before it can be corrected."
        )


# ---------------------------------------------------------------------------
# Opening and counting
# ---------------------------------------------------------------------------


def open_stocktake(store: Any, *, user: User | None, note: str = "") -> Stocktake:
    """Start a counting exercise at one location."""
    return Stocktake.objects.create(store=store, opened_by=user, note=note)


def open_session(
    stocktake: Stocktake, *, scope: str, scope_value: str = "", user: User | None
) -> CountSession:
    """Add one counter's scoped pass to an open stocktake."""
    if stocktake.status != CountStatus.OPEN:
        raise CountError("This count is closed — open a new one to count again.")
    if scope not in CountScope.values:
        raise CountError(f"Unknown count scope: {scope}")
    if scope != CountScope.STORE and not scope_value:
        raise CountError(f"A {CountScope(scope).label.lower()} count must say which one.")
    return CountSession.objects.create(
        stocktake=stocktake,
        scope=scope,
        scope_value=scope_value if scope != CountScope.STORE else "",
        counted_by=user,
    )


@transaction.atomic
def record_scans(session: CountSession, scans: dict[str, int]) -> list[CountSessionLine]:
    """Add scanned pieces to an open session — additive, so a phone that drops
    its connection mid-aisle resumes rather than restarts.

    Returns the session's lines. They carry no book quantity: this is the blind
    half of the count, and there is nothing here to withhold because nothing has
    been looked up.
    """
    if session.status != CountStatus.OPEN:
        raise CountError("This session has been submitted — its count can no longer change.")
    if not scans:
        raise CountError("No scans to record.")

    store_id = session.stocktake.store_id
    for barcode, qty in scans.items():
        if qty <= 0:
            raise CountError(f"{barcode}: a scanned quantity must be positive.")
        line, created = CountSessionLine.objects.select_for_update().get_or_create(
            session=session, sku_code=barcode, defaults={"counted_qty": 0}
        )
        if created:
            # Dims describe the piece, never its quantity — safe to read while
            # blind, and the counter needs them to see what they just scanned.
            for field_name, value in identity_dims(store_id, barcode).items():
                setattr(line, field_name, value)
        line.counted_qty += qty
        line.save()
    return list(session.lines.all())


def identity_dims(store_id: int, barcode: str) -> dict[str, str]:
    """What the books know this barcode *is* — from the location's stock where it
    holds some, else the SKU master (a surplus the books hold none of still has
    to be describable on the variance report)."""
    from masters.models import Sku

    on_hand = StockOnHand.objects.filter(store_id=store_id, sku_code=barcode).first()
    if on_hand is not None:
        return merch_dims(on_hand)
    sku = Sku.objects.filter(barcode=barcode).first()
    return merch_dims(sku) if sku is not None else dict.fromkeys(MERCH_DIM_FIELDS, "")


def book_quantities(store_id: int, sku_codes: Iterable[str]) -> dict[str, int]:
    """What the books say the location holds of each barcode, right now.

    One place, because two callers ask the same question for opposite reasons —
    the variance report asks in order to spot a piece that moved since the count,
    and an adjustment asks in order to have something to be a difference *from* —
    and they must never get different answers. A barcode the location holds none
    of is simply absent: zero is the caller's own reading of that.
    """
    return {
        row.sku_code: row.net_qty
        for row in StockOnHand.objects.filter(store_id=store_id, sku_code__in=list(sku_codes))
    }


def pieces_in_scope(session: CountSession) -> QuerySet[StockOnHand]:
    """Which pieces this session is entitled to speak about — see ``CountScope``.

    A store or brand session covers everything the books hold in that scope, so
    an unscanned piece is a real shortage. A section session covers only what was
    scanned: the books hold no floor plan, so it cannot honestly claim a piece is
    missing from a shelf the system cannot see.
    """
    held = StockOnHand.objects.filter(store_id=session.stocktake.store_id, net_qty__gt=0)
    if session.scope == CountScope.STORE:
        return held
    if session.scope == CountScope.BRAND:
        return held.filter(brand__iexact=session.scope_value)
    return held.filter(sku_code__in=session.lines.values_list("sku_code", flat=True))


@transaction.atomic
def submit_session(session: CountSession) -> CountSession:
    """Close a session and take its book snapshot — the moment counting stops
    being blind.

    Every piece in scope gets a line, not only the scanned ones: a piece the
    books hold and nobody found is exactly the shortage a count exists to find,
    and it has no scan to hang off.
    """
    if session.status != CountStatus.OPEN:
        raise CountError("This session has already been submitted.")

    scanned = {line.sku_code: line for line in session.lines.all()}
    for on_hand in pieces_in_scope(session):
        line = scanned.get(on_hand.sku_code)
        if line is None:
            line = CountSessionLine(session=session, sku_code=on_hand.sku_code, counted_qty=0)
            for field_name, value in merch_dims(on_hand).items():
                setattr(line, field_name, value)
        line.book_qty = on_hand.net_qty
        line.save()
        scanned.pop(on_hand.sku_code, None)
    # Scanned pieces the location's books hold none of — a surplus. Their book
    # is zero, which is a fact, not a missing snapshot.
    for line in scanned.values():
        line.book_qty = 0
        line.save(update_fields=["book_qty", "updated_at"])

    session.status = CountStatus.SUBMITTED
    session.submitted_at = timezone.now()
    session.save(update_fields=["status", "submitted_at", "updated_at"])
    return session


# ---------------------------------------------------------------------------
# The merged variance report
# ---------------------------------------------------------------------------


@dataclass
class VarianceLine:
    """One piece's answer, merged across every session that counted it."""

    sku_code: str
    dims: dict[str, str] = field(default_factory=dict)
    book_qty: int = 0
    #: What the counting sessions merged to — the *first* answer, kept beside the
    #: recount's so the trail reads original → recount → final (#78).
    first_counted_qty: int = 0
    #: The live book now, which is the snapshot unless the piece has since moved.
    live_book_qty: int = 0
    #: The books' cost for one piece. 0 means the books cannot price it — unknown,
    #: never free (#103).
    unit_cost_paise: int = 0
    #: The second person's answer, where one has been given. It may be **stale** —
    #: see ``recount_is_live``; a stale one is history, not an answer.
    recount: Recount | None = None
    #: Everybody whose submitted session covered this piece. They are barred from
    #: recounting it, and the screen reads it to know whose button to hide.
    counted_by_ids: frozenset[int] = frozenset()
    #: Above the policy tolerance, so one person's count may not correct it.
    above_tolerance: bool = False

    @property
    def recount_is_live(self) -> bool:
        """Does the recount still answer the question it was asked?

        A recount is a second opinion on *one* first answer by *someone else*, so
        it stops standing when either of those stops being true. Counting is not
        over when a recount happens — sessions stay open — and two things a later
        submission can change both matter:

        **The numbers.** A counter who submits afterwards can add pieces to this
        barcode or move the book snapshot, and then the recount is answering a
        question nobody is asking any more. Without this a stale recount silently
        outranks the later count: a recounter who found 3 would still post
        ``3 − book`` after another aisle brought the merge to 11, and eight
        pieces would leave the books that nobody ever said were missing.

        **The people.** A session that finds *none* of the piece moves no number
        at all, but it does add its counter to those who have now counted it. If
        that is the recounter, their own answer has quietly become a first count
        checked by nobody — the floor rule this slice exists to hold, walked
        around by doing the two halves in the other order. So the check is asked
        of the recounter as they stand *now*, not as they stood when they
        answered.
        """
        return (
            self.recount is not None
            and self.recount.first_counted_qty == self.first_counted_qty
            and self.recount.book_qty == self.book_qty
            and self.recount.recounted_by_id not in self.counted_by_ids
        )

    @property
    def live_recount(self) -> Recount | None:
        """The recount that still stands, if any — what every posting path reads."""
        return self.recount if self.recount_is_live else None

    @property
    def counted_qty(self) -> int:
        """The count that stands. A recount replaces the first answer rather than
        adding to it — that is the whole difference between a second *counter*
        (whose pieces pool into the merge) and a second *count* of one piece."""
        live = self.live_recount
        return live.counted_qty if live is not None else self.first_counted_qty

    @property
    def adj_qty(self) -> int:
        return self.counted_qty - self.book_qty

    @property
    def variance_paise(self) -> int:
        return self.adj_qty * self.unit_cost_paise

    @property
    def moved(self) -> bool:
        """Did this piece move between the count and now?"""
        return self.live_book_qty != self.book_qty

    @property
    def needs_recount(self) -> bool:
        """Waiting on a second person before it may be corrected."""
        return self.above_tolerance and self.live_recount is None

    def may_be_recounted_by(self, user: Any) -> bool:
        """The floor rule, asked as a question so the screen can offer only the
        button that would work. ``record_recount`` asks it again before writing —
        this is a courtesy, never the gate."""
        return getattr(user, "id", None) not in self.counted_by_ids

    def as_dict(self, *, for_user: Any = None) -> dict[str, Any]:
        """The line as the API answers it.

        ``for_user`` adds this caller's own answer to the floor rule, so a screen
        offers only the button that would work. Every API path passes it: the one
        that forgot would render a Recount button for the person the engine is
        about to refuse.
        """
        return {
            "sku_code": self.sku_code,
            **self.dims,
            "book_qty": self.book_qty,
            "first_counted_qty": self.first_counted_qty,
            "counted_qty": self.counted_qty,
            "adj_qty": self.adj_qty,
            "live_book_qty": self.live_book_qty,
            "moved": self.moved,
            "unit_cost_paise": self.unit_cost_paise,
            "variance_paise": self.variance_paise,
            "cost_known": self.unit_cost_paise > 0,
            "above_tolerance": self.above_tolerance,
            "needs_recount": self.needs_recount,
            "may_recount": self.may_be_recounted_by(for_user),
            # Shown even when stale, so the screen can say *why* a piece somebody
            # already recounted is back in the queue.
            "recount": _recount_dict(self.recount, stale=not self.recount_is_live),
        }


def _recount_dict(recount: Recount | None, *, stale: bool) -> dict[str, Any] | None:
    if recount is None:
        return None
    return {
        "counted_qty": recount.counted_qty,
        "first_counted_qty": recount.first_counted_qty,
        "reason": recount.reason,
        "reason_label": recount.get_reason_display(),
        "recounted_by_name": display_name(recount.recounted_by),
        "recounted_at": recount.recounted_at,
        # What the books said the piece was worth when they looked. Not the
        # number the band or the posting reads — both re-derive — but what the
        # person deciding needs in order to see what was at stake at the time.
        "unit_cost_paise": recount.unit_cost_paise,
        "stale": stale,
    }


def recount_tolerance() -> int:
    """How much a single line may be out by before a second person must count it.

    **The approval policy's tolerance, not a second number.** The stock
    adjustment family already carries the one the business tunes as data
    (Rule 12) — "a small variance auto-adjusts and is logged rather than queueing
    behind a second person" — and a count that needed a *different* threshold to
    decide who counts from the one that decides who signs would give the business
    two dials that mean the same thing and drift apart.

    A missing policy row is a closed gate everywhere else in maker-checker, and
    it is one here: with no configured tolerance every difference is big.

    The family code is read from the maker-checker table rather than spelled
    again, so the number that gates the recount and the number that gates the
    approval can never come from two different rows.
    """
    policy = ApprovalPolicy.objects.filter(kind=KINDS[StockAdjustment].code).first()
    return policy.tolerance if policy is not None else 0


def variance_report(stocktake: Stocktake) -> list[VarianceLine]:
    """Book against counted for the whole stocktake, in pieces and in value.

    Counted quantities add up across sessions — two counters covering two
    sections of the same store both contribute. Book quantities do not: the book
    is one number per piece, so the snapshot from the **latest** submitted
    session wins, which is also the one closest to the correction being applied.

    Which lines are too big for one person to correct is decided here rather than
    stored: the merge moves whenever another session is submitted, so a stored
    worklist would go stale against the difference it was describing.
    """
    sessions = [s for s in stocktake.sessions.all() if s.status != CountStatus.OPEN]
    sessions.sort(key=lambda s: (s.submitted_at or timezone.now(), s.id))

    merged: dict[str, VarianceLine] = {}
    counters: dict[str, set[int]] = {}
    for session in sessions:
        for line in session.lines.all():
            entry = merged.setdefault(line.sku_code, VarianceLine(sku_code=line.sku_code))
            entry.first_counted_qty += line.counted_qty
            entry.book_qty = line.book_qty or 0
            entry.dims = {f: getattr(line, f) or "" for f in MERCH_DIM_FIELDS}
            # A session that covered the piece counted it, whether or not the
            # scanner ever beeped on it: finding nothing there is an answer, and
            # the person who gave it may not be the one who checks it.
            if session.counted_by_id is not None:
                counters.setdefault(line.sku_code, set()).add(session.counted_by_id)

    live = book_quantities(stocktake.store_id, merged.keys())
    recounts = {r.sku_code: r for r in stocktake.recounts.all()}
    tolerance = recount_tolerance()
    for entry in merged.values():
        entry.live_book_qty = live.get(entry.sku_code, 0)
        entry.unit_cost_paise = book_unit_cost(
            stocktake.store_id, entry.sku_code, entry.dims.get("season", "")
        )
        entry.recount = recounts.get(entry.sku_code)
        entry.counted_by_ids = frozenset(counters.get(entry.sku_code, ()))
        # Sized on the *first* count, not the recount: a recount that agrees with
        # the books would otherwise retrospectively decide it was never needed,
        # and the one that follows it would be un-asked for. Magnitude, because a
        # surplus of ₹3,000 is as much a question as a shortage of ₹3,000.
        first_variance = abs((entry.first_counted_qty - entry.book_qty) * entry.unit_cost_paise)
        entry.above_tolerance = first_variance > tolerance
    return sorted(merged.values(), key=lambda v: v.sku_code)


# ---------------------------------------------------------------------------
# The recount — a second person on a big difference
# ---------------------------------------------------------------------------


def _locked(stocktake: Stocktake) -> Stocktake:
    """Re-read the count ``FOR UPDATE`` — the one way its two writers take turns.

    Recording a recount and applying the variance both read the merged report and
    then write against what they read. Run at the same moment they interleave:
    two recounters each see no answer yet and one overwrites the other, or a
    recount commits against a variance ``apply`` has already turned into a posted
    document. One lock on the count itself serialises both, because both are
    about the same count and nothing else contends for it.
    """
    return Stocktake.objects.select_for_update().get(pk=stocktake.pk)


@transaction.atomic
def record_recount(
    stocktake: Stocktake,
    *,
    sku_code: str,
    counted_qty: int,
    reason: str,
    user: User | None,
) -> Recount:
    """A second person's count of one piece, with the reason it is out.

    Four refusals, in the order a caller meets them. Only the third is a rights
    failure, and it is the one no ``Setup`` edit can reach: **a person may not
    recount a piece they counted**. It is asked of the merged report rather than
    of a stored assignment, so it holds however the recount is reached — API,
    shell, management command — and there is no column anywhere whose value
    could turn it off.

    The recount *replaces* the first count for this piece rather than adding to
    it, and both numbers stay on the row: the correction that follows is
    defensible months later because it can still say what the first pass found.

    The count is locked for the duration, which is what makes the read-then-write
    below honest: two people recounting the same piece at the same moment would
    otherwise both see no existing answer, both pass the ownership check, and one
    would silently overwrite the other — and a recount arriving mid-``apply``
    would land against a variance that had already been posted.
    """
    stocktake = _locked(stocktake)
    if stocktake.status == CountStatus.CLOSED:
        raise CountError("This count is closed — its variance has already been applied.")
    if reason not in AdjustmentReason.values:
        raise CountError(f"Unknown reason for a correction: {reason or '(none)'}")

    line = next((v for v in variance_report(stocktake) if v.sku_code == sku_code), None)
    if line is None:
        raise CountError(f"{sku_code} is not on this count — there is nothing to recount.")
    if not line.above_tolerance:
        raise CountError(
            f"{sku_code} is within tolerance. Only a difference big enough to need a second "
            "person is recounted."
        )
    if not line.may_be_recounted_by(user):
        raise SameCounterError(
            "You counted this piece. A recount is somebody else's job — that is what makes it "
            "a check."
        )
    if counted_qty < 0:
        raise CountError("A recounted quantity cannot be negative.")
    # A recount that still stands may be corrected by whoever gave it — a fat
    # finger on a phone is not an audit event — but never quietly replaced by a
    # third person, which would leave the document standing on an answer nobody
    # can find any more. A *stale* one guards nothing: the merge it answered has
    # moved, so the piece is open to whoever counts it against the one that now
    # stands.
    existing = line.live_recount
    if existing is not None and existing.recounted_by_id != getattr(user, "id", None):
        raise SameCounterError(
            f"{display_name(existing.recounted_by) or 'Somebody else'} has already recounted "
            f"{sku_code}. Only they can change their own answer."
        )

    # Somebody else taking over an expired answer starts a row of their own
    # rather than editing theirs into it: ``created_at`` means "when this answer
    # was first given", and carrying it across would date one person's count from
    # another person's. Only the author amending their own live answer updates in
    # place, which is what ``created_at`` then honestly says.
    if line.recount is not None and line.recount.recounted_by_id != getattr(user, "id", None):
        line.recount.delete()

    recount, _ = Recount.objects.update_or_create(
        stocktake=stocktake,
        sku_code=sku_code,
        defaults={
            "book_qty": line.book_qty,
            "first_counted_qty": line.first_counted_qty,
            "counted_qty": counted_qty,
            # What the books said the piece was worth as they looked — the
            # trail's record of what was at stake. The band and the posting
            # re-derive it at their own moment through the one seam (#103); a
            # caller never supplies it and never could.
            "unit_cost_paise": line.unit_cost_paise,
            "reason": reason,
            "recounted_by": user,
        },
    )
    return recount


# ---------------------------------------------------------------------------
# Applying the variance
# ---------------------------------------------------------------------------


@transaction.atomic
def apply_variance(
    stocktake: Stocktake, *, user: User | None, confirm_skus: frozenset[str] = frozenset()
) -> StockAdjustment:
    """Turn the merged variance into the one correction document, and post it if
    the count is allowed to correct itself.

    **Tolerance is the approval policy's, not a second number.** ``ApprovalKind``
    already carries a per-family tolerance the business tunes as data (Rule 12),
    and stock adjustments are the family it was written for — "a small variance
    auto-adjusts and is logged rather than queueing behind a second person". So a
    count within tolerance clears itself through the ordinary maker-checker seam
    (which writes the log entry saying why nobody was asked) and posts here.

    **Above it, two people before the books move (#78).** The same number that
    decides whether a checker is asked decides whether a *counter* is asked, so
    a line worth more than the tolerance is refused here until a second person
    has recounted that piece and said why it is out — and the document that
    follows goes to an approver picked by what it is worth, the Store Manager
    inside the policy's band and the Operations Head above it.

    **The cost is the books' cost.** Lines are priced through
    ``resolve_line_identity``, the same derive-or-refuse seam every other
    outbound document uses, so a variance can never post at zero value (#103) and
    a caller cannot supply a number of their own.

    Three steps, in this order: refuse a count that is not ready to be applied
    (`_variance_to_apply`), write the correction document (`_build_adjustment`),
    then ask maker-checker and close the take — all under the count's own lock,
    so a recount cannot commit against a variance that has already become a
    document, and two appliers cannot both find nothing applied yet.
    """
    stocktake = _locked(stocktake)
    lines = _variance_to_apply(stocktake, confirm_skus)
    adjustment = _build_adjustment(stocktake, lines, user)

    approval = request_document_approval(adjustment, requested_by=user)
    if approval is not None and approval.status in CLEARED_STATUSES:
        post_adjustment(adjustment, user=user)

    stocktake.adjustment = adjustment
    stocktake.status = CountStatus.CLOSED
    stocktake.save(update_fields=["adjustment", "status", "updated_at"])
    adjustment.refresh_from_db()
    return adjustment


def _variance_to_apply(stocktake: Stocktake, confirm_skus: frozenset[str]) -> list[VarianceLine]:
    """The lines this take may correct, or the reason it may not correct anything.

    A line whose piece moved since the snapshot applies only if named in
    ``confirm_skus``; otherwise the whole call is refused with those lines named.
    A confirmed line still applies its *counted-minus-snapshot* difference — the
    later movement is somebody else's posted document and is already on the
    books, so re-deriving against the live book would book it twice.

    A line still waiting on a recount refuses the **whole** call rather than
    dropping out of it. A count produces one correction document — that is what
    ``Stocktake.adjustment`` says — so posting the small lines now would either
    strand the big ones with no document to land on or split one count's answer
    across two vouchers a month apart.
    """
    if stocktake.adjustment_id is not None:
        raise CountError("This count's variance has already been applied.")

    sessions = list(stocktake.sessions.all())
    if not any(s.status != CountStatus.OPEN for s in sessions):
        raise CountError("Submit a count session before applying its variance.")
    # Correcting while someone is still counting would book *their* aisle as
    # shrinkage: the report only sees submitted sessions, so an unfinished
    # counter's pieces read as nobody having found them.
    if still_counting := [s for s in sessions if s.status == CountStatus.OPEN]:
        raise CountError(
            f"{len(still_counting)} session{'' if len(still_counting) == 1 else 's'} "
            f"({', '.join(s.scope_label for s in still_counting)}) "
            "have not been submitted. Their stock would be corrected as missing."
        )

    report = variance_report(stocktake)
    # Before anything else about *this* correction: the pieces nobody has checked
    # yet. Asked over the whole report rather than the lines about to be written,
    # so a big difference can never slip through by having been filtered out
    # first.
    if awaiting := [v for v in report if v.needs_recount]:
        raise RecountRequiredError(awaiting)

    lines = [v for v in report if v.adj_qty != 0]
    if not lines:
        raise CountError("The count agrees with the books — there is nothing to correct.")

    if moved := [v for v in lines if v.moved and v.sku_code not in confirm_skus]:
        raise MovedMidCountError(moved)
    return lines


def _document_reason(lines: list[VarianceLine]) -> str:
    """The whole correction's reason, from the reasons its lines gave.

    One reason if the recounts agree, ``other`` if they disagree — a single
    column cannot hold "theft on one rail and damage on the next", and picking
    one of them would be a lie on the document Tally reads. The per-piece truth
    is not lost: it is on the line. A count with no recount at all is a miscount,
    which is what slice 10 called it and what a small variance is.
    """
    reasons = {v.live_recount.reason for v in lines if v.live_recount is not None}
    if not reasons:
        return AdjustmentReason.MISCOUNT
    return reasons.pop() if len(reasons) == 1 else AdjustmentReason.OTHER


def _build_adjustment(
    stocktake: Stocktake, lines: list[VarianceLine], user: User | None
) -> StockAdjustment:
    """The one correction document the count produces, priced by the books."""
    adjustment = StockAdjustment.objects.create(
        store=stocktake.store,
        reason=_document_reason(lines),
        notes=f"Auto-adjustment from stock count #{stocktake.pk}.",
        created_by=user,
    )
    for v in lines:
        # Counted dims first, the books' answer over the top — the cost among
        # them, which is why it can never come from the count's own payload.
        identity: dict[str, int | str] = {f: v.dims.get(f, "") for f in MERCH_DIM_FIELDS}
        identity.update(
            resolve_line_identity(stocktake.store_id, v.sku_code, v.dims.get("season", ""))
        )
        StockAdjustmentLine.objects.create(
            adjustment=adjustment,
            sku_code=v.sku_code,
            book_qty=v.book_qty,
            # The recount where there is one — ``VarianceLine.counted_qty``
            # already resolves that, so the document cannot disagree with the
            # report the approver looked at.
            counted_qty=v.counted_qty,
            adj_qty=v.adj_qty,
            reason=v.live_recount.reason if v.live_recount is not None else "",
            **identity,
        )
    return adjustment


__all__ = [
    "CountError",
    "MovedMidCountError",
    "OutboundPostingError",
    "RecountRequiredError",
    "SameCounterError",
    "VarianceLine",
    "apply_variance",
    "pieces_in_scope",
    "open_session",
    "open_stocktake",
    "record_recount",
    "recount_tolerance",
    "record_scans",
    "submit_session",
    "variance_report",
]
