"""Goods-v1 evidence upload and scoped download (E120, E121).

The download used to share the ``/api/files/{id}/download`` OpenAPI path
template with the legacy integer-keyed download, so only one of the two could be
described (#303). Both views hand-build their responses, so the schemas below
are what makes them describable at all.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, parse_int_id, parse_uuid, resource_dto
from core.canonical import sha256_hex
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.offbox import OffboxError, get_store
from core.refusals import Refusal
from files.goods_models import MAX_EVIDENCE_BYTES, EvidenceObject
from files.goods_services import evidence_dto, readable_by, scope_cells, stage_upload

UPLOAD_ACTIONS = (
    "receive.arrival",
    "pt.prepare",
    "pt.prepare.opening",
    "booking.manage",
    "movement.draft",
    "transfer.move",
    "stock.accept",
    "count.run",
    "recovery.run",
    "config.draft",
    "org.site.manage",
)
FIELD_SET = frozenset({"cost", "margin", "layer_value", "personal", "financial"})


def _scope(raw: Any) -> dict[str, Any]:
    try:
        scope = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise Refusal("INVALID_REQUEST", "scope must be a JSON object.") from None
    if not isinstance(scope, dict):
        raise Refusal("INVALID_REQUEST", "scope must be a JSON object.")
    allowed = {"entity_id", "site_ids", "sbu_ids", "brand_ids", "scope_kind", "sensitive_fields"}
    if set(scope) - allowed:
        raise Refusal("INVALID_REQUEST", "scope has unknown keys.")
    site_ids = scope.get("site_ids") or []
    if not isinstance(site_ids, list) or len(site_ids) > 1000:
        raise Refusal("INVALID_REQUEST", "scope.site_ids must be a list.")
    fields = scope.get("sensitive_fields") or []
    if not isinstance(fields, list) or set(fields) - FIELD_SET:
        raise Refusal("INVALID_REQUEST", "scope.sensitive_fields holds unknown fields.")
    return {
        "entity_id": str(scope["entity_id"]) if scope.get("entity_id") else None,
        "site_ids": [str(parse_int_id(s, "site_ids")) for s in site_ids],
        "sbu_ids": [str(parse_uuid(s, "sbu_ids")) for s in scope.get("sbu_ids") or []],
        "brand_ids": [str(parse_int_id(s, "brand_ids")) for s in scope.get("brand_ids") or []],
        "scope_kind": str(scope.get("scope_kind") or "sites"),
        "sensitive_fields": sorted(set(fields)),
    }


REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}

#: `files.goods_services.evidence_dto`. `download_url` is the goods-v1 route,
#: which is the only route that will serve this object.
EVIDENCE_DATA: dict[str, Any] = {
    "type": "object",
    "description": "EvidenceDTO (E120).",
    "properties": {
        "filename": {"type": "string"},
        "media_type": {"type": "string"},
        "size": {"type": "integer"},
        "sha256": {"type": "string"},
        "download_url": {"type": "string"},
        "scope": {"type": "object", "additionalProperties": True},
        "kind": {"type": "string"},
    },
}

EVIDENCE_RESOURCE: dict[str, Any] = {
    "type": "object",
    "description": "ResourceDTO<EvidenceDTO>.",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer"},
        "content_hash": {"type": "string"},
        "state": {"type": "string", "enum": ["confirmed"]},
        "number": {"type": "string", "nullable": True},
        "version": {"type": "integer", "nullable": True},
        "context": {"type": "object", "additionalProperties": True},
        "allowed_actions": {"type": "array", "items": {"type": "string"}},
        "data": EVIDENCE_DATA,
    },
}


class EvidenceUploadView(GoodsAPIView):
    """E120: validate, store write-once, confirm, then record protected evidence."""

    parser_classes = [MultiPartParser]

    @extend_schema(
        request={
            "multipart/form-data": {
                "type": "object",
                "required": ["command_id", "contract_version", "file"],
                "properties": {
                    "command_id": {"type": "string", "format": "uuid"},
                    "contract_version": {"type": "string", "enum": ["goods-v1"]},
                    "file": {"type": "string", "format": "binary"},
                    "scope": {
                        "type": "string",
                        "description": "JSON-encoded evidence scope, narrowed to the caller's grants.",
                    },
                    "kind": {"type": "string"},
                    "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
            }
        },
        responses={
            201: EVIDENCE_RESOURCE,
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            413: REFUSAL_RESPONSE,
            422: REFUSAL_RESPONSE,
        }
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        length = int(request.META.get("CONTENT_LENGTH") or 0)
        if length > MAX_EVIDENCE_BYTES + 64_000:
            raise Refusal("FILE_TOO_LARGE", "Files larger than 20 MB are not accepted.", status=413)
        data = request.data
        if data.get("contract_version") != "goods-v1":
            raise Refusal("INVALID_REQUEST", "contract_version must be goods-v1.")
        try:
            command_id = uuid.UUID(str(data.get("command_id")))
        except ValueError:
            raise Refusal("INVALID_REQUEST", "A command_id UUID is required.") from None
        upload = request.FILES.get("file")
        if upload is None:
            raise Refusal("INVALID_REQUEST", "Attach the file as 'file'.")
        if upload.size is not None and upload.size > MAX_EVIDENCE_BYTES:
            raise Refusal("FILE_TOO_LARGE", "Files larger than 20 MB are not accepted.", status=413)
        scope = _scope(data.get("scope") or "{}")
        sites = [int(s) for s in scope["site_ids"]]
        if not any(access.holds(action) for action in UPLOAD_ACTIONS):
            raise Refusal("ACTION_DENIED", "You may not upload evidence.")
        # The whole declared scope, cell by cell: a file for two brands needs both.
        cells, entity_id = scope_cells(scope)
        if not access.covers_all(UPLOAD_ACTIONS, cells, entity_id=entity_id):
            raise Refusal("NOT_FOUND", "Part of this scope was not found.")
        self.goods_command_id = command_id
        evidence = stage_upload(
            access.principal(),
            command_id=command_id,
            data=upload.read(MAX_EVIDENCE_BYTES + 1),
            filename=upload.name or "evidence",
            kind=str(data.get("kind") or "other"),
            scope=scope,
            expected_sha256=str(data.get("expected_sha256") or ""),
            contains_fields=scope["sensitive_fields"],
        )
        dto = evidence_dto(evidence)
        return Response(
            resource_dto(
                id=evidence.pk,
                data=dto,
                revision=1,
                state="confirmed",
                context={"site_id": sites[0] if sites else None, "entity_id": scope["entity_id"]},
            ),
            status=201,
        )


class EvidenceDownloadView(GoodsAPIView):
    """E121: the original bytes, only for someone entitled to its whole declared scope."""

    @extend_schema(
        responses={
            (200, "*/*"): {
                "type": "string",
                "format": "binary",
                "description": "The stored evidence file, as an attachment (E121).",
            },
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request, pk: uuid.UUID) -> HttpResponse:
        access = self.access(request)
        evidence = EvidenceObject.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
        if evidence is None or not readable_by(access, evidence):
            raise Refusal("NOT_FOUND", "That file was not found.")
        try:
            data = get_store().get(evidence.object_key)
        except OffboxError as exc:
            raise Refusal("EVIDENCE_UNAVAILABLE", "The file is temporarily unavailable.") from exc
        if sha256_hex(data) != evidence.sha256 or len(data) != evidence.size:
            raise Refusal("EVIDENCE_UNAVAILABLE", "The stored file does not match its record.")

        def handler(run: CommandRun) -> CommandResult:
            return CommandResult(resource_type="evidence_download", resource_id=str(evidence.pk))

        execute_command(
            access.principal(),
            CommandSpec(
                "files.download",
                uuid.uuid4(),
                {"evidence": str(evidence.pk)},
                subject_key=f"evidence:{evidence.pk}",
            ),
            handler,
        )
        # Off-box retrieval and the audit command can both take time. Check the
        # live session and complete file scope once more before bytes leave.
        if not access.refresh() or not readable_by(access, evidence):
            raise Refusal("NOT_FOUND", "That file was not found.")
        response = HttpResponse(data, content_type=evidence.media_type)
        safe = evidence.filename.replace('"', "")
        response["Content-Disposition"] = f'attachment; filename="{safe}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response
