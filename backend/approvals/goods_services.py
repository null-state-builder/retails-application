"""Goods-v1 approval requests and the one decision command (design §3.1, E169/E170/E234).

``approvals`` never imports a domain app. A domain registers, from the project
composition layer, a handler for each (subject kind, requested action). Deciding
a request runs that handler inside the same command, so the decision and its
effects commit together or not at all; a domain refusal leaves the request
pending and the refused attempt in the command attempt log.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from approvals.goods_models import ApprovalDecision, ApprovalRequest
from approvals.goods_policy import PolicyPin, enforce
from core.commands import CommandRun, LockRank
from core.kernel_models import DocumentHead
from core.refusals import Refusal


@dataclass(frozen=True)
class DecisionContext:
    request: ApprovalRequest
    decision: str  # approve | reject
    reason_code: str | None
    checker_id: uuid.UUID
    access: Any
    #: Locks and rechecks the pinned approval policy; a policy-governed handler calls it
    #: after locking its subject documents and before touching custody.
    enforce_policy: Callable[[CommandRun], None] = field(default=lambda run: None)


SubjectHandler = Callable[[CommandRun, DecisionContext], dict[str, Any] | None]
_HANDLERS: dict[tuple[str, str], SubjectHandler] = {}
#: Requested actions whose approver comes from a fixed rule, not an approval policy:
#: configuration approval (distinct C-OWN, design E088) and master-proposal confirmation.
_FIXED_RULE: set[tuple[str, str]] = set()


def register_subject_handler(
    subject_kind: str,
    requested_action: str,
    handler: SubjectHandler,
    *,
    policy_governed: bool = True,
) -> None:
    _HANDLERS[(subject_kind, requested_action)] = handler
    if policy_governed:
        _FIXED_RULE.discard((subject_kind, requested_action))
    else:
        _FIXED_RULE.add((subject_kind, requested_action))


def policy_governed(subject_kind: str, requested_action: str) -> bool:
    return (subject_kind, requested_action) not in _FIXED_RULE


def registered_subjects() -> list[tuple[str, str]]:
    return sorted(_HANDLERS)


Cell = tuple[int | None, int | None]
#: Resolves the complete scope of many requests of one subject kind at once, by request pk.
SubjectCells = Callable[[list[ApprovalRequest]], dict[Any, frozenset[Cell]]]
_CELLS: dict[tuple[str, str], SubjectCells] = {}


def register_subject_cells(subject_kind: str, requested_action: str, cells: SubjectCells) -> None:
    """How a subject kind names its complete (site, brand) scope, when one cell is not all of it."""
    _CELLS[(subject_kind, requested_action)] = cells


def subject_cells_many(requests: Iterable[ApprovalRequest]) -> dict[Any, frozenset[Cell]]:
    """Every (site, brand) each request's subject holds, by request pk; by default its own
    site and brand. Resolved per subject kind in bulk, so a long inbox stays a few queries.

    Inbox, list and decision visibility use the same subset rule as the subject's own
    reads (fix ticket 04): a multi-brand PT is shown only to someone covering every brand.
    """
    groups: dict[tuple[str, str], list[ApprovalRequest]] = defaultdict(list)
    for request in requests:
        groups[(request.subject_kind, request.requested_action)].append(request)
    out: dict[Any, frozenset[Cell]] = {}
    for key, group in groups.items():
        resolver = _CELLS.get(key)
        found = resolver(group) if resolver is not None else {}
        for request in group:
            out[request.pk] = found.get(request.pk) or frozenset(
                {(request.site_id, request.brand_id)}
            )
    return out


def subject_cells(request: ApprovalRequest) -> frozenset[Cell]:
    return subject_cells_many([request])[request.pk]


def supersede_pending(
    run: CommandRun, subject_kind: str, subject_key: str, requested_action: str | None = None
) -> int:
    queryset = ApprovalRequest.objects.filter(
        tenant_id=run.tenant_id, subject_kind=subject_kind, subject_key=subject_key, state="pending"
    )
    if requested_action is not None:
        queryset = queryset.filter(requested_action=requested_action)
    count = 0
    for request in run.lock(LockRank.DOCUMENT, queryset):
        request.state = ApprovalRequest.State.SUPERSEDED
        request.decided_at = run.now
        request.save(update_fields=["state", "decided_at"])
        run.record(
            ApprovalDecision(
                request_id=request.pk,
                checker_id=None,
                outcome=ApprovalDecision.Outcome.SUPERSEDED,
                reviewed_hash=request.reviewed_hash,
                reason_code="INPUT_CHANGED",
            )
        )
        count += 1
    return count


def create_request(
    run: CommandRun,
    *,
    subject_kind: str,
    subject_key: str,
    revision: int,
    reviewed_hash: str,
    requested_action: str,
    site_id: int | None,
    brand_id: int | None = None,
    scope: dict[str, Any] | None = None,
    reconciliation: dict[str, Any] | None = None,
    require_distinct: bool = True,
    required_roles: list[str] | None = None,
    title: str = "",
    policy_version_id: uuid.UUID | None = None,
    new_subject: bool = False,
    policy: PolicyPin | None = None,
) -> ApprovalRequest:
    """Open one pending request, superseding any earlier one for the same subject.

    A policy-governed request carries the ``policy`` its submission pinned: its version,
    approver roles and two-person rule replace any caller default, and its basis (amounts,
    scope, limits) is kept for the decision to recheck.

    ``new_subject`` says the subject was created in this very command, so no earlier
    request can exist and the DOCUMENT-rank supersede lock is not taken - a caller
    already holding a higher-rank guard (a config-version counter) may still open it.
    """
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "An approval is requested by a named person.")
    if (subject_kind, requested_action) not in _HANDLERS:
        raise Refusal(
            "INVALID_REQUEST",
            f"Nothing is registered to approve {subject_kind}/{requested_action}.",
        )
    if policy_governed(subject_kind, requested_action):
        if policy is None:
            raise Refusal(
                "APPROVAL_POLICY_BLOCKED",
                f"{requested_action} needs an effective approval policy.",
                status=422,
            )
        policy_version_id = policy.version_id
        required_roles = policy.roles
        require_distinct = policy.require_distinct or require_distinct
    if not new_subject:
        supersede_pending(run, subject_kind, subject_key, requested_action)
    return ApprovalRequest.objects.create(
        tenant_id=run.tenant_id,
        subject_kind=subject_kind,
        subject_key=subject_key[:100],
        revision=revision,
        reviewed_hash=reviewed_hash,
        maker_id=run.principal.human_id,
        policy_version_id=policy_version_id,
        requested_action=requested_action,
        required_roles=required_roles or [],
        require_distinct=require_distinct,
        policy_basis=policy.basis if policy is not None else {},
        site_id=site_id,
        brand_id=brand_id,
        scope=scope or {"scope_kind": "sites", "site_ids": [str(site_id)] if site_id else []},
        reconciliation=reconciliation,
        command_key_id=run.key_id,
        title=title[:240],
    )


def decide(
    run: CommandRun,
    *,
    access: Any,
    request_id: uuid.UUID,
    decision: str,
    reviewed_hash: str,
    reason_code: str | None,
) -> tuple[ApprovalRequest, dict[str, Any] | None]:
    if decision not in ("approve", "reject"):
        raise Refusal("INVALID_REQUEST", "decision must be approve or reject.")
    # The subject's site guard ranks below documents in the fixed lock order, so
    # it is taken first: domain handlers check capability and count freezes under it.
    site_id = (
        ApprovalRequest.objects.filter(pk=request_id).values_list("site_id", flat=True).first()
    )
    if site_id is not None:
        from masters.goods_models import SiteGuard

        run.lock(LockRank.SITE, SiteGuard.objects.filter(site_id=site_id))
    locked = run.lock(LockRank.DOCUMENT, ApprovalRequest.objects.filter(pk=request_id))
    if not locked:
        raise Refusal("NOT_FOUND", "That approval was not found.")
    request = locked[0]
    if request.state != ApprovalRequest.State.PENDING:
        raise Refusal("STATE_CONFLICT", "This approval has already been decided or replaced.")
    if reviewed_hash != request.reviewed_hash:
        raise Refusal(
            "REVISION_SUPERSEDED", "What you reviewed is no longer what is waiting for approval."
        )
    checker = run.principal.human_id
    if checker is None:
        raise Refusal("SELF_APPROVAL", "A service identity can never approve.")
    if request.require_distinct and checker == request.maker_id:
        raise Refusal("SELF_APPROVAL", "The person who prepared this cannot also approve it.")
    if decision == "reject" and not reason_code:
        raise Refusal("INVALID_REQUEST", "A rejection needs a reason.")
    handler = _HANDLERS.get((request.subject_kind, request.requested_action))
    if handler is None:
        raise Refusal("STATE_CONFLICT", "Nothing is registered to decide this approval.")
    governed = policy_governed(request.subject_kind, request.requested_action)
    enforced: list[bool] = []

    def enforce_policy(inner: CommandRun) -> None:
        if governed and not enforced:
            enforce(inner, request, decision=decision, checker_id=checker, access=access)
            enforced.append(True)

    context = DecisionContext(request, decision, reason_code, checker, access, enforce_policy)
    try:
        result = handler(run, context)
        if governed and not enforced:
            raise RuntimeError(
                f"The {request.requested_action} handler decided without checking its policy."
            )
    except Refusal as refusal:
        if refusal.code in {
            "STEP_UP_REQUIRED",
            "SELF_APPROVAL",
            "ACTION_DENIED",
            "NOT_FOUND",
            "REVISION_SUPERSEDED",
            "APPROVAL_STALE",
        }:
            raise
        # E234: every other domain refusal is APPROVAL_REFUSED (422) with its typed code.
        raise Refusal(
            "APPROVAL_REFUSED",
            refusal.message,
            status=422,
            issues=refusal.issues,
            domain_code=refusal.code,
        ) from refusal
    request.state = (
        ApprovalRequest.State.APPROVED if decision == "approve" else ApprovalRequest.State.REJECTED
    )
    request.decided_at = run.now
    request.save(update_fields=["state", "decided_at"])
    run.record(
        ApprovalDecision(
            request_id=request.pk,
            checker_id=checker,
            outcome=ApprovalDecision.Outcome.APPROVED
            if decision == "approve"
            else ApprovalDecision.Outcome.REJECTED,
            reviewed_hash=reviewed_hash,
            reason_code=reason_code,
            result=result,
        )
    )
    run.audit_subject_key = request.subject_key
    run.audit_site_id = request.site_id
    return request, result


def _document_key(request: ApprovalRequest) -> uuid.UUID | None:
    if request.subject_kind != ApprovalRequest.SubjectKind.DOCUMENT:
        return None
    try:
        return uuid.UUID(request.subject_key)
    except (ValueError, AttributeError, TypeError):
        return None


def parent_documents(rows: Iterable[ApprovalRequest]) -> dict[uuid.UUID, dict[str, Any]]:
    """The stable document behind each approval: id, kind, number and revision.

    GSA-T04: an approval read carries an exact reference, so a screen opens the
    submitted document by its own id and revision instead of matching a title or
    guessing from a second read. Subjects that are not documents (a configuration
    version, a master proposal) have no parent document and are simply absent.
    Read for a whole page at once so a list costs one query, not one per row.
    """
    wanted: dict[uuid.UUID, uuid.UUID] = {}
    for row in rows:
        key = _document_key(row)
        if key is not None:
            wanted[key] = row.tenant_id
    if not wanted:
        return {}
    return {
        head.document_id: {
            "id": str(head.document_id),
            "kind": head.document.kind,
            "purpose": head.document.purpose,
            "number": head.document.official_number,
            "revision": head.revision,
        }
        for head in DocumentHead.objects.select_related("document").filter(
            document_id__in=sorted(wanted)
        )
        if head.tenant_id == wanted[head.document_id]
    }


def approval_dto(
    request: ApprovalRequest,
    *,
    show_amounts: bool,
    parents: dict[uuid.UUID, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reconciliation = request.reconciliation
    if reconciliation is not None and not show_amounts:
        reconciliation = {
            **reconciliation,
            "totals": {
                k: v for k, v in (reconciliation.get("totals") or {}).items() if k != "value_paise"
            },
        }
    last = request.decisions.order_by("-recorded_at").first() if request.pk else None
    document_key = _document_key(request)
    lookup = parents if parents is not None else parent_documents([request])
    return {
        "id": str(request.pk),
        "subject_id": request.subject_key,
        "subject_kind": request.subject_kind,
        # The exact revision this request was submitted against, and the stable
        # document it belongs to (GSA-T04). A checker's screen opens *that*
        # revision; it never reconstructs one from a later read of the subject.
        "subject_revision": request.revision,
        "parent_document": lookup.get(document_key) if document_key is not None else None,
        "reviewed_hash": request.reviewed_hash,
        "state": request.state,
        "maker": {"id": str(request.maker_id), "name": getattr(request.maker, "display_name", "")},
        "required_roles": list(request.required_roles or []),
        "requested_action": request.requested_action,
        "policy_version_id": str(request.policy_version_id) if request.policy_version_id else None,
        "site_id": str(request.site_id) if request.site_id else None,
        "brand_id": str(request.brand_id) if request.brand_id else None,
        "title": request.title,
        "requested_at": request.created_at.isoformat()
        if request.created_at and not hasattr(request.created_at, "resolve_expression")
        else None,
        "decision_at": request.decided_at.isoformat() if request.decided_at else None,
        "reason_code": last.reason_code if last is not None else None,
        "reconciliation": reconciliation,
    }
