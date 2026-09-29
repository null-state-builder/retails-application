"""What the server knows about one counter's numbering (#180, D10 step 3).

The till owns its bill counter and the server only ever *accepts* what the till
already printed, so the two copies of "where are we up to" can drift: a till that
was reinstalled has lost its counter, and a till holding six unsynced bills is
ahead of the server by six. This is the reconciliation both sides need at store
open, and it answers three separate questions rather than one.

  · **How far has the server got?** `last_accepted_seq` is the highest number
    this store has actually landed, so a till whose local counter is *behind* it
    knows it lost state and must not re-issue a number that is already on a
    printed, posted bill.
  · **What is missing underneath?** A hole is a bill that was numbered and never
    arrived - still queued on a machine, or lost with one. The server can see the
    gap but cannot fill it; a human re-enters those from their printed copies
    (contract, register handover). The list is deliberately bounded: a dead till
    at bill 5,000 leaves five thousand holes, and a boot call that answers with
    all of them is a payload nobody reads.
  · **Can this store bill at all?** `series_open` is whether the year's `SAL`
    series row exists. A missing row turns every printed bill into a sync failure
    the store cannot fix, so the till is told at open rather than at the first
    sale.

The financial year here is the *server's*, and the till compares it with its own
before believing any of the rest. The two clocks straddle 1 April for a few
minutes each year, and a till that reconciled its brand-new counter against last
year's numbers would jump to five thousand and never come back.

The **handover** below is the deliberate half of the same story (#189). Boot
reconciliation is automatic and silent because it happens every morning at every
counter; moving a store's series onto a different machine is neither, so it takes
a manager, a reason and an audit row - and it answers with the bills the old
machine never sent, which somebody has to re-enter from their printed copies.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from django.db import connection
from django.db.models import Count, Max

from core.documents import VoucherSeries
from core.fiscal import financial_year
from masters.models import Store
from sell.models import SALE_DOC_TYPE, RegisterHandover, Sale

#: How many holes a boot response will name. Enough to re-enter a bad afternoon
#: from paper; not a dead till's whole year.
HOLE_LIMIT = 200


@dataclass(frozen=True)
class RegisterState:
    """One counter's numbering, as the server has it."""

    fy: str
    last_accepted_seq: int
    holes: list[int]
    hole_count: int
    series_open: bool

    def as_payload(self) -> dict[str, Any]:
        """The wire shape, which is the fields as they stand. Spelling them out
        again here would be a second copy of the contract, one rename apart from
        answering a till a field it does not have."""
        return asdict(self)


def register_state(store: Store, fy: str | None = None) -> RegisterState:
    """The boot/recovery state of `store`'s counter for `fy`, the current year if omitted."""
    fy = fy or financial_year()
    # Numbered rows only. A number is accepted at the instant the kernel mints it
    # onto the document, so `doc_number` is the fact - a draft that never got one
    # is a bill the server has not taken, and reporting it as the frontier would
    # tell a till to skip a number nobody has printed.
    landed = Sale.objects.filter(store=store, fy=fy, doc_number__isnull=False).aggregate(
        top=Max("till_seq"), landed=Count("id")
    )
    last = landed["top"] or 0
    # `till_seq` is unique per (store, fy) and positive, both by constraint, so
    # the count and the maximum agree exactly when nothing is missing. That makes
    # the common case - a counter that has never lost a bill - two aggregates,
    # and the scan below runs only when there is genuinely a gap to describe.
    missing = last - (landed["landed"] or 0)
    holes = _holes(store, fy, last) if missing else []
    return RegisterState(
        fy=fy,
        last_accepted_seq=last,
        holes=holes,
        hole_count=missing,
        series_open=VoucherSeries.objects.filter(
            fy=fy, store_code=store.code, doc_type=SALE_DOC_TYPE
        ).exists(),
    )


@dataclass(frozen=True)
class HandoverResult:
    """What a new machine is told when it takes over a store's counter."""

    resume_from_seq: int
    unsynced_hint: list[int]
    hole_count: int

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def record_handover(store: Store, actor: Any, reason: str) -> HandoverResult:
    """Move `store`'s bill series onto a new machine, and say so out loud.

    Two things happen, and only one of them is a change: the act is recorded, and
    the state is read back. There is deliberately **no write to the counter** -
    `VoucherSeries.next_seq` is not touched, because the till is what numbers a
    bill and the server's series only ever accepts what arrives. Handing back
    `resume_from_seq` is telling the new machine where to start; a server that
    also moved its own counter would be a second opinion about a number nobody
    has printed yet.

    `unsynced_hint` is the same bounded hole list the boot call answers with, and
    it means the same thing: these numbers were printed on the old machine and
    never reached us. Every one of them is a receipt in a drawer that has to be
    keyed back in under its original number (contract, step 3), which is why the
    count travels beside the list - a till dead at bill 5,000 leaves more holes
    than a response should carry, and a screen that showed 200 of them without
    saying so would read as "that is all of them".
    """
    state = register_state(store)
    RegisterHandover.objects.create(
        store=store,
        fy=state.fy,
        reason=reason,
        last_accepted_seq=state.last_accepted_seq,
        hole_count=state.hole_count,
        actor=actor,
    )
    return HandoverResult(
        resume_from_seq=state.last_accepted_seq + 1,
        unsynced_hint=state.holes,
        hole_count=state.hole_count,
    )


def _holes(store: Store, fy: str, last: int) -> list[int]:
    """The first `HOLE_LIMIT` numbers below `last` that never arrived.

    Done in SQL against `generate_series` rather than by pulling the year's
    sequences into Python: a busy store bills tens of thousands of times a year,
    and this is on the boot path of every till every morning.
    """
    with connection.cursor() as cur:
        sales = connection.ops.quote_name(Sale._meta.db_table)
        cur.execute(
            "SELECT s.seq FROM generate_series(1, %s) AS s(seq) "
            "WHERE NOT EXISTS ("
            f"  SELECT 1 FROM {sales} x "  # noqa: S608 - a quoted table name, not input
            "  WHERE x.store_id = %s AND x.fy = %s AND x.till_seq = s.seq "
            "    AND x.doc_number IS NOT NULL) "
            "ORDER BY s.seq LIMIT %s",
            [last, store.id, fy, HOLE_LIMIT],
        )
        return [row[0] for row in cur.fetchall()]
