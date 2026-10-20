"""Nightly stock sync to the legacy software's SOH (store test run, test tenant only).

The legacy software stays the only real book. Each night its SOH export is
uploaded and this store's goods stock is brought to it, so the next day's test
starts from the real shelf. Items the store has never held go to a reviewed
top-up opening import instead of being invented here.
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
    #: New items the store has never held, staged as a reviewed opening import.
    top_up_import = models.OneToOneField("ptmapper.SohImport", null=True, on_delete=models.PROTECT, related_name="stock_sync")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "site", "source_hash"], name="uq_stock_sync_source"),
        ]


class StockSyncRow(TenantOwned):
    class Outcome(models.TextChoices):
        REMOVED = "removed"
        ADDED = "added"
        NEW_ITEM = "new_item"
        EXCEPTION = "exception"

    sync = models.ForeignKey(StockSync, on_delete=models.PROTECT, related_name="rows")
    barcode = models.CharField(max_length=128)
    soh_qty = models.IntegerField()
    book_qty = models.IntegerField()
    applied_qty = models.IntegerField(default=0)
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    exception_code = models.CharField(max_length=40, blank=True)
    exception_note = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sync", "barcode"], name="uq_stock_sync_row")]
