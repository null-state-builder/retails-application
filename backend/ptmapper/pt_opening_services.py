"""Store test run: load a staged opening source onto the floor as a durable job (synthetic only).

A test store opens from its PT files (``ptmapper.pt_opening``). Mapping hundreds of
brands and sizes by hand, verifying with a CSV and scanning every line is not what a
test run needs, so an ``opening_load`` outbox job runs the source through the same
reviewed opening path the test-store loader uses
(``accounts.test_store_loader.OpeningDriver``), one idempotent step at a time:

* it acts as the store's opening-preparer login (``*preparer@``), through a
  session-free access context - its authority now, never a synthetic session;
* every decision is the preparer's own request, decided with no second person
  when the ``pt.approve.opening`` policy in force allows it for a test store
  (``accounts.test_store_loader.OPENING_AUTO_FLAG``). Otherwise the job stops
  at ``waiting_approval``; an owner decides in Approvals and loads again.

The job holds a session-level advisory lock on the source (it dies with the
process, so nothing goes stale), works for at most ``BUDGET`` seconds - well inside
the worker's lease - and then queues its own continuation. Progress lives on the
source (``reconciliation["quick_load"]``, which the review hash does not cover) so
the page shows it; whether a load is running is the job's own state.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from django.db import connection
from django.utils import timezone

from accounts.principal import AccessContext, access_for_user
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.outbox import JobOutcome
from core.refusals import Refusal
from ptmapper import soh_services
from ptmapper.soh_models import SohImport

logger = logging.getLogger(__name__)

JOB = "opening_load"
#: The command that queues a load (from the upload, the Load button, the shell or a continuation).
ENQUEUE = "opening.load.enqueue"
#: Seconds of steps per job before it queues its continuation (the worker's lease is 120 s).
BUDGET = 90.0
#: ``quick_load.state`` values a job leaves behind; ``running`` is read from the job itself.
FINISHED = frozenset({"done", "failed", "waiting_approval"})


def subject(source_id: Any) -> str:
    return f"soh_import:{source_id}"


def _record(source_id: uuid.UUID, **fields: Any) -> None:
    source = SohImport.objects.get(pk=source_id)
    current = dict(source.reconciliation or {})
    current["quick_load"] = {**dict(current.get("quick_load") or {}), **fields, "updated_at": timezone.now().isoformat()}
    SohImport.objects.filter(pk=source_id).update(reconciliation=current)


def active_job(source: SohImport) -> Any:
    from core.kernel_models import JobState

    return (JobState.objects.filter(intent__kind=JOB, intent__subject_key=subject(source.pk),
                                    state__in=[JobState.State.PENDING, JobState.State.RUNNING])
            .order_by("-created_at").first())


def running(source: SohImport) -> bool:
    """A load job is queued or working, or a shell (``open_test_store --foreground``) holds the source."""
    return active_job(source) is not None or _lock_held(source.pk)


def status(source: SohImport) -> dict[str, Any]:
    """What the page shows: the recorded progress, ``running`` while a load is queued or working."""
    state = dict((source.reconciliation or {}).get("quick_load") or {})
    if running(source):
        state["state"] = "running"
        state.setdefault("step", "Queued")
    elif state.get("state") == "running":
        # Recorded as running, but no job or shell holds it (lost work, or a pre-job thread).
        state["state"] = "failed"
        state["error"] = state.get("error") or "The load stopped before it finished; load it again to resume."
    return state


# -- who the job acts as ----------------------------------------------------------


def job_access(tenant_id: uuid.UUID, human_id: Any) -> AccessContext:
    """A person's authority now, with no session: a job is not a browser request (``files.goods_exports``)."""
    from accounts.models import User

    user = User.objects.filter(tenant_id=tenant_id, human_id=human_id, is_active=True).first()
    if user is None:
        raise Refusal("OPENING_PREPARER_MISSING", "The store's opening preparer has no active login.", status=409)
    return access_for_user(user)


def preparer_for(source: SohImport) -> AccessContext:
    """The store's opening preparer: whoever prepared the source, else a ``*preparer@`` login that may."""
    from accounts.models import User
    from accounts.test_store_loader import _is_preparer

    if source.prepared_by_id is not None:
        return job_access(source.tenant_id, source.prepared_by_id)
    cells = {(source.site_id, None)}
    for user in User.objects.filter(tenant_id=source.tenant_id, is_active=True,
                                    email__icontains="preparer@").order_by("email"):
        if not _is_preparer(user.email):
            continue
        access = job_access(source.tenant_id, user.human_id)
        if (access.covers_all_actions({soh_services.STAGE, "stock.accept"}, cells)
                and any(access.covers_all({soh_services.PREPARE}, cells, {field}) for field in ("cost", "cost_own_pt"))):
            return access
    raise Refusal("OPENING_PREPARER_MISSING",
                  "This store has no opening preparer login. Run load_test_store for it to create one.", status=409)


# -- queueing a load --------------------------------------------------------------


def auto_opening(tenant_id: uuid.UUID) -> bool:
    from accounts.test_store_loader import opening_auto_in_force

    return opening_auto_in_force(tenant_id)


def _loadable(source: SohImport) -> None:
    from ptmapper.stock_sync_services import require_test_store

    require_test_store(source.site)
    if source.state == "withdrawn":
        raise Refusal("SOH_SOURCE_WITHDRAWN", "This source was withdrawn.", status=409)
    if status(source).get("state") == "done":
        raise Refusal("STATE_CONFLICT", "This source is already loaded.", status=409)


def may_load(access: AccessContext, source: SohImport) -> bool:
    """The uploader (who may still stage here), or - while openings need approval - an owner who approves them."""
    cells = soh_services.source_cells(source)
    if str(access.human_id) == str(source.uploaded_by_id) and access.covers_all({soh_services.STAGE}, cells):
        return True
    return (not auto_opening(source.tenant_id)
            and access.covers_all({soh_services.APPROVE}, cells, {"cost", "financial"}))


def can_load(access: AccessContext, source: SohImport) -> bool:
    """Whether the page offers Load to this caller now."""
    from masters.store_features import is_real_store

    return (not is_real_store(source.site) and source.state != "withdrawn"
            and status(source).get("state") != "done" and not running(source) and may_load(access, source))


def enqueue(run: CommandRun, source_id: uuid.UUID, *, after: uuid.UUID | None = None) -> Any:
    """Queue the source's load job inside ``run``, once.

    A load already queued or working is returned as it is. ``after`` is the request key
    of the job this one continues (it is still running, so it is not a duplicate).
    """
    from core.kernel_models import OutboxIntent

    source = SohImport.objects.get(pk=source_id)
    if after is None:
        current = active_job(source)
        if current is not None:
            return current.intent
        earlier = OutboxIntent.objects.filter(kind=JOB, subject_key=subject(source_id)).count()
        request_key = uuid.uuid5(source_id, f"load:{source.revision}:{earlier}")
    else:
        request_key = uuid.uuid5(after, "continue")
        # A job rerun after its lease ran out (the worker died after queueing) finds its continuation.
        existing = OutboxIntent.objects.filter(tenant_id=run.tenant_id, request_key=request_key).first()
        if existing is not None:
            return existing
    intent = run.outbox(JOB, subject(source_id), {"source_id": str(source_id)}, request_key=request_key)
    if after is None:
        _record(source_id, state="running", step="Queued", error=None, queued_at=run.now.isoformat())
    run.audit_after = {"source_import_id": str(source_id), "intent_id": str(intent.pk), "continues": bool(after)}
    return intent


def _enqueue_command(access: AccessContext, source: SohImport, *, after: uuid.UUID | None = None) -> Any:
    holder: dict[str, Any] = {}

    def handler(run: CommandRun) -> CommandResult:
        holder["intent"] = enqueue(run, source.pk, after=after)
        return CommandResult(resource_type="soh_import", resource_id=str(source.pk), status_code=202)

    execute_command(access.principal(), CommandSpec(
        action=ENQUEUE, command_id=uuid.uuid4(), business_input={"source_id": str(source.pk), "after": str(after or "")},
        subject_key=subject(source.pk), site_id=source.site_id), handler)
    return holder["intent"]


def request_load(access: AccessContext, source: SohImport) -> Any:
    """The Load button and ``open_test_store``: queue the load, or say why not."""
    _loadable(source)
    if running(source):
        raise Refusal("STATE_CONFLICT", "This source is already loading.", status=409)
    if not may_load(access, source):
        raise Refusal("ACTION_DENIED", "Only the person who uploaded this source, or an owner while openings "
                      "need approval, can load it.", status=403)
    preparer_for(source)  # refuse now, not in the job, when the store has nobody to prepare it
    return _enqueue_command(access, source)


# -- running a load ---------------------------------------------------------------


def _lock_key(source_id: uuid.UUID) -> int:
    return int.from_bytes(hashlib.sha256(f"{JOB}:{source_id}".encode()).digest()[:8], "big", signed=True)


def _lock_held(source_id: uuid.UUID) -> bool:
    key = _lock_key(source_id) & 0xFFFFFFFFFFFFFFFF
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s "
                       "AND objsubid = 1 AND granted "
                       "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",[key >> 32, key & 0xFFFFFFFF])
        return cursor.fetchone() is not None


@contextmanager
def source_lock(source_id: uuid.UUID) -> Iterator[bool]:
    """A session-level advisory lock on one source's load: yields whether it was taken.

    Session-level, not transaction-level: the steps commit one by one. It is released
    here, or by PostgreSQL when the process (and so its connection) dies.
    """
    key = _lock_key(source_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
        row = cursor.fetchone()
    taken = bool(row and row[0])
    try:
        yield taken
    finally:
        if taken:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])


def _rows(source: SohImport) -> list[Any]:
    return [SimpleNamespace(barcode=r.barcode, brand=r.source_brand, quantity=r.quantity, rate=r.source_rate_paise,
                            mrp=r.mrp_paise, source=r.source) for r in source.rows.order_by("ordinal")]


def driver_for(source: SohImport, *, log: Callable[[str], None]) -> Any:
    """The opening driver for ``source``, acting as its preparer (re-read before every step)."""
    from accounts.goods_demo import _owner_human_id
    from accounts.test_store_loader import PT_NOTE, PT_REASON, SOH_REASON, TEST_NOTE, OpeningDriver, catalogue_config
    from masters.goods_services import linked_warehouse_id
    from masters.models import Store

    store = source.site
    tenant = store.tenant
    pt = bool(source.source_metadata.get("pt_opening"))

    def configure() -> dict[str, Any]:
        sites = [store, *Store.objects.filter(pk=linked_warehouse_id(store) or 0)]
        return catalogue_config(tenant, _owner_human_id(tenant), sites, _rows(source), cost="pt" if pt else "soh", log=log)

    def preparer() -> AccessContext:
        return preparer_for(SohImport.objects.get(pk=source.pk))

    return OpeningDriver(tenant, store, stager=preparer, preparer=preparer, log=log, configure=configure,
                         note=PT_NOTE if pt else TEST_NOTE, reason=PT_REASON if pt else SOH_REASON)


def drive(source_id: uuid.UUID, *, budget: float | None,
          echo: Callable[[str], None] = lambda _text: None) -> tuple[JobOutcome, bool]:
    """Run steps until the source is on the floor, a decision is needed, it fails, or ``budget`` runs out.

    The caller holds ``source_lock``. Returns the job outcome and whether work is left
    (the budget ran out). ``echo`` receives each step and log line
    (``open_test_store --foreground`` prints them).
    """
    source = SohImport.objects.select_related("site__tenant").get(pk=source_id)
    previous = dict((source.reconciliation or {}).get("quick_load") or {})
    lines: list[str] = list(previous.get("log") or []) if previous.get("state") == "running" else []
    timings: list[dict[str, Any]] = list(previous.get("timings") or []) if previous.get("state") == "running" else []

    def log(text: str) -> None:
        lines.append(text)
        _record(source_id, log=lines[-20:])
        echo(text)

    started = time.monotonic()
    fields: dict[str, Any] = {"state": "running", "error": None}
    if previous.get("state") != "running" or not previous.get("started_at"):
        fields["started_at"] = timezone.now().isoformat()
    _record(source_id, **fields)
    try:
        driver = driver_for(source, log=log)
        while True:
            began = time.monotonic()
            label = driver.next_step(source_id)
            if label is None:
                break
            timings.append({"step": label, "seconds": round(time.monotonic() - began, 2)})
            _record(source_id, step=label, timings=timings[-200:])
            echo(label)
            if budget is not None and time.monotonic() - started >= budget:
                return JobOutcome("confirmed", provider_ref=f"continues after: {label}"[:200]), True
    except Exception as exc:
        from accounts.test_store_loader import WaitingForApproval

        if isinstance(exc, WaitingForApproval):
            _record(source_id, state="waiting_approval", step=str(exc), finished_at=timezone.now().isoformat())
            echo(str(exc))
            return JobOutcome("confirmed", provider_ref="waiting_approval"), False
        if isinstance(exc, Refusal):
            error = f"{exc.code}: {exc.message}"
            _record(source_id, state="failed", error=error, finished_at=timezone.now().isoformat())
            echo(error)
            return JobOutcome("failed", diagnostic=exc.code, terminal=True), False
        logger.exception("Opening load of source %s failed", source_id)
        error = f"{type(exc).__name__}: {exc}"[:500]
        _record(source_id, state="failed", error=error, finished_at=timezone.now().isoformat())
        echo(error)
        return JobOutcome("failed", diagnostic=error[:80]), False
    source.refresh_from_db()
    included = source.rows.filter(quantity__gt=0, exclusion_reason="")
    _record(source_id, state="done", step="Stock is on the floor and ready to sell",
            finished_at=timezone.now().isoformat(), loaded_rows=included.count(),
            loaded_qty=sum(included.values_list("quantity", flat=True)),
            excluded_rows=source.rows.filter(quantity__gt=0).exclude(exclusion_reason="").count())
    echo("Stock is on the floor and ready to sell")
    return JobOutcome("confirmed", provider_ref="done"), False


def run_load(intent: Any) -> JobOutcome:
    """The ``opening_load`` job handler (registered in ``ptmapper.apps``)."""
    source_id = uuid.UUID(str((intent.payload or {}).get("source_id")))
    with source_lock(source_id) as taken:
        if not taken:
            # Another job (or a shell) is loading this source; it finishes or continues it.
            return JobOutcome("confirmed", provider_ref="already_loading")
        outcome, more = drive(source_id, budget=BUDGET)
    if more:
        # Queued after the lock is released, so a worker that claims it at once can take it.
        try:
            source = SohImport.objects.get(pk=source_id)
            _enqueue_command(preparer_for(source), source, after=intent.request_key)
        except Refusal as exc:
            _record(source_id, state="failed", error=f"{exc.code}: {exc.message}",
                    finished_at=timezone.now().isoformat())
            return JobOutcome("failed", diagnostic=exc.code, terminal=True)
    return outcome
