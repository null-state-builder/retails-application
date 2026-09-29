"""Goods-v1 approval requests and decisions (design §5.2, §5.6).

Domain-independent by construction: a request names its subject by kind and key,
and the decision command dispatches to a handler the project composition layer
registered. The approval and its domain effects commit in one transaction; there
is no "approved now, post later" state.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import EvidenceRow, TenantOwned


class ActionDraft(TenantOwned):
    class SubjectKind(models.TextChoices):
        OPENING_VARIANCE = "opening_variance"

    subject_kind = models.CharField(max_length=20, choices=SubjectKind.choices)
    subject_key = models.CharField(max_length=100)
    revision = models.IntegerField(default=1)
    maker = models.ForeignKey("accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+")
    payload = models.JSONField()
    content_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "subject_kind", "subject_key", "revision"],
                name="uq_actiondraft_revision",
            ),
        ]
        indexes = [models.Index(fields=["maker", "created_at"])]


class ApprovalRequest(TenantOwned):
    class SubjectKind(models.TextChoices):
        DOCUMENT = "document"
        CONFIGURATION = "configuration"
        MANIFEST = "manifest"
        OPENING_VARIANCE = "opening_variance"
        DISPOSITION = "disposition"
        COUNT = "count"
        MASTER_PROPOSAL = "master_proposal"
        #: Goods ticket 15H (GSA-R07): closing one RTV shipment's persistent vendor
        #: acknowledgement shortfall. The subject key is the shipment's id.
        RTV_SHORTFALL = "rtv_shortfall"

    class State(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"
        SUPERSEDED = "superseded"

    subject_kind = models.CharField(max_length=20, choices=SubjectKind.choices)
    subject_key = models.CharField(max_length=100)
    revision = models.IntegerField()
    reviewed_hash = models.CharField(max_length=64)
    maker = models.ForeignKey("accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+")
    policy_version = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    requested_action = models.CharField(max_length=60)
    required_roles = models.JSONField(default=list)
    require_distinct = models.BooleanField(default=True)
    #: What submission pinned the policy against: amounts, subject scope and thresholds.
    policy_basis = models.JSONField(default=dict)
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    brand = models.ForeignKey(
        "masters.Brand", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    scope = models.JSONField()
    reconciliation = models.JSONField(null=True, blank=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    command_key = models.ForeignKey("core.CommandKey", on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    title = models.CharField(max_length=240, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "subject_kind", "subject_key", "requested_action"],
                condition=models.Q(state="pending"),
                name="uq_approvalrequest_pending",
            ),
        ]
        indexes = [models.Index(fields=["state", "created_at"])]


class ApprovalDecision(EvidenceRow):
    class Outcome(models.TextChoices):
        APPROVED = "approved"
        REJECTED = "rejected"
        REFUSED = "refused"
        SUPERSEDED = "superseded"

    request = models.ForeignKey(ApprovalRequest, on_delete=models.PROTECT, related_name="decisions")
    checker = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    reviewed_hash = models.CharField(max_length=64)
    reason_code = models.CharField(max_length=60, null=True, blank=True)
    result = models.JSONField(null=True, blank=True)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["request"],
                condition=models.Q(outcome__in=["approved", "rejected"]),
                name="uq_approvaldecision_once",
            ),
        ]
        indexes = [models.Index(fields=["request", "recorded_at"])]
