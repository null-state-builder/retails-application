"""Kernel tables for goods-v1 commands, documents, numbering and evidence (design §5.2, §5.7).

Legacy ``core.Document`` stays readable for historical documents. New versioned
goods documents use the separate identity / draft revision / official version
split below, so an official version is never an edited row.

Large draft line sets do not live inside one JSON revision payload: a 50,000-line
PT edited once would otherwise copy every line. ``DraftLine`` rows are immutable
and a revision's lines are the newest row per line key created at or before that
revision (a deletion is its own row).
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from core.goods_base import APPEND_ONLY, PROJECTION, EvidenceRow, TenantOwned


class CommandKey(TenantOwned):
    command_id = models.UUIDField()
    principal_key = models.CharField(max_length=100)
    action = models.CharField(max_length=100)
    fingerprint = models.CharField(max_length=64)
    contract_version = models.CharField(max_length=20)

    PROTECTION: ClassVar[str] = APPEND_ONLY

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "principal_key", "command_id"], name="uq_commandkey_identity"
            ),
        ]


class CommandOutcome(TenantOwned):
    class Outcome(models.TextChoices):
        SUCCEEDED = "succeeded"
        REFUSED = "refused"

    key = models.OneToOneField(CommandKey, on_delete=models.PROTECT, related_name="outcome")
    status_code = models.IntegerField()
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    result = models.JSONField()

    PROTECTION: ClassVar[str] = APPEND_ONLY

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status_code__gte=200, status_code__lte=599),
                name="ck_commandoutcome_status",
            ),
        ]


class CommandAttempt(TenantOwned):
    """A refused, failed or successful attempt, kept even when the command rolled back."""

    class Outcome(models.TextChoices):
        SUCCEEDED = "succeeded"
        REFUSED = "refused"
        TERMINAL_FAILURE = "terminal_failure"

    command_key = models.ForeignKey(
        CommandKey, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    actor = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    service_code = models.CharField(max_length=60, null=True, blank=True)
    event_at = models.DateTimeField()
    recorded_at = models.DateTimeField()
    action = models.CharField(max_length=100)
    subject_key = models.CharField(max_length=100, null=True, blank=True)
    reviewed_hash = models.CharField(max_length=64, null=True, blank=True)
    reconciliation = models.JSONField(null=True, blank=True)
    authority = models.JSONField()
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    reason_code = models.CharField(max_length=80, null=True, blank=True)
    previous_hash = models.CharField(max_length=64, null=True, blank=True)
    row_hash = models.CharField(max_length=64)

    PROTECTION: ClassVar[str] = APPEND_ONLY
    #: What the sealer has always hashed for an attempt: every column but the
    #: database-stamped ``created_at``, with ``previous_hash`` included and
    #: ``row_hash`` blank (``core.evidence.row_content``). Not ``EvidenceRow``'s
    #: set: changing what is hashed would make every attempt already sealed read
    #: as tampered.
    HASH_EXCLUDED: ClassVar[frozenset[str]] = frozenset({"created_at"})

    class Meta:
        indexes = [models.Index(fields=["subject_key", "recorded_at"])]


class AuditEvent(EvidenceRow):
    action = models.CharField(max_length=100)
    subject_key = models.CharField(max_length=100, null=True, blank=True)
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    outcome = models.CharField(max_length=40)
    reason_code = models.CharField(max_length=60, null=True, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    authority = models.JSONField()

    class Meta(EvidenceRow.Meta):
        indexes = [
            models.Index(fields=["site", "recorded_at"]),
            models.Index(fields=["actor", "recorded_at"]),
            models.Index(fields=["subject_key", "recorded_at"]),
        ]


class ChainHead(TenantOwned):
    partition_key = models.CharField(max_length=160)
    last_hash = models.CharField(max_length=64)
    last_event_id = models.UUIDField(null=True, blank=True)
    anchored_at = models.DateTimeField(null=True, blank=True)
    anchored_hash = models.CharField(max_length=64, null=True, blank=True)

    PROTECTION: ClassVar[str] = PROJECTION

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "partition_key"], name="uq_chainhead_partition"
            ),
        ]
        indexes = [models.Index(fields=["anchored_at"])]


class OutboxIntent(EvidenceRow):
    class Kind(models.TextChoices):
        NOTIFICATION = "notification"
        EMAIL = "email"
        EXPORT = "export"
        ANCHOR = "anchor"
        CEILING = "ceiling"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    subject_key = models.CharField(max_length=100)
    request_key = models.UUIDField()
    payload = models.JSONField()
    not_before = models.DateTimeField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(fields=["tenant", "request_key"], name="uq_outbox_request"),
        ]
        indexes = [models.Index(fields=["kind", "not_before"])]


class JobState(TenantOwned):
    class State(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        CONFIRMED = "confirmed"
        FAILED = "failed"
        UNKNOWN = "unknown"

    intent = models.OneToOneField(OutboxIntent, on_delete=models.PROTECT, related_name="job")
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    attempts = models.IntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    progress = models.IntegerField(default=0)
    error_code = models.CharField(max_length=80, null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(attempts__gte=0), name="ck_jobstate_attempts"
            ),
            models.CheckConstraint(
                condition=models.Q(progress__gte=0, progress__lte=100), name="ck_jobstate_progress"
            ),
        ]
        indexes = [models.Index(fields=["state", "lease_until", "created_at"])]


class DeliveryEvent(EvidenceRow):
    class State(models.TextChoices):
        ATTEMPTED = "attempted"
        CONFIRMED = "confirmed"
        FAILED = "failed"
        UNKNOWN = "unknown"

    intent = models.ForeignKey(OutboxIntent, on_delete=models.PROTECT, related_name="deliveries")
    attempt = models.IntegerField()
    state = models.CharField(max_length=10, choices=State.choices)
    provider_ref = models.CharField(max_length=200, null=True, blank=True)
    diagnostic = models.CharField(max_length=500, null=True, blank=True)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["intent", "attempt", "state"], name="uq_delivery_attempt_state"
            ),
        ]


class JobArtifact(EvidenceRow):
    intent = models.OneToOneField(OutboxIntent, on_delete=models.PROTECT, related_name="artifact")
    evidence = models.ForeignKey("files.EvidenceObject", on_delete=models.PROTECT, related_name="+")
    source_watermark = models.CharField(max_length=200)
    manifest_hash = models.CharField(max_length=64)


class DocumentIdentity(TenantOwned):
    """One goods document for life. Only ``official_number`` may be set, once.

    Every document is held at a site, except a booking saved with only its legal
    entity (GSA-T05): its ``site`` stays empty until the buyer first names a
    destination, and is then set once and never changed, like its number.
    """

    class Purpose(models.TextChoices):
        RECEIPT = "receipt"
        OPENING = "opening"
        TRANSFER = "transfer"
        BOOKING = "booking"
        GRN = "grn"
        COUNTER_GRN = "counter_grn"
        MOVEMENT = "movement"
        COUNT = "count"
        GAP_RESOLUTION = "gap_resolution"
        #: A store's bill (OPS-07). Official the moment it is printed - it is in a
        #: customer's hand - so it has no draft anybody reviews and no approver
        #: other than the person who billed. It is never a receipt or opening PT
        #: and never appears in the acceptance queue those read.
        SALE = "sale"
        #: A customer reservation (store operations ticket 20, ST-ORD-1): pieces
        #: held at a store for a named customer. It authorises the hold and its
        #: end - pickup, cancellation or expiry by its own collect-by date.
        RESERVATION = "reservation"

    kind = models.CharField(max_length=20)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    entity = models.ForeignKey("masters.LegalEntity", on_delete=models.PROTECT, related_name="+")
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    official_number = models.CharField(max_length=128, null=True, blank=True)
    maker = models.ForeignKey("accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+")
    command_key = models.ForeignKey(CommandKey, on_delete=models.PROTECT, related_name="+")

    PROTECTION: ClassVar[str] = "identity"

    @property
    def held_site_id(self) -> int:
        """The site of a document that always has one - every purpose but a booking."""
        if self.site_id is None:
            raise ValueError(f"{self.purpose} document {self.pk} has no site yet")
        return self.site_id

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "official_number"],
                condition=models.Q(official_number__isnull=False),
                name="uq_documentidentity_number",
            ),
            models.CheckConstraint(
                condition=models.Q(site__isnull=False) | models.Q(purpose="booking"),
                name="ck_documentidentity_site_unless_booking",
            ),
        ]
        indexes = [models.Index(fields=["site", "kind", "created_at"])]


class DraftRevision(EvidenceRow):
    document = models.ForeignKey(
        DocumentIdentity, null=True, blank=True, on_delete=models.PROTECT, related_name="revisions"
    )
    config_draft = models.ForeignKey(
        "masters.ConfigDraft",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    action_draft = models.ForeignKey(
        "approvals.ActionDraft",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="revisions",
    )
    revision = models.IntegerField()
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    payload = models.JSONField()
    lines_hash = models.CharField(max_length=64, null=True, blank=True)
    content_hash = models.CharField(max_length=64)
    reviewed_rows = models.JSONField(default=list)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=(
                    models.Q(
                        document__isnull=False, config_draft__isnull=True, action_draft__isnull=True
                    )
                    | models.Q(
                        document__isnull=True, config_draft__isnull=False, action_draft__isnull=True
                    )
                    | models.Q(
                        document__isnull=True, config_draft__isnull=True, action_draft__isnull=False
                    )
                ),
                name="ck_draftrevision_one_parent",
            ),
            models.UniqueConstraint(
                fields=["document", "revision"],
                condition=models.Q(document__isnull=False),
                name="uq_draftrevision_document",
            ),
            models.UniqueConstraint(
                fields=["config_draft", "revision"],
                condition=models.Q(config_draft__isnull=False),
                name="uq_draftrevision_config",
            ),
            models.UniqueConstraint(
                fields=["action_draft", "revision"],
                condition=models.Q(action_draft__isnull=False),
                name="uq_draftrevision_action",
            ),
        ]


class DraftLine(EvidenceRow):
    """One immutable draft line state; a revision reads the newest per line key."""

    document = models.ForeignKey(
        DocumentIdentity, on_delete=models.PROTECT, related_name="draft_lines"
    )
    line_key = models.UUIDField()
    created_in_revision = models.IntegerField()
    line_no = models.IntegerField()
    deleted = models.BooleanField(default=False)
    payload = models.JSONField()
    line_hash = models.CharField(max_length=64)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["document", "line_key", "created_in_revision"], name="uq_draftline_state"
            ),
        ]
        indexes = [models.Index(fields=["document", "line_key", "created_in_revision"])]


class DocumentHead(TenantOwned):
    class State(models.TextChoices):
        DRAFT = "draft"
        SUBMITTED = "submitted"
        OFFICIAL = "official"
        REVERSED = "reversed"

    document = models.OneToOneField(DocumentIdentity, on_delete=models.PROTECT, related_name="head")
    draft_revision = models.ForeignKey(
        DraftRevision, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    live_version = models.ForeignKey(
        "core.OfficialVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(max_length=10, choices=State.choices, default=State.DRAFT)
    revision = models.IntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="ck_documenthead_revision"
            ),
        ]


class OfficialVersion(EvidenceRow):
    document = models.ForeignKey(
        DocumentIdentity, on_delete=models.PROTECT, related_name="versions"
    )
    version = models.IntegerField()
    draft_revision = models.ForeignKey(DraftRevision, on_delete=models.PROTECT, related_name="+")
    profile_version = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_by = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    canonical_payload = models.JSONField()
    content_hash = models.CharField(max_length=64)
    reconciliation = models.JSONField(null=True, blank=True)
    authority_snapshot = models.JSONField()
    line_count = models.IntegerField(default=0)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["document", "version"], name="uq_officialversion_version"
            ),
        ]


class OfficialLine(EvidenceRow):
    version = models.ForeignKey(OfficialVersion, on_delete=models.PROTECT, related_name="lines")
    line_no = models.IntegerField()
    stable_line_key = models.UUIDField()
    payload = models.JSONField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(fields=["version", "line_no"], name="uq_officialline_no"),
            models.UniqueConstraint(
                fields=["version", "stable_line_key"], name="uq_officialline_key"
            ),
            models.CheckConstraint(
                condition=models.Q(line_no__gte=1, line_no__lte=50000), name="ck_officialline_no"
            ),
        ]
        indexes = [models.Index(fields=["stable_line_key"])]


class DocumentEvent(EvidenceRow):
    document = models.ForeignKey(DocumentIdentity, on_delete=models.PROTECT, related_name="events")
    version = models.ForeignKey(
        OfficialVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.ForeignKey(
        DraftRevision, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    event_kind = models.CharField(max_length=60)
    reason_code = models.CharField(max_length=60, null=True, blank=True)
    evidence = models.ForeignKey(
        "files.EvidenceObject", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    payload = models.JSONField()

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["document", "recorded_at", "id"])]


class SubjectRevision(TenantOwned):
    family = models.CharField(max_length=60)
    subject_key = models.CharField(max_length=100)
    revision = models.IntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "family", "subject_key"], name="uq_subjectrevision"
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gt=0), name="ck_subjectrevision_positive"
            ),
        ]


class SeriesCeiling(EvidenceRow):
    series = models.ForeignKey(
        "core.VoucherSeries", on_delete=models.PROTECT, related_name="ceilings"
    )
    ceiling = models.BigIntegerField()
    object_key = models.CharField(max_length=500)
    object_version = models.CharField(max_length=250)
    sha256 = models.CharField(max_length=64)
    confirmed_at = models.DateTimeField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(fields=["series", "ceiling"], name="uq_seriesceiling"),
        ]
        indexes = [models.Index(fields=["series", "ceiling"])]


class LegacyReference(TenantOwned):
    class Disposition(models.TextChoices):
        UNREVIEWED = "unreviewed"
        MAPPED = "mapped"
        HISTORY_ONLY = "history_only"

    family = models.CharField(max_length=40)
    legacy_key = models.CharField(max_length=100)
    goods_key = models.UUIDField(null=True, blank=True)
    disposition = models.CharField(max_length=20, choices=Disposition.choices)
    evidence = models.ForeignKey(
        "files.EvidenceObject", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "family", "legacy_key"], name="uq_legacyreference"
            ),
        ]


class RecoveryRecord(TenantOwned):
    class Status(models.TextChoices):
        REVIEW = "review"
        VERIFIED = "verified"
        PUBLISHED = "published"
        BLOCKED = "blocked"

    restored_at = models.DateTimeField()
    manifest_evidence = models.ForeignKey(
        "files.EvidenceObject", on_delete=models.PROTECT, related_name="+"
    )
    ceilings = models.JSONField(default=list)
    holes = models.JSONField(default=list)
    conflicts = models.JSONField(default=list)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REVIEW)
    approved_by = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reconstruction_hash = models.CharField(max_length=64, null=True, blank=True)
    revision = models.IntegerField(default=1)

    class Meta:
        indexes = [models.Index(fields=["restored_at"])]


class PrivilegedReview(EvidenceRow):
    audit_event = models.ForeignKey(AuditEvent, on_delete=models.PROTECT, related_name="reviews")
    reviewer = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    note = models.CharField(max_length=1000)
    reviewed_at = models.DateTimeField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(fields=["audit_event", "reviewer"], name="uq_privilegedreview"),
        ]
        indexes = [models.Index(fields=["reviewed_at"])]
