"""Scheduled operations health: checks, their history and the findings they own (E192).

The worker runs :func:`run_health_checks` on its anchoring clock
(``core.outbox.run_scheduled``), straight after the anchors are written. Each
pass:

1. measures every implemented check - the queue, the anchors, the numbering
   ceilings, stale uploads, overdue exceptions, the write-once store and a full
   evidence verification (``core.anchors.verify_tenant_report``);
2. records one ``AuditEvent`` per check (action ``ops.health.check``, subject
   ``health:<code>``) carrying what was observed, the threshold, what the check
   covered and the issue it points at. That sealed row is the check history:
   ``last_ok_at`` is the newest recorded ``ok`` result, so it survives a later
   failure, and nothing but a real recorded pass can produce one;
3. opens - or finds, never duplicates - an owned ``evidence_protection``
   exception for each verification finding and for an unreachable write-once
   store, and, when a check that was failing passes again, notes that recovery
   on the still-open exceptions it had raised. The exception stays open: its
   closure is "investigated and recorded", which a passing check is not.

The monitoring page (``config.goods_ops_views.OperationsHealthView``) only reads
what the last pass recorded. A page load never runs a check, so it can neither
claim a check ran nor manufacture a last success.

Checks with no registered exception kind (the queue, numbering headroom, stale
uploads) are recorded and shown but open nothing: choosing an owner and a
deadline for them is a product decision this module does not make.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db.models import F

from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    Principal,
    database_now,
    execute_command,
)
from core.refusals import Refusal

HEALTH_ACTION = "ops.health.check"
HEALTH_SERVICE = "health-monitor"
PROTECTION_KIND = "evidence_protection"

#: Every check a scheduled pass records, in display order. The monitoring read
#: reports each of them - one that has never been recorded says so.
SCHEDULED_CODES: tuple[str, ...] = (
    "outbox_backlog",
    "outbox_failed",
    "anchor_lag",
    "series_capacity",
    "unconfirmed_uploads",
    "exceptions_overdue",
    "evidence_store",
    "evidence_verifier",
)


def subject_for(code: str) -> str:
    return f"health:{code}"


@dataclass
class CheckResult:
    code: str
    ok: bool
    observed: str
    threshold: str
    owner_role: str
    scope: str
    #: The exception a person opens to act on this result, if there is one.
    issue_id: str | None = None
    #: Owned findings to open (subject_key, reason_code, note), deduplicated by subject.
    findings: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def state(self) -> str:
        return "ok" if self.ok else "attention"


#: The fixed namespace every health finding's source key is derived in.
FINDING_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "kdps:goods-v1:health-finding")


def _finding_key(tenant_id: uuid.UUID, subject_key: str) -> uuid.UUID:
    """One stable source key per finding subject: the same fault is the same work."""
    return uuid.uuid5(
        FINDING_NAMESPACE, f"{tenant_id}:{HEALTH_ACTION}:{PROTECTION_KIND}:{subject_key}"
    )


def measure(tenant_id: uuid.UUID, now: datetime) -> list[CheckResult]:
    """Every scheduled check, measured now. Reads only."""
    from alerts.goods_models import GoodsException
    from core.anchors import verify_tenant_report
    from core.documents import VoucherSeries
    from core.kernel_models import ChainHead, JobState
    from core.numbering import confirmed_ceiling
    from core.offbox import OffboxError, get_store
    from files.goods_models import UploadIntent

    results: list[CheckResult] = []
    pending = JobState.objects.filter(tenant_id=tenant_id, state="pending")
    stale = pending.filter(intent__not_before__lt=now - timedelta(minutes=5)).count()
    results.append(
        CheckResult(
            "outbox_backlog",
            stale == 0,
            f"{stale} job(s) waiting over 5 minutes",
            "0",
            "X-PLT",
            f"{pending.count()} pending job(s)",
        )
    )
    failed = JobState.objects.filter(tenant_id=tenant_id, state__in=["failed", "unknown"]).count()
    # A failed export already owns an `export_failed` exception (GSA-T18); point
    # at the oldest still open, so the check has somewhere to go.
    export_issue = (
        GoodsException.objects.filter(tenant_id=tenant_id, kind="export_failed", state="open")
        .order_by("opened_at", "id")
        .values_list("id", flat=True)
        .first()
    )
    results.append(
        CheckResult(
            "outbox_failed",
            failed == 0,
            f"{failed} failed or unknown job(s)",
            "0",
            "X-PLT",
            f"{JobState.objects.filter(tenant_id=tenant_id).count()} job(s)",
            issue_id=str(export_issue) if export_issue else None,
        )
    )
    heads = ChainHead.objects.filter(tenant_id=tenant_id).exclude(last_event_id__isnull=True)
    unanchored = heads.exclude(anchored_hash=F("last_hash")).count()
    results.append(
        CheckResult(
            "anchor_lag",
            unanchored == 0,
            f"{unanchored} partition(s) not anchored",
            "0 after 5 minutes",
            "X-PLT",
            f"{heads.count()} written partition(s)",
        )
    )
    low = 0
    series = list(VoucherSeries.objects.filter(tenant_id=tenant_id, scope_version="entity_v1"))
    for one in series:
        if confirmed_ceiling(one.pk) - one.next_seq < max(1, one.ceiling_block_size // 10):
            low += 1
    results.append(
        CheckResult(
            "series_capacity",
            low == 0,
            f"{low} series near their ceiling",
            ">10% of a block",
            "X-PLT",
            f"{len(series)} numbering series",
        )
    )
    orphans = UploadIntent.objects.filter(
        tenant_id=tenant_id, state="staged", created_at__lt=now - timedelta(hours=1)
    ).count()
    results.append(
        CheckResult(
            "unconfirmed_uploads",
            orphans == 0,
            f"{orphans} staged upload(s) older than 1 hour",
            "0",
            "X-PLT",
            "staged uploads",
        )
    )
    overdue = GoodsException.objects.filter(tenant_id=tenant_id, state="open", due_at__lt=now)
    most_overdue = overdue.order_by("due_at", "id").values_list("id", flat=True).first()
    results.append(
        CheckResult(
            "exceptions_overdue",
            most_overdue is None,
            f"{overdue.count()} overdue exception(s)",
            "0",
            "C-OWN",
            f"{GoodsException.objects.filter(tenant_id=tenant_id, state='open').count()} open "
            "exception(s)",
            issue_id=str(most_overdue) if most_overdue else None,
        )
    )

    # The write-once store, probed the cheap way: ask for one key that is not
    # there. `head` answers None for a missing key and raises only when the
    # store itself cannot be reached, which is the question being asked.
    store_ok = True
    try:
        get_store().head(f"anchors/{tenant_id}/.probe")
    except OffboxError as exc:
        store_ok = False
        results.append(
            CheckResult(
                "evidence_store",
                False,
                f"the write-once store is unreachable: {exc}",
                "reachable",
                "X-PLT",
                "one probe read",
                findings=[
                    (
                        "store:write-once",
                        "EVIDENCE_STORE_UNREACHABLE",
                        f"The write-once store did not answer: {exc}",
                    )
                ],
            )
        )
    else:
        results.append(
            CheckResult(
                "evidence_store",
                True,
                "the write-once store answered",
                "reachable",
                "X-PLT",
                "one probe read",
            )
        )

    if not store_ok:
        # Without the store there are no anchors to compare with: say the pass
        # could not complete rather than pass the chain half-checked.
        results.append(
            CheckResult(
                "evidence_verifier",
                False,
                "verification could not complete: the write-once store is unreachable",
                "0 findings",
                "X-PLT",
                "not run",
            )
        )
        return results
    report = verify_tenant_report(tenant_id)
    named = ", ".join(f.partition_key for f in report.findings[:3])
    results.append(
        CheckResult(
            "evidence_verifier",
            not report.findings,
            f"{len(report.findings)} finding(s)" + (f": {named}" if named else ""),
            "0 findings",
            "X-PLT",
            report.scope,
            findings=[
                (f"partition:{f.partition_key}", f.reason_code, f.problem[:900])
                for f in report.findings
            ],
        )
    )
    return results


def _previous_states(tenant_id: uuid.UUID) -> dict[str, Any]:
    """The most recent recorded result per check, before this pass."""
    from core.kernel_models import AuditEvent

    rows = (
        AuditEvent.objects.filter(
            tenant_id=tenant_id,
            action=HEALTH_ACTION,
            subject_key__in=[subject_for(code) for code in SCHEDULED_CODES],
        )
        .order_by("subject_key", "-recorded_at", "-id")
        .distinct("subject_key")
    )
    return {str(row.subject_key): row for row in rows}


def run_health_checks(tenant_id: uuid.UUID) -> list[CheckResult]:
    """Measure every check, record the results and open or note their findings."""
    from alerts.goods_services import add_exception_event, open_exception
    from core.kernel_models import AuditEvent

    checked_at = database_now()
    results = measure(tenant_id, checked_at)

    def handler(run: CommandRun) -> CommandResult:
        previous = _previous_states(tenant_id)
        for result in results:
            opened: list[str] = []
            for subject_key, reason_code, note in result.findings:
                exception = open_exception(
                    run,
                    kind=PROTECTION_KIND,
                    site_id=None,
                    subject_key=subject_key,
                    reason_code=reason_code,
                    source_event_key=_finding_key(tenant_id, subject_key),
                    note=note,
                    reopen=True,
                )
                opened.append(str(exception.pk))
            if opened:
                result.issue_id = opened[0]
            before = previous.get(subject_for(result.code))
            if result.ok and before is not None and before.outcome == "attention":
                _note_recovery(run, add_exception_event, before, result, checked_at)
            run.record(
                AuditEvent(
                    action=HEALTH_ACTION,
                    subject_key=subject_for(result.code),
                    site_id=None,
                    outcome=result.state,
                    reason_code=None,
                    before=None,
                    after={
                        "observed": result.observed[:500],
                        "threshold": result.threshold,
                        "owner_role": result.owner_role,
                        "scope": result.scope[:500],
                        "issue_id": result.issue_id,
                        "issue_ids": opened,
                    },
                    authority=run.authority,
                ),
                event_at=checked_at,
            )
        run.audit_subject_key = "health:pass"
        run.audit_after = {"checks": len(results), "attention": sum(not r.ok for r in results)}
        return CommandResult(resource_type="health_pass")

    execute_command(
        Principal(tenant_id=tenant_id, service_code=HEALTH_SERVICE),
        CommandSpec(
            action=HEALTH_ACTION,
            command_id=uuid.uuid4(),
            business_input={"checked_at": checked_at.isoformat()},
            subject_key="health:pass",
        ),
        handler,
    )
    return results


def _note_recovery(
    run: CommandRun,
    add_exception_event: Any,
    before: Any,
    result: CheckResult,
    checked_at: datetime,
) -> None:
    """Put the passing check on each still-open exception the failing one raised.

    Evidence of recovery, not closure: an evidence-protection finding closes
    only when someone has investigated and recorded it.
    """
    from alerts.goods_models import GoodsException

    ids = [i for i in (before.after or {}).get("issue_ids") or [] if i]
    if not ids:
        return
    still_open = GoodsException.objects.filter(
        tenant_id=run.tenant_id, pk__in=ids, state="open"
    ).order_by("id")
    for exception in still_open:
        try:
            add_exception_event(
                run,
                exception.pk,
                event_kind="note",
                owner_human_id=None,
                note=(
                    f"Scheduled check {result.code} passed at {checked_at.isoformat()} "
                    f"({result.scope}). This records recovery only; the exception stays "
                    "open until it is investigated and recorded."
                ),
            )
        except Refusal as refusal:
            # Resolved (or gone) between the read above and the lock: there is
            # nothing left to note on, and one raced note must never roll back
            # the whole pass and lose every check's recorded result.
            if refusal.code not in ("STATE_CONFLICT", "NOT_FOUND"):
                raise
