"""Durable exports (design §4.4, E189-E190; change PRD §4.2, §5.10).

An export is asked for, not streamed. The request records an immutable
``ExportSpec`` on the outbox with the business command that authorised it; the
worker produces the file later, **re-reading the requester's grants as they are
at that moment** (design §4.4: "Jobs revalidate current access for exported
payloads"). A grant revoked between the ask and the run therefore narrows or
refuses the file, and never widens it.

Two seams, so no producer has to know about jobs and no job has to know about
stock:

* ``ExportKind`` - one ``check`` that validates a spec inside the caller's scope
  with no rows loaded, and one ``produce`` that builds the bytes. Domains
  register their own (``stockledger`` registers ``stock_csv``); this module owns
  neither.
* ``JobArtifact`` + ``EvidenceObject`` - the produced bytes are protected
  evidence like any other, downloaded through E121, which re-checks scope on
  every download. That is what makes "no broader cached file is returned after
  access changes" (E190 step 5) true of the bytes, not only of the job record.

``pt_xlsx`` is deliberately **not** registered: change PRD R18 keeps ordinary PT
exports synchronous (E132/E133). ``evidence_bundle`` and ``labels`` are not in
this stage. Each of the three refuses by name rather than silently producing
nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.utils.dateparse import parse_datetime

from accounts.principal import AccessContext, effective_grants
from core.canonical import canonical_json, sha256_hex
from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.offbox import OffboxError, get_store
from core.outbox import MAX_ATTEMPTS, JobOutcome
from core.refusals import Refusal, issue
from files.goods_models import EvidenceObject, UploadIntent

#: The command action E189 records. A registered privileged kind
#: (``accounts.actions.PRIVILEGED_COMMAND_ACTIONS``), not a name prefix.
EXPORT_ACTION = "exports.request"
#: The grant every export needs, wherever its rows come from.
EXPORT_GRANT = "export.run"
#: Evidence kind and retention for a produced export file.
EXPORT_EVIDENCE_KIND = "export"
RETENTION = timedelta(days=8 * 366)

#: Every ExportSpec kind the design names (§5.3). Only the registered ones run.
SPEC_KINDS = ("pt_xlsx", "stock_csv", "evidence_bundle", "labels")
#: Why a named-but-unregistered kind refuses, so the boundary says which rule.
NOT_IN_THIS_STAGE: dict[str, str] = {
    "pt_xlsx": (
        "PT exports are synchronous (change PRD R18): use the PT's own export or export.xlsx route."
    ),
    "evidence_bundle": "Evidence bundles are not part of this stage.",
    "labels": "Label exports are not part of this stage; print jobs produce labels.",
}
SCOPE_KINDS = ("tenant", "entity", "sites", "sbus", "brands")
FIELD_SET = ("cost", "margin", "layer_value", "personal")


def _invalid(message: str, field_name: str) -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field_name)])


@dataclass(frozen=True)
class ExportSpec:
    """§5.3's ``ExportSpec``, validated. The scope is a *filter*, never authority."""

    kind: str
    scope: dict[str, Any]
    field_set: tuple[str, ...]
    subject_key: str | None = None
    as_of: str | None = None
    profile_version_id: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "field_set": list(self.field_set),
            "subject_key": self.subject_key,
            "as_of": self.as_of,
            "profile_version_id": self.profile_version_id,
        }

    @property
    def site_ids(self) -> list[int]:
        return [int(s) for s in self.scope.get("site_ids") or []]

    @property
    def brand_ids(self) -> list[int]:
        return [int(b) for b in self.scope.get("brand_ids") or []]

    @property
    def sbu_ids(self) -> list[str]:
        return [str(s) for s in self.scope.get("sbu_ids") or []]


SPEC_FIELDS = frozenset(
    {"kind", "scope", "field_set", "subject_key", "as_of", "profile_version_id"}
)
SCOPE_FIELDS = frozenset({"scope_kind", "entity_id", "site_ids", "sbu_ids", "brand_ids"})


def _parse_scope(raw: Any) -> dict[str, Any]:
    """A ``ConfigScope`` with its ids normalised. It filters; it never widens."""
    if not isinstance(raw, dict) or raw.get("scope_kind") not in SCOPE_KINDS:
        raise _invalid(f"scope.scope_kind must be one of {', '.join(SCOPE_KINDS)}.", "scope")
    unknown = sorted(set(raw) - SCOPE_FIELDS)
    if unknown:
        raise _invalid(f"Unknown scope field(s): {', '.join(unknown)}.", "scope")
    scope: dict[str, Any] = {"scope_kind": raw["scope_kind"]}
    for key in ("site_ids", "brand_ids"):
        try:
            scope[key] = sorted({int(v) for v in raw.get(key) or []})
        except (TypeError, ValueError):
            raise _invalid(f"scope.{key} must be integer ids.", f"scope.{key}") from None
    try:
        scope["sbu_ids"] = sorted({str(uuid.UUID(str(v))) for v in raw.get("sbu_ids") or []})
        scope["entity_id"] = int(raw["entity_id"]) if raw.get("entity_id") is not None else None
    except (TypeError, ValueError):
        raise _invalid("scope ids are malformed.", "scope") from None
    return scope


def _parse_optionals(raw: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    subject_key = raw.get("subject_key")
    if subject_key is not None and (not isinstance(subject_key, str) or len(subject_key) > 100):
        raise _invalid("subject_key must be at most 100 characters.", "subject_key")
    as_of = raw.get("as_of")
    if as_of is not None and (not isinstance(as_of, str) or parse_datetime(as_of) is None):
        raise _invalid("as_of must be an ISO-8601 timestamp.", "as_of")
    profile_version_id = raw.get("profile_version_id")
    if profile_version_id is not None:
        try:
            profile_version_id = str(uuid.UUID(str(profile_version_id)))
        except (TypeError, ValueError):
            raise _invalid("profile_version_id must be a UUID.", "profile_version_id") from None
    return subject_key, as_of, profile_version_id


def parse_spec(raw: Any) -> ExportSpec:
    """Validate a declared ExportSpec. Unknown keys and unknown kinds are refused."""
    if not isinstance(raw, dict):
        raise _invalid("The export spec must be an object.", "kind")
    unknown = sorted(set(raw) - SPEC_FIELDS)
    if unknown:
        raise _invalid(f"Unknown spec field(s): {', '.join(unknown)}.", "spec")
    kind = raw.get("kind")
    if kind not in SPEC_KINDS:
        raise _invalid(f"kind must be one of {', '.join(SPEC_KINDS)}.", "kind")
    fields = raw.get("field_set") or []
    if not isinstance(fields, list) or any(f not in FIELD_SET for f in fields):
        raise _invalid(f"field_set may name only {', '.join(FIELD_SET)}.", "field_set")
    subject_key, as_of, profile_version_id = _parse_optionals(raw)
    return ExportSpec(
        kind=str(kind),
        scope=_parse_scope(raw.get("scope")),
        field_set=tuple(sorted(set(fields))),
        subject_key=subject_key,
        as_of=as_of,
        profile_version_id=profile_version_id,
    )


@dataclass(frozen=True)
class ExportFile:
    """What a producer hands back: the bytes, and exactly who they belong to."""

    filename: str
    media_type: str
    data: bytes
    row_count: int
    #: Every ``(site, brand)`` cell the file's rows were authorised under. This is
    #: what E121 re-checks at download: a reader must cover *all* of them.
    cells: tuple[tuple[int | None, int | None], ...]
    #: Sensitive fields present in the bytes, so the download needs those grants.
    contains_fields: tuple[str, ...] = ()
    #: The as-of watermark the rows were read at.
    source_watermark: str = ""
    #: ``complete``/``partial``/``unknown`` where an aggregate in the file can be
    #: partial - a value total over pieces whose value is not all known.
    completeness: str = "complete"
    #: Extra manifest facts worth freezing beside the hash.
    manifest: dict[str, Any] = field(default_factory=dict)


#: ``check`` validates a spec inside the caller's scope without loading rows;
#: ``produce`` builds the file. Both run again in the worker, under the grants
#: the requester holds *then*.
Check = Callable[[AccessContext, ExportSpec], None]
Produce = Callable[[AccessContext, ExportSpec], ExportFile]


@dataclass(frozen=True)
class ExportKind:
    check: Check
    produce: Produce


_KINDS: dict[str, ExportKind] = {}


def register_export_kind(kind: str, *, check: Check, produce: Produce) -> None:
    """Register a producer. Called from an app's ``ready()``, never from this module."""
    if kind not in SPEC_KINDS:  # pragma: no cover - programmer error
        raise ValueError(f"{kind} is not a designed ExportSpec kind")
    _KINDS[kind] = ExportKind(check=check, produce=produce)


def resolve_kind(kind: str) -> ExportKind:
    found = _KINDS.get(kind)
    if found is None:
        raise Refusal(
            "CONTRACT_DISABLED",
            NOT_IN_THIS_STAGE.get(kind, f"{kind} exports are not available."),
        )
    return found


# ---------------------------------------------------------------------------
# Requesting one (E189)
# ---------------------------------------------------------------------------


def request_export(run: CommandRun, *, spec: ExportSpec, site_id: int | None) -> Any:
    """E189 step 10: persist the immutable request and its durable job.

    The intent's ``request_key`` is derived from the **whole** command identity -
    principal and command id, which is what a command identity is (§4.1) - so a
    replay by the same person finds their own job instead of queueing a second
    one, and two different people cannot collide.

    Deriving it from the client-supplied ``command_id`` alone was a disclosure:
    ``command_id`` is chosen by the caller, so anyone reusing someone else's UUID
    was handed that person's job - its id, hash and download link - while their
    own export was never queued and their command still recorded success.
    """
    from core.kernel_models import OutboxIntent

    identity = f"export:{run.principal.key}:{run.spec.command_id}"
    request_key = uuid.uuid5(uuid.NAMESPACE_URL, identity)
    existing = OutboxIntent.objects.filter(
        tenant_id=run.tenant_id, request_key=request_key, actor_id=run.principal.human_id
    ).first()
    if existing is not None:
        return existing
    return run.outbox(
        "export",
        f"export:{request_key}",
        {
            "subject_key": spec.subject_key or f"export:{request_key}",
            "site_id": str(site_id) if site_id is not None else None,
            "export_spec": spec.as_payload(),
        },
        request_key=request_key,
    )


# ---------------------------------------------------------------------------
# Running one (the worker)
# ---------------------------------------------------------------------------


def requester_access(intent: Any) -> AccessContext:
    """The requester's authority **now**, not when they asked (design §4.4).

    No session: a job is not a browser request, so nothing here can satisfy a
    step-up demand. The step-up that authorised the request was checked then;
    what is re-checked here is scope and fields.
    """
    if intent.actor_id is None:
        raise Refusal("ACTION_DENIED", "An export is requested by a named person.")
    from accounts.models import User

    if not User.objects.filter(
        tenant_id=intent.tenant_id, human_id=intent.actor_id, is_active=True
    ).exists():
        raise Refusal("ACTION_DENIED", "The export requester is no longer active.")
    return AccessContext(
        user=None,
        human_id=intent.actor_id,
        tenant_id=intent.tenant_id,
        session=None,
        grants=effective_grants(intent.actor_id),
    )


def _current_export_access(
    intent: Any, spec: ExportSpec, produced: ExportFile | None = None
) -> AccessContext:
    """Re-authorise a queued export at the point it is about to publish bytes.

    Producing a large file can take longer than the requester's assignment. The
    producer's initial check is therefore insufficient: the actual output cells
    and protected fields must still be covered immediately before publication.
    """
    access = requester_access(intent)
    if EXPORT_GRANT not in access.all_actions():
        raise Refusal("ACTION_DENIED", "You may no longer run exports.")
    resolve_kind(spec.kind).check(access, spec)
    if produced is not None and not access.covers_all(
        {EXPORT_GRANT}, produced.cells, produced.contains_fields
    ):
        raise Refusal("ACTION_DENIED", "You may no longer access the exported rows.")
    return access


def run_export(intent: Any) -> JobOutcome:
    """Produce one requested export, or fail it visibly with a reason."""
    from core.kernel_models import JobArtifact

    # The file may already exist. A worker that dies between the `exports.produce`
    # commit and the job record leaves the artifact committed and lets its lease
    # lapse, so the next claim runs this again - and re-producing reads the rows as
    # they are *now*. Different bytes mean a different digest, a different
    # `exports.produce` fingerprint and COMMAND_CONFLICT on a job whose file is
    # already downloadable. Finish the job instead of re-earning it.
    done = JobArtifact.objects.filter(intent_id=intent.pk).first()
    if done is not None:
        return JobOutcome("confirmed", provider_ref=f"evidence:{done.evidence_id}")
    try:
        spec = parse_spec((intent.payload or {}).get("export_spec"))
        access = _current_export_access(intent, spec)
        kind = resolve_kind(spec.kind)
        produced = kind.produce(access, spec)
        _current_export_access(intent, spec, produced)
    except Refusal as refusal:
        _open_failure(intent, refusal.code)
        # A refusal refuses again: five more attempts would only delay the
        # exception a person already has to act on.
        return JobOutcome("failed", diagnostic=refusal.code, terminal=True)
    except Exception as exc:  # noqa: BLE001 - a job records its failure, never raises
        return _retryable_failure(intent, type(exc).__name__, str(exc))
    try:
        evidence = _store(intent, spec, produced)
    except OffboxError as exc:
        # Storage may simply be down: this one *is* worth retrying.
        return _retryable_failure(intent, "EVIDENCE_UNAVAILABLE", str(exc))
    except Refusal as refusal:
        if refusal.code == "ACTION_DENIED":
            _open_failure(intent, refusal.code)
            return JobOutcome("failed", diagnostic=refusal.code, terminal=True)
        # A command conflict or other transient kernel refusal can retry.
        return _retryable_failure(intent, refusal.code, refusal.message)
    return JobOutcome("confirmed", provider_ref=f"evidence:{evidence.pk}")


def _retryable_failure(intent: Any, code: str, detail: str) -> JobOutcome:
    """A failure that may not repeat - storage down, a lost race, a fault.

    It is retried, but the *last* attempt is a dead export someone has to act on,
    so that one registers with ticket 08's centre exactly as a refusal does.
    Without this, only refused exports ever reached the centre and a job that
    simply ran out of attempts failed silently (GSA-T18 acceptance: "failed
    export jobs are registered with the shared centre").
    """
    job = getattr(intent, "job", None)
    if job is not None and job.attempts >= MAX_ATTEMPTS:
        _open_failure(intent, code)
    return JobOutcome("failed", diagnostic=f"{code}: {detail}"[:200])


def _store(intent: Any, spec: ExportSpec, produced: ExportFile) -> EvidenceObject:
    """Write the bytes off-box, then record the evidence and artifact in one command."""
    digest = sha256_hex(produced.data)
    object_key = f"exports/{intent.tenant_id}/{intent.request_key}/{digest}"
    stored = get_store().put(object_key, produced.data)
    if stored.sha256 != digest or stored.size != len(produced.data):
        raise OffboxError("the stored export could not be confirmed")
    manifest_hash = sha256_hex(
        canonical_json(
            {
                "spec": spec.as_payload(),
                "sha256": digest,
                "row_count": produced.row_count,
                "completeness": produced.completeness,
                "cells": [list(cell) for cell in produced.cells],
                "contains_fields": list(produced.contains_fields),
                **produced.manifest,
            }
        ).encode()
    )
    holder: dict[str, EvidenceObject] = {}

    def handler(run: CommandRun) -> CommandResult:
        from core.kernel_models import JobArtifact

        # Recheck inside the artifact transaction too. A policy edit or revoked
        # assignment during off-box storage must not publish a cached file.
        _current_export_access(intent, spec, produced)
        existing = EvidenceObject.objects.filter(
            tenant_id=run.tenant_id, object_key=stored.key, object_version=stored.version
        ).first()
        if existing is not None:
            holder["evidence"] = existing
            return CommandResult(resource_type="evidence", resource_id=str(existing.pk))
        upload, _ = UploadIntent.objects.get_or_create(
            tenant_id=run.tenant_id,
            uploader_id=intent.actor_id,
            command_id=run.spec.command_id,
            defaults={
                "scope": _evidence_scope(produced),
                "kind": EXPORT_EVIDENCE_KIND,
                "expected_hash": digest,
                "expected_size": stored.size,
                "object_key": stored.key,
                "state": UploadIntent.State.CONFIRMED,
            },
        )
        evidence = EvidenceObject(
            upload_id=upload.pk,
            kind=EXPORT_EVIDENCE_KIND,
            filename=produced.filename,
            media_type=produced.media_type,
            size=stored.size,
            sha256=stored.sha256,
            object_key=stored.key,
            object_version=stored.version,
            retention_until=run.now + RETENTION,
            scope=_evidence_scope(produced),
            contains_fields=sorted(produced.contains_fields),
        )
        run.record(evidence)
        run.record(
            JobArtifact(
                intent_id=intent.pk,
                evidence_id=evidence.pk,
                source_watermark=(produced.source_watermark or "")[:200],
                manifest_hash=manifest_hash,
            )
        )
        holder["evidence"] = evidence
        return CommandResult(resource_type="evidence", resource_id=str(evidence.pk))

    execute_command(
        Principal(tenant_id=intent.tenant_id, service_code="export-worker"),
        CommandSpec(
            action="exports.produce",
            command_id=uuid.uuid5(intent.request_key, "produce"),
            business_input={"intent": str(intent.pk), "sha256": digest},
            subject_key=f"export:{intent.request_key}",
        ),
        handler,
    )
    # A replayed command returns its stored outcome **without running the handler**
    # (`core.commands.execute_command`), so the holder can be empty here: a lease
    # that expired, a second worker, or a crash between this commit and the job
    # record. Read the evidence back the way `files.goods_services` does rather
    # than failing a job whose file already exists.
    return holder.get("evidence") or EvidenceObject.objects.get(
        tenant_id=intent.tenant_id, object_key=stored.key, object_version=stored.version
    )


def _evidence_scope(produced: ExportFile) -> dict[str, Any]:
    """The file's own scope: the exact cells it was authorised under.

    ``ConfigScope``'s arrays cannot say "site A for brand X **and** site B for
    brand Y" - their cartesian product would claim A×Y too, which would refuse a
    download the requester is entitled to. Exports therefore record the pairs
    themselves; ``files.goods_services.scope_cells`` reads them.
    """
    return {
        "scope_kind": "cells",
        "cells": [[cell[0], cell[1]] for cell in produced.cells],
    }


def _open_failure(intent: Any, code: str) -> None:
    """Register the failure with ticket 08's shared centre, owned and dated."""
    from alerts.goods_services import open_exception

    payload = intent.payload or {}
    site_id = int(payload["site_id"]) if payload.get("site_id") else None

    def handler(run: CommandRun) -> CommandResult:
        open_exception(
            run,
            kind="export_failed",
            site_id=site_id,
            subject_key=f"export:{intent.request_key}",
            reason_code=code,
            source_event_key=uuid.uuid5(intent.request_key, f"export-failed:{code}"),
            note=f"The export job could not produce its file ({code}).",
        )
        return CommandResult(resource_type="export", resource_id=str(intent.pk))

    execute_command(
        Principal(tenant_id=intent.tenant_id, service_code="export-worker"),
        CommandSpec(
            action="exports.failed",
            command_id=uuid.uuid5(intent.request_key, f"failed:{code}"),
            business_input={"intent": str(intent.pk), "code": code},
            subject_key=f"export:{intent.request_key}",
            site_id=site_id,
        ),
        handler,
    )


# ---------------------------------------------------------------------------
# Reading one (E190)
# ---------------------------------------------------------------------------


def export_job_dto(intent: Any, *, access: AccessContext) -> dict[str, Any]:
    """``ExportJobDTO`` (design §6.1): progress, confirmed hash, or a typed failure.

    ``access`` is the caller E189/E190 answers, reauthorised here. Someone who
    no longer covers every cell and sensitive field of the produced file keeps
    their job and its progress but loses the hash and the link, so a grant revoked
    after the run cannot receive its hash or link on either endpoint.
    """
    from core.kernel_models import JobArtifact, JobState
    from files.goods_services import readable_by

    job = JobState.objects.filter(intent_id=intent.pk).first()
    artifact = JobArtifact.objects.select_related("evidence").filter(intent_id=intent.pk).first()
    spec = (intent.payload or {}).get("export_spec") or {}
    withheld = artifact is not None and not readable_by(access, artifact.evidence)
    # §6.1's ExportJobDTO is a closed list: id, state, progress, as_of, sha256,
    # download_url, error_code. Nothing else goes on the wire from here.
    state = job.state if job is not None else "pending"
    # A job whose delivery confirmed but whose artifact is missing is not "ready":
    # the download link is what `ready` means, and there is none.
    if state == "confirmed" and artifact is None:  # pragma: no cover - defensive
        state = "running"
    return {
        "id": str(intent.pk),
        "state": state,
        "progress": 100 if artifact is not None else (job.progress if job is not None else 0),
        "as_of": (artifact.source_watermark if artifact is not None else None) or spec.get("as_of"),
        "sha256": None if withheld else (artifact.evidence.sha256 if artifact else None),
        "download_url": (
            None
            if withheld or artifact is None
            else f"/api/goods-v1/files/{artifact.evidence_id}/download"
        ),
        "error_code": (
            "ACTION_DENIED" if withheld else (job.error_code if job is not None else None)
        ),
    }
