"""Goods-v1 print-job endpoints (E182-E184): labels, printer outcomes, reprints."""

from __future__ import annotations

import uuid
from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    parse_meta,
    parse_uuid,
    resource_dto,
)
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from ptmapper import goods_print_services as prints
from ptmapper.goods_models import PrintJob

ACTION = "label.print"
#: A print job reader is either a printer/operator, or anyone entitled to read
#: the PT it belongs to.
READ_ACTIONS = (ACTION, "pt.view")

REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}
_READ_REFUSALS = (400, 401, 403, 404)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


PRINT_LABEL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "official_line_id": {"type": "string", "format": "uuid"},
        "alias_as_used": {"type": "string"},
        "mrp_paise": {"type": "string"},
        "copies": {"type": "integer"},
        "svg": {"type": "string"},
    },
}

PRINT_EVENT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "outcome": {"type": "string"},
        "actor_id": {"type": "string", "format": "uuid", "nullable": True},
        "recorded_at": {"type": "string", "format": "date-time"},
        "usable_counts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "official_line_id": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer", "nullable": True},
                },
            },
        },
        "reason_code": {"type": "string", "nullable": True},
        "scanned_alias": {"type": "string", "nullable": True},
        "matched_line_id": {"type": "string", "format": "uuid", "nullable": True},
    },
}

PRINT_JOB_RESOURCE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer"},
        "content_hash": {"type": "string"},
        "state": {"type": "string"},
        "number": {"type": "string", "nullable": True},
        "version": {"type": "integer", "nullable": True},
        "context": {"type": "object", "additionalProperties": True},
        "data": {
            "type": "object",
            "properties": {
                "pt_version_id": {"type": "string", "format": "uuid"},
                "site_id": {"type": "integer"},
                "state": {"type": "string"},
                "template_version_id": {"type": "string", "format": "uuid", "nullable": True},
                "reprint_of_id": {"type": "string", "format": "uuid", "nullable": True},
                "reason_code": {"type": "string", "nullable": True},
                "labels": {"type": "array", "items": PRINT_LABEL},
                "events": {"type": "array", "items": PRINT_EVENT},
            },
        },
        "allowed_actions": {"type": "array", "items": {"type": "string"}},
    },
}

_UUID = {"type": "string", "format": "uuid"}
_META = {
    "command_id": _UUID,
    "contract_version": {"type": "string", "enum": ["goods-v1"]},
    "expected_revision": {"type": "integer", "minimum": 1},
}
_PRINT_LINE_REQUEST = {
    "type": "object",
    "required": ["official_line_id", "copies"],
    "properties": {"official_line_id": _UUID, "copies": {"type": "integer", "minimum": 1}},
    "additionalProperties": False,
}
PRINT_CREATE_REQUEST = {
    "type": "object",
    "required": ["command_id", "contract_version", "pt_version_id", "lines", "template_version_id"],
    "properties": {
        **_META,
        "pt_version_id": _UUID,
        "lines": {"type": "array", "items": _PRINT_LINE_REQUEST, "minItems": 1, "maxItems": 200},
        "template_version_id": _UUID,
        "reprint_of_id": _UUID,
        "reason_code": {"type": "string"},
    },
    "additionalProperties": False,
}
PRINT_OUTCOME_REQUEST = {
    "type": "object",
    "required": ["command_id", "contract_version", "expected_revision", "outcome"],
    "properties": {
        **_META,
        "outcome": {"type": "string", "enum": sorted(prints.OUTCOMES)},
        "usable_counts": {"type": "array", "items": {
            "type": "object",
            "required": ["official_line_id", "qty"],
            "properties": {"official_line_id": _UUID, "qty": {"type": "integer", "minimum": 0, "nullable": True}},
            "additionalProperties": False,
        }},
        "reason_code": {"type": "string"},
        "scanned_alias": {"type": "string", "maxLength": 128},
        "matched_line_id": _UUID,
    },
    "additionalProperties": False,
}


def _dto(job: PrintJob) -> dict[str, Any]:
    return resource_dto(
        id=job.pk,
        data=prints.print_job_data(job),
        revision=job.revision,
        state=job.status,
        context={"site_id": job.site_id},
    )


def _load(access: AccessContext, pk: uuid.UUID) -> PrintJob:
    if not access.holds_any(READ_ACTIONS):
        raise Refusal("ACTION_DENIED", "You do not have permission to read print jobs.")
    job = PrintJob.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
    brand_id = prints.source_brand_for_job(job) if job is not None else None
    if job is None or not any(
        access.can(action, site_id=job.site_id, brand_id=brand_id) for action in READ_ACTIONS
    ):
        raise Refusal("NOT_FOUND", "That print job was not found.")
    return job


class GoodsPrintJobCreateView(GoodsAPIView):
    """E182: print an official receipt or opening PT line's frozen alias and MRP."""

    http_method_names = ["post", "options"]

    @extend_schema(request={"application/json": PRINT_CREATE_REQUEST}, responses=_responses(201, PRINT_JOB_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"pt_version_id", "lines", "template_version_id", "reprint_of_id", "reason_code"},
            required=["pt_version_id", "lines", "template_version_id"],
        )
        version_id = parse_uuid(body["pt_version_id"], "pt_version_id")
        template_version_id = parse_uuid(body["template_version_id"], "template_version_id")
        lines = prints.parse_lines(body.get("lines"))
        reprint_of_id = (
            parse_uuid(body["reprint_of_id"], "reprint_of_id")
            if body.get("reprint_of_id") is not None
            else None
        )
        reason_code = body.get("reason_code")
        if reason_code is not None and not isinstance(reason_code, str):
            raise Refusal("INVALID_REQUEST", "reason_code must be a string.")

        version = prints.load_version(version_id, tenant_id=access.tenant_id)
        site_id = version.document.held_site_id
        brand_id = prints.source_brand(version.document_id)
        access.require(ACTION, site_id=site_id, brand_id=brand_id)

        def handler(run: CommandRun) -> CommandResult:
            job = prints.create_print_job(
                run,
                version=version,
                site_id=site_id,
                lines=lines,
                template_version_id=template_version_id,
                reprint_of_id=reprint_of_id,
                reason_code=reason_code,
            )
            return CommandResult(
                resource_type="print_job",
                resource_id=str(job.pk),
                status_code=201,
                revision=job.revision,
            )

        result = self.run_command(
            request,
            access=access,
            action="ptmapper.print_jobs.create",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(version_id)],
            site_id=site_id,
            subject_key=f"document:{version.document_id}",
        )
        job = PrintJob.objects.get(pk=uuid.UUID(str(result.resource_id)))
        return Response(_dto(job), status=result.status_code)


class GoodsPrintJobOutcomeView(GoodsAPIView):
    """E183: the printer's own outcome, separate from the dialog/afterprint attempt."""

    http_method_names = ["post", "options"]

    @extend_schema(request={"application/json": PRINT_OUTCOME_REQUEST}, responses=_responses(200, PRINT_JOB_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"outcome", "usable_counts", "reason_code", "scanned_alias", "matched_line_id"},
            required=["outcome"],
        )
        outcome = body["outcome"]
        if outcome not in prints.OUTCOMES:
            raise Refusal("INVALID_REQUEST", f"outcome must be one of {sorted(prints.OUTCOMES)}.")
        reason_code = body.get("reason_code")
        if reason_code is not None and not isinstance(reason_code, str):
            raise Refusal("INVALID_REQUEST", "reason_code must be a string.")
        scanned_alias = body.get("scanned_alias")
        if scanned_alias is not None and (
            not isinstance(scanned_alias, str) or len(scanned_alias) > 128
        ):
            raise Refusal(
                "INVALID_REQUEST", "scanned_alias must be a string of at most 128 characters."
            )
        matched_line_id = body.get("matched_line_id")
        if matched_line_id is not None:
            matched_line_id = str(parse_uuid(matched_line_id, "matched_line_id"))

        job = _load(access, pk)
        access.require(ACTION, site_id=job.site_id, brand_id=prints.source_brand_for_job(job))

        def handler(run: CommandRun) -> CommandResult:
            updated = prints.record_outcome(
                run,
                job_id=pk,
                expected_revision=meta.expected_revision,
                outcome=outcome,
                usable_counts=body.get("usable_counts"),
                reason_code=reason_code,
                scanned_alias=scanned_alias,
                matched_line_id=matched_line_id,
            )
            return CommandResult(
                resource_type="print_job", resource_id=str(pk), revision=updated.revision
            )

        result = self.run_command(
            request,
            access=access,
            action="ptmapper.print_jobs.outcome",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=job.site_id,
            subject_key=f"print_job:{pk}",
        )
        job.refresh_from_db()
        return Response(_dto(job), status=result.status_code)


class GoodsPrintJobDetailView(GoodsAPIView):
    """E184: frozen printable labels and event history. A reprint is a new job."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(responses=_responses(200, PRINT_JOB_RESOURCE, _READ_REFUSALS))
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        job = _load(access, pk)
        return Response(_dto(job))
