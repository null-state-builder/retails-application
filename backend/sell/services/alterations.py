"""Alterations (store operations ticket 22, ST-ORD-3).

**The job card.** Made after the garment is billed, from its bill: staff find
the bill, pick the garment's line and say what to alter, the measurements, the
tailor (in-house or outside) and the promised date. Where the customer paid for
the work, the job card takes the paid alteration's own bill line - made at the
till like any line, at 5% under SAC 9988 - and its charge; a free alteration has
no line, no charge and no tax (baseline, CA to confirm).

**Where the work stands.** Received, with the tailor, ready, handed over - or
cancelled from any open step. When staff tell the customer it is ready, the job
card records when and who.

**Custody.** From the moment the job card is made until handover or
cancellation the store holds goods the customer has bought: a
``BilledRetainedCustody`` row (R-INV-014) says whose they are, where they are
(at the store, or with an outside tailor), when the customer expects them and
what finally became of them. The garment was sold on its bill, so it is not
stock: nothing here moves stock.

**Online only.** Every write is a request to head office; nothing is queued on
a device. Making a job card needs the store's ``alterations`` switch, gated on
OQ-49 and the CA's sign-off; moving one already made never does (ticket 01's
rule: refuse only new work). Every write is one command, audited with the job
card before and after.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db.models import Q, QuerySet, Sum
from django.utils import timezone

from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.documents import DocStatus
from core.refusals import Refusal
from masters.models import Store
from masters.store_feature_registry import ALTERATIONS
from masters.store_features import require_feature
from sell.alteration_models import AlterationJob, BilledRetainedCustody
from sell.models import Sale, SaleLine
from sell.services.customers import normalise_mobile
from sell.services.refunds import returned_so_far, with_returned

CREATE_ACTION = "sell.alteration.create"
SEND_ACTION = "sell.alteration.send_to_tailor"
READY_ACTION = "sell.alteration.ready"
TOLD_ACTION = "sell.alteration.customer_told"
HAND_OVER_ACTION = "sell.alteration.hand_over"
CANCEL_ACTION = "sell.alteration.cancel"

Status = AlterationJob.Status
OPEN = (Status.RECEIVED, Status.WITH_TAILOR, Status.READY)


def _refuse(code: str, message: str, status: int = 422) -> Refusal:
    return Refusal(code, message, status=status)


def masked(mobile: str) -> str:
    return "*" * max(len(mobile) - 4, 0) + mobile[-4:]


def snapshot(job: AlterationJob) -> dict[str, Any]:
    """The job card as the audit log keeps it. The number shows its last four digits."""
    custody = job.custody
    return {
        "ref": job.ref,
        "status": job.status,
        "bill": job.garment_line.sale.doc_number,
        "line_no": job.garment_line.line_no,
        "qty": job.qty,
        "charge_bill": job.charge_line.sale.doc_number if job.charge_line else None,
        "charge_line_no": job.charge_line.line_no if job.charge_line else None,
        "charge_paise": int(job.charge_paise),
        "customer_name": job.customer_name,
        "customer_mobile": masked(job.customer_mobile),
        "work": job.work,
        "measurements": job.measurements,
        "tailor": job.tailor,
        "tailor_name": job.tailor_name,
        "promised_on": job.promised_on.isoformat(),
        "ready_at": job.ready_at.isoformat() if job.ready_at else None,
        "customer_told_at": job.customer_told_at.isoformat() if job.customer_told_at else None,
        "cancel_reason": job.cancel_reason or None,
        "custody": {
            "location": custody.location,
            "outcome": custody.outcome or None,
        },
    }


# ---------------------------------------------------------------------------
# Who does it
# ---------------------------------------------------------------------------


def _person(actor: Any) -> Principal:
    """The named person making the change."""
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None)
    if human_id is None or tenant_id is None:
        raise _refuse(
            "SCOPE_DENIED",
            "This login is not a person in the records, so it cannot take in or hand over "
            "a customer's garment.",
            403,
        )
    return Principal(tenant_id=tenant_id, human_id=human_id, user_id=getattr(actor, "pk", None))


def _user(actor: Any) -> Any:
    return actor if getattr(actor, "is_authenticated", False) else None


# ---------------------------------------------------------------------------
# Finding the bill
# ---------------------------------------------------------------------------


def find_bill(store: Store, number: str) -> Sale | None:
    """A bill of this store that stands, by any number it was printed with."""
    number = (number or "").strip()
    if not number:
        return None
    return (
        Sale.objects.filter(store=store, docstatus=DocStatus.SUBMITTED)
        .filter(
            Q(doc_number__iexact=number)
            | Q(tax_invoice_number__iexact=number)
            | Q(till_number__iexact=number)
        )
        .order_by("-billed_at")
        .first()
    )


def open_job_qty(line: SaleLine) -> int:
    """Pieces of this garment line the store still holds on open job cards."""
    return int(
        AlterationJob.objects.filter(garment_line=line, status__in=OPEN).aggregate(n=Sum("qty"))[
            "n"
        ]
        or 0
    )


def free_to_alter(line: SaleLine) -> int:
    """Pieces of a sold garment line not given back and not on an open job card.

    A garment handed over can come back for another alteration later.
    """
    returned = returned_so_far(line)[0]
    return max(int(line.qty) - returned - open_job_qty(line), 0)


@dataclass(frozen=True)
class BillLines:
    """A bill as the job card form offers it: garments and unused charges."""

    sale: Sale
    #: ``(line, pieces free to alter)`` for every sold garment line.
    garments: list[tuple[SaleLine, int]]
    #: Alteration charge lines no job card has taken yet.
    charges: list[SaleLine]


def bill_lines(sale: Sale) -> BillLines:
    """What of ``sale`` can go on a new job card - read only, nothing locked.

    Advisory, like any figure a screen shows: ``create`` decides again under
    the store's lock (``free_to_alter``).
    """
    lines = list(
        with_returned(
            SaleLine.objects.filter(sale=sale, direction=SaleLine.Direction.SALE).order_by(
                "line_no"
            )
        )
    )
    taken = dict(
        AlterationJob.objects.filter(garment_line__sale=sale, status__in=OPEN)
        .values("garment_line")
        .annotate(n=Sum("qty"))
        .values_list("garment_line", "n")
    )
    used = set(
        AlterationJob.objects.filter(charge_line__sale=sale).values_list("charge_line", flat=True)
    )
    garments = [
        (
            line,
            max(
                int(line.qty)
                - int(getattr(line, "returned_qty", 0) or 0)
                - int(taken.get(line.pk) or 0),
                0,
            ),
        )
        for line in lines
        if not line.is_alteration
    ]
    charges = [line for line in lines if line.is_alteration and line.pk not in used]
    return BillLines(sale=sale, garments=garments, charges=charges)


# ---------------------------------------------------------------------------
# Making a job card
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Made:
    job: AlterationJob
    created: bool


def create(store: Store, actor: Any, data: dict[str, Any]) -> Made:
    """Open a job card on the garment's bill line, and custody of the garment.

    ``data``: ``id``, ``bill`` and ``line_no`` (the garment), ``qty``,
    ``charge_bill`` and ``charge_line_no`` (the paid alteration's own line, or
    none for a free one), ``customer_name``, ``customer_mobile``, ``work``,
    ``measurements``, ``tailor``, ``tailor_name``, ``promised_on``. A replay of
    the same id answers with the job card it made.
    """
    job_id: uuid.UUID = data["id"]
    existing = AlterationJob.objects.filter(pk=job_id).first()
    if existing is not None:
        if not _same_job(existing, store, data):
            raise _refuse(
                "ALTERATION_CONFLICT",
                "This job card id is already used for a different job card.",
                409,
            )
        return Made(job=existing, created=False)
    require_feature(store, ALTERATIONS)
    name, mobile = _customer(data)
    work = str(data.get("work") or "").strip()
    if not work:
        raise _refuse("VALIDATION", "Say what to alter.", 400)
    measurements = str(data.get("measurements") or "").strip()
    tailor, tailor_name = _tailor(data)
    promised_on: date = data["promised_on"]
    today = timezone.localdate()
    if promised_on < today:
        raise _refuse("VALIDATION", "The promised date cannot be in the past.", 400)
    qty = int(data.get("qty") or 1)
    principal = _person(actor)

    def handler(run: CommandRun) -> CommandResult:
        run.advisory_lock(LockRank.DOCUMENT, [f"alterations:{store.pk}"])
        garment = _garment_line(store, str(data["bill"]), int(data["line_no"]))
        free = free_to_alter(garment)
        if qty > free:
            raise _refuse(
                "ALTERATION_QTY",
                f"Only {free} of line {garment.line_no} on {garment.sale.doc_number} can go on "
                "a job card: the rest came back or is on an open job card.",
            )
        charge = _charge_line(store, data, garment)
        now = timezone.now()
        custody = BilledRetainedCustody.objects.create(
            store=store,
            sale_line=garment,
            qty=qty,
            purpose=BilledRetainedCustody.Purpose.ALTERATION,
            customer_name=name,
            customer_mobile=mobile,
            expected_by=promised_on,
            opened_at=now,
            opened_by=_user(actor),
        )
        job = AlterationJob.objects.create(
            id=job_id,
            store=store,
            ref=_next_ref(store),
            garment_line=garment,
            qty=qty,
            charge_line=charge,
            charge_paise=int(charge.net_paise) if charge is not None else 0,
            customer_name=name,
            customer_mobile=mobile,
            work=work[:500],
            measurements=measurements[:500],
            tailor=tailor,
            tailor_name=tailor_name,
            promised_on=promised_on,
            custody=custody,
            created_by=_user(actor),
        )
        run.audit_subject_key = f"alteration:{job.pk}"
        run.audit_site_id = store.pk
        run.audit_before = None
        run.audit_after = snapshot(job)
        return CommandResult(
            resource_type="alteration_job", resource_id=str(job.pk), status_code=201
        )

    execute_command(
        principal,
        CommandSpec(
            action=CREATE_ACTION,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"alteration:{job_id}"),
            business_input={
                "id": str(job_id),
                "store": store.code,
                "bill": str(data["bill"]),
                "line_no": int(data["line_no"]),
                "qty": qty,
                "charge_bill": str(data.get("charge_bill") or ""),
                "charge_line_no": data.get("charge_line_no"),
                "customer_mobile": masked(mobile),
                "tailor": tailor,
                "promised_on": promised_on.isoformat(),
            },
            site_id=store.pk,
            subject_key=f"alteration:{job_id}",
        ),
        handler,
    )
    return Made(job=AlterationJob.objects.get(pk=job_id), created=True)


def _same_job(row: AlterationJob, store: Store, data: dict[str, Any]) -> bool:
    """Is ``data`` the job card ``row`` already is? (a replay, not a new one)."""
    garment = row.garment_line
    bill = str(data.get("bill") or "").strip().upper()
    return (
        row.store_id == store.pk
        and garment.line_no == int(data.get("line_no") or 0)
        and bill
        in {
            (garment.sale.doc_number or "").upper(),
            (garment.sale.tax_invoice_number or "").upper(),
            (garment.sale.till_number or "").upper(),
        }
        and row.qty == int(data.get("qty") or 1)
        and row.promised_on == data.get("promised_on")
    )


def _customer(data: dict[str, Any]) -> tuple[str, str]:
    name = str(data.get("customer_name") or "").strip()
    if not name:
        raise _refuse("VALIDATION", "A job card is for a named customer: type their name.", 400)
    mobile = normalise_mobile(str(data.get("customer_mobile") or ""))
    if len(mobile) != 10:
        raise _refuse(
            "VALIDATION",
            "Type the customer's 10-digit mobile number, so the store can tell them it is ready.",
            400,
        )
    return name[:120], mobile


def _tailor(data: dict[str, Any]) -> tuple[str, str]:
    tailor = str(data.get("tailor") or "")
    if tailor not in AlterationJob.Tailor.values:
        raise _refuse("VALIDATION", "Say whether the tailor is in-house or outside.", 400)
    name = str(data.get("tailor_name") or "").strip()[:120]
    if tailor == AlterationJob.Tailor.OUTSIDE and not name:
        raise _refuse("VALIDATION", "Name the outside tailor the garment goes to.", 400)
    return tailor, name


def _line_on_bill(store: Store, number: str, line_no: int, what: str) -> SaleLine:
    sale = find_bill(store, number)
    if sale is None:
        raise _refuse(
            "BILL_NOT_FOUND", f"No bill {number or '(blank)'} stands at {store.code}.", 404
        )
    line = (
        SaleLine.objects.select_for_update()
        .select_related("sale")
        .filter(sale=sale, line_no=line_no, direction=SaleLine.Direction.SALE)
        .first()
    )
    if line is None:
        raise _refuse("LINE_NOT_FOUND", f"{sale.doc_number} has no {what} on line {line_no}.", 404)
    return line


def _garment_line(store: Store, number: str, line_no: int) -> SaleLine:
    line = _line_on_bill(store, number, line_no, "garment sold")
    if line.is_alteration:
        raise _refuse(
            "ALTERATION_NOT_GARMENT",
            f"Line {line_no} of {line.sale.doc_number} is an alteration charge, not a garment.",
        )
    return line


def _charge_line(store: Store, data: dict[str, Any], garment: SaleLine) -> SaleLine | None:
    """The paid alteration's own line, or ``None`` for a free alteration."""
    line_no = data.get("charge_line_no")
    if not line_no:
        return None
    number = str(data.get("charge_bill") or data["bill"])
    line = _line_on_bill(store, number, int(line_no), "alteration charge")
    if line.sale.billed_at < garment.sale.billed_at:
        raise _refuse(
            "ALTERATION_CHARGE_EARLIER",
            f"{line.sale.doc_number} was billed before the garment's bill "
            f"{garment.sale.doc_number}; the charge is on the garment's bill or a later one.",
        )
    if not line.is_alteration:
        raise _refuse(
            "ALTERATION_NOT_CHARGE",
            f"Line {line_no} of {line.sale.doc_number} is not an alteration charge.",
        )
    job = AlterationJob.objects.filter(charge_line=line).first()
    if job is not None:
        raise _refuse(
            "ALTERATION_CHARGE_USED",
            f"That alteration charge is already on job card {job.ref}.",
            409,
        )
    return line


def _next_ref(store: Store) -> str:
    """The store code and a running number, under the store's job card lock."""
    count = AlterationJob.objects.filter(store=store).count()
    return f"{store.code}-A{count + 1}"


# ---------------------------------------------------------------------------
# Moving the work on
# ---------------------------------------------------------------------------


def _locked(store: Store, job_id: Any) -> AlterationJob:
    job = (
        AlterationJob.objects.select_for_update(of=("self",))
        .select_related("custody", "garment_line__sale", "charge_line__sale")
        .filter(pk=job_id, store=store)
        .first()
    )
    if job is None:
        raise _refuse("NOT_FOUND", "That job card was not found at this store.", 404)
    return job


def _write(
    actor: Any,
    action: str,
    store: Store,
    job_id: Any,
    command_id: uuid.UUID | None,
    business: dict[str, Any],
    body: Any,
) -> None:
    """One audited change to a job card, under the screen's id for the attempt.

    A retry of the same tap replays it; a later, different attempt is judged
    afresh rather than answered with an old refusal.
    """
    subject = f"alteration:{job_id}"

    def handler(run: CommandRun) -> CommandResult:
        job = _locked(store, job_id)
        before = snapshot(job)
        body(job)
        job.refresh_from_db()
        job.custody.refresh_from_db()
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = before
        run.audit_after = snapshot(job)
        return CommandResult(resource_type="alteration_job", resource_id=str(job_id))

    execute_command(
        _person(actor),
        CommandSpec(
            action=action,
            command_id=command_id or uuid.uuid4(),
            business_input={"id": str(job_id), "store": store.code, **business},
            site_id=store.pk,
            subject_key=subject,
        ),
        handler,
    )


def _must_be(job: AlterationJob, allowed: tuple[str, ...], doing: str) -> None:
    if job.status not in allowed:
        raise _refuse(
            "ALTERATION_STEP",
            f"{job.ref} is {job.get_status_display().lower()}, so it cannot {doing}.",
            409,
        )


def _move(job: AlterationJob, location: str) -> None:
    custody = job.custody
    if custody.location != location:
        custody.location = location
        custody.save(update_fields=["location", "updated_at"])


def send_to_tailor(
    store: Store, actor: Any, job_id: Any, *, command_id: uuid.UUID | None = None
) -> None:
    """The garment goes to the tailor. An outside tailor takes it out of the store."""

    def body(job: AlterationJob) -> None:
        _must_be(job, (Status.RECEIVED,), "go to the tailor")
        job.status = Status.WITH_TAILOR
        job.save(update_fields=["status", "updated_at"])
        if job.tailor == AlterationJob.Tailor.OUTSIDE:
            _move(job, BilledRetainedCustody.Location.OUTSIDE_TAILOR)

    _write(actor, SEND_ACTION, store, job_id, command_id, {}, body)


def mark_ready(
    store: Store, actor: Any, job_id: Any, *, command_id: uuid.UUID | None = None
) -> None:
    """The work is done and the garment is back at the store."""

    def body(job: AlterationJob) -> None:
        _must_be(job, (Status.RECEIVED, Status.WITH_TAILOR), "be marked ready")
        job.status = Status.READY
        job.ready_at = timezone.now()
        job.save(update_fields=["status", "ready_at", "updated_at"])
        _move(job, BilledRetainedCustody.Location.STORE)

    _write(actor, READY_ACTION, store, job_id, command_id, {}, body)


def customer_told(
    store: Store, actor: Any, job_id: Any, *, command_id: uuid.UUID | None = None
) -> None:
    """Record when staff told the customer the garment is ready."""

    def body(job: AlterationJob) -> None:
        _must_be(job, (Status.READY,), "be told to the customer yet")
        if job.customer_told_at is not None:
            raise _refuse(
                "ALTERATION_ALREADY_TOLD",
                f"The customer was already told {job.ref} is ready.",
                409,
            )
        job.customer_told_at = timezone.now()
        job.customer_told_by = _user(actor)
        job.save(update_fields=["customer_told_at", "customer_told_by", "updated_at"])

    _write(actor, TOLD_ACTION, store, job_id, command_id, {}, body)


def _close(job: AlterationJob, actor: Any, status: str, outcome: str) -> None:
    now = timezone.now()
    job.status = status
    job.closed_at = now
    job.closed_by = _user(actor)
    job.save(update_fields=["status", "closed_at", "closed_by", "cancel_reason", "updated_at"])
    custody = job.custody
    if outcome == BilledRetainedCustody.Outcome.HANDED_OVER:
        # Handed over at the counter; a cancelled job keeps where it last was.
        custody.location = BilledRetainedCustody.Location.STORE
    custody.closed_at = now
    custody.closed_by = _user(actor)
    custody.outcome = outcome
    custody.save(update_fields=["location", "closed_at", "closed_by", "outcome", "updated_at"])


def hand_over(
    store: Store, actor: Any, job_id: Any, *, command_id: uuid.UUID | None = None
) -> None:
    """The customer collects the altered garment; the store's custody ends."""

    def body(job: AlterationJob) -> None:
        _must_be(job, (Status.READY,), "be handed over")
        _close(job, actor, Status.HANDED_OVER, BilledRetainedCustody.Outcome.HANDED_OVER)

    _write(actor, HAND_OVER_ACTION, store, job_id, command_id, {}, body)


def cancel(
    store: Store,
    actor: Any,
    job_id: Any,
    *,
    reason: str,
    command_id: uuid.UUID | None = None,
) -> None:
    """The work stops and the garment goes back to the customer as it is.

    A paid charge is not refunded here: what cancelling does to money already
    taken waits on OQ-49 (cancellation policy), so the bill stands.
    """
    reason = (reason or "").strip()
    if not reason:
        raise _refuse("VALIDATION", "Say why the job card is cancelled.", 400)

    def body(job: AlterationJob) -> None:
        _must_be(job, OPEN, "be cancelled")
        job.cancel_reason = reason[:240]
        _close(job, actor, Status.CANCELLED, BilledRetainedCustody.Outcome.CANCELLED)

    _write(actor, CANCEL_ACTION, store, job_id, command_id, {"reason": reason[:240]}, body)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def rows(store: Store) -> QuerySet[AlterationJob]:
    return AlterationJob.objects.filter(store=store).select_related(
        "custody", "garment_line__sale", "charge_line__sale"
    )
