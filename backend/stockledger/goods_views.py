"""Goods-v1 stock endpoints: acceptance sessions (E139-E142), stock reads (E171-E176, E181).

The five ledger reads and the availability read used to share their paths with
the legacy back-office readers through the old contract dispatcher, so
drf-spectacular described the legacy operation and never these (#303). They
answer under ``/api/goods-v1/`` alone now. These views hand-build their
responses, so the schemas below are what makes them describable.
"""

from __future__ import annotations

import uuid
from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    decode_cursor,
    encode_cursor,
    page,
    page_limit,
    paginate,
    parse_int_id,
    parse_meta,
    parse_uuid,
    resource_dto,
)
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.kernel_models import OfficialVersion
from core.refusals import Refusal
from masters.models import Store
from ptmapper.goods_models import GoodsPt
from stockledger import goods_acceptance as acceptance
from stockledger import goods_reads as reads
from stockledger.goods_models import AcceptanceSession

ACCEPT = "stock.accept"
#: Receivers and inventory readers at the site may read a session (E140).
READ_ACTIONS = (ACCEPT, "stock.view")
LINE_PAGE = 500
DETAIL_QUERY = frozenset({"line_cursor", "history_cursor", "limit"})

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


def _command_request(
    properties: dict[str, Any], *, required: tuple[str, ...] = (), revision_bound: bool = False
) -> dict[str, Any]:
    """The closed goods-v1 JSON command envelope plus this route's fields."""
    return {"application/json": {
        "type": "object", "additionalProperties": False,
        "required": ["command_id", "contract_version", *required,
                     *(["expected_revision"] if revision_bound else [])],
        "properties": {
            "command_id": {"type": "string", "format": "uuid"},
            "contract_version": {"type": "string", "enum": ["goods-v1"]},
            "expected_revision": {"type": "integer", "minimum": 1},
            **properties,
        },
    }}


ACCEPTANCE_OPEN_REQUEST = _command_request({
    "site_id": {"type": "integer"},
    "source_version_id": {"type": "string", "format": "uuid"},
}, required=("site_id", "source_version_id"))
ACCEPTANCE_SCAN_REQUEST = _command_request({
    "observations": {"type": "array", "minItems": 1, "maxItems": acceptance.MAX_OBSERVATIONS,
        "items": {"type": "object", "additionalProperties": False,
            "required": ["scan_key", "outcome", "condition", "qty", "alias_value", "actual_at"],
            "properties": {
                "scan_key": {"type": "string", "format": "uuid"},
                "official_line_id": {"type": "string", "format": "uuid", "nullable": True},
                "alias_value": {"type": "string", "maxLength": 128},
                "observed_ticket_mrp_paise": {"type": "integer", "nullable": True},
                "label_evidence_id": {"type": "string", "format": "uuid", "nullable": True},
                "chosen_sku_id": {"type": "string", "format": "uuid", "nullable": True},
                "candidate_hash": {"type": "string", "minLength": 64, "maxLength": 64},
                "qty": {"type": "integer", "minimum": 1, "maximum": acceptance.MAX_QTY},
                "condition": {"type": "string", "enum": list(acceptance.CONDITIONS)},
                "location_id": {"type": "string", "format": "uuid", "nullable": True},
                "outcome": {"type": "string", "enum": list(acceptance.OUTCOMES)},
                "actual_at": {"type": "string", "format": "date-time"},
            },
        }},
}, required=("observations",), revision_bound=True)
ACCEPTANCE_COMPLETE_REQUEST = _command_request({
    "confirm_complete": {"type": "boolean"},
    "reason_code": {"type": "string", "minLength": 1, "maxLength": 60},
}, required=("confirm_complete",), revision_bound=True)
ACCEPTANCE_EXTRA_REQUEST = _command_request({
    "alias_value": {"type": "string", "minLength": 1, "maxLength": 128},
    "qty": {"type": "integer", "minimum": 1, "maximum": acceptance.MAX_QTY},
    "official_line_id": {"type": "string", "format": "uuid", "nullable": True},
    "note": {"type": "string", "maxLength": acceptance.MAX_NOTE},
}, required=("alias_value", "qty"))
RETURNED_PIECES_REQUEST = _command_request({
    "site_id": {"type": "integer"},
    "sale_line_ids": {"type": "array", "minItems": 1, "items": {"type": "integer"}},
    "location_id": {"type": "string", "format": "uuid"},
}, required=("site_id", "sale_line_ids", "location_id"))


#: One line of an acceptance session's progress (``goods_acceptance.line_progress``).
ACCEPTANCE_LINE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "description": "One PT line with how much of it is still to accept.",
}

#: Ticket 07B: where extra pieces go back to (``goods_acceptance.correction_route``).
#: Null for a source with no goods receipt (an opening PT).
ACCEPTANCE_CORRECTION: dict[str, Any] = {
    "type": "object",
    "nullable": True,
    "description": (
        "AcceptanceCorrectionRoute (ticket 07B): the source documents extra pieces go "
        "back to - the goods receipt's count, its excess decision, then a supplement PT "
        "- and whether this person may start that correction (raise the counter-GRN) "
        "or hands the pieces over instead (E251). The goods receipt is named only to "
        "someone who may read it. Null when the PT has no goods receipt."
    ),
    "required": [
        "grn_id",
        "grn_number",
        "pt_id",
        "pt_number",
        "receipt_kind",
        "can_correct",
        "handoff",
    ],
    "properties": {
        "grn_id": {"type": "string", "format": "uuid", "nullable": True},
        "grn_number": {"type": "string", "nullable": True},
        "pt_id": {"type": "string", "format": "uuid"},
        "pt_number": {"type": "string", "nullable": True},
        "receipt_kind": {"type": "string", "enum": ["primary", "supplement"], "nullable": True},
        "can_correct": {"type": "boolean"},
        "handoff": {
            "type": "object",
            "nullable": True,
            "description": "The owned handoff for this goods receipt, if anyone made one.",
            "required": ["exception_id", "state", "owner_role", "opened_at"],
            "properties": {
                "exception_id": {"type": "string", "format": "uuid"},
                "state": {"type": "string", "enum": ["open", "resolved"]},
                "owner_role": {"type": "string"},
                "opened_at": {"type": "string", "format": "date-time"},
            },
        },
    },
}

#: ``AcceptanceDTO`` (E139-E142). No cost, margin or value field exists in it.
ACCEPTANCE_DATA: dict[str, Any] = {
    "type": "object",
    "description": "AcceptanceDTO: the session, its acknowledged scans and its line progress.",
    "properties": {
        "session_id": {"type": "string", "format": "uuid"},
        "source_version_id": {"type": "string", "format": "uuid"},
        "state": {"type": "string"},
        "acknowledged_scan_keys": {"type": "array", "items": {"type": "string"}},
        "lines": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": ACCEPTANCE_LINE},
                "next_cursor": {"type": "string", "nullable": True},
                "total": {"type": "integer"},
            },
        },
        "correction": ACCEPTANCE_CORRECTION,
    },
}

ACCEPTANCE_RESOURCE: dict[str, Any] = {
    "type": "object",
    "description": "ResourceDTO<AcceptanceDTO>.",
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
        "data": ACCEPTANCE_DATA,
    },
}

#: ``StockReadLimitation[]`` (R27, ``goods_reads.READ_LIMITATIONS``): the closed
#: codes a stock answer uses to say what it could not replay. Identical for
#: every reader, so declaring one discloses nothing the caller may not open.
STOCK_READ_LIMITATIONS: dict[str, Any] = {
    "type": "array",
    "description": (
        "StockReadLimitation[] (R27): which parts of this answer are still "
        "read from today's rows because they keep no history. Empty for a "
        "live read; a past as_of carries every code until those histories "
        "are versioned. A client renders these and never infers them from "
        "the timestamp it asked for."
    ),
    # From the constant itself, so the published enum - and the frontend type
    # generated from it - cannot drift from what the server actually sends.
    "items": {"type": "string", "enum": list(reads.READ_LIMITATIONS)},
}

#: ``StockDTO`` (``goods_engine.build_stock_rows``): one addressed portion with
#: every quantity the reads distinguish. A value field appears only for a basis
#: the caller's field grant allows, and never as an invented zero.
STOCK_ROW: dict[str, Any] = {
    "type": "object",
    "description": "StockDTO (E173-E176).",
    "additionalProperties": True,
    "properties": {
        "site_id": {"type": "string", "nullable": True},
        "location_id": {"type": "string", "nullable": True},
        "sku_id": {"type": "string", "nullable": True},
        "description": {"type": "string"},
        "origin_id": {"type": "string", "nullable": True},
        "source_kind": {"type": "string"},
        "condition": {"type": "string"},
        "physical_qty": {"type": "integer"},
        "valued_qty": {"type": "integer"},
        "accepted_qty": {"type": "integer"},
        "held_qty": {"type": "integer"},
        "reserved_qty": {"type": "integer"},
        "ats_qty": {"type": "integer"},
        "transferable_qty": {"type": "integer"},
        "eligibility_reasons": {"type": "array", "items": {"type": "string"}},
        "limitations": STOCK_READ_LIMITATIONS,
        "as_of": {"type": "string", "format": "date-time"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        # OPS-03. The season frozen on this row's origin, as it stands now: a
        # later governed correction shows here and the season the row opened
        # under stays beside it in `season_original`.
        "season_id": {"type": "string", "nullable": True},
        "season_label": {"type": "string"},
        "season_unknown_historical": {"type": "boolean"},
        "season_original": {
            "type": "object",
            "nullable": True,
            "description": "The season this row opened under, when a correction has moved it.",
            "additionalProperties": True,
        },
    },
}

#: One quantity pair from the operational journal (E171).
JOURNAL_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "JournalDTO (E171): one posted quantity pair, newest first.",
    "properties": {
        "document_id": {"type": "string", "format": "uuid"},
        "number": {"type": "string", "nullable": True},
        "version": {"type": "integer", "nullable": True},
        "event_id": {"type": "string"},
        "kind": {"type": "string", "description": "The posting kind, P01-P18."},
        "source": {"type": "object", "nullable": True, "additionalProperties": True},
        "destination": {"type": "object", "additionalProperties": True},
        "qty": {"type": "integer"},
        # R27, narrower than a stock row's: an entry's own addresses and
        # quantity are replayed exactly, but its value is priced from today's
        # origin records. See ``goods_reads.JOURNAL_LIMITATIONS``.
        "limitations": {
            "type": "array",
            "description": (
                "StockReadLimitation[] (R27): empty for a live read; a past "
                "as_of declares current_cost_projection, because an entry's "
                "value_paise is priced from today's origin records."
            ),
            "items": {"type": "string", "enum": list(reads.JOURNAL_LIMITATIONS)},
        },
        "event_at": {"type": "string", "format": "date-time"},
        "recorded_at": {"type": "string", "format": "date-time"},
    },
}

#: One authorised step on an origin's portions (E181). Portion offsets are never
#: shown, so nothing here reads as the history of an individual piece.
JOURNEY_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "JourneyDTO (E181).",
    "properties": {
        "event_id": {"type": "string"},
        "event_kind": {"type": "string"},
        "document_id": {"type": "string", "nullable": True},
        "number": {"type": "string", "nullable": True},
        "version": {"type": "integer", "nullable": True},
        "source_site_id": {"type": "string", "nullable": True},
        "destination_site_id": {"type": "string", "nullable": True},
        "qty": {"type": "integer"},
        "source_evidence_ref": {"type": "string", "nullable": True},
        "event_at": {"type": "string", "format": "date-time"},
        "recorded_at": {"type": "string", "format": "date-time"},
        "value_paise": {
            "type": "string",
            "description": "Present only for a caller whose field grant covers cost.",
        },
    },
}


def _watermarked_page(item_schema: dict[str, Any], description: str) -> dict[str, Any]:
    """``Page<T>`` whose ``as_of`` is the read's watermark, not the wall clock."""
    return {
        "type": "object",
        "description": description,
        "properties": {
            "items": {"type": "array", "items": item_schema},
            "next_cursor": {"type": "string", "nullable": True},
            "as_of": {"type": "string", "format": "date-time"},
        },
    }


STOCK_ROW_PAGE = _watermarked_page(STOCK_ROW, "Page<StockDTO>.")
JOURNAL_PAGE = _watermarked_page(JOURNAL_ITEM, "Page<JournalDTO>.")
JOURNEY_PAGE = _watermarked_page(JOURNEY_ITEM, "Page<JourneyDTO>.")

#: ``StockSummaryDTO`` (E172). ``value_paise`` is null when any piece in scope is
#: unvalued: an unknown value is not a zero one, so the known part is reported
#: separately alongside how complete the valuation is.
STOCK_SUMMARY_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "StockSummaryDTO (E172).",
    "properties": {
        "scope": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "nullable": True},
                "site_ids": {"type": "array", "items": {"type": "string"}},
                "sbu_ids": {"type": "array", "items": {"type": "string"}},
                "brand_ids": {"type": "array", "items": {"type": "string"}},
                "scope_kind": {"type": "string"},
            },
        },
        "basis": {"type": "string", "enum": ["quantity", "cost", "ticket"]},
        "as_of": {"type": "string", "format": "date-time"},
        "limitations": STOCK_READ_LIMITATIONS,
        "totals": {
            "type": "object",
            "properties": {
                "physical_qty": {"type": "integer"},
                "valued_qty": {"type": "integer"},
                "unvalued_qty": {"type": "integer"},
                "ats_qty": {"type": "integer"},
                "transferable_qty": {"type": "integer"},
                "transit_qty": {"type": "integer"},
                "value_paise": {"type": "string", "nullable": True},
                "valued_value_paise": {"type": "string"},
                "value_completeness": {
                    "type": "string",
                    "enum": ["complete", "partial", "unknown"],
                },
            },
        },
    },
}


def source_brand(document_id: uuid.UUID) -> int | None:
    """The brand of a source PT (its GRN arrival's brand); ``None`` when it has none."""
    brand_id: int | None = (
        GoodsPt.objects.filter(document_id=document_id)
        .values_list("grn__arrival__brand_id", flat=True)
        .first()
    )
    return brand_id


def source_brands(document_ids: list[uuid.UUID]) -> dict[uuid.UUID, int | None]:
    """The same answer for a whole page, in one query rather than one per row."""
    if not document_ids:
        return {}
    return {
        document_id: brand_id
        for document_id, brand_id in GoodsPt.objects.filter(
            document_id__in=sorted(set(document_ids))
        ).values_list("document_id", "grn__arrival__brand_id")
    }


def _session_brand(session: AcceptanceSession) -> int | None:
    document_id = (
        OfficialVersion.objects.filter(pk=session.source_version_id)
        .values_list("document_id", flat=True)
        .first()
    )
    return source_brand(document_id) if document_id is not None else None


def _session_for(
    access: AccessContext, pk: uuid.UUID, actions: tuple[str, ...]
) -> tuple[AcceptanceSession, int | None]:
    """The session in scope and its source brand; site and brand (SBU) scope both apply."""
    granted = access.all_actions()
    if not any(action in granted for action in actions):
        raise Refusal("ACTION_DENIED", "You do not have permission for acceptance sessions.")
    session = AcceptanceSession.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
    brand_id = _session_brand(session) if session is not None else None
    if session is None or not any(
        access.can(a, site_id=session.site_id, brand_id=brand_id) for a in actions
    ):
        raise Refusal("NOT_FOUND", "That acceptance session was not found.")
    return session, brand_id


def acceptance_resource(
    access: AccessContext, session: AcceptanceSession, params: dict[str, str] | None = None
) -> dict[str, Any]:
    """ResourceDTO<AcceptanceDTO>. No cost, margin or value field exists in this DTO."""
    params = params or {}
    offset = decode_cursor(params.get("line_cursor"))
    limit = page_limit(params, default=LINE_PAGE, maximum=LINE_PAGE)
    decode_cursor(params.get("history_cursor"))
    version = OfficialVersion.objects.select_related("document").get(pk=session.source_version_id)
    rows = acceptance.line_progress(version)
    brand_id = source_brand(version.document_id)
    can_accept = access.can(ACCEPT, site_id=session.site_id, brand_id=brand_id)
    correction = _correction(access, version)
    data = {
        "session_id": str(session.pk),
        "source_version_id": str(session.source_version_id),
        "state": session.state,
        "acknowledged_scan_keys": acceptance.acknowledged_scan_keys(session),
        "lines": {
            "items": rows[offset : offset + limit],
            "next_cursor": encode_cursor(offset + limit) if offset + limit < len(rows) else None,
            "total": len(rows),
        },
        "correction": correction,
    }
    allowed: list[str] = []
    if session.state == AcceptanceSession.State.OPEN and can_accept:
        allowed += ["scan", "complete"]
    if correction is not None and can_accept and acceptance.version_is_live(version):
        # Ticket 07B: anyone who may accept here may hand extra pieces over;
        # starting the correction itself is `correction.can_correct`.
        allowed.append("report_extra")
    return resource_dto(
        id=session.pk,
        data=data,
        revision=session.revision,
        state=session.state,
        context={"site_id": session.site_id, "entity_id": version.document.entity_id},
        number=version.document.official_number,
        version=version.version,
        allowed_actions=allowed,
    )


def _correction(access: AccessContext, version: OfficialVersion) -> dict[str, Any] | None:
    """The way back for extra pieces, judged for this person (ticket 07B).

    The goods receipt is named only to someone who may read it, and "may start
    the correction" is the counter-GRN's own gate - the same site and brand
    test E118 applies - so the screen never offers a route the server refuses.
    """
    # Imported here: `inbound.goods_services` reads this app's acceptance module.
    from inbound.goods_services import RECEIVE_ACTION, can_read

    source = acceptance.receipt_source(version)
    if source is None or source.grn is None:
        return None
    grn = source.grn
    site_id = grn.document.held_site_id
    brand_id = grn.arrival.brand_id
    return acceptance.correction_route(
        version,
        source,
        can_read_grn=can_read(access, grn.document.site_id, brand_id),
        can_correct=access.can(RECEIVE_ACTION, site_id=site_id, brand_id=brand_id),
    )


class AcceptanceSessionCreateView(GoodsAPIView):
    """E139: open (or resume) a session against a live receipt or opening PT version."""

    http_method_names = ["post", "options"]

    @extend_schema(
        request=ACCEPTANCE_OPEN_REQUEST,
        responses=_responses(201, ACCEPTANCE_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"site_id", "source_version_id"},
            required=["site_id", "source_version_id"],
        )
        site_id = parse_int_id(body["site_id"], "site_id")
        version_id = parse_uuid(body["source_version_id"], "source_version_id")
        if ACCEPT not in access.all_actions():
            raise Refusal("ACTION_DENIED", "You do not have permission to accept goods.")
        version = (
            OfficialVersion.objects.select_related("document")
            .filter(tenant_id=access.tenant_id, pk=version_id)
            .first()
        )
        if version is None or not Store.objects.filter(pk=site_id).exists():
            raise Refusal("NOT_FOUND", "That PT version was not found.")
        brand_id = source_brand(version.document_id)
        access.require(ACCEPT, site_id=version.document.site_id, brand_id=brand_id)
        access.require(ACCEPT, site_id=site_id, brand_id=brand_id)

        def handler(run: CommandRun) -> CommandResult:
            session, _created = acceptance.open_session(run, site_id=site_id, version_id=version_id)
            return CommandResult(
                resource_type="acceptance_session",
                resource_id=str(session.pk),
                status_code=201,
                revision=session.revision,
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.acceptance.open",
            meta=meta,
            business_input={"site_id": site_id, "source_version_id": str(version_id)},
            handler=handler,
            resource_ids=[str(version_id)],
            site_id=site_id,
            subject_key=f"document:{version.document_id}",
        )
        session = AcceptanceSession.objects.get(pk=uuid.UUID(str(result.resource_id)))
        return Response(acceptance_resource(access, session), status=result.status_code)


class AcceptanceSessionDetailView(GoodsAPIView):
    """E140: acknowledged scans and per-line remaining quantities."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(responses=_responses(200, ACCEPTANCE_RESOURCE, _READ_REFUSALS))
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, DETAIL_QUERY)
        session, _brand_id = _session_for(access, pk, READ_ACTIONS)
        return Response(acceptance_resource(access, session, params))


class _SessionCommandView(GoodsAPIView):
    action = ""
    allowed: frozenset[str] = frozenset()
    required: tuple[str, ...] = ()
    http_method_names = ["post", "options"]

    @extend_schema(responses=_responses(200, ACCEPTANCE_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, self.allowed, required=self.required)
        parsed = self.parse(body)
        session, brand_id = _session_for(access, pk, (ACCEPT,))
        access.require(ACCEPT, site_id=session.site_id, brand_id=brand_id)

        def handler(run: CommandRun) -> CommandResult:
            changed = self.execute(run, pk, parsed, meta.expected_revision)
            return CommandResult(
                resource_type="acceptance_session",
                resource_id=str(pk),
                revision=changed.revision,
            )

        result = self.run_command(
            request,
            access=access,
            action=self.action,
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=session.site_id,
            subject_key=f"acceptance:{pk}",
        )
        session.refresh_from_db()
        return Response(acceptance_resource(access, session), status=result.status_code)

    def parse(self, body: dict[str, Any]) -> Any:
        raise NotImplementedError

    def execute(
        self, run: CommandRun, pk: uuid.UUID, parsed: Any, expected_revision: int | None
    ) -> AcceptanceSession:
        raise NotImplementedError


@extend_schema_view(post=extend_schema(request=ACCEPTANCE_SCAN_REQUEST))
class AcceptanceScanView(_SessionCommandView):
    """E141: compare tags with the frozen line, then post checked/accepted/damaged scans."""

    action = "stock.acceptance.scan"
    allowed = frozenset({"observations"})
    required = ("observations",)

    def parse(self, body: dict[str, Any]) -> list[acceptance.Scan]:
        return acceptance.parse_scans(body.get("observations"))

    def execute(
        self, run: CommandRun, pk: uuid.UUID, parsed: Any, expected_revision: int | None
    ) -> AcceptanceSession:
        return acceptance.scan(run, pk, parsed, expected_revision)


@extend_schema_view(post=extend_schema(request=ACCEPTANCE_COMPLETE_REQUEST))
class AcceptanceCompleteView(_SessionCommandView):
    """E142: close the session; remaining pieces stay open work, never a write-off."""

    action = "stock.acceptance.complete"
    allowed = frozenset({"reason_code", "confirm_complete"})
    required = ("confirm_complete",)

    def parse(self, body: dict[str, Any]) -> tuple[bool, str | None]:
        confirm = body.get("confirm_complete")
        if not isinstance(confirm, bool):
            raise Refusal("INVALID_REQUEST", "confirm_complete must be true or false.")
        reason = body.get("reason_code")
        if reason is not None and (not isinstance(reason, str) or not 1 <= len(reason) <= 60):
            raise Refusal("INVALID_REQUEST", "reason_code is 1 to 60 characters.")
        return confirm, reason

    def execute(
        self, run: CommandRun, pk: uuid.UUID, parsed: Any, expected_revision: int | None
    ) -> AcceptanceSession:
        confirm, reason = parsed
        return acceptance.complete(
            run,
            pk,
            confirm_complete=confirm,
            reason_code=reason,
            expected_revision=expected_revision,
        )


class AcceptanceExtraReportView(GoodsAPIView):
    """E251 (ticket 07B): hand extra pieces over; nothing is accepted and the PT is untouched.

    For the person at the goods who cannot correct the goods receipt themselves:
    it opens one owned ``acceptance_discrepancy`` for that receipt (or adds a note
    to the open one) and answers the session, whose ``correction.handoff`` then
    says who owns it. Not revision-bound - it changes no session, scan or stock -
    and replayed by its command identity like every other command.
    """

    http_method_names = ["post", "options"]

    @extend_schema(
        request=ACCEPTANCE_EXTRA_REQUEST,
        responses=_responses(200, ACCEPTANCE_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, acceptance.EXTRA_FIELDS, required=["alias_value", "qty"])
        report = acceptance.parse_extra_report(body)
        session, brand_id = _session_for(access, pk, (ACCEPT,))
        access.require(ACCEPT, site_id=session.site_id, brand_id=brand_id)

        def handler(run: CommandRun) -> CommandResult:
            acceptance.report_extra(run, pk, report)
            return CommandResult(resource_type="acceptance_session", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action="stock.acceptance.report_extra",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=session.site_id,
            subject_key=f"acceptance:{pk}",
        )
        session.refresh_from_db()
        return Response(acceptance_resource(access, session), status=result.status_code)


# ---------------------------------------------------------------------------
# Goods stock reads (E171-E176) and the origin journey (E181)
# ---------------------------------------------------------------------------


class _StockReadView(GoodsAPIView):
    """A goods-v1 stock read: closed query, trusted scope, basis field grant, watermark."""

    http_method_names = ["get", "head", "options"]
    entries = False

    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, reads.STOCK_QUERY_KEYS)
        query = reads.resolve_query(access, params, entries=self.entries)
        return Response(self.read(query, params))

    def read(self, query: reads.StockQuery, params: dict[str, str]) -> dict[str, Any]:
        raise NotImplementedError


@extend_schema_view(get=extend_schema(responses=_responses(200, JOURNAL_PAGE, _READ_REFUSALS)))
class GoodsStockEntriesView(_StockReadView):
    """E171: operational journal quantity pairs touching the caller's scope."""

    entries = True

    def read(self, query: reads.StockQuery, params: dict[str, str]) -> dict[str, Any]:
        return reads.entries_page(query, params)


@extend_schema_view(
    get=extend_schema(responses=_responses(200, STOCK_SUMMARY_RESPONSE, _READ_REFUSALS))
)
class GoodsStockSummaryView(_StockReadView):
    """E172: physical, valued, available and in-transit totals over one scope."""

    def read(self, query: reads.StockQuery, params: dict[str, str]) -> dict[str, Any]:
        return reads.summary(query)


@extend_schema_view(get=extend_schema(responses=_responses(200, STOCK_ROW_PAGE, _READ_REFUSALS)))
class GoodsStockOnHandView(_StockReadView):
    """E173: physical presence in every condition and encumbrance state."""

    def read(self, query: reads.StockQuery, params: dict[str, str]) -> dict[str, Any]:
        return reads.rows_page(query, reads.on_hand_rows(query), params)


@extend_schema_view(get=extend_schema(responses=_responses(200, STOCK_ROW_PAGE, _READ_REFUSALS)))
class GoodsStockInTransitView(_StockReadView):
    """E174: dispatched portions not yet checked in or resolved short."""

    def read(self, query: reads.StockQuery, params: dict[str, str]) -> dict[str, Any]:
        return reads.rows_page(query, reads.transit_rows(query), params)


@extend_schema_view(get=extend_schema(responses=_responses(200, STOCK_ROW_PAGE, _READ_REFUSALS)))
class GoodsStockQuarantineView(_StockReadView):
    """E175: quarantine locations, stock not in good condition and held stock."""

    def read(self, query: reads.StockQuery, params: dict[str, str]) -> dict[str, Any]:
        return reads.rows_page(query, reads.quarantine_rows(query), params)


@extend_schema_view(get=extend_schema(responses=_responses(200, STOCK_ROW_PAGE, _READ_REFUSALS)))
class GoodsStockAvailabilityView(_StockReadView):
    """E176: store ATS and warehouse transferable quantity with every failing reason."""

    def read(self, query: reads.StockQuery, params: dict[str, str]) -> dict[str, Any]:
        return reads.rows_page(query, reads.availability_rows(query), params)


PENDING_ACCEPTANCE_ITEM: dict[str, Any] = {
    "type": "object",
    "description": (
        "PendingAcceptanceDTO (design §6.2, E248). Only what a person holding "
        "stock.accept needs to open or resume an acceptance session: the official "
        "version and its PT reference, the site, how much is still to accept and "
        "when the work last changed. No PT rows, costs or exception detail."
    ),
    "properties": {
        "official_version_id": {"type": "string", "format": "uuid"},
        "pt_id": {"type": "string", "format": "uuid"},
        "pt_number": {"type": "string", "nullable": True},
        "site_id": {"type": "string"},
        "remaining_qty": {"type": "integer"},
        "updated_at": {"type": "string", "format": "date-time"},
    },
}

PENDING_ACCEPTANCE_PAGE: dict[str, Any] = {
    "type": "object",
    "description": "Page<PendingAcceptanceDTO>, oldest actionable work first.",
    "properties": {
        "items": {"type": "array", "items": PENDING_ACCEPTANCE_ITEM},
        "next_cursor": {"type": "string", "nullable": True},
        "as_of": {"type": "string", "format": "date-time"},
    },
}


class PendingAcceptanceView(GoodsAPIView):
    """E248: the acceptance work waiting for this person, and nothing wider.

    Discovering your own work must not require ``pt.view`` or ``exception.view``
    (GSA-T07). A receiver holding only ``stock.accept`` reads this, sees the
    official versions still to accept inside their own site scope, and opens or
    resumes E139 from one of them.
    """

    http_method_names = ["get", "head", "options"]

    @extend_schema(responses=_responses(200, PENDING_ACCEPTANCE_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, frozenset({"site_id", "cursor", "limit"}))
        if ACCEPT not in access.all_actions():
            raise Refusal("ACTION_DENIED", "You do not have permission to accept goods.")
        wanted = parse_int_id(params["site_id"], "site_id") if params.get("site_id") else None
        reach = access.site_reach(ACCEPT)
        if wanted is not None:
            reach = {wanted} if reach is None else (reach & {wanted})
        found = acceptance.pending_acceptance(access.tenant_id, reach)
        brands = source_brands([row.pt_id for row in found])
        rows = [
            row
            for row in found
            if access.can(ACCEPT, site_id=row.site_id, brand_id=brands.get(row.pt_id))
        ]
        window, cursor = paginate(rows, params, default=50, maximum=100)
        return Response(
            page(
                [
                    {
                        "official_version_id": str(row.official_version_id),
                        "pt_id": str(row.pt_id),
                        "pt_number": row.pt_number,
                        "site_id": str(row.site_id),
                        "remaining_qty": row.remaining_qty,
                        "updated_at": row.updated_at.isoformat(),
                    }
                    for row in window
                ],
                cursor,
            )
        )


#: One return leg waiting to be put away (`sell.services.returned_pieces`).
RETURNED_PIECES_PAGE: dict[str, Any] = {
    "type": "object",
    "description": "Customer-return legs with pieces still standing in receiving.",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "required": [
            "sale_line_id", "site_id", "doc_number", "till_number", "barcode",
            "description", "qty", "returned_at",
        ], "properties": {
            **{name: {"type": "string"} for name in (
                "sale_line_id", "site_id", "doc_number", "till_number", "barcode",
                "description"
            )},
            "qty": {"type": "integer"},
            "returned_at": {"type": "string", "format": "date-time"},
        }}},
        "next_cursor": {"type": "string", "nullable": True},
    },
}


class ReturnedPiecesView(GoodsAPIView):
    """OPS-09: the pieces customers brought back, and putting them away.

    `GET` lists the return legs still standing unaccepted in a store's receiving;
    `POST` accepts named ones through OPS-07's own `accept_returned_pieces`, which
    writes the same acceptance evidence a transfer's arrival does - a session
    against the bill's own frozen version, and an event naming who put which
    portion where. Only after that does the piece appear on the counter's shelf
    again (PRD §10.4).

    Gated on `stock.accept` at the store, exactly as putting away a delivery is:
    it is the same act on the same shelf, and a returned piece is not a lesser one.
    """

    http_method_names = ["get", "post", "head", "options"]

    @extend_schema(responses=_responses(200, RETURNED_PIECES_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        from sell.services.returned_pieces import pending_returns

        access = self.access(request)
        params = check_query(request, frozenset({"site_id", "cursor", "limit"}))
        if ACCEPT not in access.all_actions():
            raise Refusal("ACTION_DENIED", "You do not have permission to accept goods.")
        wanted = parse_int_id(params["site_id"], "site_id") if params.get("site_id") else None
        reach = access.site_reach(ACCEPT)
        if wanted is not None:
            reach = {wanted} if reach is None else (reach & {wanted})
        rows = [row for row in pending_returns(reach) if access.can(ACCEPT, site_id=row.store_id)]
        window, cursor = paginate(rows, params, default=50, maximum=100)
        return Response(
            page(
                [
                    {
                        "sale_line_id": str(row.sale_line_id),
                        "site_id": str(row.store_id),
                        "doc_number": row.doc_number,
                        "till_number": row.till_number,
                        "barcode": row.barcode,
                        "description": row.description,
                        "qty": row.qty,
                        "returned_at": row.returned_at.isoformat(),
                    }
                    for row in window
                ],
                cursor,
            )
        )

    @extend_schema(
        request=RETURNED_PIECES_REQUEST,
        responses=_responses(200, RETURNED_PIECES_PAGE, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        from sell.models import SaleLine
        from sell.services.goods_sale import accept_returned_pieces

        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"site_id", "sale_line_ids", "location_id"},
            required=["site_id", "sale_line_ids", "location_id"],
        )
        site_id = parse_int_id(body["site_id"], "site_id")
        location_id = parse_uuid(body["location_id"], "location_id")
        raw = body["sale_line_ids"]
        if not isinstance(raw, list) or not raw:
            raise Refusal("INVALID_REQUEST", "Name at least one returned line to put away.")
        line_ids = [parse_int_id(value, "sale_line_ids") for value in raw]
        access.require(ACCEPT, site_id=site_id)
        store = Store.objects.filter(pk=site_id).first()
        if store is None:
            raise Refusal("NOT_FOUND", "That site was not found.")
        lines = list(
            SaleLine.objects.filter(
                pk__in=line_ids,
                direction=SaleLine.Direction.RETURN,
                sale__store_id=site_id,
            ).select_related("sale")
        )
        if len(lines) != len(set(line_ids)):
            raise Refusal("NOT_FOUND", "One of those returned lines is not this store's.")

        counted: dict[str, int] = {"qty": 0}

        def handler(run: CommandRun) -> CommandResult:
            for line in lines:
                counted["qty"] += accept_returned_pieces(
                    run, store=store, sale_line=line, location_id=location_id
                )
            run.audit_after = {"accepted_qty": counted["qty"]}
            return CommandResult(
                resource_type="sale_line", resource_id=str(lines[0].pk), status_code=200
            )

        self.run_command(
            request,
            access=access,
            action="sell.return.accept",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(line.pk) for line in lines],
            site_id=site_id,
            subject_key=f"sale_line:{lines[0].pk}",
        )
        return Response({"accepted_qty": counted["qty"], "lines": len(lines)})


class OriginJourneyView(GoodsAPIView):
    """E181: an origin's frozen evidence and the authorised events on its portions."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(responses=_responses(200, JOURNEY_PAGE, _READ_REFUSALS))
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, reads.JOURNEY_QUERY_KEYS)
        return Response(reads.journey_page(access, pk, params))
