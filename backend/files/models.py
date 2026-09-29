"""Uploaded documents kept as Postgres blobs + metadata."""

from __future__ import annotations

from typing import Any

from django.db import models

from core.base import TimeStampedModel

#: Hard cap on an uploaded blob (brand PT / invoice files sit well under this).
#: Bounds memory + DB blob size and blunts oversized-upload / zip-bomb abuse.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


class UploadTooLarge(Exception):
    """An uploaded file exceeds MAX_UPLOAD_BYTES."""


class StoredFile(TimeStampedModel):
    class Kind(models.TextChoices):
        BOOKING_RECEIPT = "booking_receipt", "Booking receipt"
        INVOICE = "invoice", "Invoice"
        PT_FILE = "pt_file", "PT file"
        BANK_STATEMENT = "bank_statement", "Bank statement"
        OTHER = "other", "Other"

    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120)
    size = models.IntegerField(default=0)
    content = models.BinaryField()
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
    uploaded_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL
    )

    def __str__(self) -> str:
        return f"{self.filename} ({self.kind})"

    @classmethod
    def from_upload(cls, upload: Any, kind: str, user: Any = None) -> StoredFile:
        size = getattr(upload, "size", None)
        if size is not None and size > MAX_UPLOAD_BYTES:
            raise UploadTooLarge(f"File too large ({size} bytes); limit is {MAX_UPLOAD_BYTES}.")
        data = upload.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadTooLarge(
                f"File too large ({len(data)} bytes); limit is {MAX_UPLOAD_BYTES}."
            )
        return cls.objects.create(
            filename=getattr(upload, "name", "upload"),
            content_type=getattr(upload, "content_type", "application/octet-stream")
            or "application/octet-stream",
            size=len(data),
            content=data,
            kind=kind,
            uploaded_by=user if getattr(user, "is_authenticated", False) else None,
        )


# Goods-v1 protected evidence (design §4.4), registered with this app.
from files.goods_models import EvidenceLink, EvidenceObject, UploadIntent  # noqa: E402, F401
