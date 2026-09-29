"""Goods-v1 bookings (design §5.2).

A booking's header and lines live in the versioned document kernel. Progress is
derived from dated receipt links and their counters, never from mutable received
counters; a confirmed booking changes only by appended corrections. Legacy
``Booking`` rows remain read-only history.
"""

from __future__ import annotations

from django.db import models

from core.goods_base import EvidenceRow, TenantOwned


class GoodsBooking(TenantOwned):
    document = models.OneToOneField(
        "core.DocumentIdentity", on_delete=models.PROTECT, related_name="booking"
    )
    vendor = models.ForeignKey("vendors.Vendor", on_delete=models.PROTECT, related_name="+")
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=60, null=True, blank=True)


class BookingReceiptLink(EvidenceRow):
    booking = models.ForeignKey(
        GoodsBooking, on_delete=models.PROTECT, related_name="receipt_links"
    )
    booking_line_key = models.UUIDField()
    grn_line_key = models.UUIDField()
    grn = models.ForeignKey("core.DocumentIdentity", on_delete=models.PROTECT, related_name="+")
    linked_qty = models.IntegerField()
    counter_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="counters"
    )

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(condition=models.Q(linked_qty__gt=0), name="ck_receiptlink_qty"),
        ]
        indexes = [models.Index(fields=["booking", "booking_line_key"])]


class BookingCorrectionEvent(EvidenceRow):
    booking = models.ForeignKey(GoodsBooking, on_delete=models.PROTECT, related_name="corrections")
    prior_version = models.ForeignKey(
        "core.OfficialVersion", on_delete=models.PROTECT, related_name="+"
    )
    reason_code = models.CharField(max_length=60)
    effective_at = models.DateTimeField()
    payload = models.JSONField()
    evidence_ids = models.JSONField(default=list)

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["booking", "effective_at"])]
