"""One store's trading day, in money (#188, D10 step 6).

Two screens ask the same question a day apart. The Dashboard asks "how is today
going" and the Money section asks "what did the *day* come to, by tender" - and
the second is the one somebody counts a drawer against. So the arithmetic lives
here once, and both read it.

**The tenders are the source, not the cash ledger.** `finledger` writes a receipt
row per tender (`post_sale_collection`), and the contract's sketch named it - but
a cash row carries no store and its clock is the *server's*, stamped when the
bill synced rather than when the counter took the money. A till that syncs a
Tuesday bill on Wednesday morning would put Tuesday's cash on Wednesday's summary,
which is precisely the number a store person is checking against a drawer they
counted on Tuesday night. `sell_saletender` hangs off the bill, and the bill
carries both the store and the till's own clock, so it is the honest reading. The
cash rows are written from these same tenders, so the two agree by construction
rather than by reconciliation.

**A cancelled bill is not a day's trade.** The kernel corrects by reversal, so a
cancelled sale keeps its row and its tenders; counting them would leave a summary
that a drawer can never match again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.db.models import QuerySet, Sum
from django.db.models.functions import TruncDate

from core.documents import DocStatus
from masters.models import Store
from sell.models import CreditNote, Sale, SaleLine, SaleTender

#: Every way a bill can be paid, in the order a summary reads them. Spelled here
#: rather than taken off `SaleTender.Mode.values` so a day with no card sales
#: still has a card tile: a mode that vanishes when nobody used it makes a store
#: person wonder what happened to it.
TENDER_MODES: tuple[str, ...] = (
    SaleTender.Mode.CASH,
    SaleTender.Mode.CARD,
    SaleTender.Mode.UPI,
    SaleTender.Mode.CREDIT_NOTE,
)

#: Every way a UPI tender can be proven, spelled out the same way as
#: `TENDER_MODES` so the split can't drift from the model either.
UPI_STATES: tuple[str, ...] = (
    SaleTender.UpiState.CONFIRMED,
    SaleTender.UpiState.MANUAL,
)


@dataclass(frozen=True)
class DayMoney:
    """What one store took on one day, and what it gave back."""

    #: Paise per tender mode - every mode present, nought where unused.
    collections: dict[str, int] = field(default_factory=dict)
    #: `collections["upi"]`, split by how the money was proven. The two sum to
    #: `collections["upi"]` by construction - both are read off the same tender
    #: rows in one query - and this is the day's only control on manual UPI: no
    #: manager PIN, just visibility the same evening.
    upi_split: dict[str, int] = field(default_factory=lambda: {state: 0 for state in UPI_STATES})
    #: Bills the server has accepted for the day. A held cart is not a bill.
    bills: int = 0
    #: Pieces sold, net of nothing - the returns are counted separately, because
    #: "we sold 40 and took 2 back" is two facts and averaging them hides one.
    pieces: int = 0
    #: Pieces given back. Exchange legs today; the plain-return document (#184)
    #: joins this count through the same lines when it lands.
    returns: int = 0
    #: What the bills took, summed: what the customer paid plus any bank offer
    #: the bank owes (ticket 11), which is part of the bill's value. Equals Σ collections by
    #: construction (the accept pipeline refuses a bill whose tenders do not come
    #: to its net), so it is derived from the tenders rather than from a second
    #: column that could drift.
    net_sales_paise: int = 0
    #: Face value of the credit notes this store handed out on the day.
    credit_notes_issued_paise: int = 0

    @property
    def avg_bill_paise(self) -> int:
        """The average bill, or nought on a day with none - never a divide by it."""
        return self.net_sales_paise // self.bills if self.bills else 0


def sales_on(store: Store, day: date) -> QuerySet[Sale]:
    """The bills that count as this store's trade on `day`.

    Numbered and not cancelled. `doc_number` is the fact of acceptance - the same
    reading `register_state` takes of the frontier - so a draft that never got one
    is a bill the server has not taken and is not part of anybody's day.
    """
    return Sale.objects.filter(store=store, billed_at__date=day, doc_number__isnull=False).exclude(
        docstatus=DocStatus.CANCELLED
    )


def _sum_by(
    rows: Iterable[dict[str, Any]], group_field: str, defaults: dict[str, int]
) -> dict[str, int]:
    """Group `rows` (a `.values(group_field).annotate(total=Sum(...))` queryset)
    into a dict seeded with `defaults`, so a key nobody used still renders.

    A key `rows` yields that `defaults` has never heard of is dropped rather
    than raising.
    """
    totals = dict(defaults)
    for row in rows:
        if row[group_field] in totals:
            totals[row[group_field]] = int(row["total"] or 0)
    return totals


def money_for(store: Store, day: date) -> DayMoney:
    """`store`'s day, by tender."""
    bills = sales_on(store, day)
    collections = _sum_by(
        SaleTender.objects.filter(sale__in=bills)
        .values("mode")
        .annotate(total=Sum("amount_paise")),
        "mode",
        {mode: 0 for mode in TENDER_MODES},
    )
    # Ticket 11: a bank offer is a payment only where GST after discount is on.
    # It joins the day's modes only on a day that has one, so every store with
    # the switch off reads exactly as before (B5). Never in the drawer.
    banked = int(
        SaleTender.objects.filter(sale__in=bills, mode=SaleTender.Mode.BANK_OFFER).aggregate(
            total=Sum("amount_paise")
        )["total"]
        or 0
    )
    if banked:
        collections[SaleTender.Mode.BANK_OFFER] = banked
    upi_split = _sum_by(
        SaleTender.objects.filter(sale__in=bills, mode=SaleTender.Mode.UPI)
        .values("upi_state")
        .annotate(total=Sum("amount_paise")),
        "upi_state",
        {state: 0 for state in UPI_STATES},
    )
    # Ticket 22: an alteration charge is a service, not a piece.
    lines = (
        SaleLine.objects.filter(sale__in=bills)
        .exclude(kind=SaleLine.Kind.ALTERATION)
        .values("direction")
        .annotate(qty=Sum("qty"))
    )
    pieces = {row["direction"]: int(row["qty"] or 0) for row in lines}
    return DayMoney(
        collections=collections,
        upi_split=upi_split,
        bills=bills.count(),
        pieces=pieces.get(SaleLine.Direction.SALE, 0),
        returns=pieces.get(SaleLine.Direction.RETURN, 0),
        net_sales_paise=sum(collections.values()),
        credit_notes_issued_paise=_notes_issued(store, day),
    )


def _notes_issued(store: Store, day: date) -> int:
    """Face value of the notes this store issued on `day`.

    Dated by the **bill** that issued the note rather than by the note's own
    `created_at`, for the reason the module docstring gives about the cash ledger:
    a note is minted when the bill syncs, and the bill was written at the counter.
    A note with no bill behind it cannot exist today - every credit note is issued
    by an exchange that netted negative - and if one ever does, it is dated by
    when it was written, which is all there is to go on.
    """
    return int(
        CreditNote.objects.filter(store=store, doc_number__isnull=False)
        .exclude(docstatus=DocStatus.CANCELLED)
        .filter(source_sale__billed_at__date=day)
        .aggregate(total=Sum("value_paise"))["total"]
        or 0
    )


def net_sales_by_day(store: Store, days: list[date]) -> dict[date, int]:
    """Net takings per day for a run of days, in one query.

    The sparkline asks for seven and the month-to-date bar asks for thirty-one;
    both would otherwise be a query a day on the store's Home screen.
    """
    if not days:
        return {}
    rows = (
        SaleTender.objects.filter(
            sale__store=store,
            sale__doc_number__isnull=False,
            sale__billed_at__date__gte=min(days),
            sale__billed_at__date__lte=max(days),
        )
        .exclude(sale__docstatus=DocStatus.CANCELLED)
        # `TruncDate` rather than a `__date` lookup in `values()`: both convert to
        # `TIME_ZONE` in SQL, and this one says out loud that the grouping key is
        # the *store's* day and not the UTC one the column stores.
        .annotate(day=TruncDate("sale__billed_at"))
        .values("day")
        .annotate(total=Sum("amount_paise"))
    )
    totals = {row["day"]: int(row["total"] or 0) for row in rows}
    return {day: totals.get(day, 0) for day in days}
