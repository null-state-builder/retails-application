"""C08 reviewed full-store SOH deltas; source files never overwrite the ledger."""

from django.db import models

from core.goods_base import EvidenceRow, TenantOwned


class SohReconciliation(TenantOwned):
    source_import = models.OneToOneField("ptmapper.SohImport", on_delete=models.PROTECT, related_name="stock_reconciliation")
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    document = models.OneToOneField("core.DocumentIdentity", on_delete=models.PROTECT, related_name="+")
    maker = models.ForeignKey("accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=20, default="frozen")
    frozen_at = models.DateTimeField()
    content_hash = models.CharField(max_length=64)
    payload = models.JSONField()
    approval_request = models.ForeignKey("approvals.ApprovalRequest", null=True, on_delete=models.PROTECT, related_name="+")
    official_version = models.ForeignKey("core.OfficialVersion", null=True, on_delete=models.PROTECT, related_name="+")
    journal_batch = models.ForeignKey("stockledger.JournalBatch", null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["site"], condition=models.Q(state__in=["frozen", "submitted"]), name="uq_soh_count_open_site")]


class SohReconciliationEvidence(EvidenceRow):
    reconciliation = models.ForeignKey(SohReconciliation, on_delete=models.PROTECT, related_name="evidence")
    revision = models.PositiveIntegerField()
    outcome = models.CharField(max_length=20)
    content_hash = models.CharField(max_length=64)
    payload = models.JSONField()

    class Meta(EvidenceRow.Meta):
        constraints = [*EvidenceRow.Meta.constraints, models.UniqueConstraint(fields=["reconciliation", "revision"], name="uq_soh_count_evidence_revision")]
