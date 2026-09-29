"""A customer's advance for goods not yet supplied (tickets 20 and 21).

A customer reservation (ST-ORD-1) and a special order (ST-ORD-2) take their
advance the same way, so the money lives in one place: an RV-series receipt
voucher when it is taken, then one ``AdvanceMovement`` row per change - used on
the bill, refunded, forfeited - never an edited total. The balance is the sum.

The books (baseline B104): taking it posts Dr tender account / Cr
``CUSTOMER_ADVANCE`` under the RV number, no GST (Notification 66/2017-CT), with
a cash-ledger receipt. A refund is the reverse, paid the way it came, with a
cash-ledger payment. A forfeiture moves it to ``FORFEITED_ADVANCE``, no GST.

The *holder* is the reservation or the special order; both carry ``ref``,
``voucher`` and ``advance_movements``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.utils import timezone

from core.gl import GLAccount
from core.posting import cr, dr, post_entries
from finledger.models import CashLedgerEntry
from finledger.posting import account_for_mode
from masters.document_series import DocumentSeries, issue_number
from masters.models import Store
from sell.reservation_models import AdvanceMovement, CustomerReservation, ReceiptVoucher

TENDER_MODES = ("cash", "card", "upi")
#: The advance's tender account in the value books (as a bill's tender posts).
TENDER_ACCOUNT = {
    "cash": GLAccount.CASH,
    "card": GLAccount.CARD_CLEARING,
    "upi": GLAccount.UPI_CLEARING,
}

#: Said on every voucher that carries an advance.
NO_GST_NOTE = "Advance for goods. No GST is charged on it (Notification 66/2017-CT)."


def _owner(holder: Any) -> dict[str, Any]:
    """The foreign key a voucher or movement row names its holder by."""
    if isinstance(holder, CustomerReservation):
        return {"reservation": holder}
    return {"special_order": holder}


def _label(holder: Any) -> str:
    return "Reservation" if isinstance(holder, CustomerReservation) else "Special order"


def user_of(actor: Any) -> Any:
    return actor if getattr(actor, "is_authenticated", False) else None


def balance_paise(holder: Any) -> int:
    """What the advance still holds for the customer: received less every use."""
    total = 0
    for row in holder.advance_movements.all():
        total += (
            row.amount_paise if row.kind == AdvanceMovement.Kind.RECEIVED else -row.amount_paise
        )
    return total


def outcome(holder: Any, *, open_: bool) -> str:
    """What became of the advance, in one word the screen shows.

    ``open_`` - the reservation or order has not ended, so money held is just held.
    """
    kinds = {row.kind for row in holder.advance_movements.all()}
    if AdvanceMovement.Kind.RECEIVED not in kinds:
        return "none"
    if not open_ and balance_paise(holder) > 0:
        return "refund_due"
    if AdvanceMovement.Kind.FORFEITED in kinds:
        return "forfeited"
    if AdvanceMovement.Kind.REFUNDED in kinds and AdvanceMovement.Kind.USED not in kinds:
        return "refunded"
    if AdvanceMovement.Kind.USED in kinds:
        return "used"
    return "held"


def take(
    store: Store,
    actor: Any,
    holder: Any,
    *,
    amount: int,
    mode: str,
    reference: str,
    on: date,
) -> ReceiptVoucher:
    """The receipt voucher, the customer advance it raises, and the drawer's receipt."""
    label = _label(holder)
    number = issue_number(
        series=DocumentSeries.RECEIPT_VOUCHER,
        site=store,
        on=on,
        document_type="receipt_voucher",
        document_ref=str(holder.pk),
    )
    voucher = ReceiptVoucher.objects.create(
        **_owner(holder),
        store=store,
        number=number.number,
        issued_on=on,
        amount_paise=amount,
        mode=mode,
        reference=reference[:64],
        created_by=actor,
    )
    voucher.post()
    post_entries(
        voucher,
        [
            dr(TENDER_ACCOUNT[mode], amount, memo=f"{label} advance {holder.ref}"),
            cr(GLAccount.CUSTOMER_ADVANCE, amount, memo=f"Held for {holder.ref}"),
        ],
        posted_by=actor,
    )
    CashLedgerEntry.objects.create(
        account=account_for_mode(mode),
        amount=amount,
        kind=CashLedgerEntry.Kind.RECEIPT,
        doc_number=voucher.number,
        description=f"{label} advance {holder.ref}",
        mode=mode,
        posted_by=user_of(actor),
    )
    AdvanceMovement.objects.create(
        **_owner(holder),
        kind=AdvanceMovement.Kind.RECEIVED,
        mode=mode,
        amount_paise=amount,
        reference=reference[:64],
        at=timezone.now(),
        by=user_of(actor),
    )
    return voucher


def pay_back(holder: Any, actor: Any, amount: int) -> None:
    """Refund ``amount`` the way the advance was paid: the drawer or the machine pays out."""
    voucher = holder.voucher
    mode = voucher.mode
    post_entries(
        voucher,
        [
            dr(GLAccount.CUSTOMER_ADVANCE, amount, memo=f"Advance refunded {holder.ref}"),
            cr(TENDER_ACCOUNT[mode], amount, memo=f"Advance refunded {holder.ref}"),
        ],
        posted_by=actor,
    )
    CashLedgerEntry.objects.create(
        account=account_for_mode(mode),
        amount=amount,
        kind=CashLedgerEntry.Kind.PAYMENT,
        doc_number=voucher.number,
        description=f"{_label(holder)} advance refunded {holder.ref}",
        mode=mode,
        posted_by=user_of(actor),
    )
    AdvanceMovement.objects.create(
        **_owner(holder),
        kind=AdvanceMovement.Kind.REFUNDED,
        mode=mode,
        amount_paise=amount,
        at=timezone.now(),
        by=user_of(actor),
    )


def forfeit(holder: Any, actor: Any, amount: int, *, why: str) -> None:
    """Keep ``amount`` by the holder's own terms. Not a sale, so no GST."""
    post_entries(
        holder.voucher,
        [
            dr(GLAccount.CUSTOMER_ADVANCE, amount, memo=f"Advance forfeited {holder.ref}"),
            cr(GLAccount.FORFEITED_ADVANCE, amount, memo=f"{why} {holder.ref}"),
        ],
        posted_by=actor,
    )
    AdvanceMovement.objects.create(
        **_owner(holder),
        kind=AdvanceMovement.Kind.FORFEITED,
        amount_paise=amount,
        at=timezone.now(),
        by=user_of(actor),
    )


def use(holder: Any, actor: Any, amount: int, sale: Any) -> None:
    """``amount`` of the advance paid towards ``sale`` as an ``advance`` tender."""
    AdvanceMovement.objects.create(
        **_owner(holder),
        kind=AdvanceMovement.Kind.USED,
        amount_paise=amount,
        sale=sale,
        at=timezone.now(),
        by=user_of(actor),
    )
