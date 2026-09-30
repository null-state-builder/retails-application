"""C08 scoped full-store SOH counts: blind maker, protected independent review."""

from __future__ import annotations

import uuid
from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, parse_meta, parse_uuid
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.goods_history import document_history
from core.refusals import Refusal
from masters.goods_models import SiteGuard
from outbound import goods_soh_reconciliation as services
from outbound.goods_soh_models import SohReconciliation
from ptmapper.soh_models import SohImport

META: dict[str, Any] = {"command_id": {"type": "string", "format": "uuid"}, "contract_version": {"type": "string", "enum": ["goods-v1"]}, "expected_revision": {"type": "integer", "minimum": 1}}
START: dict[str, Any] = {"type": "object", "additionalProperties": False,
    "required": [*META, "source_import_id", "full_store_export", "whole_store_physically_counted", "omissions_are_zero", "reason_code"],
    "properties": {**META, "source_import_id": {"type": "string", "format": "uuid"},
        **{key: {"type": "boolean", "enum": [True]} for key in ("full_store_export", "whole_store_physically_counted", "omissions_are_zero")},
        "reason_code": {"type": "string", "minLength": 1, "maxLength": 60}}}
MUTATE: dict[str, Any] = {"type": "object", "additionalProperties": False, "required": [*META],
    "properties": {**META, "reviewed_hash": {"type": "string", "minLength": 64, "maxLength": 64}, "reason": {"type": "string", "maxLength": 500}}}
SCHEMA: dict[str, Any] = {"type": "object", "required": ["id", "record_contract", "revision", "state", "content_hash", "allowed_actions", "data"],
    "properties": {"id": {"type": "string", "format": "uuid"}, "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer"}, "state": {"type": "string", "enum": ["frozen", "submitted", "closed", "cancelled"]},
        "content_hash": {"type": "string"}, "allowed_actions": {"type": "array", "items": {"type": "string"}},
        "data": {"type": "object", "properties": {
            "source_import_id": {"type": "string", "format": "uuid"}, "site_id": {"type": "string"},
            "cutoff_at": {"type": "string", "format": "date-time"}, "frozen_at": {"type": "string", "format": "date-time"},
            "number": {"type": "string", "nullable": True}, "freeze_active": {"type": "boolean"},
            "approval_request_id": {"type": "string", "format": "uuid", "nullable": True},
            "journal_batch_id": {"type": "string", "format": "uuid", "nullable": True},
            "pending_owned_differences": {"type": "boolean"}, "review": {"type": "object", "additionalProperties": True, "nullable": True},
            "field_access": {"type": "object", "properties": {key: {"type": "array", "items": {"type": "string"}} for key in ("readable_fields", "writable_fields")}},
            "history": {"type": "object", "additionalProperties": True}}}}}


def may_read(access: AccessContext, site_id: int) -> bool:
    return any(access.covers_all({action}, {(site_id, None)}) for action in (services.RUN, services.REVIEW))


def load(access: AccessContext, pk: uuid.UUID) -> SohReconciliation:
    row = SohReconciliation.objects.filter(tenant_id=access.tenant_id, pk=pk).select_related("document", "source_import", "site__gstin__legal_entity").first()
    if row is None or not may_read(access, row.site_id):
        raise Refusal("NOT_FOUND", "That full-store count was not found.")
    return row


def dto(access: AccessContext, row: SohReconciliation, history_cursor: str | None = None) -> dict[str, Any]:
    source = row.source_import
    participants = {str(row.maker_id), str(source.uploaded_by_id), str(source.prepared_by_id),
                    *(str(person) for person in source.rows.values_list("verification__observer_id", flat=True) if person)}
    reviewer = str(access.human_id) not in participants and access.covers_all({services.REVIEW}, {(row.site_id, None)}, services.FIELDS)
    maker = access.human_id == row.maker_id and access.covers_all({services.RUN}, {(row.site_id, None)})
    active = row.state in {"frozen", "submitted"}
    allowed = []
    if active and (maker or access.covers_all({services.REVIEW}, {(row.site_id, None)})):
        allowed.append("cancel")
    if row.state == "frozen" and maker and not row.payload["problems"]:
        allowed.append("submit")
    if row.state == "submitted" and reviewer:
        allowed.append("approve")
    history = document_history(row.document_id, cursor=history_cursor).as_dto()
    # Generic document events contain no frozen book/values. Actor names are
    # projected by the ordinary history contract; restricted fields are not
    # embedded in event details by this writer.
    return {"id": str(row.pk), "record_contract": "goods-v1", "revision": row.revision, "state": row.state,
            "content_hash": row.content_hash, "allowed_actions": allowed, "data": {
                "source_import_id": str(source.pk), "site_id": str(row.site_id), "cutoff_at": row.payload["cutoff_at"],
                "frozen_at": row.frozen_at.isoformat(), "number": row.document.official_number,
                "freeze_active": active and SiteGuard.objects.filter(site_id=row.site_id, freeze_id=row.pk).exists(),
                "approval_request_id": str(row.approval_request_id) if row.approval_request_id else None,
                "journal_batch_id": str(row.journal_batch_id) if row.journal_batch_id else None,
                "pending_owned_differences": bool(row.payload["problems"]),
                "field_access": {"readable_fields": list(services.FIELDS) if reviewer else [], "writable_fields": []},
                "review": row.payload if reviewer else None, "history": history}}


class SohReconciliationStartView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_soh_reconciliation_start", request={"application/json": START}, responses={201: SCHEMA})
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"source_import_id", "full_store_export", "whole_store_physically_counted", "omissions_are_zero", "reason_code"})
        source_id = parse_uuid(body.pop("source_import_id", None), "source_import_id")
        source = SohImport.objects.filter(tenant_id=access.tenant_id, pk=source_id).select_related("site__gstin__legal_entity").first()
        if source is None or not access.covers_all({services.RUN}, {(source.site_id, None)}):
            raise Refusal("NOT_FOUND", "That full-store source was not found.")
        def handler(run: CommandRun) -> CommandResult:
            row = services.begin(run, access, source, body, meta.expected_revision)
            return CommandResult(resource_type="soh_reconciliation", resource_id=str(row.pk), status_code=201)
        result = self.run_command(request, access=access, action=services.RUN, meta=meta,
            business_input={"source_import_id": str(source_id), **body}, handler=handler, site_id=source.site_id,
            resource_ids=[str(source.pk)], reviewed_hash=source.reviewed_hash)
        response = dto(access, load(access, uuid.UUID(str(result.resource_id))))
        access.revalidate_delivery()
        return Response(response, status=201)


class SohReconciliationDetailView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_soh_reconciliation_detail", responses={200: SCHEMA})
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, {"history_cursor"})
        response = dto(access, load(access, pk), params.get("history_cursor"))
        access.revalidate_delivery()
        return Response(response)


class SohReconciliationMutationView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_soh_reconciliation_mutate", request={"application/json": MUTATE}, responses={200: SCHEMA})
    def post(self, request: Request, pk: uuid.UUID, operation: str) -> Response:
        access = self.access(request)
        row = load(access, pk)
        meta = parse_meta(request.data, revision_bound=True)
        if operation not in {"submit", "cancel"}:
            raise Refusal("NOT_FOUND", "That count operation was not found.")
        body = business_body(request.data, {"reviewed_hash"} if operation == "submit" else {"reason"})
        def handler(run: CommandRun) -> CommandResult:
            if operation == "submit":
                services.submit(run, access, row, meta.expected_revision, str(body.get("reviewed_hash") or ""))
            else:
                services.cancel(run, access, row, meta.expected_revision, str(body.get("reason") or ""))
            return CommandResult(resource_type="soh_reconciliation", resource_id=str(row.pk))
        action = services.RUN if access.human_id == row.maker_id else services.REVIEW
        self.run_command(request, access=access, action=action, meta=meta, business_input=body, handler=handler,
                         site_id=row.site_id, resource_ids=[str(row.pk)], reviewed_hash=row.content_hash)
        response = dto(access, load(access, pk))
        access.revalidate_delivery()
        return Response(response)
