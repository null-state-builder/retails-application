"""Which bills and consent answers are one customer's (store operations ticket 17).

A bill keeps the number printed on it forever (Rule 3), so a customer's history
is found through numbers, never by rewriting a bill:

* **their own number** - from the moment it became theirs. A recycled number
  (somebody moved away from it, ``CustomerNumber`` reason ``moved``) is theirs
  only after that person's move, so a new holder never sees the old holder's
  bills or answers;
* **a merged number** - a record merged into theirs. Still their number, so
  everything on it is theirs (again, only after any earlier holder moved away);
* **a moved number** - the one they moved away from, up to the move.

Bills and answers at the moment of a move belong to the one who moved: the
move's own withdrawal of the old number's consents is recorded at that moment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Max, Q

from masters.models import Customer, CustomerNumber


@dataclass(frozen=True)
class Number:
    mobile: str
    #: ``own``, ``merged`` or ``moved``.
    reason: str
    #: Theirs only after this moment (an earlier holder's move), or always.
    since: datetime | None
    #: Theirs only up to this moment (they moved away), or still.
    until: datetime | None


def _taken_over_at(mobile: str, customer: Customer, before: datetime | None) -> datetime | None:
    """When somebody else last moved away from this number (before ``before``,
    the end of this customer's own time on it, if it has ended), if ever. A
    handover whose mover was since erased still counts (``customer`` null)."""
    rows = CustomerNumber.objects.filter(mobile=mobile, reason=CustomerNumber.Reason.MOVED).filter(
        Q(customer__isnull=True) | ~Q(customer=customer)
    )
    if before is not None:
        rows = rows.filter(until__lt=before)
    at: datetime | None = rows.aggregate(at=Max("until"))["at"]
    return at


def _later(*moments: datetime | None) -> datetime | None:
    known = [moment for moment in moments if moment is not None]
    return max(known) if known else None


def numbers_of(customer: Customer) -> list[Number]:
    """Every number whose bills and answers are this customer's, own number first."""
    rows = [
        Number(
            customer.mobile,
            "own",
            _later(_taken_over_at(customer.mobile, customer, None), customer.mobile_since),
            None,
        )
    ]
    for other in customer.other_numbers.order_by("recorded_at", "id"):
        since = _later(_taken_over_at(other.mobile, customer, other.until), other.since)
        rows.append(Number(other.mobile, other.reason, since, other.until))
    return rows


def _window(numbers: list[Number], mobile_field: str, time_field: str) -> Q:
    matched = Q(pk__in=[])
    for number in numbers:
        one = Q(**{mobile_field: number.mobile})
        if number.since is not None:
            one &= Q(**{f"{time_field}__gt": number.since})
        if number.until is not None:
            one &= Q(**{f"{time_field}__lte": number.until})
        matched |= one
    return matched


def bills_q(numbers: list[Number]) -> Q:
    """The bills (``sell.Sale``) that are this customer's."""
    return _window(numbers, "customer_mobile", "billed_at")


def answers_q(numbers: list[Number]) -> Q:
    """The consent answers (``sell.ConsentAnswer``) that are this customer's."""
    return _window(numbers, "mobile", "answered_at")


def not_the_holders(customer: Customer, made_at: datetime | None) -> bool:
    """Was a bill made then, on this customer's own number, somebody else's: made
    before they moved onto it, or before an earlier holder moved away from it?"""
    if made_at is None:
        return False
    if customer.mobile_since is not None and made_at <= customer.mobile_since:
        return True
    return moved_away_after(customer.mobile, made_at)


def moved_away_after(mobile: str, made_at: datetime | None) -> bool:
    """Did somebody move away from this number at or after ``made_at``? Then a
    bill made then is theirs, and must not bring a record for the number back."""
    if made_at is None:
        return False
    return CustomerNumber.objects.filter(
        mobile=mobile, reason=CustomerNumber.Reason.MOVED, until__gte=made_at
    ).exists()
