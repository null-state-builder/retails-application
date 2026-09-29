"""Owned goods exceptions and recipient notifications (design §5.2, §5.7).

An exception is a projection of append-only events: its cause is never deleted,
and only the named domain command can resolve it. A notification is a separate
recipient-scoped fact; reading it never resolves the business cause. The legacy
``Alert``/``AlertSeen`` rows remain as history.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import EvidenceRow, TenantOwned


class GoodsException(TenantOwned):
    class State(models.TextChoices):
        OPEN = "open"
        RESOLVED = "resolved"

    kind = models.CharField(max_length=60)
    #: Nullable since GSA-T18: a durable export can be asked for across the whole
    #: tenant, so its failure belongs to no one site. A null site is a tenant-wide
    #: exception, and `AccessContext.covers_store(..., None)` already answers it
    #: the right way - only a tenant- or entity-scoped grant reaches it, and a
    #: site-scoped list filter (`site_id__in`) never returns it.
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    subject_key = models.CharField(max_length=100)
    owner_role = models.CharField(max_length=40)
    owner_human = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Nullable since GSA-T08 (ticket 08C): a working-day deadline needs an
    #: approved working calendar, and a business that has approved none has no
    #: working week to measure against. The exception is still opened and still
    #: owned — it simply has no deadline until a calendar exists, rather than one
    #: inferred from a week nobody approved. Goods activation readiness is what
    #: stops an activated site living like that
    #: (`masters.goods_services._working_calendar_check`).
    due_at = models.DateTimeField(null=True, blank=True)
    calendar_version = models.ForeignKey(
        "masters.ConfigVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="GSA-T08: the approved working_calendar version `due_at` was computed "
        "against, when a working-day SLA had one approved. Frozen at open — reassignment "
        "and a later calendar version never recompute an existing exception's due_at.",
    )
    state = models.CharField(max_length=10, choices=State.choices, default=State.OPEN)
    reason_code = models.CharField(max_length=60, null=True, blank=True)
    source_event_key = models.UUIDField()
    resolution_event_key = models.UUIDField(null=True, blank=True)
    opened_at = models.DateTimeField()
    allowed_resolution_actions = models.JSONField(default=list)
    revision = models.IntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "kind", "source_event_key"], name="uq_goodsexception_source"
            ),
        ]
        indexes = [models.Index(fields=["site", "state", "due_at"])]


class ExceptionEvent(EvidenceRow):
    class Kind(models.TextChoices):
        OPENED = "opened"
        ASSIGNED = "assigned"
        NOTE = "note"
        RESOLVED = "resolved"
        REOPENED = "reopened"

    exception = models.ForeignKey(GoodsException, on_delete=models.PROTECT, related_name="events")
    event_kind = models.CharField(max_length=10, choices=Kind.choices)
    reason_code = models.CharField(max_length=60, null=True, blank=True)
    payload = models.JSONField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=~models.Q(event_kind__in=["opened", "reopened"])
                | models.Q(reason_code__isnull=False),
                name="ck_exceptionevent_reason",
            ),
        ]
        indexes = [models.Index(fields=["exception", "recorded_at"])]


class OriginInvestigationOutcome(EvidenceRow):
    """GSA-T10: closes a synthetic-opening "historical origin unavailable" investigation.

    References the opening row and the exception it closes; it is not an edit to
    OpeningManifestRow, OfficialVersion or source chronology (design §5.8, E241).
    """

    class Outcome(models.TextChoices):
        HISTORICAL_ORIGIN_UNAVAILABLE = "historical_origin_unavailable"

    exception = models.ForeignKey(GoodsException, on_delete=models.PROTECT, related_name="+")
    manifest_row = models.ForeignKey(
        "ptmapper.OpeningManifestRow", on_delete=models.PROTECT, related_name="+"
    )
    reviewed_hash = models.CharField(max_length=64)
    reason_code = models.CharField(max_length=60)
    evidence_ids = models.JSONField(default=list)
    outcome = models.CharField(
        max_length=32, choices=Outcome.choices, default=Outcome.HISTORICAL_ORIGIN_UNAVAILABLE
    )

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["exception"])]


class GoodsNotification(EvidenceRow):
    intent = models.ForeignKey("core.OutboxIntent", on_delete=models.PROTECT, related_name="+")
    recipient = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    subject_key = models.CharField(max_length=100)
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    brand = models.ForeignKey(
        "masters.Brand", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    event_kind = models.CharField(max_length=60)
    title = models.CharField(max_length=240)
    due_at = models.DateTimeField(null=True, blank=True)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["intent", "recipient"], name="uq_goodsnotification_recipient"
            ),
        ]
        indexes = [models.Index(fields=["recipient", "created_at"])]


class NotificationAcknowledgement(EvidenceRow):
    notification = models.ForeignKey(
        GoodsNotification, on_delete=models.PROTECT, related_name="acks"
    )
    recipient = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    seen_at = models.DateTimeField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["notification", "recipient"], name="uq_notificationack"
            ),
        ]
        indexes = [models.Index(fields=["recipient", "seen_at"])]
