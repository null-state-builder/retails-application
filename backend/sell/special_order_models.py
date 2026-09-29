"""Special orders (store operations ticket 21, ST-ORD-2).

A customer asks for an item no store has. Staff record what they want - brand,
style, size and colour - and, if the customer pays one, an advance with a
receipt voucher (``ReceiptVoucher`` and ``AdvanceMovement``, shared with ticket
20's reservations). The order then becomes a transfer request or a booking
line, and is tracked to collection:

    asked -> ordered -> arrived -> customer told -> collected
                 (or cancelled from any open step)

Nothing is held while the order is open: the item is not in the store yet. It
is collected on a normal bill, where the advance is used as a tender.
"""

from __future__ import annotations

import uuid

from django.db import models

from core.base import TimeStampedModel


class SpecialOrder(TimeStampedModel):
    class Status(models.TextChoices):
        ASKED = "asked", "Asked"
        ORDERED = "ordered", "Ordered"
        ARRIVED = "arrived", "Arrived"
        TOLD = "told", "Customer told"
        COLLECTED = "collected", "Collected"
        CANCELLED = "cancelled", "Cancelled"

    class Route(models.TextChoices):
        #: A transfer request to the site that holds the item.
        TRANSFER = "transfer", "Transfer request"
        #: A line on a booking a buyer placed with the brand.
        BOOKING = "booking", "Booking line"

    class Told(models.TextChoices):
        CALL = "call", "Phone call"
        MESSAGE = "message", "Message"
        IN_PERSON = "in_person", "In person"

    class CloseReason(models.TextChoices):
        COLLECTED = "collected", "Collected on a bill"
        CUSTOMER_CANCELLED = "customer_cancelled", "Cancelled by the customer"
        STORE_CANCELLED = "store_cancelled", "Cancelled by the store"

    #: The screen's own id, so a replay is the same order.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    #: What staff and the customer quote: the store code and a running number.
    ref = models.CharField(max_length=32)
    customer_name = models.CharField(max_length=120)
    customer_mobile = models.CharField(max_length=15, db_index=True)
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    style = models.CharField(max_length=80)
    size = models.CharField(max_length=24)
    colour = models.CharField(max_length=40)
    note = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ASKED)
    asked_on = models.DateField()
    #: Ticket 20's advance policy, frozen when the order was taken.
    advance_policy = models.CharField(
        max_length=8, choices=[("refund", "Refunded"), ("keep", "Kept")]
    )
    #: The terms as the customer was given them, frozen.
    terms = models.TextField()
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    # -- ordered ---------------------------------------------------------------
    route = models.CharField(max_length=10, choices=Route.choices, blank=True, default="")
    transfer_request = models.ForeignKey(
        "outbound.TransferRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    #: The barcode asked for on the transfer request.
    ordered_barcode = models.CharField(max_length=64, blank=True, default="")
    booking = models.ForeignKey(
        "vendors.GoodsBooking", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    booking_line_key = models.UUIDField(null=True, blank=True)
    ordered_at = models.DateTimeField(null=True, blank=True)
    ordered_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    # -- arrived ---------------------------------------------------------------
    #: The piece that came for the customer; the collection bill must carry it.
    arrived_barcode = models.CharField(max_length=64, blank=True, default="")
    arrived_at = models.DateTimeField(null=True, blank=True)
    arrived_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    # -- customer told ---------------------------------------------------------
    told_how = models.CharField(max_length=10, choices=Told.choices, blank=True, default="")
    told_at = models.DateTimeField(null=True, blank=True)
    told_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    # -- closed ----------------------------------------------------------------
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    close_reason = models.CharField(
        max_length=20, choices=CloseReason.choices, blank=True, default=""
    )
    #: The collection bill.
    sale = models.ForeignKey(
        "sell.Sale", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "sell_special_order"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["store", "status"], name="sell_sord_store_status_idx")]
        constraints = [
            models.UniqueConstraint(fields=["store", "ref"], name="uq_special_order_store_ref"),
            models.CheckConstraint(
                condition=models.Q(advance_policy__in=["refund", "keep"]),
                name="ck_special_order_policy",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(route="", transfer_request__isnull=True, booking__isnull=True)
                    | models.Q(
                        route="transfer", transfer_request__isnull=False, booking__isnull=True
                    )
                    | models.Q(
                        route="booking",
                        booking__isnull=False,
                        booking_line_key__isnull=False,
                        transfer_request__isnull=True,
                    )
                ),
                name="ck_special_order_route_links",
            ),
            models.CheckConstraint(
                condition=~models.Q(status__in=["ordered", "arrived", "told", "collected"])
                | ~models.Q(route=""),
                name="ck_special_order_ordered_has_route",
            ),
            models.CheckConstraint(
                condition=~models.Q(status__in=["arrived", "told", "collected"])
                | ~models.Q(arrived_barcode=""),
                name="ck_special_order_arrived_has_piece",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=["collected", "cancelled"],
                    closed_at__isnull=False,
                )
                & ~models.Q(close_reason="")
                | models.Q(
                    status__in=["asked", "ordered", "arrived", "told"],
                    closed_at__isnull=True,
                    close_reason="",
                ),
                name="ck_special_order_closed_iff_ended",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    sale__isnull=True,
                    status__in=["asked", "ordered", "arrived", "told", "cancelled"],
                )
                | models.Q(sale__isnull=False, status="collected"),
                name="ck_special_order_sale_iff_collected",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.ref} ({self.status})"

    @property
    def is_open(self) -> bool:
        return self.status not in (self.Status.COLLECTED, self.Status.CANCELLED)
