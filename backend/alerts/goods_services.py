"""Owned goods exceptions and recipient notifications (design §5.6-§5.7, Phase 1 §6.9).

Opening an exception names its cause, owner and due date from the Phase 1
defaults. Only the domain command that removes the cause resolves it; assigning
or adding a note never does. Notifications are outbox intents a worker turns into
per-recipient rows after checking who currently holds the owning role in scope.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.core.mail import send_mail

from alerts.goods_models import ExceptionEvent, GoodsException, GoodsNotification
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    database_now,
    execute_command,
)
from core.outbox import JobOutcome
from core.refusals import Refusal


@dataclass(frozen=True)
class ExceptionDefault:
    owner_role: str
    sla_value: int
    sla_unit: str  # working_days | days | hours
    closure: str
    #: Roles told alongside the owner when it opens. Design §4.4: an
    #: evidence-protection failure or mismatch reaches X-PLT *and* C-OWN.
    also_notify: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkingCalendar:
    """One approved ``working_calendar`` ConfigVersion, read once at open (GSA-T08).

    Pinned onto the exception it dated (``GoodsException.calendar_version``) and
    onto its "opened" event payload as evidence — never re-read to recompute a
    due date that already exists (design §5.8: "Reassignment and subsequent
    configuration never recalculate an existing due date").
    """

    version_id: uuid.UUID
    timezone: str
    working_weekdays: frozenset[int]  # ISO weekday: 1=Monday .. 7=Sunday
    excluded_dates: frozenset[date]


def resolve_calendar(tenant_id: uuid.UUID, at: datetime) -> WorkingCalendar | None:
    """The tenant's one approved working calendar at ``at``, or ``None``.

    ``None`` means the tenant has no ``working_calendar`` configuration in force.
    Nothing takes its place: GSA-T08 forbids the pre-existing Monday-Saturday
    guess, so ``due_at`` answers ``None`` rather than inventing a working week,
    and the exception is opened with no deadline. Goods activation readiness is
    what stops an activated site living that way — it cannot be approved while
    the business has no calendar in force, or while an effective working-days
    notification policy names none (``masters.goods_services._working_calendar_check``).
    """
    from django.utils.dateparse import parse_date

    from masters.goods_config import approved_working_calendar

    version = approved_working_calendar(tenant_id, at)
    if version is None:
        return None
    payload = version.payload
    # Every entry was already validated as a real date at config approval
    # (masters.goods_services._date); a still-unparseable one here would mean
    # approved configuration is corrupt, worth failing loudly on rather than
    # silently dropping from the excluded set.
    excluded = []
    for raw in payload["excluded_dates"]:
        parsed = parse_date(raw)
        assert parsed is not None, f"approved working_calendar has an invalid date: {raw!r}"
        excluded.append(parsed)
    return WorkingCalendar(
        version_id=version.pk,
        timezone=payload["timezone"],
        working_weekdays=frozenset(payload["working_weekdays"]),
        excluded_dates=frozenset(excluded),
    )


#: Phase 1 §6.9, restricted to the kinds stage 1 activates. Deferred selling,
#: seasonal and mail kinds are deliberately absent, so they cannot be opened.
EXCEPTION_DEFAULTS: dict[str, ExceptionDefault] = {
    "grn_awaiting_pt": ExceptionDefault("C-WHO", 2, "working_days", "PT submitted"),
    "approval_pending": ExceptionDefault("C-INV", 1, "working_days", "approved or rejected"),
    "receipt_discrepancy": ExceptionDefault("C-INV", 3, "working_days", "disposition recorded"),
    "unbooked_arrival": ExceptionDefault(
        "C-BUY", 5, "working_days", "booking linked or no booking confirmed"
    ),
    # Store operations ticket 37 (ST-REC-1): what was booked, invoiced and counted
    # disagree on a line. Owned by the buyer, who holds the booking and its cost;
    # it closes when a corrected booking, invoice or count makes every line agree.
    "three_way_mismatch": ExceptionDefault(
        "C-BUY", 3, "working_days", "booking, invoice and count agree"
    ),
    # Store operations ticket 35 (ST-INV-3): a scheduled count was not done by
    # its day. The store's own work, owned by the store role; the Owner, who set
    # the schedule, is told too. It closes when that brand (or the whole store) is
    # counted, or when the Owner stops the schedule.
    "scheduled_count_missed": ExceptionDefault(
        "M-STR", 1, "working_days", "counted, or the schedule stopped", also_notify=("C-OWN",)
    ),
    # Store operations ticket 41 (ST-MNY-2, R-FIN-015): a day-close cash count
    # that did not match the expected cash, confirmed by a manager's own PIN.
    # Owned by the store manager; the Owner is told too. Never a balancing entry.
    "cash_variance": ExceptionDefault(
        "M-STR", 2, "working_days", "explained and approved", also_notify=("C-OWN",)
    ),
    # Store operations ticket 42 (ST-MNY-3, R-FIN-014): a petty cash spend saved
    # with no photo of its bill. Owned by the store manager; closed only by
    # attaching the photo (or by the Owner rejecting the spend).
    "petty_cash_no_bill": ExceptionDefault(
        "M-STR", 2, "working_days", "bill photo attached", also_notify=("C-OWN",)
    ),
    "unresolved_identity": ExceptionDefault("C-PMO", 2, "working_days", "SKU or alias resolved"),
    "transfer_stalled": ExceptionDefault(
        "C-WHO", 5, "days", "arrival recorded or counter-document"
    ),
    "transfer_discrepancy": ExceptionDefault("C-INV", 5, "days", "counter-document"),
    "acceptance_discrepancy": ExceptionDefault(
        "C-INV", 3, "working_days", "disposition or movement recorded"
    ),
    "rtv_not_dispatched": ExceptionDefault("C-WHO", 14, "days", "dispatched or cancelled"),
    # Goods ticket 15F. A shipment sent to the vendor for delivery is not yet a
    # return: departure is not receipt. Owned by the warehouse role on the same
    # 14-day reminder as the uncollected balance (Anand's 15B decision 6) - a
    # follow-up, never a deadline that closes or releases anything.
    "rtv_receipt_pending": ExceptionDefault(
        "C-WHO", 14, "days", "vendor receipt acknowledged or the goods recorded back at the source"
    ),
    # Goods ticket 15F, GSA-T15: the vendor acknowledged fewer pieces than the
    # shipment carried. The Owner closes a persistent difference (goods PRD
    # §14.10 GSA-R07, ticket 15H), with a 30-day follow-up that is a reminder,
    # not an automatic close. It ends when the Owner approves the site's
    # prepared closure, a later full acknowledgement, or the goods coming back.
    "rtv_acknowledgement_discrepancy": ExceptionDefault(
        "C-OWN", 30, "days", "difference closed by the Owner, acknowledged, or returned to source"
    ),
    "eway_missing": ExceptionDefault("M-STR", 1, "working_days", "reference attached"),
    "count_session_stale": ExceptionDefault("C-INV", 1, "working_days", "completed or cancelled"),
    "setup_item_missing": ExceptionDefault("C-OWN", 30, "days", "configuration approved"),
    "series_hole": ExceptionDefault("C-OWN", 5, "working_days", "explained"),
    "post_restore_recovery": ExceptionDefault("C-OWN", 30, "days", "recovered or none reported"),
    "site_readiness_failed": ExceptionDefault("C-STO", 30, "days", "item passed or owner override"),
    "site_closure_residual": ExceptionDefault("C-STO", 30, "days", "zero or owner approval"),
    "evidence_protection": ExceptionDefault(
        "X-PLT", 10, "minutes", "investigated and recorded", also_notify=("C-OWN",)
    ),
    "acceptance_remaining": ExceptionDefault(
        "M-STR", 3, "working_days", "accepted or disposition recorded"
    ),
    "label_print_failed": ExceptionDefault(
        "C-WHO", 1, "working_days", "reprinted or a non-failed outcome recorded"
    ),
    # GSA-T18: a durable export job that could not produce its file. Owned by the
    # platform, because the cause is a worker, a store or a scope that moved under
    # the job - never something the requester can fix by asking again.
    "export_failed": ExceptionDefault("X-PLT", 1, "working_days", "re-requested or explained"),
    # GSA-T03 "Access follow-up": a privileged change still waiting for its
    # distinct-person review. Owned by the C-OWN role, not a named person; it
    # closes only through that review. One working day, ruled by Anand
    # (19 September 2026), the same as other approvals waiting here.
    "privileged_change_review": ExceptionDefault(
        "owner", 1, "working_days", "reviewed by a different authorised person"
    ),
    # GSA-T10: opening manifest exceptions, registered with this shared centre
    # per ticket 08's own rule (docs/features/goods-to-store-acceptance/
    # tickets/08-exceptions-and-notification-centre.md).
    "opening_variance": ExceptionDefault(
        "C-OWN", 2, "working_days", "variance approved or rejected"
    ),
    "opening_origin_unavailable": ExceptionDefault(
        "C-OWN", 30, "days", "historical origin confirmed unavailable"
    ),
    # GSA-T12: stock put on hold is stock nobody can sell or send. It is owned
    # work until the hold is lifted through its approved release (or the goods
    # leave through their own disposal route), registered with this shared
    # centre per ticket 08's rule.
    "stock_hold_active": ExceptionDefault(
        "C-INV", 3, "working_days", "hold released or the goods disposed"
    ),
    # GSA-T04 (ticket 04B, design §5.8 "Master/configuration exceptions"): the
    # three kinds that exist because a change created actionable work, never
    # because a version, a retirement or a withdrawal merely happened.
    #
    # Required store setup that is missing or invalid. Owned by the site
    # transition owner, whose readiness queue it belongs in; the company owner
    # holds `exception.manage` over the same site and so keeps oversight.
    "setup_configuration_invalid": ExceptionDefault(
        "C-STO", 30, "days", "the setup item passes its readiness check"
    ),
    # Governed master or crosswalk work nobody has finished — a source key
    # mapped to nothing yet. Owned by the product master owner.
    "master_resolution_required": ExceptionDefault(
        "C-PMO", 2, "working_days", "the master or crosswalk is given its target, or retired"
    ),
    # A counted code that matched more than one product. Owned by the product
    # master owner; it closes only on an E091 pick bound to that exact scan.
    "identity_ambiguity": ExceptionDefault(
        "C-PMO", 2, "working_days", "an authorised person chose between the candidates"
    ),
}


def due_at(
    start: datetime, default: ExceptionDefault, calendar: WorkingCalendar | None = None
) -> datetime | None:
    """The deadline: `start` plus `default`'s SLA, in `calendar`'s working days.

    Day, hour and minute SLAs are elapsed time and need no calendar — GSA-T08
    does not replace their existing meanings. A `working_days` SLA counts whole
    working days in the calendar's own timezone, skipping non-working weekdays
    and excluded dates, and lands at the same local time as the triggering event
    on the day it reaches. (A local time the zone skips over — the hour lost to a
    spring-forward — resolves by Python's ordinary `fold=0` rule, so it moves by
    the offset that changed; an ambiguous repeated hour takes its first
    occurrence. Naming this rather than pretending otherwise: no calendar this
    product ships uses a zone with a transition, Asia/Kolkata least of all.)

    A `working_days` SLA with no calendar (`calendar=None`) has **no deadline**:
    `None`, never a guess. A business that has approved no working calendar has
    no working week to measure against, and GSA-T08 forbids inferring one; the
    exception is still opened and still owned, it simply carries no due date
    until a calendar exists. Goods activation readiness is what stops an
    activated site living that way
    (`masters.goods_services._working_calendar_check`).
    """
    if default.sla_unit == "hours":
        return start + timedelta(hours=default.sla_value)
    if default.sla_unit == "minutes":
        return start + timedelta(minutes=default.sla_value)
    if default.sla_unit == "days":
        return start + timedelta(days=default.sla_value)
    if calendar is None:
        return None
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(calendar.timezone)
    local = start.astimezone(zone)
    local_date = local.date()
    remaining = default.sla_value
    while remaining > 0:
        local_date += timedelta(days=1)
        if local_date.isoweekday() in calendar.working_weekdays and local_date not in (
            calendar.excluded_dates
        ):
            remaining -= 1
    # Rebuilt from the local wall clock rather than advanced in elapsed days, so a
    # zone that changes its offset between the two dates still ends at the time
    # the event happened.
    return datetime.combine(local_date, local.time(), tzinfo=zone)


def open_exception(
    run: CommandRun,
    *,
    kind: str,
    site_id: int | None,
    subject_key: str,
    reason_code: str,
    source_event_key: uuid.UUID,
    allowed_resolution_actions: list[str] | None = None,
    note: str | None = None,
    reopen: bool = False,
) -> GoodsException:
    default = EXCEPTION_DEFAULTS.get(kind)
    if default is None:
        raise Refusal("CONTRACT_DISABLED", f"Exception kind {kind} is not active in this stage.")
    existing = GoodsException.objects.filter(
        tenant_id=run.tenant_id, kind=kind, source_event_key=source_event_key
    ).first()
    if existing is not None and not (reopen and existing.state != "open"):
        return existing
    # Resolved once per occurrence: due_at and the calendar version it used are
    # evidence frozen at open (GSA-T08). Only meaningful for a working-day SLA —
    # a days/hours/minutes default has no calendar to pin.
    calendar = (
        resolve_calendar(run.tenant_id, run.now) if default.sla_unit == "working_days" else None
    )
    if existing is not None:
        # GSA-T04: a condition whose producer re-checks it — a readiness item —
        # can genuinely come back after it was fixed. The same upsert key then
        # names the same work again rather than a second row, so the deadline is
        # the new occurrence's, not the old one's.
        #
        # Locked like every other write to this row (`add_exception_event`):
        # the caller's own lock is on the thing that broke, not on this, so it
        # does not exclude somebody assigning or annotating the exception at the
        # same moment.
        locked = run.lock(LockRank.DOCUMENT, GoodsException.objects.filter(pk=existing.pk))
        reopened: GoodsException = locked[0]
        if reopened.state == "open":
            return reopened  # somebody else reopened it while we waited for the lock
        previous_owner = reopened.owner_human_id
        reopened.state = GoodsException.State.OPEN
        reopened.resolution_event_key = None
        reopened.site_id = site_id
        reopened.subject_key = subject_key[:100]
        reopened.owner_role = default.owner_role
        # A new occurrence is unassigned: whoever took the last one may be gone,
        # and the old assignment would read as a person already on this. Who it
        # was is kept in the reopening event rather than lost.
        reopened.owner_human_id = None
        reopened.reason_code = reason_code
        reopened.opened_at = run.now
        reopened.due_at = due_at(run.now, default, calendar)
        reopened.calendar_version_id = calendar.version_id if calendar else None
        reopened.allowed_resolution_actions = allowed_resolution_actions or []
        reopened.revision += 1
        reopened.save(
            update_fields=[
                "state",
                "resolution_event_key",
                "site",
                "subject_key",
                "owner_role",
                "owner_human",
                "reason_code",
                "opened_at",
                "due_at",
                "calendar_version",
                "allowed_resolution_actions",
                "revision",
            ]
        )
        _record_opened(
            run,
            reopened,
            default,
            calendar,
            reason_code,
            note,
            subject_key=subject_key,
            site_id=site_id,
            reopened=True,
            previous_owner_id=previous_owner,
        )
        return reopened
    exception = GoodsException.objects.create(
        tenant_id=run.tenant_id,
        kind=kind,
        site_id=site_id,
        subject_key=subject_key[:100],
        owner_role=default.owner_role,
        due_at=due_at(run.now, default, calendar),
        calendar_version_id=calendar.version_id if calendar else None,
        reason_code=reason_code,
        source_event_key=source_event_key,
        opened_at=run.now,
        allowed_resolution_actions=allowed_resolution_actions or [],
    )
    _record_opened(
        run,
        exception,
        default,
        calendar,
        reason_code,
        note,
        subject_key=subject_key,
        site_id=site_id,
    )
    return exception


def _record_opened(
    run: CommandRun,
    exception: GoodsException,
    default: ExceptionDefault,
    calendar: WorkingCalendar | None,
    reason_code: str,
    note: str | None,
    *,
    subject_key: str,
    site_id: int | None,
    reopened: bool = False,
    previous_owner_id: uuid.UUID | None = None,
) -> None:
    """The opening evidence and its owner's notification, for a first open or a reopen.

    ``subject_key`` and ``site_id`` are the caller's own, not the row's, so the
    notification an existing kind sends is exactly the one it sent before.
    """
    payload: dict[str, Any] = {
        "reason_code": reason_code,
        "owner_role": default.owner_role,
        "note": note,
        "sla_value": default.sla_value,
        "sla_unit": default.sla_unit,
        "calendar_version_id": str(calendar.version_id) if calendar else None,
        "calendar_timezone": calendar.timezone if calendar else None,
        "calendar_working_weekdays": sorted(calendar.working_weekdays) if calendar else None,
    }
    if reopened:
        # Who held the previous occurrence, so replaying the events still answers
        # it after the reopen cleared the assignment.
        payload["previous_owner_human_id"] = str(previous_owner_id) if previous_owner_id else None
    run.record(
        ExceptionEvent(
            exception_id=exception.pk,
            event_kind=ExceptionEvent.Kind.REOPENED if reopened else ExceptionEvent.Kind.OPENED,
            reason_code=reason_code,
            payload=payload,
        )
    )
    notify(
        run,
        event_kind=f"exception.{exception.kind}",
        subject_key=subject_key,
        site_id=site_id,
        title=f"{exception.kind.replace('_', ' ').capitalize()}: {reason_code}",
        roles=[default.owner_role, *default.also_notify],
        due=exception.due_at,
    )


def resolve_exceptions(
    run: CommandRun,
    *,
    kind: str,
    subject_key: str,
    reason_code: str = "RESOLVED",
    source_event_key: uuid.UUID | None = None,
) -> int:
    queryset = GoodsException.objects.filter(
        tenant_id=run.tenant_id, kind=kind, subject_key=subject_key[:100], state="open"
    )
    if source_event_key is not None:
        queryset = queryset.filter(source_event_key=source_event_key)
    count = 0
    for exception in queryset:
        exception.state = GoodsException.State.RESOLVED
        exception.resolution_event_key = run.key_id
        exception.revision += 1
        exception.save(update_fields=["state", "resolution_event_key", "revision"])
        run.record(
            ExceptionEvent(
                exception_id=exception.pk,
                event_kind=ExceptionEvent.Kind.RESOLVED,
                reason_code=reason_code,
                payload={"resolution_command_id": str(run.spec.command_id)},
            )
        )
        count += 1
    return count


def add_exception_event(
    run: CommandRun,
    exception_id: uuid.UUID,
    *,
    event_kind: str,
    owner_human_id: uuid.UUID | None,
    note: str | None,
) -> GoodsException:
    if event_kind not in ("assigned", "note"):
        raise Refusal(
            "EXCEPTION_RESOLUTION_ROUTE",
            "An exception closes only through the command that fixes its cause.",
        )
    locked = run.lock(LockRank.DOCUMENT, GoodsException.objects.filter(pk=exception_id))
    if not locked:
        raise Refusal("NOT_FOUND", "That exception was not found.")
    exception: GoodsException = locked[0]
    if exception.state != "open":
        raise Refusal("STATE_CONFLICT", "This exception is already resolved.")
    if event_kind == "assigned":
        exception.owner_human_id = owner_human_id
    exception.revision += 1
    exception.save(update_fields=["owner_human", "revision"])
    run.record(
        ExceptionEvent(
            exception_id=exception.pk,
            event_kind=event_kind,
            payload={
                "owner_human_id": str(owner_human_id) if owner_human_id else None,
                "note": (note or "")[:1000] or None,
            },
        )
    )
    return exception


def notify(
    run: CommandRun,
    *,
    event_kind: str,
    subject_key: str,
    site_id: int | None,
    title: str,
    roles: list[str],
    due: datetime | None = None,
    brand_id: int | None = None,
    email: bool = False,
) -> None:
    run.outbox(
        "notification",
        subject_key,
        {
            "subject_key": subject_key[:100],
            "site_id": str(site_id) if site_id else None,
            "template_key": event_kind[:80],
            "recipient_role": ",".join(roles)[:40],
            "title": title[:240],
            "due_at": due.isoformat() if due else None,
            "brand_id": str(brand_id) if brand_id else None,
            "email": email,
        },
    )


def _recipients(
    tenant_id: uuid.UUID, roles: list[str], site_id: int | None, brand_id: int | None
) -> list[Any]:
    """People holding a named role whose grant covers the notification's site and brand."""
    from accounts.goods_models import HumanIdentity
    from accounts.principal import AccessContext, effective_grants

    people = []
    # Grants start and end at database-clock stamps; judge them by that clock.
    moment = database_now()
    for human in HumanIdentity.objects.filter(tenant_id=tenant_id, active=True):
        grants = [g for g in effective_grants(human.pk, moment) if g.role_code in roles]
        if not grants:
            continue
        context = AccessContext(
            user=None, human_id=human.pk, tenant_id=tenant_id, session=None, grants=grants
        )
        if any(context.grant_covers(g, site_id, brand_id) for g in grants):
            people.append(human)
    return people


def deliver_notification(intent: Any) -> JobOutcome:
    """Outbox job: one notification row per current recipient in scope."""
    payload = intent.payload or {}
    roles = [r for r in str(payload.get("recipient_role") or "").split(",") if r]
    site_id = int(payload["site_id"]) if payload.get("site_id") else None
    brand_id = int(payload["brand_id"]) if payload.get("brand_id") else None
    recipients = _recipients(intent.tenant_id, roles, site_id, brand_id)

    def handler(run: CommandRun) -> CommandResult:
        existing = set(
            GoodsNotification.objects.filter(intent_id=intent.pk).values_list(
                "recipient_id", flat=True
            )
        )
        for human in recipients:
            if human.pk in existing:
                continue
            run.record(
                GoodsNotification(
                    intent_id=intent.pk,
                    recipient_id=human.pk,
                    subject_key=str(payload.get("subject_key") or "")[:100],
                    site_id=site_id,
                    brand_id=brand_id,
                    event_kind=str(payload.get("template_key") or "")[:60],
                    title=str(payload.get("title") or "")[:240],
                    due_at=payload.get("due_at"),
                )
            )
        return CommandResult(resource_type="notification", resource_id=str(intent.pk))

    execute_command(
        Principal(tenant_id=intent.tenant_id, service_code="notifier"),
        CommandSpec(
            action="notification.deliver",
            command_id=uuid.uuid5(intent.request_key, "notification"),
            business_input={
                "intent": str(intent.pk),
                "recipients": sorted(str(h.pk) for h in recipients),
            },
            subject_key=f"intent:{intent.pk}",
        ),
        handler,
    )
    if payload.get("email"):
        from accounts.models import User

        emails = list(
            User.objects.filter(
                human_id__in=[h.pk for h in recipients], email__isnull=False
            ).values_list("email", flat=True)
        )
        if emails:
            send_mail(
                str(payload.get("title") or "RetailsOps"),
                str(payload.get("title") or ""),
                None,
                emails,
            )
    return JobOutcome("confirmed", provider_ref=f"recipients:{len(recipients)}")


def exception_dto(exception: GoodsException, now: datetime) -> dict[str, Any]:
    return {
        "id": str(exception.pk),
        "kind": exception.kind,
        "subject_id": exception.subject_key,
        "site_id": str(exception.site_id) if exception.site_id is not None else None,
        "owner_role": exception.owner_role,
        "owner_human_id": str(exception.owner_human_id) if exception.owner_human_id else None,
        "opened_at": exception.opened_at.isoformat(),
        # Null when the business has approved no working calendar and this kind's
        # SLA is measured in working days (GSA-T08): no deadline, never a guessed
        # one. Nothing with no deadline can be overdue.
        "due_at": exception.due_at.isoformat() if exception.due_at else None,
        "age_seconds": int((now - exception.opened_at).total_seconds()),
        "overdue": (
            exception.state == "open" and exception.due_at is not None and exception.due_at < now
        ),
        "state": exception.state,
        "reason_code": exception.reason_code,
        "allowed_resolution_actions": list(exception.allowed_resolution_actions or []),
        "evidence_ids": [],
        "revision": exception.revision,
        # GSA-T08: which approved working_calendar version (if any) fixed
        # `due_at`. Absent for a days/hours/minutes SLA and for a working-day
        # SLA opened before any tenant approved one — never a server-local guess.
        "calendar_version_id": str(exception.calendar_version_id)
        if exception.calendar_version_id
        else None,
    }


def exception_event_dto(event: ExceptionEvent) -> dict[str, Any]:
    """One row of an exception's event log (opened/assigned/note/resolved/reopened) —
    the notification centre's drawer reads this newest-first (design §8.2's own
    words for the screen: "the event log (raised, assigned, notes) newest first")."""
    return {
        "id": str(event.pk),
        "event_kind": event.event_kind,
        "actor_id": str(event.actor_id) if event.actor_id else None,
        "recorded_at": event.recorded_at.isoformat(),
        "reason_code": event.reason_code,
        "payload": event.payload,
    }


def notification_dto(notification: GoodsNotification, seen: bool) -> dict[str, Any]:
    return {
        "id": str(notification.pk),
        "event_kind": notification.event_kind,
        "subject_id": notification.subject_key,
        "site_id": str(notification.site_id) if notification.site_id else None,
        "title": notification.title,
        "created_at": notification.recorded_at.isoformat(),
        "due_at": notification.due_at.isoformat() if notification.due_at else None,
        "seen": seen,
    }
