"""A master sheet import package: one uploaded KDPS master sheet, reviewed and approved.

The package is the reviewed snapshot (``parsed``) of one upload, the reviewer's
choices (``selections``), what the tenant's lists looked like when it was planned
(``base``) and the plan itself. Nothing here is a list: approval writes vocabulary
versions, brand and season masters and ITEM rules through their own writers
(``masters.master_sheet_services``) and records what it wrote in ``result``.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import EvidenceRow, TenantOwned


class MasterSheetImport(TenantOwned):
    class State(models.TextChoices):
        REVIEW = "review"
        SUBMITTED = "submitted"
        APPROVED = "approved"
        WITHDRAWN = "withdrawn"

    source_evidence = models.ForeignKey(
        "files.EvidenceObject", on_delete=models.PROTECT, related_name="+"
    )
    source_hash = models.CharField(max_length=64)
    source_name = models.CharField(max_length=240)
    parsed = models.JSONField(default=dict)
    selections = models.JSONField(default=dict)
    #: The tenant's lists when the plan was made; a different live snapshot is stale.
    base = models.JSONField(default=dict)
    base_hash = models.CharField(max_length=64, blank=True)
    plan = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    reviewed_hash = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.REVIEW)
    uploaded_by = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    approved_by = models.ForeignKey(
        "accounts.HumanIdentity", null=True, on_delete=models.PROTECT, related_name="+"
    )
    approval_request = models.ForeignKey(
        "approvals.ApprovalRequest",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    result = models.JSONField(default=dict)

    class Meta:
        indexes = [models.Index(fields=["tenant", "state", "created_at"])]


class MasterSheetImportReview(EvidenceRow):
    """What each revision of a package showed its reviewer (append-only)."""

    source_import = models.ForeignKey(
        MasterSheetImport, on_delete=models.PROTECT, related_name="reviews"
    )
    revision = models.PositiveIntegerField()
    content_hash = models.CharField(max_length=64)
    payload = models.JSONField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["source_import", "revision"],
                name="uq_master_sheet_review_revision",
            ),
        ]
