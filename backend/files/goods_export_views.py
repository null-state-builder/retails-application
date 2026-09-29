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
    """E190: the original requester, or an administrator whose scope covers the job.

    "Scoped administrator" is `audit.view` - the grant that reads what other people
    did - checked at the export's own site, or tenant-wide when it names none.
    """
    if intent.actor_id is not None and intent.actor_id == access.human_id:
        return True
    payload = intent.payload or {}
    site_id = int(payload["site_id"]) if payload.get("site_id") else None
    return access.can("audit.view", site_id=site_id)


class GoodsExportCreateView(GoodsAPIView):
    """E189: ask for a durable export, scoped to exactly what the asker may see."""

    http_method_names = ["post", "options"]

    @extend_schema(responses=_responses(202, EXPORT_JOB_RESPONSE, _WRITE_REFUSALS))
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
        return Response(exports.export_job_dto(intent), status=result.status_code)


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
        # E190 step 5: reauthorise *now*, not at request time. A caller who no
        # longer covers the file's cells and fields still sees their job and its
        # progress, but neither its hash nor a link - "no broader cached file is
        # returned after access changes" is about this answer, not only E121's.
        return Response(exports.export_job_dto(intent, access=access))
