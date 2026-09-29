"""Brand payables for outright brands (store operations PRD ST-MNY-4; ticket 28).

Three tables, read together by ``finledger.payables``:

* ``PayableInvoice`` - one vendor tax invoice for one brand, received at one
  store: its number, date and total including tax, as Accounts typed it from the
  vendor's invoice. The model and payment days are read from the brand's terms
  in force on the invoice date for its season (ticket 23) and frozen here, with
  the due date they give. An invoice of a brand with no recorded model is kept
  as ``unknown`` and listed apart.
* ``PayablePayment`` - one payment Accounts made to one vendor for one store,
  outside the system: the day, the amount, how it was paid and the reference.
* ``PayableAllocation`` - how much of a payment went to which invoice. A payment
  can clear several of the vendor's invoices, of any brand; an invoice can be
  paid in parts.

Nothing is edited except to cancel: an invoice with nothing paid against it, or a
payment, is cancelled with a reason and stops counting. What an invoice owes and
how it falls due are frozen when it is recorded; what a payment paid and where
it went are frozen too; nothing is deleted. Migration 0008 puts all three tables
behind forced row-level security and a trigger that enforces this.
"""

from __future__ import annotations

from django.db import models
from django.db.models.functions import Lower, Trim

from core.base import TimeStampedModel
from core.goods_base import CurrentTenant
from core.money import MoneyField


def _tenant() -> models.ForeignKey:  # type: ignore[type-arg]
    return models.ForeignKey(
        "masters.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
        db_default=CurrentTenant(),
    )


def _user() -> models.ForeignKey:  # type: ignore[type-arg]
    return models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )


class PayableInvoice(TimeStampedModel):
    class Model(models.TextChoices):
        OUTRIGHT = "outright", "Outright"
        #: No approved terms for the brand and season on the invoice date (D9).
        UNKNOWN = "unknown", "Model unknown"

    class Status(models.TextChoices):
        OPEN = "open", "Recorded"
        CANCELLED = "cancelled", "Cancelled"

    tenant = _tenant()
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    vendor = models.ForeignKey("vendors.Vendor", on_delete=models.PROTECT, related_name="+")
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    #: The season whose terms apply to the goods on the invoice.
    season = models.ForeignKey("masters.Season", on_delete=models.PROTECT, related_name="+")
    invoice_number = models.CharField(max_length=60)
    invoice_date = models.DateField()
    #: The invoice's total including tax, in paise.
    amount_paise = MoneyField()
    #: Frozen from the terms in force on the invoice date when it was recorded.
    model = models.CharField(max_length=10, choices=Model.choices)
    terms_version_id = models.UUIDField(null=True, blank=True)
    #: Null: the terms leave payment days blank, so the due date is unknown.
    payment_days = models.PositiveIntegerField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=240, blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    #: Bumped by every change, so a stale screen is refused.
    revision = models.PositiveIntegerField(default=1)
    recorded_by = _user()
    cancelled_by = _user()
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=240, blank=True, default="")

    class Meta:
        db_table = "finledger_payable_invoice"
        ordering = ["invoice_date", "id"]
        indexes = [
            models.Index(fields=["store", "vendor"]),
            models.Index(fields=["brand"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="ck_payableinvoice_amount"
            ),
            models.CheckConstraint(
                condition=models.Q(model__in=["outright", "unknown"]),
                name="ck_payableinvoice_model",
            ),
            # The due date is there exactly when the payment days are.
            models.CheckConstraint(
                condition=models.Q(payment_days__isnull=True, due_date__isnull=True)
                | models.Q(payment_days__isnull=False, due_date__isnull=False),
                name="ck_payableinvoice_due",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="cancelled") | ~models.Q(cancel_reason=""),
                name="ck_payableinvoice_cancel_reason",
            ),
            # One live invoice per vendor and number, compared the way people type it.
            models.UniqueConstraint(
                "tenant",
                "vendor",
                Lower(Trim("invoice_number")),
                condition=models.Q(status="open"),
                name="uq_payableinvoice_vendor_number",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.invoice_number} ({self.vendor_id})"


class PayablePayment(TimeStampedModel):
    class Mode(models.TextChoices):
        BANK = "bank", "Bank transfer"
        CHEQUE = "cheque", "Cheque"
        UPI = "upi", "UPI"
        CASH = "cash", "Cash"

    class Status(models.TextChoices):
        RECORDED = "recorded", "Recorded"
        CANCELLED = "cancelled", "Cancelled"

    tenant = _tenant()
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    vendor = models.ForeignKey("vendors.Vendor", on_delete=models.PROTECT, related_name="+")
    paid_on = models.DateField()
    amount_paise = MoneyField()
    mode = models.CharField(max_length=10, choices=Mode.choices)
    #: The bank's UTR, the cheque number or the UPI reference.
    reference = models.CharField(max_length=60)
    note = models.CharField(max_length=240, blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECORDED)
    revision = models.PositiveIntegerField(default=1)
    recorded_by = _user()
    cancelled_by = _user()
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=240, blank=True, default="")

    class Meta:
        db_table = "finledger_payable_payment"
        ordering = ["-paid_on", "-id"]
        indexes = [models.Index(fields=["store", "vendor"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="ck_payablepayment_amount"
            ),
            models.CheckConstraint(
                condition=~models.Q(status="cancelled") | ~models.Q(cancel_reason=""),
                name="ck_payablepayment_cancel_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.reference} ({self.vendor_id})"


class PayableAllocation(models.Model):
    """How much of one payment went to one invoice."""

    tenant = _tenant()
    payment = models.ForeignKey(
        PayablePayment, on_delete=models.PROTECT, related_name="allocations"
    )
    invoice = models.ForeignKey(
        PayableInvoice, on_delete=models.PROTECT, related_name="allocations"
    )
    amount_paise = MoneyField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "finledger_payable_allocation"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="ck_payableallocation_amount"
            ),
            models.UniqueConstraint(
                fields=["payment", "invoice"], name="uq_payableallocation_payment_invoice"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.payment_id} -> {self.invoice_id}"
