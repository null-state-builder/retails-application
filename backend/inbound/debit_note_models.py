"""Debit notes drafted for receiving shortages (store operations ST-REC-3; ticket 38).

When a shortage is approved - the vendor invoiced pieces that were never received,
and an ``accept_shortage`` disposition says so - a debit note to the vendor is
drafted at the invoice's cost plus tax, linked to the GRN and to the vendor's
claim (the invoice claim version the shortage was accepted against). Accounts
reviews it, the Owner approves issuing it in the approvals inbox, and Accounts
issues it, which gives it head office's ``XXX/DN/2627/n`` number for the GSTIN.

An issued note is never edited: nothing in the code changes its figures once it
has a number (store operations PRD §6 principle 3). Posting it through the
accounting export waits for OQ-47, so nothing here posts anything.

The row has an integer key because the shared approvals spine
(``approvals.Approval``) points at its subject by an integer id. Each row still
belongs to one tenant: the column defaults to the connection's trusted tenant
(as ``core.goods_base.InheritedTenantMaster``), and migration 0014 puts both
tables behind forced row-level security. The same migration's trigger refuses
any change to an issued note, and any delete.
"""

from __future__ import annotations

from django.db import models

from core.base import TimeStampedModel
from core.goods_base import CurrentTenant


def _tenant() -> models.ForeignKey:  # type: ignore[type-arg]
    return models.ForeignKey(
        "masters.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
        db_default=CurrentTenant(),
    )


class DebitNote(TimeStampedModel):
    class Status(models.TextChoices):
        #: Being reviewed by Accounts, waiting for the Owner, or approved to issue.
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        #: Accounts decided not to issue it. Nothing was numbered.
        CANCELLED = "cancelled", "Cancelled"

    tenant = _tenant()
    #: The receiving site whose GRN was short.
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: The registration the note is issued under: head office's prefix for it.
    gstin = models.ForeignKey("masters.Gstin", on_delete=models.PROTECT, related_name="+")
    vendor = models.ForeignKey("vendors.Vendor", on_delete=models.PROTECT, related_name="+")
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    grn = models.ForeignKey("inbound.GoodsGrn", on_delete=models.PROTECT, related_name="+")
    #: The vendor's claim: the invoice claim version the shortage was accepted against.
    claim = models.ForeignKey(
        "inbound.InvoiceClaimVersion", on_delete=models.PROTECT, related_name="+"
    )
    invoice_number = models.CharField(max_length=80, blank=True, default="")
    invoice_date = models.DateField(null=True, blank=True)
    #: One entry per accepted shortage: which claim line, how many pieces, the
    #: invoice's cost per piece before tax, the GST rate and the money
    #: (``inbound.debit_notes.Line``). Money is whole paise, as text.
    lines = models.JSONField(default=list)
    taxable_paise = models.BigIntegerField(default=0)
    tax_paise = models.BigIntegerField(default=0)
    total_paise = models.BigIntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    #: Bumped by every change, so a stale screen is refused.
    revision = models.PositiveIntegerField(default=1)
    #: Accounts' review note.
    review_note = models.CharField(max_length=240, blank=True, default="")
    #: The figures the Owner was asked to approve (``inbound.debit_notes.figures_hash``).
    approval_hash = models.CharField(max_length=64, blank=True, default="")
    #: Unique in its tenant (``IssuedDocumentNumber`` is the register behind it).
    #: Null until issued, so the unique constraint ignores unnumbered notes.
    number = models.CharField(max_length=16, null=True, blank=True)  # noqa: DJ001
    issued_on = models.DateField(null=True, blank=True)
    issued_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancel_reason = models.CharField(max_length=240, blank=True, default="")
    cancelled_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "inbound_debit_note"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["grn", "status"]), models.Index(fields=["site", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"],
                condition=models.Q(number__isnull=False),
                name="uq_debitnote_tenant_number",
            ),
            # A number is taken exactly when the note is issued, and never before.
            models.CheckConstraint(
                condition=(
                    models.Q(status="issued", number__isnull=False, issued_on__isnull=False)
                    | (
                        ~models.Q(status="issued")
                        & models.Q(number__isnull=True, issued_on__isnull=True)
                    )
                ),
                name="ck_debitnote_number_when_issued",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="cancelled") | ~models.Q(cancel_reason=""),
                name="ck_debitnote_cancel_reason",
            ),
        ]

    def __str__(self) -> str:
        return self.number or f"Debit note draft {self.pk}"


class DebitNoteSource(models.Model):
    """Which accepted shortage a debit note line came from.

    One row per ``accept_shortage`` disposition, ever: the unique key is what makes
    drafting safe to repeat - a shortage is never charged to the vendor twice.
    """

    tenant = _tenant()
    disposition = models.OneToOneField(
        "inbound.Disposition", on_delete=models.PROTECT, related_name="+"
    )
    note = models.ForeignKey(DebitNote, on_delete=models.PROTECT, related_name="sources")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inbound_debit_note_source"

    def __str__(self) -> str:
        return f"{self.disposition_id} -> {self.note_id}"
