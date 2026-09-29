"""Goods-v1 durable export endpoints (E189, E190).

E189 asks for a file and answers 202 with a job; E190 polls that job. The bytes
themselves are never served from here - they are protected evidence, downloaded
through E121, which re-checks the caller's scope on every download. So an export
whose requester loses a grant tomorrow stops being downloadable tomorrow, and
the job record is not the thing standing between a person and the rows.
"""

from __future__ import annotations

import uuid
from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, parse_meta
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.kernel_models import OutboxIntent
from core.refusals import Refusal
from files import goods_exports as exports

REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}
_READ_REFUSALS = (400, 401, 403, 404, 503)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 503)

#: `files.goods_exports.export_job_dto`.
EXPORT_JOB_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "ExportJobDTO (E189, E190).",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "state": {
            "type": "string",
            "enum": ["pending", "running", "confirmed", "failed", "unknown"],
        },
        "progress": {"type": "integer"},
        "as_of": {"type": "string", "nullable": True},
        "sha256": {"type": "string", "nullable": True},
        "download_url": {"type": "string", "nullable": True},
        "error_code": {"type": "string", "nullable": True},
    },
}


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


def _readable(access: AccessContext, intent: OutboxIntent) -> bool:
    """Only current authority over the job's declared and produced data may see it."""
    from core.kernel_models import JobArtifact
    from files.goods_services import readable_by, scope_cells

    try:
        spec = exports.parse_spec((intent.payload or {}).get("export_spec"))
        kind = exports.resolve_kind(spec.kind)
    except Refusal:
        return False
    artifact = JobArtifact.objects.select_related("evidence").filter(intent_id=intent.pk).first()
    if artifact is not None and not readable_by(access, artifact.evidence):
        return False
    if intent.actor_id is not None and intent.actor_id == access.human_id:
        if exports.EXPORT_GRANT not in access.all_actions():
            return False
        try:
            kind.check(access, spec)
        except Refusal:
            return False
        return True
    cells, entity_id = scope_cells(spec.scope)
    return access.covers_all({"audit.view"}, cells, spec.field_set, entity_id=entity_id)


class GoodsExportCreateView(GoodsAPIView):
    """E189: ask for a durable export, scoped to exactly what the asker may see."""

    http_method_names = ["post", "options"]

    @extend_schema(
        request={
            "application/json": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command_id", "contract_version", "kind", "scope"],
            "properties": {
                "command_id": {"type": "string", "format": "uuid"},
                "contract_version": {"type": "string", "enum": ["goods-v1"]},
                "expected_revision": {"type": "integer", "minimum": 1},
                "kind": {"type": "string", "enum": sorted(exports.SPEC_KINDS)},
                "scope": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["scope_kind"],
                    "properties": {
                        "scope_kind": {"type": "string", "enum": sorted(exports.SCOPE_KINDS)},
                        "entity_id": {"type": "integer"},
                        "site_ids": {"type": "array", "items": {"type": "integer"}},
                        "sbu_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}},
                        "brand_ids": {"type": "array", "items": {"type": "integer"}},
                    },
                },
                "field_set": {
                    "type": "array", "items": {"type": "string", "enum": sorted(exports.FIELD_SET)},
                },
                "subject_key": {"type": "string", "maxLength": 100},
                "as_of": {"type": "string", "format": "date-time"},
                "profile_version_id": {"type": "string", "format": "uuid"},
            },
            },
        },
        responses=_responses(202, EXPORT_JOB_RESPONSE, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        # E189's input is "ExportSpec + MutationMeta": the spec's own fields at the
        # top level beside the meta, the way every other bare-DTO body is sent.
        body = business_body(request.data, exports.SPEC_FIELDS, required=["kind", "scope"])
        spec = exports.parse_spec(body)

        access.require_action(exports.EXPORT_GRANT)
        # Every export is a single-identity privileged action (Phase 1 §9.3), so
        # every export needs the same fresh confirmation - not only the bundle.
        access.require_step_up()
        kind = exports.resolve_kind(spec.kind)
        kind.check(access, spec)
        site_id = spec.site_ids[0] if len(spec.site_ids) == 1 else None

        def handler(run: CommandRun) -> CommandResult:
            intent = exports.request_export(run, spec=spec, site_id=site_id)
            run.audit_after = {"kind": spec.kind, "scope": spec.scope}
            return CommandResult(
                resource_type="export", resource_id=str(intent.pk), status_code=202
            )

        result = self.run_command(
            request,
            access=access,
            action=exports.EXPORT_ACTION,
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=site_id,
            subject_key=spec.subject_key or f"export:{meta.command_id}",
        )
        intent = OutboxIntent.objects.get(pk=uuid.UUID(str(result.resource_id)))
        if not access.refresh():
            raise Refusal("AUTH_REQUIRED", "Your access changed. Sign in again.")
        return Response(exports.export_job_dto(intent, access=access), status=result.status_code)


class GoodsExportDetailView(GoodsAPIView):
    """E190: how far the job got, and where its confirmed file is."""

    http_method_names = ["get", "options"]

    @extend_schema(responses=_responses(200, EXPORT_JOB_RESPONSE, _READ_REFUSALS))
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, set())
        intent = OutboxIntent.objects.filter(
            tenant_id=access.tenant_id, pk=pk, kind="export"
        ).first()
        if intent is None or not _readable(access, intent):
            raise Refusal("NOT_FOUND", "That export was not found.")
        # E190 step 5: reauthorise now, including the file when it exists.
        return Response(exports.export_job_dto(intent, access=access))
