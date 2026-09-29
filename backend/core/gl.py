"""The value General Ledger (ADR-0006).

One immutable, append-only table of balanced postings. Every business event fans
out into ≥2 legs whose signed paise sum to zero, written ONLY by
`core.posting.post_entries`. `amount` is signed paise — a **debit is positive, a
credit negative** — so a single voucher's legs sum to 0 and the whole ledger's
trial balance is exactly `Σ(amount) = 0` (the books-tie checksum).

This is the *value* GL kept alongside (not replacing) the quantity-bearing stock
ledger and Tally (the statutory book). Dimensions are snapshotted onto each leg at
write time: the master is the editable present, the posting is the frozen past.
"""

from __future__ import annotations

from django.db import models

from core.ledger import LedgerEntry


class GLAccount:
    """The chart of value accounts every posting slice draws on.

    Plain stable string codes (a full account master is a later slice). Sign
    convention is carried by the leg amount (dr +, cr −), not the account. This
    is code, not a table: an account code is a posting-catalog fact that ships
    with the slice that posts to it, so a typo is an import error rather than a
    silently mis-filed rupee.
    """

    # -- inbound → PT → vendor (D1/D2) ---------------------------------------
    INVENTORY = "INVENTORY"  # owned stock value (asset)
    SOR_STOCK = "SOR_STOCK"  # brand-owned stock held on SOR/consignment (memo asset)
    SOR_CONTRA = "SOR_CONTRA"  # off-book contra for SOR/consignment stock
    VENDOR_PAYABLE = "VENDOR_PAYABLE"  # accounts payable to vendors (liability)
    GRNI = "GRNI"  # goods received not invoiced (interim)
    INPUT_GST = "INPUT_GST"  # recoverable input GST (asset)
    CASH = "CASH"  # cash / bank / UPI (asset)
    SUSPENSE = "SUSPENSE"  # balancing holding account

    # -- selling (D10) --------------------------------------------------------
    # The sale's money side (event A) and cost side (event B). Card and UPI are
    # *clearing* accounts, not CASH: the customer has paid but the money has not
    # reached the bank, and the gap between the two is what the daily settlement
    # reconciliation is for. Keeping them apart is the only way a store's drawer
    # can be counted against CASH alone.
    SALES_REVENUE = "SALES_REVENUE"  # net-of-GST, post-discount takings (income)
    OUTPUT_GST = "OUTPUT_GST"  # GST collected on sale, payable to the state (liability)
    COGS = "COGS"  # cost of goods sold (expense)
    CARD_CLEARING = "CARD_CLEARING"  # card tender awaiting acquirer settlement (asset)
    UPI_CLEARING = "UPI_CLEARING"  # UPI tender awaiting settlement (asset)
    CREDIT_NOTE_LIABILITY = "CREDIT_NOTE_LIABILITY"  # unspent credit notes owed to customers
    ROUND_OFF = "ROUND_OFF"  # the rupee-rounding line that makes a bill balance
    # A bank instant discount recorded as a payment (store operations ticket 11):
    # owed by the bank that ran the offer, never cash in a drawer (asset).
    BANK_OFFER_RECEIVABLE = "BANK_OFFER_RECEIVABLE"
    # A customer reservation's advance (store operations ticket 20, ST-ORD-1):
    # money held for goods not yet supplied, owed back to the customer until the
    # pickup bill uses it or it is refunded (liability). No GST is due on it
    # (Notification 66/2017-CT).
    CUSTOMER_ADVANCE = "CUSTOMER_ADVANCE"
    # An advance kept because the goods were not collected by the date on the
    # voucher, where the policy keeps it. Not a sale and not a cancellation fee
    # (Circular 178/10/2022), so no GST (baseline, CA to confirm) (income).
    FORFEITED_ADVANCE = "FORFEITED_ADVANCE"
    # A paid alteration (store operations ticket 22, ST-ORD-3): the tailoring
    # service billed as its own line under SAC 9988, kept apart from the sale of
    # goods (income). Its GST goes to OUTPUT_GST like any line's.
    ALTERATION_REVENUE = "ALTERATION_REVENUE"
    # A gift voucher sold and not yet spent (store operations ticket 19,
    # ST-POS-4): owed to whoever holds it until a bill uses it (liability).
    # Neither goods nor services, so no GST when sold (Circular 243/37/2024).
    GIFT_VOUCHER_LIABILITY = "GIFT_VOUCHER_LIABILITY"
    # What a gift voucher still held on its last day, written off when it
    # expired unused. No GST is due on it (Circular 243/37/2024) (income).
    EXPIRED_GIFT_VOUCHERS = "EXPIRED_GIFT_VOUCHERS"

    # -- partner store billing (outbound, configurable) ----------------------
    # A partner store is a distinct business behind our own network (e.g. a
    # franchisee), so stock it receives is billed to it at Purchase Price rather
    # than simply restocked. Whether that also posts here is a chain-wide dial
    # (`outbound.BillingPolicy`) — this account only exists for when it does.
    PARTNER_RECEIVABLE = "PARTNER_RECEIVABLE"  # owed by a partner store, billed at PP (asset)


class GLEntry(LedgerEntry):
    """One leg of a balanced value posting (append-only general ledger).

    Inherits the append-only ORM guard + DB trigger from `LedgerEntry`; written only
    by `core.posting.post_entries`. `amount` (paise) is signed: debit +, credit −.
    """

    account = models.CharField(max_length=32, db_index=True)
    doc_type = models.CharField(max_length=16)
    doc_number = models.CharField(max_length=128, db_index=True)
    against_voucher = models.CharField(max_length=128, blank=True, default="")
    party_type = models.CharField(max_length=16, blank=True, default="")
    party_code = models.CharField(max_length=64, blank=True, default="")
    store = models.ForeignKey(
        "masters.Store",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="gl_entries",
    )
    gstin = models.ForeignKey(
        "masters.Gstin",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="gl_entries",
    )
    brand = models.CharField(max_length=120, blank=True, default="")
    season = models.CharField(max_length=120, blank=True, default="")
    line_no = models.IntegerField(default=0)
    memo = models.CharField(max_length=240, blank=True, default="")
    posted_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        db_table = "core_gl_entry"
        ordering = ["-created_at", "line_no"]

    def __str__(self) -> str:
        return f"{self.doc_number} · {self.account} {self.amount}"


def account_balance(account: str, **filters: object) -> int:
    """Net paise on an account = Σ(amount) (debits +, credits −)."""
    qs = GLEntry.objects.filter(account=account, **filters)
    return qs.aggregate(b=models.Sum("amount"))["b"] or 0


def trial_balance(**filters: object) -> int:
    """Whole-ledger checksum: Σ(amount) over every leg. MUST be 0 when the books
    tie (each posting is balanced, so their sum is balanced)."""
    qs = GLEntry.objects.filter(**filters) if filters else GLEntry.objects.all()
    return qs.aggregate(b=models.Sum("amount"))["b"] or 0
