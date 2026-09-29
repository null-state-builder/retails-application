"""The countersignature on a commitment (D11 §3).

A booking spends money on stock that does not exist yet, six to eight months
before it can be sold, and the ratified access table has always said the Owner
approves one while the Brand Manager places it. The code did not: a booking went
from draft to booked on its maker's own say-so. This is that promise kept, on the
spine every other family already uses.

The decision *is* the booking, the same way an approved offer is a published
offer: nothing else has to happen afterwards, so nothing else can be forgotten.
"""

from __future__ import annotations

from typing import Any

from approvals.hooks import register_on_approved
from approvals.models import Approval, ApprovalPolicy
from approvals.services import ApprovalError, ApprovalRequired, request_approval
from vendors.models import Booking

KIND = "booking"
LABEL = "Booking"


def _policy() -> ApprovalPolicy:
    try:
        return ApprovalPolicy.objects.get(kind=KIND)
    except ApprovalPolicy.DoesNotExist as exc:
        raise ApprovalRequired(
            "No approval policy is configured for bookings, so none can be placed."
        ) from exc


def headline(booking: Booking) -> str:
    """Brand, season, pieces and what it commits — in one line, at cost of MRP."""
    pieces = sum(line.booked_qty for line in booking.lines.all())
    value = booking.estimated_value_paise or 0
    worth = f" · ₹{value / 100:,.0f} at expected MRP" if value else ""
    return f"{booking.brand.name} {booking.season.code} · {booking.number} · {pieces} pcs{worth}"


def ask_for_approval(booking: Booking, *, requested_by: Any) -> Approval:
    policy = _policy()
    return request_approval(
        booking,
        kind=KIND,
        kind_label=LABEL,
        title=headline(booking),
        made_by=booking.created_by or requested_by,
        requested_by=requested_by,
        approver_roles=policy.approver_roles_for(0),
        store=booking.destination_store,
        brand=booking.brand.name,
        # Stated, not banded: the value is the *expected* one, and expected value
        # is not a threshold anybody should be able to slip under by typing a
        # smaller MRP. Every booking gets a second person (D11 Q8).
        value_paise=0,
    )


def place(booking: Booking, *, actor: Any) -> None:
    """Approved → booked. Idempotent, and never re-opens an ended booking."""
    if booking.status in (Booking.Status.CLOSED, Booking.Status.CANCELLED):
        raise ApprovalError(
            f"This booking was {booking.get_status_display().lower()} while it waited."
        )
    if booking.status != Booking.Status.SUBMITTED:
        return
    booking.status = Booking.Status.BOOKED
    booking.approved_by = actor
    booking.save(update_fields=["status", "approved_by", "updated_at"])


def register() -> None:
    register_on_approved(Booking, place)
