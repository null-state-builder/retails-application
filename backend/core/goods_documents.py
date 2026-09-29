"""Versioned goods documents: identity, draft revisions, official versions (design §4.3).

* ``DocumentIdentity`` is the document for life and receives one official
  number, once.
* A draft is a chain of immutable ``DraftRevision`` headers plus immutable
  ``DraftLine`` states; revision *n*'s lines are the newest state per line key
  created at or before *n*. Editing never rewrites an earlier revision.
* ``OfficialVersion`` + ``OfficialLine`` freeze exactly what was approved.
  Reissue appends a new version under the same identity and number.
* ``DocumentHead`` is the rebuildable pointer: current draft, live version,
  state and the revision a client must quote to change it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from django.db import connection

from core.canonical import content_hash
from core.commands import CommandRun, LockRank
from core.kernel_models import (
    DocumentEvent,
    DocumentHead,
    DocumentIdentity,
    DraftLine,
    DraftRevision,
    OfficialLine,
    OfficialVersion,
)
from core.refusals import Refusal

MAX_LINES = 50_000


def new_document(
    run: CommandRun, *, kind: str, purpose: str, entity_id: int, site_id: int | None
) -> tuple[DocumentIdentity, DocumentHead]:
    """A new document held at ``site_id``. Only a booking may start with none (GSA-T05)."""
    assert site_id is not None or purpose == DocumentIdentity.Purpose.BOOKING
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A document is made by a named person.")
    identity = DocumentIdentity.objects.create(
        tenant_id=run.tenant_id,
        kind=kind,
        purpose=purpose,
        entity_id=entity_id,
        site_id=site_id,
        maker_id=run.principal.human_id,
        command_key_id=run.key_id,
    )
    head = DocumentHead.objects.create(tenant_id=run.tenant_id, document=identity)
    return identity, head


def lock_heads(run: CommandRun, document_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, DocumentHead]:
    """Row-lock document heads at DOCUMENT rank and read them with their pointers.

    Only the head rows are locked (``FOR UPDATE OF``): the draft and live-version
    pointers are nullable, and PostgreSQL cannot lock the nullable side of an outer
    join. The pointed-to revisions and versions are immutable, so they need no lock.
    """
    heads = run.lock(
        LockRank.DOCUMENT,
        DocumentHead.objects.select_related("document", "draft_revision", "live_version").filter(
            document_id__in=list(document_ids)
        ),
        of=("self",),
    )
    return {head.document_id: head for head in heads}


def line_hash(line_no: int, payload: Mapping[str, Any]) -> str:
    return content_hash({"line_no": line_no, "payload": payload})


@dataclass
class LineState:
    line_key: uuid.UUID
    line_no: int
    payload: dict[str, Any]
    line_hash: str


def revision_lines(document_id: uuid.UUID, revision: int) -> list[LineState]:
    """Every live line of ``revision`` in line order."""
    rows = (
        DraftLine.objects.filter(document_id=document_id, created_in_revision__lte=revision)
        .order_by("line_key", "-created_in_revision")
        .distinct("line_key")
        .values("line_key", "line_no", "payload", "line_hash", "deleted")
    )
    states = [
        LineState(r["line_key"], r["line_no"], r["payload"], r["line_hash"])
        for r in rows
        if not r["deleted"]
    ]
    states.sort(key=lambda s: s.line_no)
    return states


def lines_digest(states: Iterable[LineState]) -> str:
    return content_hash([[s.line_no, str(s.line_key), s.line_hash] for s in states])


def append_revision(
    run: CommandRun,
    head: DocumentHead,
    *,
    header: Mapping[str, Any],
    line_changes: Mapping[uuid.UUID, Mapping[str, Any] | None] | None = None,
    replace_lines: list[tuple[uuid.UUID, Mapping[str, Any]]] | None = None,
    reviewed_rows: list[dict[str, Any]] | None = None,
) -> DraftRevision:
    """Record a new immutable draft revision and move the head to it."""
    previous = head.draft_revision
    number = (previous.revision + 1) if previous is not None else 1
    current: dict[uuid.UUID, LineState] = (
        {s.line_key: s for s in revision_lines(head.document_id, previous.revision)}
        if previous
        else {}
    )
    new_rows: list[DraftLine] = []
    if replace_lines is not None:
        wanted = {key for key, _ in replace_lines}
        for key in current:
            if key not in wanted:
                new_rows.append(
                    _line_row(head, key, current[key].line_no, {}, number, deleted=True)
                )
        for index, (key, payload) in enumerate(replace_lines, start=1):
            state = current.get(key)
            digest = line_hash(index, payload)
            if state is None or state.line_hash != digest:
                new_rows.append(_line_row(head, key, index, dict(payload), number))
    for key, change in (line_changes or {}).items():
        state = current.get(key)
        if change is None:
            if state is not None:
                new_rows.append(_line_row(head, key, state.line_no, {}, number, deleted=True))
            continue
        line_no = (
            state.line_no
            if state is not None
            else (max((s.line_no for s in current.values()), default=0) + 1)
        )
        new_rows.append(_line_row(head, key, line_no, dict(change), number))
        current[key] = LineState(key, line_no, dict(change), line_hash(line_no, change))
    effective = _effective_after(current, new_rows)
    if len(effective) > MAX_LINES:
        raise Refusal("INVALID_REQUEST", f"A document can hold at most {MAX_LINES} lines.")
    digest = lines_digest(effective)
    revision = DraftRevision(
        document_id=head.document_id,
        revision=number,
        parent_id=previous.pk if previous is not None else None,
        payload=dict(header),
        lines_hash=digest,
        content_hash=content_hash({"header": header, "lines": digest}),
        reviewed_rows=reviewed_rows or [],
    )
    run.record(revision)
    for row in new_rows:
        run.record(row)
    head.draft_revision = revision
    head.state = DocumentHead.State.DRAFT
    head.revision += 1
    head.save(update_fields=["draft_revision", "state", "revision", "updated_at"])
    return revision


def _line_row(
    head: DocumentHead,
    key: uuid.UUID,
    line_no: int,
    payload: dict[str, Any],
    revision: int,
    *,
    deleted: bool = False,
) -> DraftLine:
    return DraftLine(
        document_id=head.document_id,
        line_key=key,
        created_in_revision=revision,
        line_no=line_no,
        deleted=deleted,
        payload=payload,
        line_hash=line_hash(line_no, payload)
        if not deleted
        else content_hash({"deleted": str(key)}),
    )


def _effective_after(
    current: dict[uuid.UUID, LineState], new_rows: list[DraftLine]
) -> list[LineState]:
    states = dict(current)
    for row in new_rows:
        if row.deleted:
            states.pop(row.line_key, None)
        else:
            states[row.line_key] = LineState(row.line_key, row.line_no, row.payload, row.line_hash)
    return sorted(states.values(), key=lambda s: s.line_no)


def pending_lines(run: CommandRun, head: DocumentHead) -> list[LineState]:
    """Lines of the head's draft revision, including rows recorded in this command."""
    revision = head.draft_revision
    if revision is None:
        return []
    persisted = {s.line_key: s for s in revision_lines(head.document_id, revision.revision)}
    for row in run.evidence.pending:
        if (
            isinstance(row, DraftLine)
            and row.document_id == head.document_id
            and row.created_in_revision == revision.revision
        ):
            if row.deleted:
                persisted.pop(row.line_key, None)
            else:
                persisted[row.line_key] = LineState(
                    row.line_key, row.line_no, row.payload, row.line_hash
                )
    return sorted(persisted.values(), key=lambda s: s.line_no)


def record_event(
    run: CommandRun,
    document_id: uuid.UUID,
    kind: str,
    *,
    payload: Mapping[str, Any] | None = None,
    version_id: uuid.UUID | None = None,
    revision_id: uuid.UUID | None = None,
    reason_code: str | None = None,
    evidence_id: uuid.UUID | None = None,
) -> DocumentEvent:
    event = DocumentEvent(
        document_id=document_id,
        version_id=version_id,
        revision_id=revision_id,
        event_kind=kind,
        reason_code=reason_code,
        evidence_id=evidence_id,
        payload=dict(payload or {"details": []}),
    )
    run.record(event)
    return event


def officialise(
    run: CommandRun,
    head: DocumentHead,
    *,
    approved_by_id: uuid.UUID,
    canonical_header: Mapping[str, Any],
    lines: list[tuple[uuid.UUID, Mapping[str, Any]]],
    authority: Mapping[str, Any],
    reconciliation: Mapping[str, Any] | None = None,
    profile_version_id: uuid.UUID | None = None,
    number: str | None = None,
) -> tuple[OfficialVersion, list[OfficialLine]]:
    """Freeze an official version of ``head``'s current draft revision."""
    if head.draft_revision is None:
        raise Refusal("STATE_CONFLICT", "There is no draft to make official.")
    if len(lines) > MAX_LINES:
        raise Refusal("INVALID_REQUEST", f"An official document holds at most {MAX_LINES} lines.")
    last = (
        OfficialVersion.objects.filter(document_id=head.document_id)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
    )
    version_no = int(last or 0) + 1
    body_hash = content_hash(
        {"header": canonical_header, "lines": [[str(key), payload] for key, payload in lines]}
    )
    version = OfficialVersion(
        document_id=head.document_id,
        version=version_no,
        draft_revision_id=head.draft_revision.pk,
        profile_version_id=profile_version_id,
        approved_by_id=approved_by_id,
        canonical_payload=dict(canonical_header),
        content_hash=body_hash,
        reconciliation=dict(reconciliation) if reconciliation is not None else None,
        authority_snapshot=dict(authority),
        line_count=len(lines),
    )
    run.record(version)
    official_lines = [
        OfficialLine(
            version_id=version.pk, line_no=index, stable_line_key=key, payload=dict(payload)
        )
        for index, (key, payload) in enumerate(lines, start=1)
    ]
    for line in official_lines:
        run.record(line)
    identity = head.document
    if number is not None:
        if identity.official_number is not None and identity.official_number != number:
            raise Refusal("STATE_CONFLICT", "This document already carries a different number.")
        if identity.official_number is None:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE core_documentidentity SET official_number = %s "
                    "WHERE id = %s AND official_number IS NULL",
                    [number, identity.pk],
                )
            identity.official_number = number
    head.live_version = version
    head.state = DocumentHead.State.OFFICIAL
    head.revision += 1
    head.save(update_fields=["live_version", "state", "revision", "updated_at"])
    record_event(
        run,
        head.document_id,
        "officialised",
        version_id=version.pk,
        revision_id=head.draft_revision.pk,
        payload={"to_state": "official", "official_version_id": str(version.pk), "details": []},
    )
    return version, official_lines


def set_state(
    run: CommandRun, head: DocumentHead, state: str, *, event: str, reason_code: str | None = None
) -> None:
    from_state = head.state
    head.state = state
    head.revision += 1
    head.save(update_fields=["state", "revision", "updated_at"])
    record_event(
        run,
        head.document_id,
        event,
        reason_code=reason_code,
        revision_id=head.draft_revision.pk if head.draft_revision else None,
        payload={"from_state": from_state, "to_state": state, "details": []},
    )
