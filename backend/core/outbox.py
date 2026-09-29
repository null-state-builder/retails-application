"""The durable outbox worker (design §4.4).

Intents commit with the business event that caused them. A worker claims due jobs
with a lease (``FOR UPDATE SKIP LOCKED``), runs the provider call with no database
lock held, then records the attempt and its outcome - attempted, confirmed,
failed or unknown - as its own service command. A failed email or export never
undoes the goods event that asked for it.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import transaction

from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    Principal,
    database_now,
    execute_command,
)
from core.tenancy import tenant_context

logger = logging.getLogger(__name__)

WORKER_SERVICE = "outbox-worker"
MAX_ATTEMPTS = 5
LEASE = timedelta(seconds=120)
ANCHOR_INTERVAL = timedelta(minutes=5)


@dataclass
class JobOutcome:
    state: str  # confirmed | failed | unknown
    provider_ref: str | None = None
    diagnostic: str | None = None
    progress: int = 100
    #: A failure that will fail the same way again - a refusal, not a provider
    #: being down. Retrying it four more times only delays the visible failure
    #: and the exception someone has to act on (GSA-T18).
    terminal: bool = False


JobHandler = Callable[[Any], JobOutcome]
_HANDLERS: dict[str, JobHandler] = {}


def register_job_handler(kind: str, handler: JobHandler) -> None:
    _HANDLERS[kind] = handler


def claim(tenant_id: uuid.UUID, limit: int = 10) -> list[Any]:
    from core.kernel_models import JobState

    # Due and lease times are database-clock stamps (``run.now``); judge them by it.
    now = database_now()
    with transaction.atomic():
        jobs = list(
            JobState.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("intent")
            .filter(
                tenant_id=tenant_id,
                state__in=[JobState.State.PENDING, JobState.State.RUNNING],
                intent__not_before__lte=now,
            )
            .exclude(lease_until__gt=now)
            .order_by("created_at")[:limit]
        )
        for job in jobs:
            job.state = JobState.State.RUNNING
            job.attempts += 1
            job.lease_until = now + LEASE
            job.save(update_fields=["state", "attempts", "lease_until"])
    return jobs


def _record(tenant_id: uuid.UUID, job: Any, outcome: JobOutcome) -> None:
    from core.kernel_models import DeliveryEvent, JobState

    def handler(run: CommandRun) -> CommandResult:
        locked = JobState.objects.select_for_update().get(pk=job.pk)
        run.record(
            DeliveryEvent(
                intent_id=locked.intent_id,
                attempt=locked.attempts,
                state=outcome.state,
                provider_ref=outcome.provider_ref,
                diagnostic=(outcome.diagnostic or "")[:500] or None,
            )
        )
        if outcome.state == "confirmed":
            locked.state = JobState.State.CONFIRMED
            locked.progress = 100
            locked.error_code = None
            locked.lease_until = None
        elif outcome.state == "failed" and (outcome.terminal or locked.attempts >= MAX_ATTEMPTS):
            locked.state = JobState.State.FAILED
            locked.error_code = (outcome.diagnostic or "failed")[:80]
            locked.lease_until = None
        elif outcome.state == "unknown":
            locked.state = JobState.State.UNKNOWN
            locked.error_code = "outcome_unknown"
            locked.lease_until = None
        else:
            locked.state = JobState.State.PENDING
            locked.lease_until = run.now + timedelta(seconds=30 * locked.attempts)
            locked.progress = outcome.progress
        locked.save(update_fields=["state", "progress", "error_code", "lease_until"])
        return CommandResult(resource_type="job", resource_id=str(locked.pk))

    execute_command(
        Principal(tenant_id=tenant_id, service_code=WORKER_SERVICE),
        CommandSpec(
            action="outbox.delivery",
            command_id=uuid.uuid5(job.intent_id, f"attempt:{job.attempts}"),
            business_input={"state": outcome.state, "attempt": job.attempts},
            subject_key=f"intent:{job.intent_id}",
        ),
        handler,
    )


def run_job(tenant_id: uuid.UUID, job: Any) -> JobOutcome:
    handler = _HANDLERS.get(job.intent.kind)
    if handler is None:
        outcome = JobOutcome("failed", diagnostic=f"no handler for {job.intent.kind}")
    else:
        try:
            outcome = handler(job.intent)
        except Exception as exc:  # noqa: BLE001 - every provider failure is recorded, never raised
            outcome = JobOutcome("failed", diagnostic=f"{type(exc).__name__}: {exc}")
    _record(tenant_id, job, outcome)
    return outcome


def run_due(tenant_id: uuid.UUID, limit: int = 10) -> int:
    with tenant_context(tenant_id):
        jobs = claim(tenant_id, limit)
        for job in jobs:
            run_job(tenant_id, job)
    return len(jobs)


def drain(tenant_id: uuid.UUID, rounds: int = 20) -> int:
    """Run every due job until none are left (tests, seeds, single-process dev)."""
    total = 0
    for _ in range(rounds):
        done = run_due(tenant_id, 50)
        total += done
        if done == 0:
            break
    return total


#: Work the worker does on the anchoring clock rather than per job: something
#: that must happen every interval whether or not anything was asked for. The
#: health producer (``alerts.goods_health``) registers here, so the kernel runs
#: it beside anchoring without importing the apps that own exceptions.
ScheduledTask = Callable[[uuid.UUID], Any]
_SCHEDULED: dict[str, ScheduledTask] = {}


def register_scheduled(name: str, task: ScheduledTask) -> None:
    _SCHEDULED[name] = task


def run_scheduled(tenant_id: uuid.UUID) -> None:
    """One anchoring pass, then every registered scheduled task, in that order.

    Anchoring first, so a verification that follows compares the chain with the
    anchors just written. A task that fails is logged and the next one still
    runs: one broken check must not silence the others, and must not stop the
    worker delivering jobs.
    """
    from core.anchors import anchor_changed_heads

    with tenant_context(tenant_id):
        anchor_changed_heads(tenant_id)
    for name, task in list(_SCHEDULED.items()):
        try:
            with tenant_context(tenant_id):
                task(tenant_id)
        except Exception:  # noqa: BLE001 - recorded, never raised into the worker
            logger.exception("goods-v1 scheduled task %s failed", name)


def loop(tenant_id: uuid.UUID, *, idle_sleep: float = 2.0, stop_after: float | None = None) -> None:
    started = time.monotonic()
    last_anchor = 0.0
    while stop_after is None or time.monotonic() - started < stop_after:
        done = run_due(tenant_id)
        if time.monotonic() - last_anchor >= ANCHOR_INTERVAL.total_seconds():
            run_scheduled(tenant_id)
            last_anchor = time.monotonic()
        if done == 0:
            time.sleep(idle_sleep)
