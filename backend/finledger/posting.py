"""Posting services for the vendor & cash ledgers.

These subledgers are append-only running balances *and* — since the F1 hardening —
every event also posts a **balanced value voucher** through the single kernel
posting engine (`core.post_entries`). So the value GL is the one book of record:
its `VENDOR_PAYABLE` control account equals the vendor subledger sum, and its
`CASH` control account equals the cash subledger sum. A vendor payment now clears
the payable against cash (`Dr VENDOR_PAYABLE / Cr CASH`) in the GL, not just in the
subledgers — the trial balance ties across inbound *and* money-out.

Gap-free voucher numbers are minted from `core.VoucherSeries` under a synthetic
store_code 'HO' (head office) — these ledgers are not store-scoped. Vendor doc_type
'VEND', cash doc_type 'CASH'. Corrections are append-only reversing rows (subledger)
plus the negated GL mirror.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from django.db import transaction

from core.documents import VoucherSeries
from core.gl import GLAccount, GLEntry
from core.posting import Leg, PostingRef, cr, dr, post_entries
from finledger.models import CashLedgerEntry, PartnerLedgerEntry, VendorLedgerEntry

HO_CODE = "HO"
VENDOR_DOC = "VEND"
CASH_DOC = "CASH"
PARTNER_DOC = "PSET"

#: Which value-GL control account each cash-ledger account rolls up into.
#:
#: Cash and bank share one control account because the GL does not split them yet;
#: card and UPI have their own, because the customer has paid and the bank has not
#: settled, and the gap between the two is what the daily settlement reconciliation
#: is for. One map, used by everything that writes a cash row *and* by the
#: books-health tie - so the F1 reconciliation can be checked account by account
#: instead of as one lump total that a mis-file inside the family would hide.
CASH_CONTROL_ACCOUNTS: dict[str, str] = {
    CashLedgerEntry.Account.CASH: GLAccount.CASH,
    CashLedgerEntry.Account.BANK: GLAccount.CASH,
    CashLedgerEntry.Account.CARD: GLAccount.CARD_CLEARING,
    CashLedgerEntry.Account.UPI: GLAccount.UPI_CLEARING,
}


def gl_control_for(account: str) -> str:
    """The value account a cash-ledger account answers to.

    An account nobody has mapped answers to CASH, which is where every cash row
    went before this map existed - a row that fell through would otherwise have no
    control account at all and would break the tie rather than merely be filed
    coarsely.
    """
    return CASH_CONTROL_ACCOUNTS.get(account, GLAccount.CASH)


#: The `mode` field is free-text/descriptive (what the payer/payee said "how"
#: they paid, e.g. the dropdown on the Partner Settlement or Vendor Payment
#: form); this is the one place that turns "how" into "which bucket" for the
#: cash ledger, so a bank-transfer or UPI payment actually lands in BANK/UPI
#: instead of silently defaulting to CASH just because the caller only ever
#: passed `mode`, never a separate `account`. Feeds both the Daily Cash
#: Dashboard's per-account split and Bank Reconciliation's "only bank/UPI/
#: cheque money can appear on a bank statement" scope.
MODE_TO_ACCOUNT: dict[str, str] = {
    "bank": CashLedgerEntry.Account.BANK,
    "neft": CashLedgerEntry.Account.BANK,
    "rtgs": CashLedgerEntry.Account.BANK,
    "cheque": CashLedgerEntry.Account.BANK,
    "upi": CashLedgerEntry.Account.UPI,
    "card": CashLedgerEntry.Account.CARD,
}


def account_for_mode(mode: str) -> str:
    return MODE_TO_ACCOUNT.get((mode or "").strip().lower(), CashLedgerEntry.Account.CASH)


class AlreadyReversedError(Exception):
    """A ledger entry that already has a live reversal cannot be reversed again
    (a second reversal would over-credit the vendor / over-pay the cash account)."""


def financial_year(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def rupees_to_paise(value: object) -> int:
    """Tolerant rupee→paise (accepts str/Decimal/number; never lets float into money)."""
    if value in (None, ""):
        return 0
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0


def _allocate(doc_type: str) -> str:
    fy = financial_year(date.today())
    VoucherSeries.objects.get_or_create(fy=fy, store_code=HO_CODE, doc_type=doc_type)
    _, number = VoucherSeries.allocate(fy=fy, store_code=HO_CODE, doc_type=doc_type)
    return number


def _user(user: Any) -> Any:
    return user if getattr(user, "is_authenticated", False) else None


# --- GL bridge (F1: one balanced book of record) --------------------------


def _post_gl(doc_type: str, doc_number: str, legs: list[Leg], user: Any) -> None:
    """Post one balanced GL voucher for a finledger event. Vendor/cash are HO-level,
    so no store/gstin dims. Balanced-or-fail via the single kernel posting engine."""
    ref = PostingRef(doc_type=doc_type, doc_number=doc_number, posted_by=_user(user))
    post_entries(ref, legs, posted_by=_user(user))


def _reverse_gl(orig_doc_number: str, rev_doc_number: str, doc_type: str, user: Any) -> None:
    """Append the negated mirror of a finledger event's GL voucher, if it had one.

    A subledger-only bill (``gl=False``) has no GL leg on its VEND number (its caller
    books the payable itself), and a payment's paired cash row has no GL leg on its
    CASH number (the GL cash leg lives on the vendor voucher) — in both cases there is
    nothing to mirror here, so this is a safe no-op that never double-reverses."""
    source = list(GLEntry.objects.filter(doc_number=orig_doc_number))
    if not source:
        return
    ref = PostingRef(doc_type=doc_type, doc_number=rev_doc_number, posted_by=_user(user))
    legs = [
        Leg(
            account=g.account,
            amount=-g.amount,
            party_type=g.party_type,
            party_code=g.party_code,
            against_voucher=g.against_voucher,
            memo=f"reversal of {orig_doc_number}",
        )
        for g in source
    ]
    post_entries(ref, legs, posted_by=_user(user))


# --- Vendor ledger ---------------------------------------------------------


@transaction.atomic
def post_vendor_bill(
    vendor: Any,
    amount_paise: int,
    description: str,
    user: Any,
    *,
    booking: Any = None,
    reference: str = "",
    gl: bool = True,
) -> VendorLedgerEntry:
    """+amount: increases what we owe the vendor.

    ``gl=True`` (manual bill) also books the balanced value voucher
    Dr SUSPENSE / Cr VENDOR_PAYABLE, so the vendor subledger and the GL payable
    control account stay equal. A caller that books the payable itself in the value
    GL passes ``gl=False``; the finledger bill is then subledger detail only, so the
    payable is never double-booked.
    """
    entry = VendorLedgerEntry.objects.create(
        vendor=vendor,
        amount=amount_paise,
        kind=VendorLedgerEntry.Kind.BILL,
        doc_number=_allocate(VENDOR_DOC),
        description=description,
        reference=reference,
        booking=booking,
        posted_by=_user(user),
    )
    if gl and amount_paise:
        _post_gl(
            VENDOR_DOC,
            entry.doc_number,
            [
                dr(GLAccount.SUSPENSE, amount_paise, memo=description or "vendor bill"),
                cr(
                    GLAccount.VENDOR_PAYABLE,
                    amount_paise,
                    party_type="vendor",
                    party_code=vendor.code,
                    against_voucher=reference,
                ),
            ],
            user,
        )
    return entry


@transaction.atomic
def post_vendor_payment(
    vendor: Any,
    amount_paise: int,
    description: str,
    user: Any,
    *,
    mode: str = "",
    account: str = "CASH",
    also_cash: bool = True,
) -> VendorLedgerEntry:
    """−amount on the vendor ledger; optionally a paired cash-out on the cash ledger.

    Also books ONE balanced GL voucher Dr VENDOR_PAYABLE / Cr CASH (or Cr SUSPENSE if
    the payment isn't paired to a cash movement). The paired cash subledger row is
    detail only — the GL cash leg lives on this voucher, so cash is booked once."""
    entry = VendorLedgerEntry.objects.create(
        vendor=vendor,
        amount=-abs(amount_paise),
        kind=VendorLedgerEntry.Kind.PAYMENT,
        doc_number=_allocate(VENDOR_DOC),
        description=description,
        mode=mode,
        posted_by=_user(user),
    )
    if also_cash and amount_paise:
        CashLedgerEntry.objects.create(
            account=account,
            amount=-abs(amount_paise),
            kind=CashLedgerEntry.Kind.PAYMENT,
            doc_number=_allocate(CASH_DOC),
            description=description or f"Payment to {vendor.name}",
            mode=mode,
            vendor=vendor,
            link_doc=entry.doc_number,
            posted_by=_user(user),
        )
    if amount_paise:
        # Paid out of whichever account the cash row named, so the payment relieves
        # the same control account the subledger row does.
        credit_account = gl_control_for(account) if also_cash else GLAccount.SUSPENSE
        _post_gl(
            VENDOR_DOC,
            entry.doc_number,
            [
                dr(
                    GLAccount.VENDOR_PAYABLE,
                    abs(amount_paise),
                    party_type="vendor",
                    party_code=vendor.code,
                ),
                cr(
                    credit_account,
                    abs(amount_paise),
                    memo=description or f"Payment to {vendor.name}",
                ),
            ],
            user,
        )
    return entry


@transaction.atomic
def reverse_vendor_entry(entry: VendorLedgerEntry, user: Any) -> VendorLedgerEntry:
    """Append a negative mirror; also reverse a paired cash-out if one exists, and
    append the negated GL mirror of the entry's value voucher.

    Refuses to reverse a reversal, or to reverse the same entry twice (a second
    reversal would over-credit the vendor)."""
    if entry.kind == VendorLedgerEntry.Kind.REVERSAL:
        raise AlreadyReversedError("a reversal cannot itself be reversed")
    if VendorLedgerEntry.objects.filter(reverses=entry).exists():
        raise AlreadyReversedError(f"{entry.doc_number} has already been reversed")
    number = _allocate(VENDOR_DOC)
    rev = VendorLedgerEntry.objects.create(
        vendor=entry.vendor,
        amount=-entry.amount,
        kind=VendorLedgerEntry.Kind.REVERSAL,
        doc_number=number,
        description=f"Reversal of {entry.doc_number}",
        booking=entry.booking,
        reverses=entry,
        posted_by=_user(user),
    )
    for cash in CashLedgerEntry.objects.filter(
        link_doc=entry.doc_number, kind=CashLedgerEntry.Kind.PAYMENT
    ):
        if CashLedgerEntry.objects.filter(reverses=cash).exists():
            continue  # this paired cash-out was already reversed
        CashLedgerEntry.objects.create(
            account=cash.account,
            amount=-cash.amount,
            kind=CashLedgerEntry.Kind.REVERSAL,
            doc_number=_allocate(CASH_DOC),
            description=f"Reversal of {cash.doc_number}",
            mode=cash.mode,
            vendor=cash.vendor,
            link_doc=number,
            reverses=cash,
            posted_by=_user(user),
        )
    _reverse_gl(entry.doc_number, number, VENDOR_DOC, user)
    return rev


def post_sale_sor_liability(
    vendor: Any, amount_paise: int, description: str, user: Any, *, reference: str = ""
) -> VendorLedgerEntry | None:
    """The SOR/consignment liability a *bill* raises, as vendor-subledger detail.

    Brand-owned stock raises nothing when it is received:
    the goods were never ours, so what we owe the brand is not known until a piece
    sells. It accrues here, at the settlement rate frozen on the piece - never at
    the price the customer happened to pay for it.

    Detail only, deliberately: the Sale's own cost event books the payable in the
    value GL (Dr COGS / Cr VENDOR_PAYABLE), so this row must not book it a second
    time. What it buys is that the vendor's account shows the accrual, and the
    subledger sum still equals the GL payable control account (F1).

    A negative amount is an exchange handing a brand-owned piece back inside the
    bill: what we owe falls, so the row is a reversal of accrual, not a bill.
    """
    if vendor is None or not amount_paise:
        return None
    return VendorLedgerEntry.objects.create(
        vendor=vendor,
        amount=amount_paise,
        kind=(VendorLedgerEntry.Kind.BILL if amount_paise > 0 else VendorLedgerEntry.Kind.REVERSAL),
        doc_number=_allocate(VENDOR_DOC),
        description=description,
        reference=reference,
        posted_by=_user(user),
    )


def post_sale_collection(
    *,
    account: str,
    amount_paise: int,
    doc_number: str,
    description: str,
    mode: str,
    user: Any,
) -> CashLedgerEntry:
    """One tender on a bill, as a cash-ledger receipt row.

    Projection only, and that is the whole point of it not going through
    `post_cash_movement`: the Sale's money event has already booked this tender in
    the value GL (Dr CASH / CARD_CLEARING / UPI_CLEARING), and that helper would
    book its own balanced voucher against SUSPENSE - the same rupees twice.

    The row carries the *bill's* number rather than a CASH-series one, because it
    is not an event of its own: it is what the bill collected, and the store's cash
    summary and the D4 three-way audit both want to get from a collection back to
    the bill that made it.
    """
    return CashLedgerEntry.objects.create(
        account=account,
        amount=abs(amount_paise),
        kind=CashLedgerEntry.Kind.RECEIPT,
        doc_number=doc_number,
        description=description,
        mode=mode,
        posted_by=_user(user),
    )


@transaction.atomic
def reverse_sale_collections(doc_number: str, user: Any) -> int:
    """Un-take every collection a bill's tenders wrote. Returns how many.

    The subledger half of cancelling a bill (#220). The value GL's cash, card and
    UPI legs are mirrored by the kernel reversal; these rows are the detail behind
    them, and the books-health tie is checked **per control account**
    (`finledger.health`), so leaving them standing would report the store's drawer
    as holding money the general ledger had already given back.

    The mirror keeps the bill's own number for the reason the receipt does: it is
    not an event of its own, and the cash summary and the D4 three-way audit both
    want to get from a row back to the bill behind it. `reverses` is what says
    which row is the undoing, and what would refuse a second one.
    """
    count = 0
    for receipt in CashLedgerEntry.objects.filter(
        doc_number=doc_number, kind=CashLedgerEntry.Kind.RECEIPT
    ):
        if CashLedgerEntry.objects.filter(reverses=receipt).exists():
            continue  # already reversed — never double-reverse
        CashLedgerEntry.objects.create(
            account=receipt.account,
            amount=-receipt.amount,
            kind=CashLedgerEntry.Kind.REVERSAL,
            doc_number=receipt.doc_number,
            description=f"Reversal of {receipt.doc_number}",
            mode=receipt.mode,
            vendor=receipt.vendor,
            reverses=receipt,
            posted_by=_user(user),
        )
        count += 1
    return count


@transaction.atomic
def reverse_sale_sor_liability(reference: str, user: Any) -> int:
    """Un-accrue what a bill said we owed a brand. Returns how many rows.

    The other subledger a bill writes into. The GL payable is mirrored by the
    kernel reversal, and the vendor subledger sum must equal that control account
    (F1) - so this is not detail that can be skipped, it is half of an equality
    the Books Health screen checks on every load.

    `kind` follows the sign the way `post_sale_sor_liability` sets it, so a mirror
    of an accrual reads as a reversal and a mirror of an exchange's give-back reads
    as the bill it re-raises.
    """
    if not reference:  # an empty reference would match every unreferenced row
        return 0
    count = 0
    for entry in VendorLedgerEntry.objects.filter(reference=reference).exclude(
        kind=VendorLedgerEntry.Kind.PAYMENT
    ):
        if VendorLedgerEntry.objects.filter(reverses=entry).exists():
            continue  # already reversed — never double-reverse
        amount = -entry.amount
        VendorLedgerEntry.objects.create(
            vendor=entry.vendor,
            amount=amount,
            kind=(VendorLedgerEntry.Kind.BILL if amount > 0 else VendorLedgerEntry.Kind.REVERSAL),
            doc_number=_allocate(VENDOR_DOC),
            description=f"Reversal of {entry.doc_number}",
            reference=reference,
            reverses=entry,
            posted_by=_user(user),
        )
        count += 1
    return count


# --- Partner ledger ---------------------------------------------------------


@transaction.atomic
def post_partner_settlement(
    store: Any,
    amount_paise: int,
    description: str,
    user: Any,
    *,
    mode: str = "",
    account: str = "CASH",
    reference: str = "",
    also_cash: bool = True,
) -> PartnerLedgerEntry:
    """+amount: a payment received from a partner store, reducing what it owes
    (billed off `StoreTransfer.partner_billing_value_paise` — there is no BILL
    kind on this ledger to net against; see the model docstring).

    Optionally a paired cash-in on the cash ledger (``also_cash``). Also books
    ONE balanced GL voucher Dr CASH / Cr PARTNER_RECEIVABLE — but only when the
    chain's `BillingPolicy` actually posted the receivable in the first place
    (`GL_POSTING` mode): under `INFORMATIONAL` there is nothing in the GL to
    clear, so a settlement stays subledger-only too, exactly mirroring how the
    bill side already behaves (`outbound.posting._bill_partner_store`).

    Deferred import of `BillingPolicy`: `outbound` already imports
    `finledger.posting` (for vendor bills), so importing it back at module
    load time here would cycle.
    """
    from outbound.models import BillingPolicy

    entry = PartnerLedgerEntry.objects.create(
        store=store,
        amount=abs(amount_paise),
        kind=PartnerLedgerEntry.Kind.PAYMENT,
        doc_number=_allocate(PARTNER_DOC),
        description=description,
        reference=reference,
        mode=mode,
        posted_by=_user(user),
    )
    if also_cash and amount_paise:
        CashLedgerEntry.objects.create(
            account=account,
            amount=abs(amount_paise),
            kind=CashLedgerEntry.Kind.RECEIPT,
            doc_number=_allocate(CASH_DOC),
            description=description or f"Settlement from {store.name}",
            mode=mode,
            link_doc=entry.doc_number,
            posted_by=_user(user),
        )
    if amount_paise and BillingPolicy.current().mode == BillingPolicy.Mode.GL_POSTING:
        debit_account = gl_control_for(account) if also_cash else GLAccount.SUSPENSE
        _post_gl(
            PARTNER_DOC,
            entry.doc_number,
            [
                dr(
                    debit_account,
                    abs(amount_paise),
                    memo=description or f"Settlement from {store.name}",
                ),
                cr(
                    GLAccount.PARTNER_RECEIVABLE,
                    abs(amount_paise),
                    party_type="store",
                    party_code=store.code,
                    memo=f"Settlement from {store.code}",
                ),
            ],
            user,
        )
    return entry


@transaction.atomic
def reverse_partner_settlement(entry: PartnerLedgerEntry, user: Any) -> PartnerLedgerEntry:
    """Append a negative mirror; also reverse a paired cash-in if one exists, and
    append the negated GL mirror of the entry's value voucher (if any)."""
    if entry.kind == PartnerLedgerEntry.Kind.REVERSAL:
        raise AlreadyReversedError("a reversal cannot itself be reversed")
    if PartnerLedgerEntry.objects.filter(reverses=entry).exists():
        raise AlreadyReversedError(f"{entry.doc_number} has already been reversed")
    number = _allocate(PARTNER_DOC)
    rev = PartnerLedgerEntry.objects.create(
        store=entry.store,
        amount=-entry.amount,
        kind=PartnerLedgerEntry.Kind.REVERSAL,
        doc_number=number,
        description=f"Reversal of {entry.doc_number}",
        reverses=entry,
        posted_by=_user(user),
    )
    for cash in CashLedgerEntry.objects.filter(
        link_doc=entry.doc_number, kind=CashLedgerEntry.Kind.RECEIPT
    ):
        if CashLedgerEntry.objects.filter(reverses=cash).exists():
            continue  # this paired cash-in was already reversed
        CashLedgerEntry.objects.create(
            account=cash.account,
            amount=-cash.amount,
            kind=CashLedgerEntry.Kind.REVERSAL,
            doc_number=_allocate(CASH_DOC),
            description=f"Reversal of {cash.doc_number}",
            mode=cash.mode,
            link_doc=number,
            reverses=cash,
            posted_by=_user(user),
        )
    _reverse_gl(entry.doc_number, number, PARTNER_DOC, user)
    return rev


# --- Cash ledger -----------------------------------------------------------


@transaction.atomic
def post_cash_movement(
    direction: str,
    amount_paise: int,
    description: str,
    user: Any,
    *,
    account: str = "CASH",
    mode: str = "",
) -> CashLedgerEntry:
    is_in = direction == "in"
    entry = CashLedgerEntry.objects.create(
        account=account or "CASH",
        amount=abs(amount_paise) if is_in else -abs(amount_paise),
        kind=CashLedgerEntry.Kind.RECEIPT if is_in else CashLedgerEntry.Kind.PAYMENT,
        doc_number=_allocate(CASH_DOC),
        description=description,
        mode=mode,
        posted_by=_user(user),
    )
    if amount_paise:
        # Standalone cash movement books against SUSPENSE — a holding account until it
        # is classified (to store collection / expense) in a later slice; keeps Σ=0.
        control = gl_control_for(entry.account)
        legs = (
            [
                dr(control, abs(amount_paise), memo=description),
                cr(GLAccount.SUSPENSE, abs(amount_paise), memo=description),
            ]
            if is_in
            else [
                dr(GLAccount.SUSPENSE, abs(amount_paise), memo=description),
                cr(control, abs(amount_paise), memo=description),
            ]
        )
        _post_gl(CASH_DOC, entry.doc_number, legs, user)
    return entry


@transaction.atomic
def reverse_cash_entry(entry: CashLedgerEntry, user: Any) -> CashLedgerEntry:
    if entry.kind == CashLedgerEntry.Kind.REVERSAL:
        raise AlreadyReversedError("a reversal cannot itself be reversed")
    if CashLedgerEntry.objects.filter(reverses=entry).exists():
        raise AlreadyReversedError(f"{entry.doc_number} has already been reversed")
    number = _allocate(CASH_DOC)
    rev = CashLedgerEntry.objects.create(
        account=entry.account,
        amount=-entry.amount,
        kind=CashLedgerEntry.Kind.REVERSAL,
        doc_number=number,
        description=f"Reversal of {entry.doc_number}",
        mode=entry.mode,
        vendor=entry.vendor,
        link_doc=entry.doc_number,
        reverses=entry,
        posted_by=_user(user),
    )
    _reverse_gl(entry.doc_number, number, CASH_DOC, user)
    return rev
