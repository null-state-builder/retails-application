"""Vendor (accounts-payable), Partner (accounts-receivable) and Cash ledgers —
append-only over `core.LedgerEntry`.

Sign conventions (so a running Σ(amount) IS the balance):
  VendorLedgerEntry  +amount = we owe more (a bill/credit), −amount = a payment.
                     Σ = outstanding payable to that vendor.
  PartnerLedgerEntry +amount = a payment received from the partner store.
                     Σ, subtracted from `partner_billing_value_paise` billed to
                     it, = what the partner store still owes (there is no BILL
                     kind here — see the class docstring).
  CashLedgerEntry    +amount = cash in (receipt),           −amount = cash out (payment).
                     Σ = cash on hand in that account.

Append-only (ADR-0004): every correction is a new today-dated reversing row, never
an edit/delete (ORM base + BEFORE UPDATE/DELETE trigger + REVOKE).
"""

from __future__ import annotations

from django.db import models

from core.base import TimeStampedModel
from core.ledger import LedgerEntry


class VendorLedgerEntry(LedgerEntry):
    class Kind(models.TextChoices):
        BILL = "bill", "Bill / purchase"
        PAYMENT = "payment", "Payment"
        REVERSAL = "reversal", "Reversal"

    vendor = models.ForeignKey(
        "vendors.Vendor", on_delete=models.PROTECT, related_name="ledger_entries"
    )
    kind = models.CharField(max_length=12, choices=Kind.choices)
    doc_number = models.CharField(max_length=128, db_index=True)
    description = models.CharField(max_length=240, blank=True, default="")
    reference = models.CharField(max_length=120, blank=True, default="")  # vendor invoice no etc.
    mode = models.CharField(max_length=24, blank=True, default="")  # cash / bank / upi (payments)
    booking = models.ForeignKey(
        "vendors.Booking",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vendor_entries",
    )
    posted_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    # The entry this row reverses (a REVERSAL points back at its BILL/PAYMENT). Lets us
    # reject a *second* reversal of the same source row (double-reversal → over-credit).
    reverses = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )

    class Meta:
        db_table = "finledger_vendor_entry"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.doc_number} · {self.vendor_id} · {self.amount}"


class PartnerLedgerEntry(LedgerEntry):
    """Payments received from a partner store, offset against what
    `outbound.PartnerDuesView` says it owes (billed at Purchase Price on every
    transfer it receives — `outbound.posting._bill_partner_store`).

    There is no BILL kind here: unlike the vendor ledger, the bill side of this
    account is not posted through this table at all — it is
    `StoreTransfer.partner_billing_value_paise`, computed once at dispatch. This
    ledger only ever records what came back in payment of that running total,
    so "what a partner still owes" is billed-off-transfers minus Σ(amount) here.
    """

    class Kind(models.TextChoices):
        PAYMENT = "payment", "Payment received"
        REVERSAL = "reversal", "Reversal"

    store = models.ForeignKey(
        "masters.Store", on_delete=models.PROTECT, related_name="partner_ledger_entries"
    )
    kind = models.CharField(max_length=12, choices=Kind.choices)
    doc_number = models.CharField(max_length=128, db_index=True)
    description = models.CharField(max_length=240, blank=True, default="")
    reference = models.CharField(max_length=120, blank=True, default="")
    mode = models.CharField(max_length=24, blank=True, default="")  # cash / bank / upi / cheque
    posted_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    # The entry this row reverses — guards against a second reversal (over-crediting
    # the partner, i.e. understating what it still owes).
    reverses = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )

    class Meta:
        db_table = "finledger_partner_entry"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.doc_number} · {self.store_id} · {self.amount}"


class CashLedgerEntry(LedgerEntry):
    class Kind(models.TextChoices):
        RECEIPT = "receipt", "Receipt (cash in)"
        PAYMENT = "payment", "Payment (cash out)"
        REVERSAL = "reversal", "Reversal"

    class Account(models.TextChoices):
        """Where the money physically is - which is not the same as whether we hold it.

        Card and UPI are the counter's two "paid but not settled" buckets: the
        customer has handed the money over and the bank has not passed it on, so
        they roll up into their own value-GL clearing accounts rather than into
        CASH. `finledger.posting.CASH_CONTROL_ACCOUNTS` is that mapping, and the
        books-health tie is checked against it per account.
        """

        CASH = "CASH", "Cash drawer"
        BANK = "BANK", "Bank"
        CARD = "CARD", "Card, awaiting settlement"
        UPI = "UPI", "UPI, awaiting settlement"

    account = models.CharField(max_length=24, choices=Account.choices, default=Account.CASH)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    doc_number = models.CharField(max_length=128, db_index=True)
    description = models.CharField(max_length=240, blank=True, default="")
    mode = models.CharField(max_length=24, blank=True, default="")
    vendor = models.ForeignKey(
        "vendors.Vendor",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cash_entries",
    )  # set when this cash-out pays a vendor
    link_doc = models.CharField(max_length=128, blank=True, default="")  # paired vendor doc_number
    posted_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    # The entry this row reverses — guards against a second reversal (over-credit).
    reverses = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )

    class Meta:
        db_table = "finledger_cash_entry"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.doc_number} · {self.account} · {self.amount}"


class BankStatementImport(TimeStampedModel):
    """One uploaded bank statement file. Not a ledger — this is import
    metadata (which file, how many rows matched), mutable by design (ADR-0004
    only binds the money facts in `*LedgerEntry`, not the paperwork about
    them), so a batch's summary counters can be corrected as its lines are
    reviewed without any append-only churn."""

    file = models.OneToOneField(
        "files.StoredFile", on_delete=models.PROTECT, related_name="bank_statement_import"
    )
    bank_label = models.CharField(max_length=80, blank=True, default="")
    uploaded_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL
    )
    row_count = models.IntegerField(default=0)
    matched_count = models.IntegerField(default=0)

    class Meta:
        db_table = "finledger_bank_statement_import"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.file.filename} ({self.matched_count}/{self.row_count} matched)"


class BankStatementLine(TimeStampedModel):
    """One row of an uploaded statement, and — once
    `finledger.reconciliation` has had a look — what it thinks that row is:
    `matched` (confident enough to auto-link), `review` (candidates exist,
    none certain), `unmatched` (nothing plausible), or `ignored` (Accounts
    said so — a bank charge/interest line with no ledger counterpart at all).
    `matched_entry` is a plain FK onto the append-only `CashLedgerEntry`
    table: this row is mutable and gets corrected freely; the ledger row it
    points at never does."""

    class Status(models.TextChoices):
        MATCHED = "matched", "Matched"
        REVIEW = "review", "Needs review"
        UNMATCHED = "unmatched", "Unmatched"
        IGNORED = "ignored", "Ignored"

    import_batch = models.ForeignKey(
        BankStatementImport, on_delete=models.CASCADE, related_name="lines"
    )
    txn_date = models.DateField()
    narration = models.CharField(max_length=500, blank=True, default="")
    debit_paise = models.BigIntegerField(default=0)
    credit_paise = models.BigIntegerField(default=0)
    balance_paise = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.UNMATCHED)
    matched_entry = models.OneToOneField(
        "finledger.CashLedgerEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bank_statement_line",
    )
    match_confidence = models.IntegerField(default=0)
    #: Top few candidates the matcher considered at import time (entry id,
    #: score, and enough of the entry to show without a join) — cached so the
    #: review screen doesn't re-score on every page load.
    candidates = models.JSONField(default=list, blank=True)
    matched_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        db_table = "finledger_bank_statement_line"
        ordering = ["-txn_date", "-id"]

    def __str__(self) -> str:
        return f"{self.txn_date} · {self.narration[:40]} · {self.status}"


# Store operations ticket 28 (ST-MNY-4): brand payables for outright brands.
from finledger.payable_models import (  # noqa: E402, F401
    PayableAllocation,
    PayableInvoice,
    PayablePayment,
)
