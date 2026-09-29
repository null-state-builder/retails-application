"""What a bill line does to the shelf, and the one table that says which (#186).

Extracted from the accept pipeline because a second caller arrived. A line sold
before its paperwork moved *nothing* when the bill was taken - the piece was
never inwarded, so there was no shelf to take it off - and the movement posts
later, when the PT prices the cohort and the sweep releases it. Two places
writing stock legs for a bill means the mapping from "what kind of line is this"
to "which bucket, which ledger kind, which sign" has to live in one of them or in
neither, and a duplicated table is exactly the failure the original comment
warned about: two conditions choosing the bucket independently, and a damaged
return going back on the shelf on one path but not the other.

A third caller arrived with the plain return (#184). A `ReturnLine` is the same
event as a bill's exchange leg - a piece coming back, to the shelf or to
quarantine - so it comes through here rather than growing a second table of
buckets beside this one. That is why the two functions below ask a row what it
*is* (`is_return`, `condition`) rather than reading a `direction` column only one
of the two has.

The value side of the same line lives in `sell.services.postings`; this module
never touches money beyond the unit cost it hands the ledger.
"""

from __future__ import annotations

from typing import Any

from sell.models import Return, ReturnLine, Sale, SaleLine
from stockledger.models import StockLedgerEntry
from stockledger.projections import post_on_hand_movement, post_quarantine_movement

#: Either shape of line a bill or a return is made of: a `SaleLine` (sold, or an
#: exchange leg coming back) and a `ReturnLine`, which is always coming back.
#:
#: Declared once, here, and imported by `postings` rather than spelled again -
#: the two modules ask the same three questions of a line (is this coming back,
#: what did it cost, what is it), and a second alias would be a second thing to
#: keep in step.
SellLine = SaleLine | ReturnLine

#: Either document those lines hang off, and either document value posts under.
SellDocument = Sale | Return


def movement_of(row: SellLine) -> str:
    """Which of the three things a line does to stock."""
    if not row.is_return:
        return "sold"
    return "returned_damaged" if row.condition == SaleLine.Condition.DAMAGED else "returned_good"


#: The three movements a bill can make, each as `(writer, ledger kind, sign)`.
#: One table rather than a pair of branches, so the bucket a piece lands in and
#: the kind that names it can never be chosen by two different conditions and
#: disagree - which is the failure that would put a damaged return on the shelf.
MOVEMENTS = {
    "sold": (post_on_hand_movement, StockLedgerEntry.Kind.SALE_OUT, -1),
    "returned_good": (post_on_hand_movement, StockLedgerEntry.Kind.SALE_RETURN_IN, 1),
    "returned_damaged": (post_quarantine_movement, StockLedgerEntry.Kind.QUARANTINE_IN, 1),
}


def post_stock_move(doc: SellDocument, store: Any, row: SellLine, actor: Any) -> StockLedgerEntry:
    """Move one line's piece, at the cost frozen on the line.

    The row describes itself (Rule 9): the seven merchandising dims are
    snapshotted off it onto the leg, so a ledger read years later does not depend
    on the masters still saying what they said. Nothing here checks the cost -
    the ledger refuses a movement at nought value on its own (`ZeroValueMovement`,
    Rule 5), which is what keeps the "never at zero" law in one place.
    """
    mover, kind, sign = MOVEMENTS[movement_of(row)]
    return mover(
        store=store,
        gstin=store.gstin,
        sku_code=row.barcode,
        source=row,
        qty=sign * row.qty,
        unit_cost_paise=row.unit_cost_paise,
        kind=kind,
        doc_number=doc.doc_number or "",
        line_no=row.line_no,
        posted_by=actor,
    )
