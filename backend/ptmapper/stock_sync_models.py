"""Nightly stock sync to the legacy software's SOH (store test run, test tenant only).

The legacy software stays the only real book. Each night its SOH export is
uploaded and this store's goods stock is brought to it, so the next day's test
starts from the real shelf. Every row that differs is flagged by category for
the store manager to review. A barcode in no PT is flagged only: it is never
invented here, and becomes real when a PT carrying it is uploaded.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import TenantOwned


class StockSync(TenantOwned):
    class State(models.TextChoices):
        PREVIEWED = "previewed"
        APPLIED = "applied"
        DISCARDED = "discarded"

    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    source_evidence = models.ForeignKey("files.EvidenceObject", on_delete=models.PROTECT, related_name="+")
    source_hash = models.CharField(max_length=64)
    source_name = models.CharField(max_length=240)
    #: When the legacy SOH was exported. Test bills after the previous sync's
    #: cutoff and up to this one are put back before the store is matched.
    cutoff_at = models.DateTimeField()
    metadata = models.JSONField(default=dict)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PREVIEWED)
    uploaded_by = models.ForeignKey("accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+")
    applied_by = models.ForeignKey("accounts.HumanIdentity", null=True, on_delete=models.PROTECT, related_name="+")
    applied_at = models.DateTimeField(null=True)
    outcome = models.JSONField(default=dict)
    document = models.OneToOneField("core.DocumentIdentity", null=True, on_delete=models.PROTECT, related_name="+")
    journal_batch = models.ForeignKey("stockledger.JournalBatch", null=True, on_delete=models.PROTECT, related_name="+")
    #: Historical only: syncs applied before flag-only SOH rows staged new items
    #: as a reviewed opening import. Never set now.
    top_up_import = models.OneToOneField("ptmapper.SohImport", null=True, on_delete=models.PROTECT, related_name="stock_sync")

    class Meta:
        constraints = [
            # A discarded upload does not hold its file: the same SOH can come back with the right export time.
            models.UniqueConstraint(fields=["tenant", "site", "source_hash"], condition=~models.Q(state="discarded"),
                                    name="uq_stock_sync_source"),
        ]


class StockSyncRow(TenantOwned):
    """One barcode where the book and the legacy SOH differ (or the MRP does).

    ``category`` is the row's primary flag. A quantity difference wins
    (``SOH_ONLY``, ``PT_ONLY``, ``SOH_ABOVE``, ``SOH_BELOW``); a row whose
    quantities agree but whose MRP does not is ``MRP_DIFFERS``. ``mrp_differs``
    is set on every row whose SOH MRP is not the SKU's MRP, so an MRP mismatch
    on a quantity-flagged row is still found by the ``MRP_DIFFERS`` filter.
    Acknowledging a row only marks it reviewed: it never changes stock or items.
    """

    class Outcome(models.TextChoices):
        REMOVED = "removed"
        ADDED = "added"
        #: Historical: a barcode staged in the top-up opening import.
        NEW_ITEM = "new_item"
        EXCEPTION = "exception"
        #: Flagged for review, stock not changed (SOH-only barcode, MRP-only difference).
        FLAGGED = "flagged"

    class Category(models.TextChoices):
        #: In the SOH, in no PT: not created, not stocked, not sellable.
        SOH_ONLY = "SOH_ONLY"
        #: The book has stock, the SOH has none (absent or 0). Taken to 0.
        PT_ONLY = "PT_ONLY"
        #: A PT item; the SOH holds more than the book did. Added.
        SOH_ABOVE = "SOH_ABOVE"
        #: Both hold it; the SOH holds fewer. Removed oldest-first.
        SOH_BELOW = "SOH_BELOW"
        #: Quantities agree; the SOH's MRP is not the SKU's.
        MRP_DIFFERS = "MRP_DIFFERS"

    class ReviewState(models.TextChoices):
        OPEN = "open"
        ACKNOWLEDGED = "acknowledged"

    sync = models.ForeignKey(StockSync, on_delete=models.PROTECT, related_name="rows")
    barcode = models.CharField(max_length=128)
    soh_qty = models.IntegerField()
    book_qty = models.IntegerField()
    applied_qty = models.IntegerField(default=0)
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    exception_code = models.CharField(max_length=40, blank=True)
    exception_note = models.CharField(max_length=300, blank=True)
    category = models.CharField(max_length=12, choices=Category.choices, null=True, blank=True)
    mrp_differs = models.BooleanField(default=False)
    soh_mrp_paise = models.BigIntegerField(null=True, blank=True)
    book_mrp_paise = models.BigIntegerField(null=True, blank=True)
    review_state = models.CharField(max_length=12, choices=ReviewState.choices, default=ReviewState.OPEN)
    reviewed_by = models.ForeignKey("accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sync", "barcode"], name="uq_stock_sync_row")]
        indexes = [models.Index(fields=["sync", "category"], name="ix_stock_sync_row_category")]
