"""What cancelling a bill or a return actually has to undo (#220).

A counter document leaves marks in five places, and until this module `cancel()`
touched none of them: it walked the row to `cancelled` and stopped. So a bill's
legs stayed in the general ledger, its pieces stayed off the shelf, the store's
drawer still showed the money, the brand was still owed for a piece nobody sold,
and the credit note the customer walked out with stayed spendable for money the
books now said had never been given back.

The five, in the order they are undone:

1. **the value ledger** - mirrored by the kernel (`core.reversal`), which is where
   the append-only law is kept: nothing is edited, a negated voucher is appended
   under the same document number,
2. **the stock ledger** - the same shape, in `stockledger.projections`: the pieces
   go back to the bucket they came out of, at the cost frozen on the original leg,
3. **the cash subledger** - the collections un-taken, because books-health checks
   the drawer against the GL's cash control account and would otherwise report a
   drift that is really a cancel half-done,
4. **the vendor subledger** - the SOR/consignment accrual un-raised, for the same
   reason against the payable control account,
5. **the credit note** - voided, so a customer is not left holding a live note for
   a refund the books say never happened.

And one thing deliberately *not* undone: the returnable quantity on the original
line, which `sell.services.refunds` already frees by excluding cancelled documents.
That is correct - the piece is back with the customer and their receipt - and it is
also exactly why step 5 matters. Freeing the quantity without voiding the note
would let one piece be returned twice, for two live notes: the first still
spendable, the second issued against a line the books had just re-opened.

**The one branch that refuses.** A note that has already been *partly spent* is not
this module's decision. The money left the counter with a customer who has used
it, and taking it back is a ruling on the same ground as the CA-gated no-reposting
rule - so a cancel that would have to withdraw a spent note is refused by name,
rather than guessed at (#291, #290 item 5).
"""

from __future__ import annotations

from typing import Any

from django.db import models

from core.documents import DocStatus
from core.reversal import ReversalRefused, reverse_value_legs
from finledger.posting import reverse_sale_collections, reverse_sale_sor_liability
from sell.alteration_models import AlterationJob
from sell.models import CreditNote, DeferredCosting, Sale
from sell.reservation_models import CustomerReservation
from sell.services.movements import SellDocument
from sell.special_order_models import SpecialOrder
from stockledger.projections import reverse_movements


def reverse_sell_document(doc: SellDocument, actor: Any = None) -> dict[str, int]:
    """Undo every mark a bill or a standalone return left. Returns what moved.

    Runs inside `Document.cancel()`'s transaction, so any refusal below leaves the
    document submitted and every posting exactly where it was - a cancel is never
    half-applied.
    """
    number = doc.doc_number or ""
    notes = _live_notes(doc)
    _refuse_if_a_note_has_been_spent(doc, notes)
    _refuse_if_it_collected_a_reservation(doc)
    _refuse_if_on_a_job_card(doc)

    summary = {
        "value_legs": len(reverse_value_legs(doc, actor)),
        "stock_legs": len(reverse_movements(doc_number=number, posted_by=actor)),
        "collections": reverse_sale_collections(number, actor),
        "vendor_accruals": reverse_sale_sor_liability(number, actor),
        "credit_notes_voided": _void(notes, actor),
        "deferred_costings_dropped": _drop_deferred(doc),
    }
    return summary


def _refuse_if_it_collected_a_reservation(doc: SellDocument) -> None:
    """Refuse to cancel a bill that collected a customer reservation (ticket 20).

    The bill released the reservation's pieces and used any advance it held. Undoing it
    would owe the advance back to a reservation already closed, with the pieces
    sold. What a cancel does to a collected reservation is not decided, so the
    bill stands until it is.
    """
    if isinstance(doc, Sale) and CustomerReservation.objects.filter(sale=doc).exists():
        raise ReversalRefused(
            f"{doc.doc_number} collected a customer reservation, so "
            "it cannot be cancelled here. Take the pieces back as a return instead."
        )
    # Ticket 19: a bill that spent a gift voucher. Giving the balance back to a
    # voucher that may since have been spent again or expired is not decided, so
    # the bill stands (baseline B306); take the pieces back as a return instead.
    if isinstance(doc, Sale) and doc.tenders.filter(gift_voucher__isnull=False).exists():
        raise ReversalRefused(
            f"{doc.doc_number} was paid with a gift voucher, so it cannot be cancelled "
            "here. Take the pieces back as a return instead."
        )
    # Ticket 21: the same for a special order's collection bill, for the same reason.
    if isinstance(doc, Sale) and SpecialOrder.objects.filter(sale=doc).exists():
        raise ReversalRefused(
            f"{doc.doc_number} collected a special order, so it cannot be cancelled "
            "here. Take the piece back as a return instead."
        )


def _refuse_if_on_a_job_card(doc: SellDocument) -> None:
    """Refuse to cancel a bill a live alteration job card points at (ticket 22).

    An open job card holds the garment for the customer and may carry the charge
    paid on this bill; cancelling the bill under it would leave a job for goods
    nobody bought. Hand it over or cancel the job card first. A finished job card
    is history and blocks nothing; what a cancel does to its money waits on OQ-49.
    """
    if not isinstance(doc, Sale):
        return
    jobs = AlterationJob.objects.filter(
        models.Q(garment_line__sale=doc) | models.Q(charge_line__sale=doc),
        status__in=[
            AlterationJob.Status.RECEIVED,
            AlterationJob.Status.WITH_TAILOR,
            AlterationJob.Status.READY,
        ],
    )
    job = jobs.order_by("ref").first()
    if job is not None:
        raise ReversalRefused(
            f"{doc.doc_number} is on open alteration job card {job.ref}, so it cannot be "
            "cancelled here. Hand it over or cancel the job card first."
        )


def _live_notes(doc: SellDocument) -> list[CreditNote]:
    """The credit notes this document issued that are still standing.

    Both sources are read through the one related name the two document types
    share, so a note issued at the end of a bill and one issued at the end of a
    standalone return are found by the same query.
    """
    return list(doc.credit_notes_issued.exclude(docstatus=DocStatus.CANCELLED))


def _refuse_if_a_note_has_been_spent(doc: SellDocument, notes: list[CreditNote]) -> None:
    """Refuse the whole cancel if any note it issued has been spent against a bill.

    "Spent" means spent on a bill that still stands: a redemption on a sale that
    has itself been cancelled did not happen (`CreditNote.spent_paise` says so),
    and blocking on it would strand a document behind a spend the books have
    already unwound.
    """
    for note in notes:
        if note.spent_paise:
            raise ReversalRefused(
                f"{doc.doc_number} issued credit note {note.doc_number}, and "
                f"{note.spent_paise} paise of it has already been spent at the counter. "
                "What a cancel does to a partly-spent note is an open money ruling "
                "(#291, on the same ground as the CA-gated no-reposting rule), so this "
                "document cannot be cancelled until that is settled."
            )


def _void(notes: list[CreditNote], actor: Any) -> int:
    """Cancel each unspent note in the same transaction as the document.

    A note posts no value of its own - the liability is raised by the bill or
    return that issued it, and reversed above - so its own reversal is empty and
    all this does is walk it to `cancelled`, which is what `CreditNote.status`
    reads to stop it being spendable.
    """
    for note in notes:
        note.cancel(actor)
    return len(notes)


def _drop_deferred(doc: SellDocument) -> int:
    """Close the costing queue rows this document's lines are waiting in.

    A line sold before its paperwork has no cost event yet and no stock leg
    either, so there is nothing to reverse - but there is something to stop. Left
    waiting, the row would be released by the next PT that prices the cohort and
    would post a cost event and take a piece off a shelf, for a bill the books say
    never happened. The sweep also refuses to act on a cancelled bill's rows; this
    is the other half, so the Dashboard's count and the daily check's ageing stop
    carrying a bill that is gone.
    """
    if not isinstance(doc, Sale):  # a standalone return never queued a costing row
        return 0
    return DeferredCosting.objects.filter(
        sale_line__sale=doc, status=DeferredCosting.Status.WAITING
    ).update(status=DeferredCosting.Status.CANCELLED)
