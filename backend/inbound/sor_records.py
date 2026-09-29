"""Recording what SOR ageing needs about a delivery (store operations ticket 24, ST-BRD-5).

Two facts, each a new ``EvidenceRow`` every time it is recorded, never an edit:

* the brand's dispatch date - at receiving (``record_arrival`` passes it here) or
  afterwards for a delivery received without one (``record_dispatch_date``);
* the brand's invoice that settles the delivery's SOR pieces (``record_brand_invoice``).

Changing either is a new row with a reason; the audit record of each write holds
the value before and after. The date checks are the same everywhere: the brand
cannot have dispatched after the goods arrived, nor on a day still to come.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.db.models.functions import Now
from django.utils import timezone

from core.commands import CommandRun, LockRank
from core.refusals import Refusal, issue
from inbound.goods_models import Arrival
from inbound.sor_models import BrandDispatchDate, SorBrandInvoice

AUDIT_DISPATCH = "inbound.brand_dispatch_date.record"
AUDIT_INVOICE = "inbound.sor_brand_invoice.record"
REASON_LENGTH = 240
NUMBER_LENGTH = 80


def arrived_on(arrival: Arrival) -> date:
    return timezone.localtime(arrival.actual_arrival_at).date()


def dispatch_problems(dispatch: date, arrived: date, today: date) -> list[dict[str, Any]]:
    """Why a dispatch date cannot stand for goods that arrived on ``arrived``."""
    if dispatch > today:
        return [
            issue(
                "IN_FUTURE",
                "The brand's dispatch date cannot be in the future.",
                field="brand_dispatch_date",
            )
        ]
    if dispatch > arrived:
        return [
            issue(
                "AFTER_ARRIVAL",
                "The brand cannot have dispatched the goods after they arrived.",
                field="brand_dispatch_date",
            )
        ]
    return []


def standing_dispatch(arrival_id: Any) -> BrandDispatchDate | None:
    return (
        BrandDispatchDate.objects.filter(arrival_id=arrival_id)
        .order_by("-recorded_at", "-event_at")
        .first()
    )


def standing_invoice(arrival_id: Any) -> SorBrandInvoice | None:
    return (
        SorBrandInvoice.objects.filter(arrival_id=arrival_id)
        .order_by("-recorded_at", "-event_at")
        .first()
    )


def _refuse_if_changed_meanwhile(model: Any, arrival_id: Any) -> None:
    """Refuse when another save for this delivery committed after this one began.

    A row's ``recorded_at`` is its transaction's start, and the lock is taken
    later, so without this a save that waited for the lock would be stored as
    older than the one it waited for: the value shown would not be its after."""
    if model.objects.filter(arrival_id=arrival_id, recorded_at__gt=Now()).exists():
        raise Refusal(
            "CHANGED_MEANWHILE",
            "Somebody recorded this for the same delivery a moment ago. Read it again.",
            status=409,
        )


def _reason(before: object | None, reason: str) -> str:
    """A change needs a reason; the first record does not."""
    text = reason.strip()
    if before is not None and not text:
        raise Refusal(
            "INVALID_REQUEST",
            "Say why the recorded value is being changed.",
            status=422,
            issues=[issue("REASON_REQUIRED", "A reason is required for a change.", field="reason")],
        )
    if len(text) > REASON_LENGTH:
        raise Refusal(
            "INVALID_REQUEST",
            f"The reason can be at most {REASON_LENGTH} characters.",
            status=422,
            issues=[issue("TOO_LONG", "reason is too long", field="reason")],
        )
    return text


def record_dispatch_date(
    run: CommandRun, arrival: Arrival, dispatch: date, reason: str = ""
) -> BrandDispatchDate:
    """Record the brand's dispatch date for ``arrival``, audited before and after."""
    run.advisory_lock(LockRank.DOCUMENT, [f"sor-delivery:{arrival.pk}"])
    _refuse_if_changed_meanwhile(BrandDispatchDate, arrival.pk)
    problems = dispatch_problems(dispatch, arrived_on(arrival), timezone.localdate(run.now))
    if problems:
        raise Refusal(
            "DISPATCH_DATE_INVALID",
            "The brand's dispatch date cannot be recorded as entered.",
            status=422,
            issues=problems,
        )
    before = standing_dispatch(arrival.pk)
    text = _reason(before, reason)
    if before is not None and before.dispatch_date == dispatch:
        raise Refusal(
            "NO_CHANGE",
            "That is already the recorded dispatch date for this delivery.",
            status=409,
        )
    row = run.record(BrandDispatchDate(arrival_id=arrival.pk, dispatch_date=dispatch, reason=text))
    run.audit_before = {
        "arrival_id": str(arrival.pk),
        "brand_dispatch_date": before.dispatch_date.isoformat() if before else None,
    }
    run.audit_after = {
        "arrival_id": str(arrival.pk),
        "brand_dispatch_date": dispatch.isoformat(),
        **({"reason": text} if text else {}),
    }
    return row  # type: ignore[no-any-return]


def record_brand_invoice(
    run: CommandRun, arrival: Arrival, number: str, invoice_date: date, reason: str = ""
) -> SorBrandInvoice:
    """Record the brand's invoice settling ``arrival``'s SOR pieces, audited before and after."""
    run.advisory_lock(LockRank.DOCUMENT, [f"sor-delivery:{arrival.pk}"])
    _refuse_if_changed_meanwhile(SorBrandInvoice, arrival.pk)
    problems: list[dict[str, Any]] = []
    text_number = number.strip()
    if not text_number:
        problems.append(
            issue("REQUIRED", "The brand's invoice number is required.", field="invoice_number")
        )
    elif len(text_number) > NUMBER_LENGTH:
        problems.append(
            issue(
                "TOO_LONG",
                f"The invoice number can be at most {NUMBER_LENGTH} characters.",
                field="invoice_number",
            )
        )
    if invoice_date > timezone.localdate(run.now):
        problems.append(
            issue("IN_FUTURE", "The invoice date cannot be in the future.", field="invoice_date")
        )
    if problems:
        raise Refusal(
            "BRAND_INVOICE_INVALID",
            "The brand's invoice cannot be recorded as entered.",
            status=422,
            issues=problems,
        )
    before = standing_invoice(arrival.pk)
    text = _reason(before, reason)
    if (
        before is not None
        and before.invoice_number == text_number
        and before.invoice_date == invoice_date
    ):
        raise Refusal(
            "NO_CHANGE", "That brand invoice is already recorded for this delivery.", status=409
        )
    row = run.record(
        SorBrandInvoice(
            arrival_id=arrival.pk,
            invoice_number=text_number,
            invoice_date=invoice_date,
            reason=text,
        )
    )

    def shown(value: SorBrandInvoice | None) -> dict[str, str] | None:
        if value is None:
            return None
        return {"number": value.invoice_number, "invoice_date": value.invoice_date.isoformat()}

    run.audit_before = {"arrival_id": str(arrival.pk), "brand_invoice": shown(before)}
    run.audit_after = {
        "arrival_id": str(arrival.pk),
        "brand_invoice": shown(row),
        **({"reason": text} if text else {}),
    }
    return row  # type: ignore[no-any-return]
