"""Write one stock movement and fold it into the projection it belongs in.

The ledger is the truth and the projections (`StockOnHand`, `QuarantineStock`)
are caches of it, maintained inside the caller's posting transaction and
rebuildable by ``manage.py rebuild_stock_on_hand``. Keeping "append the leg" and
"move the bucket" in one function is what stops the two drifting: there is no way
to write the row and forget the projection, because it is one call.

This lives in `stockledger`, next to the models, because more than one module
posts stock and - per ADR-0002 - none of them may import each other. `outbound`
still carries its own equivalents (`_write_stock_entry`, `_write_quarantine_entry`);
they predate this module and are left where they are rather than refactored under
a money slice, so the honest reading is "this is the shared home, and outbound has
not moved in yet".
"""

from __future__ import annotations

from typing import Any

from stockledger.contracts import authorised_legacy_reversal
from stockledger.models import (
    QUARANTINE_KINDS,
    TRANSIT_KINDS,
    QuarantineStock,
    StockLedgerEntry,
    StockOnHand,
    merch_dims,
)


class UnreversibleMovement(Exception):
    """A leg this module cannot mirror back into the bucket it came out of.

    Only the in-transit kinds reach it. A transit leg's bucket is keyed by the
    *transfer*, not by a store, and the transfer family writes and unwinds it
    itself (`outbound.posting`) - so a generic mirror here would append a leg the
    in-transit projection never saw. Refusing is the point: the alternative is a
    cancel that looks complete and leaves stock in a bucket nobody is counting.
    """


class ZeroValueMovement(Exception):
    """Stock was asked to move at no value (Rule 5).

    A movement with no cost of record is not a cheap movement, it is an unpriced
    one: post it and the books say pieces left the building for nothing, and no
    later document can tell that apart from a genuine giveaway. The caller decides
    what to do about it - the sale, for instance, holds the line back as deferred
    costing rather than refusing the bill - but nobody gets to write the zero.
    """


def _append_leg(
    *,
    store: Any,
    gstin: Any,
    sku_code: str,
    dims: dict[str, str],
    qty: int,
    unit_cost_paise: int,
    kind: str,
    doc_number: str,
    line_no: int,
    posted_by: Any = None,
) -> StockLedgerEntry:
    if not unit_cost_paise or unit_cost_paise <= 0:
        raise ZeroValueMovement(
            f"{sku_code} on {doc_number} carries no unit cost, so this movement cannot "
            "be valued. Stock never moves at zero value."
        )
    return StockLedgerEntry.objects.create(
        store=store,
        gstin=gstin,
        sku_code=sku_code,
        **dims,
        qty=qty,
        amount=qty * unit_cost_paise,
        kind=kind,
        doc_number=doc_number,
        line_no=line_no,
        posted_by=posted_by if getattr(posted_by, "is_authenticated", False) else None,
    )


def post_on_hand_movement(
    *,
    store: Any,
    gstin: Any,
    sku_code: str,
    source: Any,
    qty: int,
    unit_cost_paise: int,
    kind: str,
    doc_number: str,
    line_no: int,
    posted_by: Any = None,
) -> StockLedgerEntry:
    """Append an at-location leg and move `StockOnHand` by it.

    `qty` is signed the way the ledger is signed: negative leaves the shelf.
    `source` is anything carrying the seven merchandising dims (a document line,
    a cohort, another ledger row); they are snapshotted onto the leg so the ledger
    stays self-describing (Rule 9).

    The projection row is *locked* before its read-modify-write. Two tills cannot
    race here - one POS per store - but a sale and a transfer receipt can, and
    without the lock one of the two updates is silently lost and the projection
    starts disagreeing with the ledger it is a cache of. A count below zero is
    allowed on purpose: a store whose local count is wrong still sells the piece
    in its hand, and the next stocktake reconciles (grill Q5).
    """
    dims = merch_dims(source)
    entry = _append_leg(
        store=store,
        gstin=gstin,
        sku_code=sku_code,
        dims=dims,
        qty=qty,
        unit_cost_paise=unit_cost_paise,
        kind=kind,
        doc_number=doc_number,
        line_no=line_no,
        posted_by=posted_by,
    )
    StockOnHand.objects.get_or_create(
        store=store,
        sku_code=sku_code,
        defaults={"gstin": gstin, **dims, "net_qty": 0, "net_value_paise": 0},
    )
    row = StockOnHand.objects.select_for_update().get(store=store, sku_code=sku_code)
    row.net_qty += entry.qty
    row.net_value_paise = int(row.net_value_paise or 0) + int(entry.amount or 0)
    if entry.qty > 0:  # an inward is the freshest description of the piece
        for field, value in dims.items():
            setattr(row, field, value)
        row.gstin = gstin
    row.save()
    return entry


def post_quarantine_movement(
    *,
    store: Any,
    gstin: Any,
    sku_code: str,
    source: Any,
    qty: int,
    unit_cost_paise: int,
    kind: str,
    doc_number: str,
    line_no: int,
    posted_by: Any = None,
) -> StockLedgerEntry:
    """Append a quarantine leg and move the `QuarantineStock` bucket by it.

    Quarantine is a state in the ledger, not a flag on a stock row, so a damaged
    piece coming back over the counter enters here directly and never touches the
    sellable shelf. A bucket that reaches zero leaves no row, matching what the
    rebuild command would produce from the same legs.
    """
    dims = merch_dims(source)
    entry = _append_leg(
        store=store,
        gstin=gstin,
        sku_code=sku_code,
        dims=dims,
        qty=qty,
        unit_cost_paise=unit_cost_paise,
        kind=kind,
        doc_number=doc_number,
        line_no=line_no,
        posted_by=posted_by,
    )
    QuarantineStock.objects.get_or_create(
        store=store,
        sku_code=sku_code,
        defaults={"gstin": gstin, **dims, "qty": 0, "value_paise": 0},
    )
    bucket = QuarantineStock.objects.select_for_update().get(store=store, sku_code=sku_code)
    bucket.qty += entry.qty
    bucket.value_paise = int(bucket.value_paise or 0) + int(entry.amount or 0)
    if entry.qty > 0:
        bucket.marked_by = entry.posted_by
        bucket.marked_at = entry.created_at
    if bucket.qty == 0:
        bucket.delete()
    else:
        bucket.save()
    return entry


def reverse_movements(*, doc_number: str, posted_by: Any = None) -> list[StockLedgerEntry]:
    """Mirror every stock leg a document wrote, back into the bucket it came from.

    The stock half of a document's cancel (#220). The ledger is append-only by DB
    trigger, so the pieces come back the only way they can: a new leg of the same
    kind with the sign turned round, folded into the same projection the original
    moved. Same kind rather than a `…_reversal` kind of its own, because kind is
    what says *which bucket* - `rebuild_stock_on_hand` places a leg by its kind and
    sums the quantities - so a mirror that renamed itself would rebuild into a
    bucket the original never touched.

    The unit cost is read back off the original leg rather than re-derived, which
    is the same argument that freezes cost on a sale line: a cohort re-priced
    between the sale and its cancel would otherwise put the pieces back at
    today's cost and strand the difference for ever, with the trial balance still
    at nought.

    Written under the document's own number, so the reversal reads as that
    document being undone rather than as a movement of its own.
    """
    originals = list(StockLedgerEntry.objects.filter(doc_number=doc_number).order_by("id"))
    unreversible = sorted({e.kind for e in originals} & TRANSIT_KINDS)
    if unreversible:
        raise UnreversibleMovement(
            f"{doc_number} moved stock in transit ({', '.join(unreversible)}), which is "
            "the transfer family's own bucket to unwind, not this cascade's."
        )
    mirrors = []
    with authorised_legacy_reversal("reverse a cancelled legacy stock document"):
        for original in originals:
            if not original.qty:
                continue
            mover = (
                post_quarantine_movement
                if original.kind in QUARANTINE_KINDS
                else post_on_hand_movement
            )
            mirrors.append(
                mover(
                    store=original.store,
                    gstin=original.gstin,
                    sku_code=original.sku_code,
                    source=original,
                    qty=-original.qty,
                    # `amount` is `qty × unit_cost` by construction, so this divides
                    # exactly; both are negated together, which leaves the rate positive.
                    unit_cost_paise=int(original.amount or 0) // original.qty,
                    kind=original.kind,
                    doc_number=original.doc_number,
                    line_no=original.line_no,
                    posted_by=posted_by,
                )
            )
    return mirrors
