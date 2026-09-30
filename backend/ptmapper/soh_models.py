"""Reviewed source snapshots for opening stock; these never write balances."""

from __future__ import annotations

from django.db import models

from core.goods_base import EvidenceRow, TenantOwned


class SohImport(TenantOwned):
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    source_evidence = models.ForeignKey("files.EvidenceObject", on_delete=models.PROTECT, related_name="+")
    source_hash = models.CharField(max_length=64)
    source_name = models.CharField(max_length=240)
    source_metadata = models.JSONField(default=dict)
    uploaded_by = models.ForeignKey("accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+")
    prepared_by = models.ForeignKey("accounts.HumanIdentity", null=True, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey("accounts.HumanIdentity", null=True, on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=20, default="uploaded")
    cutoff_at = models.DateTimeField(null=True)
    configuration = models.JSONField(default=dict)
    reviewed_hash = models.CharField(max_length=64, blank=True)
    approval_request = models.ForeignKey("approvals.ApprovalRequest", null=True, on_delete=models.PROTECT, related_name="+")
    reconciliation = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "site", "source_hash"], name="uq_soh_source_snapshot")]


class SohImportRow(TenantOwned):
    source_import = models.ForeignKey(SohImport, on_delete=models.PROTECT, related_name="rows")
    ordinal = models.PositiveIntegerField()
    source_row_key = models.CharField(max_length=100)
    barcode = models.CharField(max_length=128)
    source_brand = models.CharField(max_length=240)
    quantity = models.IntegerField()
    mrp_paise = models.BigIntegerField(null=True)
    source_rate_paise = models.BigIntegerField(null=True)
    source = models.JSONField(default=dict)
    mapping = models.JSONField(default=dict)
    verification = models.JSONField(default=dict)
    exclusion_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_import", "ordinal"], name="uq_soh_source_row"),
            models.UniqueConstraint(fields=["source_import", "barcode"], name="uq_soh_source_barcode"),
        ]


class SohImportBatch(TenantOwned):
    source_import = models.ForeignKey(SohImport, on_delete=models.PROTECT, related_name="batches")
    batch_index = models.PositiveIntegerField()
    manifest = models.OneToOneField("ptmapper.OpeningManifest", on_delete=models.PROTECT, related_name="soh_batch")
    row_count = models.PositiveIntegerField()
    quantity = models.PositiveIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source_import", "batch_index"], name="uq_soh_child_batch")]


class SohImportReview(EvidenceRow):
    source_import = models.ForeignKey(SohImport, on_delete=models.PROTECT, related_name="reviews")
    revision = models.PositiveIntegerField()
    content_hash = models.CharField(max_length=64)
    payload = models.JSONField()

    class Meta(EvidenceRow.Meta):
        constraints = [*EvidenceRow.Meta.constraints, models.UniqueConstraint(fields=["source_import", "revision"], name="uq_soh_review_revision")]
