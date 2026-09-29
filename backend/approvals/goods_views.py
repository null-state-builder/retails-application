"""Goods-v1 approval inbox, list and decision endpoints (E169, E170, E234).

These are the three operations Ticket 08A left partial. The inbox and list used
to share their paths with the legacy approvals views through the old contract
dispatcher, and ``approvals/{id}/decide`` collapsed onto the same OpenAPI path
template as its legacy ``int`` sibling, so drf-spectacular described the legacy
operation in each case and never these (#303). They now answer under
``/api/goods-v1/approvals...`` alone, and the schemas below are what they emit.
``approval_dto`` builds the payload in ``approvals.goods_services``; ``APPROVAL_DATA``
is that shape, stated here because the view hand-builds its response rather than
running it through a serializer.
"""

from __future__ import annotations

import uuid
from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    page,
    paginate,
    parse_meta,
    resource_dto,
)
from approvals.goods_models import ApprovalRequest
from approvals.goods_services import (
    approval_dto,
    decide,
    parent_documents,
    subject_cells,
    subject_cells_many,
)
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal

# ---------------------------------------------------------------------------
# Documented responses (ticket 08B)
#
# The envelopes are repeated rather than imported so this module documents its
# own contract, as ``masters/goods_views.py`` and ``vendors/goods_views.py`` do.
# ---------------------------------------------------------------------------

REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}

#: What ``approvals.goods_services.approval_dto`` answers. ``reconciliation`` and
#: its ``value_paise`` total appear only for a caller whose grant covers the
#: amount fields; ``parent_document`` is the stable document reference GSA-T04
#: requires, so a checker opens the exact revision rather than matching by title.
APPROVAL_DATA: dict[str, Any] = {
    "type": "object",
    "description": "ApprovalDTO (E169/E170/E234).",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "subject_id": {"type": "string", "nullable": True},
        "subject_kind": {"type": "string"},
        "subject_revision": {"type": "integer"},
        "parent_document": {
            "type": "object",
            "nullable": True,
            "description": "The subject's stable parent document (GSA-T04).",
            "properties": {
                "id": {"type": "string"},
                "kind": {"type": "string"},
                "number": {"type": "string", "nullable": True},
                "revision": {"type": "integer", "nullable": True},
            },
        },
        "reviewed_hash": {"type": "string", "nullable": True},
        "state": {"type": "string", "enum": ["pending", "approved", "rejected", "withdrawn"]},
        "maker": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
        },
        "required_roles": {"type": "array", "items": {"type": "string"}},
        "requested_action": {"type": "string"},
        "policy_version_id": {"type": "string", "nullable": True},
        "site_id": {"type": "string", "nullable": True},
        "brand_id": {"type": "string", "nullable": True},
        "title": {"type": "string"},
        "requested_at": {"type": "string", "format": "date-time", "nullable": True},
        "decision_at": {"type": "string", "format": "date-time", "nullable": True},
        "reason_code": {"type": "string", "nullable": True},
        "reconciliation": {"type": "object", "nullable": True, "additionalProperties": True},
    },
}

APPROVAL_PAGE: dict[str, Any] = {
    "type": "object",
    "description": "Page<ApprovalDTO>, newest-first for the list and oldest-first for the inbox.",
    "properties": {
        "items": {"type": "array", "items": APPROVAL_DATA},
        "next_cursor": {"type": "string", "nullable": True},
        "as_of": {"type": "string", "format": "date-time"},
    },
}

APPROVAL_RESOURCE: dict[str, Any] = {
    "type": "object",
    "description": "ResourceDTO<ApprovalDTO> for the decided request.",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer"},
        "content_hash": {"type": "string"},
        "state": {"type": "string"},
        "number": {"type": "string", "nullable": True},
        "version": {"type": "integer", "nullable": True},
        "context": {"type": "object", "additionalProperties": True},
        "allowed_actions": {"type": "array", "items": {"type": "string"}},
        "data": APPROVAL_DATA,
    },
}

_READ_REFUSALS = (400, 401, 403, 404)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


def _filtered(
    access: Any, rows: list[ApprovalRequest], params: dict[str, str]
) -> list[ApprovalRequest]:
    site = params.get("site_id")
    brand = params.get("brand_id")
    if site:
        rows = [r for r in rows if str(r.site_id) == site]
    if brand:
        rows = [r for r in rows if str(r.brand_id) == brand]
    return rows


def _dto(
    access: Any,
    request: ApprovalRequest,
    cells: frozenset[Any],
    parents: dict[uuid.UUID, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # Amounts come only from a grant that also lets this person see or decide the approval.
    actions = {request.requested_action, "approvals.view"}
    show = any(access.covers_all(actions, cells, {field}) for field in ("cost", "layer_value"))
    return approval_dto(request, show_amounts=show, parents=parents)


class GoodsApprovalInboxView(GoodsAPIView):
    """E169: pending approvals this person may decide, never their own."""

    @extend_schema(responses=_responses(200, APPROVAL_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        rows = list(
            ApprovalRequest.objects.select_related("maker")
            .filter(tenant_id=access.tenant_id, state=ApprovalRequest.State.PENDING)
            .order_by("created_at", "id")
        )
        cells = subject_cells_many(rows)
        visible = [
            r
            for r in rows
            if not (r.require_distinct and r.maker_id == access.human_id)
            and access.covers_all({r.requested_action}, cells[r.pk])
        ]
        window, cursor = paginate(_filtered(access, visible, params), params)
        parents = parent_documents(window)
        return Response(page([_dto(access, r, cells[r.pk], parents) for r in window], cursor))


class GoodsApprovalListView(GoodsAPIView):
    """E170: approvals the person made, can decide, or may view in scope."""

    @extend_schema(responses=_responses(200, APPROVAL_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        rows = list(
            ApprovalRequest.objects.select_related("maker")
            .filter(tenant_id=access.tenant_id)
            .order_by("-created_at", "id")
        )
        cells = subject_cells_many(rows)
        visible = [
            r
            for r in rows
            if r.maker_id == access.human_id
            or access.covers_all({r.requested_action, "approvals.view"}, cells[r.pk])
        ]
        window, cursor = paginate(_filtered(access, visible, params), params)
        parents = parent_documents(window)
        return Response(page([_dto(access, r, cells[r.pk], parents) for r in window], cursor))


class GoodsApprovalDecideView(GoodsAPIView):
    """E234: approve or reject one exact reviewed revision, with its domain effects."""

    @extend_schema(
        request={
            "application/json": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "command_id", "contract_version", "expected_revision", "decision", "reviewed_hash",
            ],
            "properties": {
                "command_id": {"type": "string", "format": "uuid"},
                "contract_version": {"type": "string", "enum": ["goods-v1"]},
                "expected_revision": {"type": "integer", "minimum": 1},
                "decision": {"type": "string", "enum": ["approve", "reject"]},
                "reviewed_hash": {"type": "string"},
                "reason_code": {"type": "string"},
            },
            },
        },
        responses=_responses(200, APPROVAL_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"decision", "reviewed_hash", "reason_code"},
            required=["decision", "reviewed_hash"],
        )
        if body["decision"] not in ("approve", "reject"):
            raise Refusal("INVALID_REQUEST", "decision must be approve or reject.")
        target = ApprovalRequest.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
        covered = target is not None and access.covers_all(
            {target.requested_action}, subject_cells(target)
        )
        if target is None or not (covered or target.maker_id == access.human_id):
            raise Refusal("NOT_FOUND", "That approval was not found.")
        if not covered:
            raise Refusal("ACTION_DENIED", "You do not have authority to decide this approval.")
        if body["decision"] == "approve":
            access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            current = (
                ApprovalRequest.objects.filter(pk=pk).values_list("revision", flat=True).first()
            )
            if current is not None and meta.expected_revision != current:
                raise Refusal("REVISION_SUPERSEDED", "The approval request changed; reload it.")
            decided, _result = decide(
                run,
                access=access,
                request_id=pk,
                decision=str(body["decision"]),
                reviewed_hash=str(body["reviewed_hash"]),
                reason_code=body.get("reason_code"),
            )
            return CommandResult(resource_type="approval", resource_id=str(decided.pk))

        result = self.run_command(
            request,
            access=access,
            action=f"approvals.decide.{target.requested_action}",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=target.subject_key,
            site_id=target.site_id,
            reviewed_hash=str(body["reviewed_hash"]),
        )
        decided = ApprovalRequest.objects.select_related("maker").get(pk=pk)
        dto = _dto(access, decided, subject_cells(decided))
        return Response(
            resource_dto(
                id=decided.pk,
                data=dto,
                revision=decided.revision,
                state=decided.state,
                context={"site_id": decided.site_id, "brand_id": decided.brand_id},
            ),
            status=result.status_code,
        )
