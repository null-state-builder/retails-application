"""Protected evidence objects for goods-v1 (design §4.4, §5.2).

Bytes live in the write-once store, not in PostgreSQL. A business record may
link evidence only after the store confirmed its hash and version, so a crash
can leave an unlinked object but never a record that claims missing evidence.
Legacy ``StoredFile`` blobs stay readable where they are.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import EvidenceRow, TenantOwned

MAX_EVIDENCE_BYTES = 20_000_000


class UploadIntent(TenantOwned):
    class State(models.TextChoices):
        STAGED = "staged"
        CONFIRMED = "confirmed"
        REFUSED = "refused"

    command_id = models.UUIDField()
    uploader = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    scope = models.JSONField()
    kind = models.CharField(max_length=20)
    expected_hash = models.CharField(max_length=64)
    expected_size = models.BigIntegerField()
    object_key = models.CharField(max_length=500)
    state = models.CharField(max_length=10, choices=State.choices, default=State.STAGED)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "uploader", "command_id"], name="uq_uploadintent_command"
            ),
            models.CheckConstraint(
                condition=models.Q(expected_size__gte=1, expected_size__lte=MAX_EVIDENCE_BYTES),
                name="ck_uploadintent_size",
            ),
        ]
        indexes = [models.Index(fields=["state", "created_at"])]


class EvidenceObject(EvidenceRow):
    upload = models.OneToOneField(UploadIntent, on_delete=models.PROTECT, related_name="evidence")
    kind = models.CharField(max_length=20)
    filename = models.CharField(max_length=255)
    media_type = models.CharField(max_length=100)
    size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    object_key = models.CharField(max_length=500)
    object_version = models.CharField(max_length=250)
    retention_until = models.DateTimeField()
    legal_hold = models.BooleanField(default=False)
    scope = models.JSONField()
    contains_fields = models.JSONField(default=list)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "object_key", "object_version"], name="uq_evidenceobject_object"
            ),
        ]


class EvidenceLink(EvidenceRow):
    evidence = models.ForeignKey(EvidenceObject, on_delete=models.PROTECT, related_name="links")
    subject_key = models.CharField(max_length=100)
    relationship = models.CharField(max_length=60)
    subject_revision = models.IntegerField(null=True, blank=True)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["evidence", "subject_key", "relationship", "subject_revision"],
                name="uq_evidencelink",
            ),
        ]
        indexes = [models.Index(fields=["subject_key"])]
