"""Customer reservations (store operations ticket 20, ST-ORD-1).

Staff hold specific accepted pieces at a store for a named customer. It is a
stock reservation, not a sale: the pieces leave available-to-sell under a hold
(R-INV-003) and nothing is billed until the customer collects them.

Three records, each answering one question:

* ``CustomerReservation`` - who the pieces are held for, until when, and what
  became of the reservation. Its status moves forward only: active, then
  collected, cancelled or expired.
* ``ReservationPiece`` - which pieces are held, as the counter would bill them,
  and the exact portions of stock the hold covers.
* ``ReceiptVoucher`` and ``AdvanceMovement`` - the advance. The voucher is the
  RV-series document issued when the advance is taken (no GST: Notification
  66/2017-CT). Every later change to what is held - used on the pickup bill,
  refunded, forfeited - is its own movement row, never an edited total, so the
  balance is the sum of the rows.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db import models

from core.base import TimeStampedModel
from core.documents import Document, MintedNumber
from core.money import MoneyField

#: The receipt voucher's GL document type (ST-CMP-5 series RV).
RECEIPT_VOUCHER_DOC_TYPE = "RV"


class CustomerReservation(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Reserved"
        COLLECTED = "collected", "Collected"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    class Policy(models.TextChoices):
        #: The advance is given back on expiry or on the customer's cancellation.
        REFUND = "refund", "Refunded"
        #: The advance is kept (forfeited) on expiry or on the customer's cancellation.
        KEEP = "keep", "Kept"

    class CloseReason(models.TextChoices):
        COLLECTED = "collected", "Collected on a bill"
        CUSTOMER_CANCELLED = "customer_cancelled", "Cancelled by the customer"
        STORE_CANCELLED = "store_cancelled", "Cancelled by the store"
        EXPIRED = "expired", "Not collected by the date"

    #: The till's or screen's own id, so a replay is the same reservation.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: What staff and the customer quote: the store code and a running number.
    ref = models.CharField(max_length=32)
    customer_name = models.CharField(max_length=120)
    customer_mobile = models.CharField(max_length=15, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    reserved_on = models.DateField()
    #: The last day the pieces can be collected, inclusive.
    collect_by = models.DateField()
    days = models.PositiveSmallIntegerField()
    #: A sale period was running at the store when it was made (the shorter length).
    sale_period = models.BooleanField(default=False)
    advance_policy = models.CharField(max_length=8, choices=Policy.choices)
    #: The terms as the customer was given them, frozen.
    terms = models.TextField()
    #: The goods document that authorises the hold and its end.
    hold_version = models.ForeignKey(
        "core.OfficialVersion", on_delete=models.PROTECT, related_name="+"
    )
    hold_key = models.UUIDField(default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    close_reason = models.CharField(
        max_length=20, choices=CloseReason.choices, blank=True, default=""
    )
    #: The pickup bill, once collected.
    sale = models.ForeignKey(
        "sell.Sale", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "sell_customer_reservation"
        ordering = ["collect_by", "created_at"]
        indexes = [models.Index(fields=["store", "status"], name="sell_resv_store_status_idx")]
        constraints = [
            # Store codes are unique within a company, so the reference is too.
            models.UniqueConstraint(fields=["store", "ref"], name="uq_reservation_store_ref"),
            models.CheckConstraint(
                condition=models.Q(collect_by__gte=models.F("reserved_on")),
                name="ck_reservation_collect_after_reserved",
            ),
            models.CheckConstraint(
                condition=models.Q(status="active", closed_at__isnull=True, close_reason="")
                | (
                    ~models.Q(status="active")
                    & models.Q(closed_at__isnull=False)
                    & ~models.Q(close_reason="")
                ),
                name="ck_reservation_closed_iff_not_active",
            ),
            models.CheckConstraint(
                condition=models.Q(sale__isnull=True) | models.Q(status="collected"),
                name="ck_reservation_sale_only_when_collected",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.ref} ({self.status})"


class ReservationPiece(models.Model):
    """One reserved piece line: what it is, and the stock portions held for it."""

    reservation = models.ForeignKey(
        CustomerReservation, on_delete=models.CASCADE, related_name="pieces"
    )
    line_no = models.PositiveSmallIntegerField()
    barcode = models.CharField(max_length=64)
    season = models.CharField(max_length=24, blank=True, default="")
    sku_id = models.UUIDField()
    qty = models.PositiveIntegerField()
    #: The piece as the counter bills it (design, brand, item, size, colour, HSN,
    #: MRP), frozen when reserved - no cost.
    item = models.JSONField(default=dict)
    #: ``[{lot_id, lower, upper}]`` - the exact stock portions under the hold.
    portions = models.JSONField(default=list)

    class Meta:
        db_table = "sell_reservation_piece"
        ordering = ["reservation_id", "line_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["reservation", "line_no"], name="uq_reservation_piece_line"
            ),
            models.CheckConstraint(condition=models.Q(qty__gt=0), name="ck_reservation_piece_qty"),
        ]

    def __str__(self) -> str:
        return f"{self.barcode} x{self.qty}"


class ReceiptVoucher(Document):
    """The RV-series receipt voucher for an advance (ST-CMP-5): a reservation's
    (ticket 20) or a special order's (ticket 21), never both.

    Issued once, when the advance is taken, and never changed. Its number comes
    from the store's RV series (``masters.document_series.issue_number``) and is
    set before the voucher is posted; ``mint_number`` only hands it over.
    """

    reservation = models.OneToOneField(
        CustomerReservation,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="voucher",
    )
    special_order = models.OneToOneField(
        "sell.SpecialOrder",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="voucher",
    )
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    number = models.CharField(max_length=32, unique=True)
    issued_on = models.DateField()
    amount_paise = MoneyField()
    mode = models.CharField(max_length=8)
    reference = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(Document.Meta):
        db_table = "sell_receipt_voucher"
        ordering = ["-created_at"]
        constraints = [
            *Document.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="ck_receiptvoucher_amount_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(mode__in=["cash", "card", "upi"]),
                name="ck_receiptvoucher_mode",
            ),
            models.CheckConstraint(
                condition=models.Q(reservation__isnull=False, special_order__isnull=True)
                | models.Q(reservation__isnull=True, special_order__isnull=False),
                name="ck_receiptvoucher_one_holder",
            ),
        ]

    def __str__(self) -> str:
        return self.number

    @property
    def doc_type(self) -> str:
        return RECEIPT_VOUCHER_DOC_TYPE

    def mint_number(self) -> MintedNumber:
        # The number was issued from the RV series before posting; nothing to allocate.
        return MintedNumber(series=None, doc_number=self.number)  # type: ignore[arg-type]


class AdvanceMovement(models.Model):
    """One change to what an advance holds - a reservation's or a special
    order's (ticket 21). Append-only, in the database too (the shared ledger
    trigger, migration 0036)."""

    class Kind(models.TextChoices):
        RECEIVED = "received", "Advance received"
        USED = "used", "Used on the pickup bill"
        REFUNDED = "refunded", "Refunded"
        FORFEITED = "forfeited", "Forfeited"

    reservation = models.ForeignKey(
        CustomerReservation,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="advance_movements",
    )
    special_order = models.ForeignKey(
        "sell.SpecialOrder",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="advance_movements",
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    #: Cash, card or UPI for money received or refunded; blank when used or forfeited.
    mode = models.CharField(max_length=8, blank=True, default="")
    amount_paise = MoneyField()
    reference = models.CharField(max_length=64, blank=True, default="")
    sale = models.ForeignKey(
        "sell.Sale", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    at = models.DateTimeField()
    by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "sell_reservation_advance"
        ordering = ["reservation_id", "at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_paise__gt=0), name="ck_advance_amount_positive"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind__in=["received", "refunded"], mode__in=["cash", "card", "upi"])
                    | models.Q(kind__in=["used", "forfeited"], mode="")
                ),
                name="ck_advance_mode_by_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(sale__isnull=True) | models.Q(kind="used"),
                name="ck_advance_sale_only_when_used",
            ),
            models.CheckConstraint(
                condition=models.Q(reservation__isnull=False, special_order__isnull=True)
                | models.Q(reservation__isnull=True, special_order__isnull=False),
                name="ck_advance_one_holder",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.amount_paise}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValueError("an advance movement is never changed; add a new one")
        super().save(*args, **kwargs)
