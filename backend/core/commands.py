"""One boundary for every goods-v1 official change (design §4.1).

``execute_command`` runs a handler exactly once per command identity:

* the whole attempt is one PostgreSQL SERIALIZABLE transaction, retried from the
  start on 40001/40P01 only, at most five times with jittered backoff;
* the command key is claimed first; a committed duplicate with the same
  fingerprint replays its stored outcome, and different content under the same
  identity is ``COMMAND_CONFLICT``;
* the handler runs inside a savepoint. A typed ``Refusal`` rolls the business
  effects back and commits only the refused outcome, the attempt and the audit;
* evidence rows are sealed and inserted at the end, so number allocation and
  chain-head locks come after every lower-ranked lock;
* a connection lost while committing is ``OUTCOME_UNKNOWN``: the caller asks for
  the outcome (``outcome_status``) and retries the same identity, never a new one;
  a lost connection before the commit is a known ``SERVICE_UNAVAILABLE`` failure;
* a command that ends without a stored outcome - retries exhausted, an unexpected
  fault, an unavailable database or an unknown commit - still leaves one bounded
  ``terminal_failure`` ``CommandAttempt`` whose reason names what happened, or
  off-box evidence if the database cannot take it;
* a person's authority is re-checked inside the transaction (``Principal.guard``):
  under the security lock before the handler, and again after the business effects
  just before the outcome is written. Authority withdrawn by then rolls the whole
  attempt back - command key included, so the same identity can retry once access
  is restored - and only a refused ``CommandAttempt`` is written.

When already inside an atomic block (the test suite wraps each test in one) the
isolation level cannot be set and a retry cannot restart the outer transaction,
so the command runs once inside a savepoint with the same semantics otherwise.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any

from django.db import (
    DatabaseError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    connection,
    transaction,
)
from django.db.models import Model, QuerySet

from core.canonical import content_hash
from core.evidence import EvidenceSealer, chain_hash, row_content
from core.refusals import Refusal, issue
from core.tenancy import ACTOR_SETTING, TENANT_SETTING

CONTRACT_VERSION = "goods-v1"
RETRY_WINDOWS = (0.05, 0.1, 0.2, 0.4, 0.8)
RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})
#: Server shutdown codes: the connection is gone, whatever the transaction did.
_SHUTDOWN_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})

logger = logging.getLogger("kdps.goods.operational")


class LockRank(IntEnum):
    """The fixed acquisition order (design §4.1)."""

    COMMAND = 1
    SECURITY = 2
    SITE = 3
    DOCUMENT = 4
    DRAFT = 5
    LOT = 6
    ALLOCATION = 7
    RESERVATION = 8
    SERIES = 9
    CHAIN = 10


class LockOrderViolation(RuntimeError):
    """A handler asked for a lower-ranked lock after a higher-ranked one."""


class _RestartTransaction(Exception):  # noqa: N818 - control flow, not an error
    pass


class _OutcomeUnknown(Exception):  # noqa: N818 - control flow, not an error
    """The connection failed while committing: the commit may or may not have happened."""


class _AuthorityWithdrawn(Exception):  # noqa: N818 - control flow, not an error
    """Authority failed its in-transaction re-check; the attempt must leave nothing."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.code)
        self.refusal = refusal


def _check_authority(principal: Principal, run: CommandRun, *, final: bool) -> None:
    if principal.guard is None:
        return
    try:
        principal.guard(run, final)
    except Refusal as refusal:
        raise _AuthorityWithdrawn(refusal) from None


@dataclass(frozen=True)
class Principal:
    tenant_id: uuid.UUID
    human_id: uuid.UUID | None = None
    service_code: str | None = None
    user_id: int | None = None
    session_id: uuid.UUID | None = None
    role_grant_ids: tuple[str, ...] = ()
    step_up_at: datetime | None = None
    #: Re-checks the person's authority inside the command: ``guard(run, final)``.
    guard: Callable[[CommandRun, bool], None] | None = field(
        default=None, compare=False, repr=False
    )

    @property
    def key(self) -> str:
        return f"human:{self.human_id}" if self.human_id else f"service:{self.service_code}"

    def authority(self) -> dict[str, Any]:
        return {
            "human_id": str(self.human_id) if self.human_id else None,
            "service_code": self.service_code,
            "role_grant_ids": list(self.role_grant_ids),
            "policy_version_ids": [],
            "step_up_at": self.step_up_at.isoformat() if self.step_up_at else None,
            "maker_id": None,
            "scope": {},
            "thresholds": {"qty": None, "value_paise": None},
        }


@dataclass
class CommandSpec:
    action: str
    command_id: uuid.UUID
    business_input: Any
    resource_ids: list[str] = field(default_factory=list)
    evidence_hashes: list[str] = field(default_factory=list)
    subject_key: str | None = None
    site_id: int | None = None
    reviewed_hash: str | None = None
    contract_version: str = CONTRACT_VERSION

    def fingerprint(self) -> str:
        return content_hash(
            {
                "action": self.action,
                "contract_version": self.contract_version,
                "resource_ids": sorted(self.resource_ids),
                "input": self.business_input,
                "evidence": sorted(self.evidence_hashes),
            }
        )


@dataclass
class CommandResult:
    resource_type: str
    resource_id: str | None = None
    status_code: int = 200
    document_number: str | None = None
    version: int | None = None
    revision: int | None = None
    event_ids: list[str] = field(default_factory=list)
    issue_codes: list[str] = field(default_factory=list)
    replayed: bool = False

    def as_json(self) -> dict[str, Any]:
        body = {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "document_number": self.document_number,
            "version": self.version,
            "revision": self.revision,
            "event_ids": self.event_ids,
            "issue_codes": self.issue_codes,
        }
        body["result_hash"] = content_hash(body)
        return body

    @classmethod
    def from_json(cls, body: dict[str, Any], status_code: int) -> CommandResult:
        return cls(
            resource_type=body["resource_type"],
            resource_id=body.get("resource_id"),
            status_code=status_code,
            document_number=body.get("document_number"),
            version=body.get("version"),
            revision=body.get("revision"),
            event_ids=list(body.get("event_ids") or []),
            issue_codes=list(body.get("issue_codes") or []),
            replayed=True,
        )


class CommandRun:
    """What a handler receives: identity, time, locks, evidence and side records."""

    def __init__(
        self, principal: Principal, spec: CommandSpec, key_id: uuid.UUID, now: datetime
    ) -> None:
        self.principal = principal
        self.spec = spec
        self.key_id = key_id
        self.now = now
        self.tenant_id = principal.tenant_id
        self.authority = principal.authority()
        self.reconciliation: dict[str, Any] | None = None
        self.audit_before: Any = None
        self.audit_after: Any = None
        self.audit_site_id: int | None = spec.site_id
        self.audit_subject_key: str | None = spec.subject_key
        #: The id this command's own ``AuditEvent`` will carry, known before it is
        #: written so a follow-up raised inside the command can name it (ticket 03D).
        self.audit_event_id = uuid.uuid4()
        self._max_rank = 0
        self.evidence = EvidenceSealer(
            tenant_id=principal.tenant_id,
            command_key_id=key_id,
            actor_id=principal.human_id,
            service_code=principal.service_code,
            now=now,
        )

    def record(self, row: Any, *, event_at: datetime | None = None) -> Any:
        return self.evidence.add(row, event_at=event_at)

    def claim_rank(self, rank: LockRank) -> None:
        """Record that a lock of ``rank`` is being taken; lower ranks are closed after it."""
        if rank < self._max_rank:
            raise LockOrderViolation(
                f"lock rank {rank.name} requested after rank {LockRank(self._max_rank).name}"
            )
        self._max_rank = max(self._max_rank, int(rank))

    def lock(
        self, rank: LockRank, queryset: QuerySet[Any], *, of: tuple[str, ...] = ()
    ) -> list[Any]:
        """Row-lock ``queryset`` at ``rank``, in primary-key order.

        ``of`` narrows ``FOR UPDATE`` to the named relations. Pass ``("self",)`` when the
        query ``select_related``s a nullable foreign key: PostgreSQL refuses to lock the
        nullable side of an outer join, and the rank covers the queried rows only.
        """
        self.claim_rank(rank)
        return list(queryset.select_for_update(of=of).order_by("pk"))

    def advisory_lock(self, rank: LockRank, keys: list[str]) -> None:
        """Transaction-scoped advisory locks for append-only subjects that cannot be row-locked."""
        self.claim_rank(rank)
        with connection.cursor() as cursor:
            for key in sorted(set(keys)):
                number = int(content_hash([str(self.tenant_id), key])[:15], 16)
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [number])

    def outbox(
        self,
        kind: str,
        subject_key: str,
        payload: dict[str, Any],
        *,
        not_before: datetime | None = None,
        request_key: uuid.UUID | None = None,
    ) -> Any:
        from core.kernel_models import JobState, OutboxIntent

        intent = OutboxIntent(
            kind=kind,
            subject_key=subject_key[:100],
            request_key=request_key or uuid.uuid4(),
            payload=payload,
            not_before=not_before or self.now,
        )
        self.record(intent)
        JobState.objects.create(tenant_id=self.tenant_id, intent_id=intent.pk)
        return intent


def _sqlstate(exc: BaseException) -> str | None:
    cause: BaseException | None = exc
    while cause is not None:
        state = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
        if state:
            return str(state)
        cause = cause.__cause__
    return None


def _connection_lost(exc: DatabaseError) -> bool:
    """A failure of the connection itself, not a refusal by the database."""
    if isinstance(exc, InterfaceError):
        return True
    if not isinstance(exc, OperationalError):
        return False
    state = _sqlstate(exc)
    return state is None or state.startswith("08") or state in _SHUTDOWN_SQLSTATES


def database_now() -> datetime:
    """The database clock. Every goods timestamp and due time is judged by this clock.

    Commands stamp ``run.now`` from it, so anything compared with a stamped time (an
    outbox intent's ``not_before``, a lease) must read it too - the application host's
    clock can differ from the database's by more than a command takes.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        row = cursor.fetchone()
    assert row is not None
    value: datetime = row[0]
    return value


def _bind_session(principal: Principal) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config(%s, %s, true), set_config(%s, %s, true)",
            [TENANT_SETTING, str(principal.tenant_id), ACTOR_SETTING, principal.key],
        )


def write_attempt(
    principal: Principal,
    *,
    action: str,
    outcome: str,
    reason_code: str | None,
    key_id: uuid.UUID | None = None,
    subject_key: str | None = None,
    reviewed_hash: str | None = None,
    reconciliation: dict[str, Any] | None = None,
    authority: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Append one ``CommandAttempt`` to its chain (used in and out of commands)."""
    from core.evidence import seal_rows
    from core.kernel_models import CommandAttempt

    moment = now or database_now()
    attempt = CommandAttempt(
        tenant_id=principal.tenant_id,
        command_key_id=key_id,
        actor_id=principal.human_id,
        service_code=None if principal.human_id else (principal.service_code or "anonymous"),
        event_at=moment,
        recorded_at=moment,
        action=action[:100],
        subject_key=subject_key[:100] if subject_key else None,
        reviewed_hash=reviewed_hash,
        reconciliation=reconciliation,
        authority=authority or principal.authority(),
        outcome=outcome,
        reason_code=reason_code,
    )
    seal_rows(principal.tenant_id, [attempt])


def record_refusal_outside_command(
    principal: Principal, *, action: str, refusal: Refusal, subject_key: str | None = None
) -> None:
    """Safe pre-command refusal evidence: identifiers and the reason, nothing more."""
    try:
        with transaction.atomic():
            _bind_session(principal)
            write_attempt(
                principal,
                action=action,
                outcome="refused",
                reason_code=refusal.code,
                subject_key=subject_key,
            )
    except DatabaseError:
        _offbox_failure(
            principal,
            action=action,
            subject_key=subject_key,
            command_id=None,
            reason=refusal.code,
            outcome="refused",
        )


def execute_command(
    principal: Principal,
    spec: CommandSpec,
    handler: Callable[[CommandRun], CommandResult],
) -> CommandResult:
    nested = connection.in_atomic_block
    attempts = 0
    while True:
        try:
            return _run_once(principal, spec, handler, nested=nested)
        except _AuthorityWithdrawn as withdrawn:
            record_refusal_outside_command(
                principal,
                action=spec.action,
                refusal=withdrawn.refusal,
                subject_key=spec.subject_key,
            )
            raise withdrawn.refusal from None
        except _RestartTransaction:
            attempts += 1
            if attempts > len(RETRY_WINDOWS):
                _terminal_attempt(principal, spec, "RETRY_EXHAUSTED")
                raise Refusal(
                    "RETRY_EXHAUSTED", "The command could not complete; retry it."
                ) from None
            continue
        except _OutcomeUnknown as unknown:
            _terminal_attempt(principal, spec, "OUTCOME_UNKNOWN")
            raise Refusal(
                "OUTCOME_UNKNOWN",
                "We could not confirm whether this was saved. Check the command's status, "
                "then retry with the same command_id.",
                issues=[
                    issue(
                        "OUTCOME_UNKNOWN",
                        "Look up this command_id, then retry it unchanged; never use a new one.",
                        field="command_id",
                    )
                ],
            ) from unknown.__cause__
        except Refusal:
            raise
        except DatabaseError as exc:
            state = _sqlstate(exc)
            if not nested and state in RETRYABLE_SQLSTATES:
                attempts += 1
                if attempts > len(RETRY_WINDOWS):
                    _terminal_attempt(principal, spec, "RETRY_EXHAUSTED")
                    raise Refusal(
                        "RETRY_EXHAUSTED",
                        "The system was busy and the command did not complete; retry it.",
                    ) from exc
                time.sleep(random.uniform(0, RETRY_WINDOWS[attempts - 1]))  # noqa: S311 - jitter only
                continue
            lost = _connection_lost(exc)
            if not nested:
                # Inside an outer transaction a database error may have aborted it, so
                # nothing more can be written there; the outer caller owns that failure.
                _terminal_attempt(
                    principal, spec, "SERVICE_UNAVAILABLE" if lost else "INTERNAL_ERROR"
                )
            if lost:
                raise Refusal(
                    "SERVICE_UNAVAILABLE",
                    "The database is not available; nothing was saved. Retry with the same "
                    "command_id.",
                ) from exc
            raise
        except Exception:
            if not nested:
                # Nested, the caller's transaction owns the failure and would roll it back.
                _terminal_attempt(principal, spec, "INTERNAL_ERROR")
            raise


def _terminal_attempt(principal: Principal, spec: CommandSpec, reason: str) -> None:
    """Post-rollback evidence for a command that left no stored outcome (design §4.1).

    It is a ``terminal_failure`` attempt whose reason classifies what happened:
    ``RETRY_EXHAUSTED``, ``INTERNAL_ERROR``, ``SERVICE_UNAVAILABLE`` or
    ``OUTCOME_UNKNOWN`` (the commit may have happened; ``outcome_status`` says).
    """
    outcome = "terminal_failure"
    try:
        with transaction.atomic():
            _bind_session(principal)
            write_attempt(
                principal,
                action=spec.action,
                outcome=outcome,
                reason_code=reason,
                subject_key=spec.subject_key,
                reviewed_hash=spec.reviewed_hash,
            )
        return
    except DatabaseError:
        pass
    _offbox_failure(
        principal,
        action=spec.action,
        subject_key=spec.subject_key,
        command_id=spec.command_id,
        reason=reason,
        outcome=outcome,
    )


def _offbox_failure(
    principal: Principal,
    *,
    action: str,
    subject_key: str | None,
    command_id: uuid.UUID | None,
    reason: str,
    outcome: str,
) -> None:
    """The database could not take the attempt: keep safe identifiers outside it instead.

    A refusal before any command has no ``command_id``; it is filed under ``no-command``.
    """
    from core.offbox import OffboxError, get_store

    record = {
        "tenant_id": str(principal.tenant_id),
        "principal_key": principal.key,
        "command_id": str(command_id) if command_id is not None else None,
        "action": action[:100],
        "subject_key": subject_key,
        "outcome": outcome,
        "reason_code": reason,
        "noted_at": time.time_ns(),
    }
    folder = str(command_id) if command_id is not None else "no-command"
    key = f"operational-failures/{principal.tenant_id}/{folder}/{uuid.uuid4()}.json"
    try:
        get_store().put(key, json.dumps(record, sort_keys=True).encode())
    except (OffboxError, OSError):
        logger.error("goods-v1 command failure not recorded anywhere durable: %s", record)


def _claim_key(principal: Principal, spec: CommandSpec, fingerprint: str) -> tuple[Any, bool]:
    from core.kernel_models import CommandKey

    key_id = uuid.uuid4()
    # Command keys are append-only, so the application role cannot row-lock them
    # (FOR UPDATE needs UPDATE privilege). A transaction-scoped advisory lock on
    # the identity serialises duplicates instead; the unique index backs it up.
    lock_key = int.from_bytes(
        content_hash([str(principal.tenant_id), principal.key, str(spec.command_id)])[:16].encode(),
        "big",
    ) % (2**63)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])
        cursor.execute(
            "INSERT INTO core_commandkey "
            "(id, tenant_id, command_id, principal_key, action, fingerprint, contract_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, principal_key, command_id) DO NOTHING RETURNING id",
            [
                key_id,
                principal.tenant_id,
                spec.command_id,
                principal.key,
                spec.action[:100],
                fingerprint,
                spec.contract_version,
            ],
        )
        created = cursor.fetchone() is not None
    key = CommandKey.objects.get(
        tenant_id=principal.tenant_id, principal_key=principal.key, command_id=spec.command_id
    )
    return key, created


def _run_once(
    principal: Principal,
    spec: CommandSpec,
    handler: Callable[[CommandRun], CommandResult],
    *,
    nested: bool,
) -> CommandResult:
    from core.kernel_models import AuditEvent, CommandOutcome

    fingerprint = spec.fingerprint()
    refusal: Refusal | None = None
    result: CommandResult | None = None
    committing = False
    try:
        with transaction.atomic():
            if not nested:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            _bind_session(principal)
            now = database_now()
            key, created = _claim_key(principal, spec, fingerprint)
            if not created:
                if key.fingerprint != fingerprint:
                    refusal = Refusal(
                        "COMMAND_CONFLICT",
                        "This command identity was already used with different contents.",
                    )
                else:
                    stored = CommandOutcome.objects.filter(key=key).first()
                    if stored is not None:
                        # A replay answers only to someone who still holds the authority.
                        _check_authority(
                            principal, CommandRun(principal, spec, key.pk, now), final=False
                        )
                        if stored.outcome == CommandOutcome.Outcome.REFUSED:
                            refusal = Refusal.from_body(stored.result, stored.status_code)
                        else:
                            result = CommandResult.from_json(stored.result, stored.status_code)
            if refusal is None and result is None:
                run = CommandRun(principal, spec, key.pk, now)
                sid = transaction.savepoint()
                try:
                    _check_authority(principal, run, final=False)
                    result = handler(run)
                    for follow_up in _SUCCESS_FOLLOW_UPS:
                        follow_up(run)
                    run.evidence.flush()
                    _check_authority(principal, run, final=True)
                except Refusal as business_refusal:
                    transaction.savepoint_rollback(sid)
                    run.evidence.discard()
                    refusal = business_refusal
                except IntegrityError as exc:
                    transaction.savepoint_rollback(sid)
                    run.evidence.discard()
                    mapped = map_integrity_error(exc)
                    if mapped is None:
                        raise
                    refusal = mapped
                else:
                    transaction.savepoint_commit(sid)
                if refusal is not None:
                    CommandOutcome.objects.create(
                        tenant_id=principal.tenant_id,
                        key=key,
                        status_code=refusal.status,
                        outcome=CommandOutcome.Outcome.REFUSED,
                        result=refusal.body(),
                    )
                    outcome_word = "refused"
                    reason = refusal.code
                else:
                    assert result is not None
                    CommandOutcome.objects.create(
                        tenant_id=principal.tenant_id,
                        key=key,
                        status_code=result.status_code,
                        outcome=CommandOutcome.Outcome.SUCCEEDED,
                        result=result.as_json(),
                    )
                    outcome_word = "succeeded"
                    reason = None
                audit = EvidenceSealer(
                    tenant_id=principal.tenant_id,
                    command_key_id=key.pk,
                    actor_id=principal.human_id,
                    service_code=principal.service_code,
                    now=now,
                )
                audit.add(
                    AuditEvent(
                        id=run.audit_event_id,
                        action=spec.action[:100],
                        subject_key=(run.audit_subject_key or "")[:100] or None,
                        site_id=run.audit_site_id,
                        outcome=outcome_word,
                        reason_code=reason,
                        before=run.audit_before,
                        after=run.audit_after if refusal is None else None,
                        authority=run.authority,
                    )
                )
                audit.flush()
                write_attempt(
                    principal,
                    action=spec.action,
                    outcome=outcome_word,
                    reason_code=reason,
                    key_id=key.pk,
                    subject_key=spec.subject_key,
                    reviewed_hash=spec.reviewed_hash,
                    reconciliation=run.reconciliation,
                    authority=run.authority,
                    now=now,
                )
            # Leaving the block commits. A lost connection from here on cannot say
            # whether the commit happened.
            committing = True
    except DatabaseError as exc:
        if committing and not nested and _connection_lost(exc):
            raise _OutcomeUnknown() from exc
        raise
    except (_RestartTransaction, _AuthorityWithdrawn):
        raise
    except Refusal:
        raise
    if refusal is not None:
        raise refusal
    assert result is not None
    return result


#: Constraint name → refusal, registered by the domain apps that own the tables.
_INTEGRITY_REFUSALS: dict[str, tuple[str, str]] = {}

#: Run inside every succeeded command's savepoint, after its handler and before its
#: evidence is sealed, registered by the domain apps that own the follow-up. A
#: refusal from one rolls the whole command back like the handler's own.
_SUCCESS_FOLLOW_UPS: list[Callable[[CommandRun], None]] = []


def register_success_follow_up(follow_up: Callable[[CommandRun], None]) -> None:
    if follow_up not in _SUCCESS_FOLLOW_UPS:
        _SUCCESS_FOLLOW_UPS.append(follow_up)


def register_integrity_refusal(constraint: str, code: str, message: str) -> None:
    _INTEGRITY_REFUSALS[constraint] = (code, message)


def map_integrity_error(exc: IntegrityError) -> Refusal | None:
    text = str(exc)
    for constraint, (code, message) in _INTEGRITY_REFUSALS.items():
        if constraint in text:
            return Refusal(code, message)
    return None


def outcome_status(principal: Principal, command_id: uuid.UUID) -> dict[str, Any]:
    """``CommandStatusDTO`` for one of the principal's own command identities."""
    return outcome_status_for(principal.tenant_id, principal.key, command_id)


def outcome_status_for(
    tenant_id: uuid.UUID, principal_key: str, command_id: uuid.UUID
) -> dict[str, Any]:
    """``CommandStatusDTO`` for a command identity named by its principal key.

    A command identity is (tenant, principal, command id): the key is part of the
    identity, not a filter on it, so an audit reader asking about someone else's
    command (E191's ``principal_key``) has to say whose.
    """
    from core.kernel_models import CommandKey, CommandOutcome

    key = CommandKey.objects.filter(
        tenant_id=tenant_id, principal_key=principal_key, command_id=command_id
    ).first()
    if key is None:
        return {"status": "not_recorded", "http_status": None, "result": None}
    stored = CommandOutcome.objects.filter(key=key).first()
    if stored is None:
        return {"status": "not_recorded", "http_status": None, "result": None}
    return {
        "status": stored.outcome,
        "http_status": stored.status_code,
        "result": stored.result if stored.outcome == CommandOutcome.Outcome.SUCCEEDED else None,
    }


def verify_row(row: Model, previous: str | None) -> bool:
    return chain_hash(previous, row._meta.db_table, row_content(row)) == getattr(
        row, "row_hash", None
    )
