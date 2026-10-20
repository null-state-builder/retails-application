"""Nightly legacy sales imports for a synthetic test tenant (store test run).

The incremental companion of the SOH stock sync (`ptmapper.stock_sync_models`):
the legacy software's sales report takes out what the shop really sold. No
customer data is stored and no bill or tax document is created.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import TenantOwned


class LegacySalesImport(TenantOwned):
    class State(models.TextChoices):
        PREVIEWED = "previewed"
        APPLIED = "applied"
        DISCARDED = "discarded"

    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    source_hash = models.CharField(max_length=64)
    source_name = models.CharField(max_length=240)
    date_from = models.DateField()
    date_to = models.DateField()
    metadata = models.JSONField(default=dict)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PREVIEWED)
    uploaded_by = models.ForeignKey("accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+")
    applied_by = models.ForeignKey("accounts.HumanIdentity", null=True, on_delete=models.PROTECT, related_name="+")
    applied_at = models.DateTimeField(null=True)
    #: What the apply did: reversed test bills, consumed and exception counts.
    outcome = models.JSONField(default=dict)
    document = models.OneToOneField("core.DocumentIdentity", null=True, on_delete=models.PROTECT, related_name="+")
    journal_batch = models.ForeignKey("stockledger.JournalBatch", null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "site", "source_hash"], name="uq_legacy_sales_source"),
        ]


class LegacySalesRow(TenantOwned):
    class Outcome(models.TextChoices):
        PENDING = "pending"
        CONSUMED = "consumed"
        RESTORED = "restored"
        EXCEPTION = "exception"

    source_import = models.ForeignKey(LegacySalesImport, on_delete=models.PROTECT, related_name="rows")
    ordinal = models.PositiveIntegerField()
    business_date = models.DateField()
    bill_no = models.CharField(max_length=60)
    barcode = models.CharField(max_length=128)
    #: Positive for a sale, negative for a legacy return.
    quantity = models.IntegerField()
    rate_paise = models.BigIntegerField(null=True)
    net_paise = models.BigIntegerField(null=True)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.PENDING)
    applied_qty = models.IntegerField(default=0)
    exception_code = models.CharField(max_length=40, blank=True)
    exception_note = models.CharField(max_length=300, blank=True)
    #: The exact portions this row consumed or restored, so a legacy return can
    #: find the legacy sale it undoes.
    portions = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source_import", "ordinal"], name="uq_legacy_sales_row")]
        indexes = [models.Index(fields=["tenant", "barcode"], name="ix_legacy_sales_barcode")]
