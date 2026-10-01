"""Master sheet import routes under ``/api/goods-v1/masters/master-sheet-imports``.

Upload one KDPS master sheet, review and adjust what it would change, submit it
as one package; a different ``config.approve`` holder decides it through the one
approvals command (``POST /api/goods-v1/approvals/<id>/decide``).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    decode_cursor,
    encode_cursor,
    page,
    parse_meta,
)
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun, database_now
from core.refusals import Refusal
from files.goods_models import MAX_EVIDENCE_BYTES
from files.goods_services import XLSX, check_workbook, detect_media_type, stage_upload
from masters import master_sheet_services as services
from masters.master_sheet import read_master_sheet
from masters.master_sheet_models import MasterSheetImport

READ_ACTIONS = (services.DRAFT_ACTION, services.APPROVE_ACTION)
PAGE_SIZE = 25

_TEXT = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_NULLABLE_TEXT = {"type": "string", "nullable": True}
_TEXTS = {"type": "array", "items": _TEXT}
_PROBLEM = {
    "type": "object",
    "required": ["column", "row", "text", "code", "message", "blocking"],
    "properties": {
        "column": _TEXT,
        "row": _INT,
        "text": _TEXT,
        "code": _TEXT,
        "message": _TEXT,
        "blocking": _BOOL,
    },
}
_ENTRY = {
    "type": "object",
    "required": ["text", "row"],
    "properties": {"text": _TEXT, "row": _INT},
}
_VALUE = {
    "type": "object",
    "required": ["value_id", "label"],
    "properties": {"value_id": _TEXT, "label": _TEXT},
}
_COUNTS = {"type": "object", "additionalProperties": _INT}
_DIMENSION = {
    "type": "object",
    "required": [
        "dimension",
        "column",
        "current_version_id",
        "changed",
        "added",
        "left_out",
        "present",
        "back_in_use",
        "not_in_sheet",
        "problems",
        "counts",
    ],
    "properties": {
        "dimension": _TEXT,
        "column": _TEXT,
        "current_version_id": _NULLABLE_TEXT,
        "changed": _BOOL,
        "added": {"type": "array", "items": _ENTRY},
        "left_out": {"type": "array", "items": _ENTRY},
        "present": {"type": "array", "items": _VALUE},
        "back_in_use": {"type": "array", "items": _VALUE},
        "not_in_sheet": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["value_id", "label", "retire"],
                "properties": {"value_id": _TEXT, "label": _TEXT, "retire": _BOOL},
            },
        },
        "problems": {"type": "array", "items": _PROBLEM},
        "counts": _COUNTS,
    },
}
_RULE_PICK = {
    "type": "object",
    "nullable": True,
    "required": ["value", "options", "target_id", "from", "rule_id"],
    "properties": {
        "value": _TEXT,
        "options": _TEXTS,
        "target_id": _TEXT,
        "from": _NULLABLE_TEXT,
        "rule_id": _NULLABLE_TEXT,
    },
}
_RULE = {
    "type": "object",
    "required": ["item", "row"],
    "properties": {
        "item": _TEXT,
        "row": _INT,
        "sub_category": _RULE_PICK,
        "type": _RULE_PICK,
    },
}
_TO_CREATE = {
    "type": "object",
    "required": ["name", "code", "include"],
    "properties": {"name": _TEXT, "code": _TEXT, "include": _BOOL, "row": _INT},
}
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "dimensions",
        "brands",
        "seasons",
        "item_rules",
        "ignored_columns",
        "warnings",
        "change_count",
        "retires",
        "ready",
    ],
    "properties": {
        "dimensions": {"type": "array", "items": _DIMENSION},
        "brands": {
            "type": "object",
            "required": [
                "to_create",
                "present_count",
                "not_in_sheet",
                "terms_note",
                "problems",
            ],
            "properties": {
                "to_create": {"type": "array", "items": _TO_CREATE},
                "present_count": _INT,
                "not_in_sheet": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "name"],
                        "properties": {"id": _INT, "name": _TEXT},
                    },
                },
                "terms_note": _TEXT,
                "problems": {"type": "array", "items": _PROBLEM},
            },
        },
        "seasons": {
            "type": "object",
            "required": ["to_create"],
            "properties": {"to_create": {"type": "array", "items": _TO_CREATE}},
        },
        "item_rules": {
            "type": "object",
            "required": ["new", "changed", "same_count", "problems"],
            "properties": {
                "new": {"type": "array", "items": _RULE},
                "changed": {"type": "array", "items": _RULE},
                "same_count": _INT,
                "problems": {"type": "array", "items": _PROBLEM},
            },
        },
        "ignored_columns": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["column", "values", "reason"],
                "properties": {"column": _TEXT, "values": _TEXTS, "reason": _TEXT},
            },
        },
        "warnings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "code", "dimension", "message", "acknowledged"],
                "properties": {
                    "id": _TEXT,
                    "code": _TEXT,
                    "dimension": _TEXT,
                    "message": _TEXT,
                    "acknowledged": _BOOL,
                },
            },
        },
        "change_count": _INT,
        "retires": _BOOL,
        "ready": _BOOL,
    },
}
SELECTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "retire": _TEXTS,
        "skip_values": {"type": "object", "additionalProperties": _TEXTS},
        "skip_brands": _TEXTS,
        "skip_seasons": _TEXTS,
        "rule_choices": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"sub_category": _TEXT, "type": _TEXT},
            },
        },
        "acknowledged": _TEXTS,
    },
}
IMPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "id",
        "record_contract",
        "revision",
        "state",
        "content_hash",
        "source_name",
        "source_hash",
        "allowed_actions",
        "data",
    ],
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer", "minimum": 1},
        "state": {"type": "string", "enum": [s.value for s in MasterSheetImport.State]},
        "content_hash": _TEXT,
        "source_name": _TEXT,
        "source_hash": _TEXT,
        "created_at": {"type": "string", "format": "date-time"},
        "allowed_actions": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["select", "refresh", "submit", "withdraw", "approve"],
            },
        },
        "data": {
            "type": "object",
            "required": [
                "sheet",
                "rows_read",
                "stale",
                "uploaded_by",
                "approved_by",
                "approval_request_id",
                "selections",
                "plan",
                "result",
            ],
            "properties": {
                "sheet": _TEXT,
                "rows_read": _INT,
                "stale": _BOOL,
                "uploaded_by": _TEXT,
                "approved_by": _NULLABLE_TEXT,
                "approval_request_id": {
                    "type": "string",
                    "format": "uuid",
                    "nullable": True,
                },
                "selections": SELECTIONS_SCHEMA,
                "plan": PLAN_SCHEMA,
                "result": {"type": "object", "additionalProperties": True},
            },
        },
    },
}
PAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items", "next_cursor"],
    "properties": {
        "items": {"type": "array", "items": IMPORT_SCHEMA},
        "next_cursor": _NULLABLE_TEXT,
    },
}
UPLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["file", "command_id", "contract_version", "expected_sha256"],
    "properties": {
        "file": {"type": "string", "format": "binary"},
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
    },
}
_META = {
    "command_id": {"type": "string", "format": "uuid"},
    "contract_version": {"type": "string", "enum": ["goods-v1"]},
    "expected_revision": {"type": "integer", "minimum": 1},
}
MUTATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["command_id", "contract_version", "expected_revision"],
    "properties": {**_META, "selections": SELECTIONS_SCHEMA, "reviewed_hash": _TEXT},
}
OPERATIONS = {
    "selections": {"selections"},
    "refresh": set(),
    "submit": {"reviewed_hash"},
    "withdraw": set(),
}


def _reader(access: AccessContext) -> None:
    if not any(access.can(action) for action in READ_ACTIONS):
        raise Refusal(
            "ACTION_DENIED", "You do not have permission to see product list imports."
        )


def _load(access: AccessContext, pk: uuid.UUID) -> MasterSheetImport:
    _reader(access)
    found = MasterSheetImport.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
    if found is None:
        raise Refusal("NOT_FOUND", "That master sheet import was not found.")
    return found


def _person(human_id: Any) -> str:
    from accounts.models import User

    user = User.objects.filter(human_id=human_id).order_by("pk").first()
    if user is None:
        return ""
    return user.get_full_name() or user.email


def import_dto(access: AccessContext, source: MasterSheetImport) -> dict[str, Any]:
    open_ = source.state in services.OPEN_STATES
    stale = open_ and services.is_stale(source, database_now())
    allowed: list[str] = []
    if open_ and source.uploaded_by_id == access.human_id:
        allowed += ["select", "refresh", "withdraw"]
        if (
            source.state == MasterSheetImport.State.REVIEW
            and not stale
            and source.plan.get("ready")
        ):
            allowed.append("submit")
    if (
        source.state == MasterSheetImport.State.SUBMITTED
        and source.uploaded_by_id != access.human_id
        and access.can(services.APPROVE_ACTION)
    ):
        allowed.append("approve")
    return {
        "id": str(source.pk),
        "record_contract": "goods-v1",
        "revision": source.revision,
        "state": source.state,
        "content_hash": source.reviewed_hash,
        "source_name": source.source_name,
        "source_hash": source.source_hash,
        "created_at": source.created_at.isoformat() if source.created_at else None,
        "allowed_actions": allowed,
        "data": {
            "sheet": source.parsed.get("sheet", ""),
            "rows_read": source.parsed.get("rows_read", 0),
            "stale": stale,
            "uploaded_by": _person(source.uploaded_by_id),
            "approved_by": _person(source.approved_by_id)
            if source.approved_by_id
            else None,
            "approval_request_id": str(source.approval_request_id)
            if source.approval_request_id
            else None,
            "selections": source.selections,
            "plan": source.plan,
            "result": source.result,
        },
    }


class MasterSheetImportListView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_master_sheet_import_list", responses={200: PAGE_SCHEMA}
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, {"cursor", "state"})
        _reader(access)
        rows = MasterSheetImport.objects.filter(tenant_id=access.tenant_id)
        if params.get("state"):
            rows = rows.filter(state=params["state"])
        offset = decode_cursor(params.get("cursor"))
        window = list(
            rows.order_by("-created_at", "id")[offset : offset + PAGE_SIZE + 1]
        )
        result = page(
            [import_dto(access, row) for row in window[:PAGE_SIZE]],
            encode_cursor(offset + PAGE_SIZE) if len(window) > PAGE_SIZE else None,
        )
        access.revalidate_delivery()
        return Response(result)


SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "lists",
        "brands",
        "seasons",
        "item_rules",
        "p_rate",
        "open_import_id",
    ],
    "properties": {
        "lists": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["dimension", "column", "active", "retired"],
                "properties": {
                    "dimension": _TEXT,
                    "column": _TEXT,
                    "active": _INT,
                    "retired": _INT,
                },
            },
        },
        "brands": _INT,
        "seasons": _INT,
        "item_rules": _INT,
        "p_rate": {
            "type": "object",
            "required": ["expected_factor", "in_force"],
            "properties": {
                "expected_factor": _TEXT,
                "in_force": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "rates_version_id",
                            "transport_pct",
                            "factor",
                            "expected",
                        ],
                        "properties": {
                            "rates_version_id": _TEXT,
                            "transport_pct": _TEXT,
                            "factor": _TEXT,
                            "expected": _BOOL,
                        },
                    },
                },
            },
        },
        "open_import_id": {"type": "string", "format": "uuid", "nullable": True},
    },
}


class MasterSheetSummaryView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_master_sheet_summary", responses={200: SUMMARY_SCHEMA}
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        check_query(request, set())
        _reader(access)
        result = services.summary(access.tenant_id, database_now())
        access.revalidate_delivery()
        return Response(result)


TEMPLATE_READERS = (*READ_ACTIONS, "pt.prepare", "pt.prepare.opening")


class MasterSheetTemplateView(GoodsAPIView):
    """The KDPS PT file from the lists in force: Master Sheet plus a blank Work Sheet."""

    @extend_schema(
        operation_id="goods_v1_master_sheet_template",
        responses={(200, XLSX): {"type": "string", "format": "binary"}},
    )
    def get(self, request: Request) -> HttpResponse:
        from masters.master_sheet_template import template_bytes

        access = self.access(request)
        check_query(request, set())
        if not any(access.can(action) for action in TEMPLATE_READERS):
            raise Refusal(
                "ACTION_DENIED", "You do not have permission to download the PT file."
            )
        now = database_now()
        rates = services.summary(access.tenant_id, now)["p_rate"]["in_force"]
        factors = {Decimal(r["factor"]) for r in rates}
        factor = (
            factors.pop()
            if len(factors) == 1
            else 1 + services.EXPECTED_TRANSPORT_PCT / 100
        )
        data = template_bytes(access.tenant_id, now, factor)
        access.revalidate_delivery()
        response = HttpResponse(data, content_type=XLSX)
        response["Content-Disposition"] = 'attachment; filename="KDPS PT FILE.xlsx"'
        return response


class MasterSheetImportUploadView(GoodsAPIView):
    parser_classes = [MultiPartParser]

    @extend_schema(
        operation_id="goods_v1_master_sheet_import_upload",
        request={"multipart/form-data": UPLOAD_SCHEMA},
        responses={201: IMPORT_SCHEMA},
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        access.require(services.DRAFT_ACTION)
        uploaded = request.FILES.get("file")
        if uploaded is None or (
            uploaded.size is not None and uploaded.size > MAX_EVIDENCE_BYTES
        ):
            raise Refusal(
                "MASTER_SHEET_INVALID",
                "Attach an XLSX workbook of at most 20 MB.",
                status=422,
            )
        data = uploaded.read(MAX_EVIDENCE_BYTES + 1)
        if detect_media_type(data, uploaded.name or "master-sheet.xlsx") != XLSX:
            raise Refusal(
                "MASTER_SHEET_INVALID",
                "The master sheet must be an .xlsx workbook.",
                status=422,
            )
        check_workbook(data)
        parsed = read_master_sheet(data)
        try:
            command_id = uuid.UUID(str(request.data.get("command_id")))
        except ValueError:
            raise Refusal("INVALID_REQUEST", "Provide a command_id UUID.") from None
        if request.data.get("contract_version") != "goods-v1":
            raise Refusal("INVALID_REQUEST", "contract_version must be goods-v1.")
        evidence = stage_upload(
            access.principal(),
            command_id=uuid.uuid5(command_id, "source"),
            data=data,
            filename=uploaded.name or "master-sheet.xlsx",
            kind="other",
            scope={
                "scope_kind": "sites",
                "site_ids": [],
                "brand_ids": [],
                "sensitive_fields": [],
            },
            expected_sha256=str(request.data.get("expected_sha256") or ""),
            contains_fields=[],
        )
        meta = parse_meta(
            {"command_id": str(command_id), "contract_version": "goods-v1"},
            revision_bound=False,
        )

        def handler(run: CommandRun) -> CommandResult:
            source = services.create_import(run, evidence=evidence, parsed=parsed)
            return CommandResult(
                resource_type="master_sheet_import",
                resource_id=str(source.pk),
                status_code=201,
            )

        result = self.run_command(
            request,
            access=access,
            action="masters.master_sheet.upload",
            meta=meta,
            business_input={"source_hash": evidence.sha256},
            handler=handler,
            evidence_hashes=[evidence.sha256],
        )
        dto = import_dto(access, _load(access, uuid.UUID(str(result.resource_id))))
        access.revalidate_delivery()
        return Response(dto, status=201)


class MasterSheetImportDetailView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_master_sheet_import_detail",
        responses={200: IMPORT_SCHEMA},
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, set())
        dto = import_dto(access, _load(access, pk))
        access.revalidate_delivery()
        return Response(dto)


class MasterSheetImportMutationView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_master_sheet_import_mutate",
        request={"application/json": MUTATION_SCHEMA},
        responses={200: IMPORT_SCHEMA},
    )
    def post(self, request: Request, pk: uuid.UUID, operation: str) -> Response:
        access = self.access(request)
        if operation not in OPERATIONS:
            raise Refusal("NOT_FOUND", "That master sheet operation was not found.")
        source = _load(access, pk)
        access.require(services.DRAFT_ACTION)
        meta = parse_meta(request.data, revision_bound=True)
        fields = OPERATIONS[operation]
        body = business_body(request.data, fields, required=fields)

        def handler(run: CommandRun) -> CommandResult:
            if operation == "selections":
                services.update_selections(
                    run, access, source, body["selections"], meta.expected_revision
                )
            elif operation == "refresh":
                services.refresh(run, access, source, meta.expected_revision)
            elif operation == "submit":
                services.submit(
                    run,
                    access,
                    source,
                    meta.expected_revision,
                    str(body["reviewed_hash"]),
                )
            else:
                services.withdraw(run, access, source, meta.expected_revision)
            return CommandResult(
                resource_type="master_sheet_import", resource_id=str(source.pk)
            )

        self.run_command(
            request,
            access=access,
            action=f"masters.master_sheet.{operation}",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"{services.SUBJECT}:{pk}",
            reviewed_hash=str(body["reviewed_hash"]) if operation == "submit" else None,
        )
        dto = import_dto(access, _load(access, pk))
        access.revalidate_delivery()
        return Response(dto)
