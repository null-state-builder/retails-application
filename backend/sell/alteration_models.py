"""Alterations (store operations ticket 22, ST-ORD-3).

A customer's garment is altered after it is billed: trousers shortened, a waist
taken in. Two records, each answering one question:

* ``AlterationJob`` - the job card. Which bill line the garment was sold on,
  what to alter, the measurements, the tailor (in-house or outside), the
  promised date, any charge and where the work stands. Its status moves forward
  only: received, with the tailor, ready, then handed over - or cancelled.
* ``BilledRetainedCustody`` - the goods the customer has bought and the store
  still holds (overall PRD R-INV-014). The garment is sold, so it is no longer
  stock anybody can sell, move or return to a brand; this row says whose it is,
  where it is and what finally became of it. A job card opens one and closes it
  at handover or cancellation. The shape is not alteration-specific, so home
  delivery (ST-ORD-4) can hold goods the same way once OQ-49 is decided.

A paid alteration is its own bill line (``SaleLine.Kind.ALTERATION``) at 5%
under SAC 9988, made at the till like any other line; the job card points at
it. A free alteration has no line, no charge and no tax (baseline, CA to
confirm).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import models

from core.base import TimeStampedModel
from core.money import MoneyField

#: The service accounting code of an alteration line (§6 baseline, CA to confirm).
ALTERATION_SAC = "9988"
#: Its GST rate, whatever the charge (§6 baseline, CA to confirm). The PRD lists
#: no setting for it; the till reads it from the dataset, never its own copy.
ALTERATION_GST_RATE = Decimal("5.00")
#: What an alteration line carries in place of a barcode, and how it reads.
ALTERATION_CODE = "ALTERATION"
ALTERATION_DESCRIPTION = "Alteration charge (SAC 9988)"


class BilledRetainedCustody(TimeStampedModel):
    """Goods a customer has bought that the store still holds (R-INV-014).

    Opened and closed by the work that holds them (today only an alteration job
    card). Never edited back: once closed, the outcome stands.
    """

    class Purpose(models.TextChoices):
        ALTERATION = "alteration", "Alteration"

    class Location(models.TextChoices):
        STORE = "store", "At the store"
        OUTSIDE_TAILOR = "outside_tailor", "With an outside tailor"

    class Outcome(models.TextChoices):
        HANDED_OVER = "handed_over", "Handed to the customer"
        CANCELLED = "cancelled", "Given back, work cancelled"

    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: The bill line the goods were sold on.
    sale_line = models.ForeignKey("sell.SaleLine", on_delete=models.PROTECT, related_name="+")
    qty = models.PositiveIntegerField()
    purpose = models.CharField(max_length=12, choices=Purpose.choices)
    customer_name = models.CharField(max_length=120)
    customer_mobile = models.CharField(max_length=15)
    location = models.CharField(max_length=16, choices=Location.choices, default=Location.STORE)
    #: When the customer expects to collect it.
    expected_by = models.DateField()
    opened_at = models.DateTimeField()
    opened_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    outcome = models.CharField(max_length=12, choices=Outcome.choices, blank=True, default="")

    class Meta:
        db_table = "sell_billed_retained_custody"
        ordering = ["expected_by", "opened_at"]
        indexes = [models.Index(fields=["store", "closed_at"], name="sell_custody_store_open_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(qty__gt=0), name="ck_custody_qty"),
            models.CheckConstraint(
                condition=models.Q(closed_at__isnull=True, outcome="")
                | (models.Q(closed_at__isnull=False) & ~models.Q(outcome="")),
                name="ck_custody_closed_iff_outcome",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.purpose} x{self.qty} ({self.outcome or 'held'})"

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class AlterationJob(TimeStampedModel):
    """One alteration job card, linked to the bill line of the garment."""

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        WITH_TAILOR = "with_tailor", "With the tailor"
        READY = "ready", "Ready"
        HANDED_OVER = "handed_over", "Handed over"
        CANCELLED = "cancelled", "Cancelled"

    class Tailor(models.TextChoices):
        IN_HOUSE = "in_house", "In-house"
        OUTSIDE = "outside", "Outside"

    #: The screen's own id, so a retry of the same save is the same job card.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: What staff and the customer quote: the store code and a running number.
    ref = models.CharField(max_length=32)
    #: The garment's line on its bill.
    garment_line = models.ForeignKey(
        "sell.SaleLine", on_delete=models.PROTECT, related_name="alteration_jobs"
    )
    qty = models.PositiveSmallIntegerField(default=1)
    #: The paid alteration's own bill line; none for a free alteration.
    charge_line = models.OneToOneField(
        "sell.SaleLine",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="alteration_job_charged",
    )
    #: What the customer paid for the work, GST included; nought when free.
    charge_paise = MoneyField(default=0)
    customer_name = models.CharField(max_length=120)
    customer_mobile = models.CharField(max_length=15, db_index=True)
    #: What to alter, in the staff's words.
    work = models.CharField(max_length=500)
    measurements = models.CharField(max_length=500, blank=True, default="")
    tailor = models.CharField(max_length=8, choices=Tailor.choices)
    tailor_name = models.CharField(max_length=120, blank=True, default="")
    promised_on = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RECEIVED)
    custody = models.OneToOneField(
        BilledRetainedCustody, on_delete=models.PROTECT, related_name="alteration_job"
    )
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    ready_at = models.DateTimeField(null=True, blank=True)
    #: When staff told the customer it is ready, and who told them.
    customer_told_at = models.DateTimeField(null=True, blank=True)
    customer_told_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancel_reason = models.CharField(max_length=240, blank=True, default="")

    class Meta:
        db_table = "sell_alteration_job"
        ordering = ["promised_on", "created_at"]
        indexes = [models.Index(fields=["store", "status"], name="sell_alter_store_status_idx")]
        constraints = [
            models.UniqueConstraint(fields=["store", "ref"], name="uq_alteration_store_ref"),
            models.CheckConstraint(condition=models.Q(qty__gt=0), name="ck_alteration_qty"),
            models.CheckConstraint(
                condition=models.Q(charge_line__isnull=True, charge_paise=0)
                | models.Q(charge_line__isnull=False, charge_paise__gt=0),
                name="ck_alteration_charge_iff_line",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=["received", "with_tailor", "ready"], closed_at__isnull=True
                )
                | models.Q(status__in=["handed_over", "cancelled"], closed_at__isnull=False),
                name="ck_alteration_closed_iff_ended",
            ),
            models.CheckConstraint(
                condition=models.Q(customer_told_at__isnull=True)
                | models.Q(ready_at__isnull=False),
                name="ck_alteration_told_after_ready",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.ref} ({self.status})"

    @property
    def is_open(self) -> bool:
        return self.status not in (self.Status.HANDED_OVER, self.Status.CANCELLED)
