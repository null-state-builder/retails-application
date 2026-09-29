"""What a sold line is still worth back, and what has already been given back.

One module because there are **two stores of return history** and they share one
ceiling. New activity is an exchange leg - a `SaleLine` with `direction=return`
pointing at the line it gives back. Before #273, standalone returns were stored
as `ReturnLine` rows on `SRT` documents. Both must count for ever or a customer
could return a piece once through each generation of the counter.

So the ledger of what has been given back is read from both tables, always, and
under the same lock. The lock is on the *original* line, which is what makes it
work: two returns of the last piece of a line, arriving at once by either route,
serialise behind it rather than both reading "none returned yet" and both paying
out. No constraint downstream would catch that - each refund is individually
correct - so the lock is the whole of the defence.

Cancelled documents are not counted, on either side. The books say that exchange
or that return never happened, and the customer is still holding the piece and
the receipt: counting it would refuse the next legitimate return as
`ALREADY_RETURNED` and under-pay the one after it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db.models import IntegerField, OuterRef, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from core.documents import DocStatus
from sell.models import ReturnLine, SaleLine

#: What `with_returned` annotates. Named here because two readers use them - the
#: bill's read shape, and the counter deciding what a piece is worth back - and a
#: string spelled twice is a rename waiting to break one of them.
RETURNED_QTY = "returned_qty"
RETURNED_PAISE = "returned_paise"
#: Ticket 13 (B60): what earlier return legs took off the line's bank-offer
#: share. Only exchange legs carry one; a historical plain return never did.
RETURNED_BANK_PAISE = "returned_bank_paise"


def returned_so_far(original: SaleLine) -> tuple[int, int]:
    """`(quantity, paise)` already given back against one sold line, both ways.

    Locks the original line first - see the module docstring for why that lock is
    the ceiling rather than a nicety.
    """
    SaleLine.objects.select_for_update().filter(pk=original.pk).first()
    legs = (
        SaleLine.objects.filter(original_line=original, direction=SaleLine.Direction.RETURN)
        .exclude(sale__docstatus=DocStatus.CANCELLED)
        .aggregate(qty=Sum("qty"), paise=Sum("net_paise"))
    )
    plain = (
        ReturnLine.objects.filter(original_line=original)
        .exclude(return_doc__docstatus=DocStatus.CANCELLED)
        .aggregate(qty=Sum("qty"), paise=Sum("refund_paise"))
    )
    return (
        int(legs["qty"] or 0) + int(plain["qty"] or 0),
        int(legs["paise"] or 0) + int(plain["paise"] or 0),
    )


def with_returned(lines: QuerySet[SaleLine]) -> QuerySet[SaleLine]:
    """`lines` with what has already come back off each one - pieces and paise.

    The read-only twin of `returned_so_far`, and the only other place the "both
    tables, neither cancelled" rule is spelled. It exists because a *list* of
    lines must not pay four queries a row for two numbers - and because the fix
    for that must not be a third copy of the rule.

    Both numbers, not just the count, and the paise are the load-bearing one. The
    counter works out what an exchange leg is worth back **offline**, and the
    server checks that figure to the paisa before it will take the bill: the last
    piece of a line settles the remainder of what has not been given back yet
    (`entitled_refund`), so a till that knew only how many pieces had gone would
    get the second partial return of a line wrong - and would find out after the
    receipt had printed.

    Deliberately no lock: this is what a person is shown and what a counter prices
    from, not what a refund is finally decided against. The decision locks
    (`returned_so_far`), which is what makes the screen's figure advisory and the
    refusal authoritative.
    """

    def _off(
        model: type[SaleLine] | type[ReturnLine], cancelled: str, amount: str
    ) -> QuerySet[Any, dict[str, Any]]:
        return (
            model.objects.filter(original_line=OuterRef("pk"))
            .exclude(**{cancelled: DocStatus.CANCELLED})
            .values("original_line")
            .annotate(qty_total=Sum("qty"), paise_total=Sum(amount))
        )

    legs = _off(SaleLine, "sale__docstatus", "net_paise").filter(
        direction=SaleLine.Direction.RETURN
    )
    plain = _off(ReturnLine, "return_doc__docstatus", "refund_paise")
    bank = (
        SaleLine.objects.filter(original_line=OuterRef("pk"), direction=SaleLine.Direction.RETURN)
        .exclude(sale__docstatus=DocStatus.CANCELLED)
        .values("original_line")
        .annotate(bank_total=Sum("bank_offer_paise"))
    )
    return lines.annotate(
        **{
            RETURNED_QTY: _summed(legs, "qty_total") + _summed(plain, "qty_total"),
            RETURNED_PAISE: _summed(legs, "paise_total") + _summed(plain, "paise_total"),
            RETURNED_BANK_PAISE: _summed(bank, "bank_total"),
        }
    )


def _summed(rows: QuerySet[Any, dict[str, Any]], column: str) -> Coalesce:
    """One column of a per-original-line aggregate, as nought where there is none."""
    return Coalesce(Subquery(rows.values(column), output_field=IntegerField()), Value(0))


def refund_share(
    *,
    paid_paise: int,
    line_qty: int,
    returning: int,
    returned_qty: int,
    returned_paise: int,
) -> int:
    """What `returning` pieces of a line are worth back, in whole paise (D2).

    Pure arithmetic, taking its five inputs rather than fetching them, because
    **the counter computes this too** - offline, on a bill it is about to print -
    and the two answers have to agree to the paisa or the accept pipeline refuses
    the whole bill after the receipt is in a customer's hand. The cases live in
    `sell/vectors/refunds.json` and `src/till/refund.vectors.test.ts` reads the
    same file, which is the "two engines, one set of golden files" shape the
    offer rulebook and the GSTIN checker already hold to.

    Two rules, and the second is the one that is easy to miss.

    A share is rounded **half-up**, never by Python's `round()` - that is
    banker's rounding on a float, so a ₹10.05 pair refunds ₹5.02 instead of
    ₹5.03 and the till's correct figure is the one refused.

    The **last** piece of a line settles the remainder of what has not been given
    back yet, so the parts always sum to exactly what the customer paid: three
    pieces at ₹10.00 refund 333 + 333 + 334, not 333 three times with a paisa
    left in the books for ever.
    """
    if returned_qty + returning >= line_qty:  # the last of it - settle the remainder
        return paid_paise - returned_paise
    share = Decimal(paid_paise) * returning / line_qty
    return int(share.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def entitled_refund(original: SaleLine, qty: int) -> int:
    """`refund_share` for one sold line, told what has already come back off it."""
    returned_qty, returned_paise = returned_so_far(original)
    return refund_share(
        paid_paise=int(original.net_paise or 0),
        line_qty=original.qty,
        returning=qty,
        returned_qty=returned_qty,
        returned_paise=returned_paise,
    )
