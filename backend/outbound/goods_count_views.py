"""Non-trading counts over HTTP (goods ticket 17), under ``/api/goods-v1/outbound/``.

Design E102/E103 (reads), E159 (start), E160 (open a pass), E161 (scan), E162
(submit), E163 (variance), E164 (recount), E166 (cancel) and E211 (blind
lookup), plus the pass resume and the zero-variance close this ticket aligns in
design §7.3. Every route checks the grant at the counted site itself; a count,
a pass or a variance outside the caller's reach is ``NOT_FOUND`` alike.
Differences are never applied here - that is ticket 17A's reviewed, Owner-approved
correction - and nothing here ever sets book stock to a scan total.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    page,
    paginate,
    parse_int_id,
    parse_meta,
    parse_uuid,
)
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from outbound import goods_count_reads as reads
from outbound import goods_counts as counts
from outbound.goods_models import GoodsCountPass, GoodsStocktake

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
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


def _meta(*, revision_bound: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
    }
    if revision_bound:
        out["expected_revision"] = {"type": "integer", "minimum": 1}
    return out


def _required(*, revision_bound: bool) -> list[str]:
    return ["command_id", "contract_version", *(["expected_revision"] if revision_bound else [])]


PERSON: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "nullable": True},
        "name": {"type": "string"},
    },
    "required": ["id", "name"],
}

SCOPE: dict[str, Any] = {
    "type": "object",
    "description": (
        "CountScope (design §5.3): the whole site, one location and everything under "
        "it, or one brand's goods at the site. `count_kind` is `full` or `cycle`."
    ),
    "properties": {
        "kind": {"type": "string", "enum": list(counts.SCOPE_KINDS)},
        "count_kind": {"type": "string", "enum": list(counts.COUNT_KINDS)},
        "location_id": {"type": "string", "nullable": True},
        "location_name": {"type": "string", "nullable": True},
        "brand_id": {"type": "string", "nullable": True},
        "brand_name": {"type": "string", "nullable": True},
    },
    "required": ["kind", "count_kind", "location_id", "location_name", "brand_id", "brand_name"],
}

_SUMMARY_PROPERTIES: dict[str, Any] = {
    "id": {"type": "string", "format": "uuid"},
    "record_contract": {"type": "string", "enum": ["goods-v1"]},
    "number": {"type": "string", "nullable": True},
    "state": {"type": "string", "enum": list(GoodsStocktake.State.values)},
    "revision": {"type": "integer"},
    "site": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "code": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["id", "code", "name"],
    },
    "scope": SCOPE,
    "frozen_at": {"type": "string", "nullable": True},
    "freeze_active": {
        "type": "boolean",
        "description": "True while the count holds the site's freeze (it is open).",
    },
    "started_by": PERSON,
    "started_at": {"type": "string", "format": "date-time"},
    "last_activity_at": {"type": "string", "format": "date-time"},
    "open_passes": {"type": "integer"},
    "submitted_passes": {"type": "integer"},
    "stale_passes": {"type": "integer"},
}

COUNT_SUMMARY: dict[str, Any] = {
    "type": "object",
    "description": "One count as a list row. Carries no book, on-hand or valued quantity.",
    "properties": _SUMMARY_PROPERTIES,
    "required": list(_SUMMARY_PROPERTIES),
}

PASS_ROW: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "pass_no": {"type": "integer"},
        "counter": PERSON,
        "kind": {"type": "string", "enum": ["count", "location", "recount"]},
        "scope_label": {"type": "string"},
        "location_id": {"type": "string", "nullable": True},
        "state": {"type": "string", "enum": list(GoodsCountPass.State.values)},
        "stale": {"type": "boolean"},
        "opened_at": {"type": "string", "format": "date-time"},
        "last_activity_at": {"type": "string", "nullable": True},
        "submitted_at": {"type": "string", "nullable": True},
        "replaces_pass_id": {"type": "string", "nullable": True},
        "reason_code": {"type": "string", "nullable": True},
        "observed_qty": {
            "type": "integer",
            "nullable": True,
            "description": (
                "Pieces this pass observed. Only its own counter, or a reviewer who is "
                "not counting in this count, reads it; null for everyone else."
            ),
        },
    },
    "required": [
        "id",
        "pass_no",
        "counter",
        "kind",
        "scope_label",
        "location_id",
        "state",
        "stale",
        "opened_at",
        "last_activity_at",
        "submitted_at",
        "replaces_pass_id",
        "reason_code",
        "observed_qty",
    ],
}

HISTORY_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "kind": {"type": "string"},
        "actor_id": {"type": "string", "nullable": True},
        "recorded_at": {"type": "string", "format": "date-time"},
        "revision": {"type": "integer", "nullable": True},
        "outcome": {"type": "string", "nullable": True},
        "reason_code": {"type": "string", "nullable": True},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "related_document_id": {"type": "string", "nullable": True},
    },
    "required": [
        "id",
        "kind",
        "actor_id",
        "recorded_at",
        "revision",
        "outcome",
        "reason_code",
        "evidence_ids",
        "related_document_id",
    ],
}

_DETAIL_PROPERTIES: dict[str, Any] = {
    **_SUMMARY_PROPERTIES,
    "non_trading_event_id": {
        "type": "string",
        "description": "The approved non-trading declaration the count started under.",
    },
    "locations": {
        "type": "array",
        "description": (
            "The locations this count covers, by name - what a pass may be assigned. "
            "Identity only; the site's location directory keeps its own grant."
        ),
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "kind": {"type": "string"},
            },
            "required": ["id", "name", "kind"],
        },
    },
    "progress": {
        "type": "string",
        "enum": [
            "counting",
            "awaiting_review",
            "incomplete",
            "differences_pending",
            "matches_book",
            "closed_matching",
            "cancelled",
        ],
        "description": (
            "Where the count stands. `differences_pending`: counted differs from the "
            "frozen book - it waits, frozen, for the difference review and the Owner's "
            "approval (ticket 17A) and is not a completed count. `matches_book`: ready "
            "to close with no posting. Only a reviewer not counting here is told "
            "`incomplete`, `differences_pending` or `matches_book`; a counter sees "
            "`awaiting_review`."
        ),
    },
    "review": {
        "type": "object",
        "nullable": True,
        "description": "The reviewer's summary of the submitted passes; null for counters.",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["incomplete", "differences_pending", "matches_book"],
            },
            "variance_hash": {"type": "string"},
            "differing_lines": {"type": "integer"},
        },
        "required": ["outcome", "variance_hash", "differing_lines"],
    },
    "passes": {"type": "array", "items": PASS_ROW},
    "my_open_pass_id": {"type": "string", "nullable": True},
    "counters": {
        "type": "array",
        "description": "For a reviewer: the people counting in this count, for a recount.",
        "items": PERSON,
    },
    "decision": {
        "type": "object",
        "nullable": True,
        "properties": {
            "id": {"type": "string"},
            "decided_by": PERSON,
            "decided_at": {"type": "string", "format": "date-time"},
            "variance_hash": {"type": "string"},
            "lines": {"type": "integer"},
        },
        "required": ["id", "decided_by", "decided_at", "variance_hash", "lines"],
    },
    "allowed_actions": {
        "type": "array",
        "items": {
            "type": "string",
            "enum": [
                "open_pass",
                "continue_pass",
                "lookup",
                "variance",
                "recount",
                "close",
                "cancel",
            ],
        },
    },
    "history": {"type": "array", "items": HISTORY_ITEM},
    "next_history_cursor": {"type": "string", "nullable": True},
}

COUNT_DETAIL: dict[str, Any] = {
    "type": "object",
    "description": (
        "One count (E103). Blind: nothing here carries a book, on-hand, held, reserved "
        "or valued quantity."
    ),
    "properties": _DETAIL_PROPERTIES,
    "required": list(_DETAIL_PROPERTIES),
}

_PASS_PROPERTIES: dict[str, Any] = {
    "id": {"type": "string", "format": "uuid"},
    "stocktake_id": {"type": "string", "format": "uuid"},
    "stocktake_number": {"type": "string", "nullable": True},
    "stocktake_state": {"type": "string", "enum": list(GoodsStocktake.State.values)},
    "site_id": {"type": "string"},
    "pass_no": {"type": "integer"},
    "counter": PERSON,
    "state": {"type": "string", "enum": list(GoodsCountPass.State.values)},
    "stale": {
        "type": "boolean",
        "description": "Idle for 24 hours: it needs an explicit resume before more scans.",
    },
    "revision": {"type": "integer"},
    "observation_hash": {
        "type": "string",
        "description": "What a submission names as `reviewed_hash`: the scope and every scan.",
    },
    "scope": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["count", "location", "recount"]},
            "label": {"type": "string"},
            "location_id": {"type": "string", "nullable": True},
            "cells": {
                "type": "array",
                "description": "A recount's items and locations - identity only, no quantity.",
                "items": {
                    "type": "object",
                    "properties": {
                        "sku_id": {"type": "string", "nullable": True},
                        "item": {"type": "string"},
                        "description": {"type": "string"},
                        "location_id": {"type": "string"},
                        "location_name": {"type": "string"},
                    },
                    "required": ["sku_id", "item", "description", "location_id", "location_name"],
                },
            },
        },
        "required": ["kind", "label", "location_id", "cells"],
    },
    "reason_code": {"type": "string", "nullable": True},
    "replaces_pass_id": {"type": "string", "nullable": True},
    "locations": {
        "type": "array",
        "description": "The locations this pass is assigned (to scan into and to affirm).",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "kind": {"type": "string"},
            },
            "required": ["id", "name", "kind"],
        },
    },
    "opened_at": {"type": "string", "format": "date-time"},
    "last_activity_at": {"type": "string", "nullable": True},
    "submitted_at": {"type": "string", "nullable": True},
    "affirmation": {
        "type": "object",
        "nullable": True,
        "properties": {
            "counter": PERSON,
            "recorded_at": {"type": "string", "format": "date-time"},
            "observation_hash": {"type": "string"},
            "observation_revision": {"type": "integer"},
            "scope_hash": {"type": "string"},
        },
        "required": [
            "counter",
            "recorded_at",
            "observation_hash",
            "observation_revision",
            "scope_hash",
        ],
    },
    "observed_qty": {"type": "integer"},
    "totals": {
        "type": "object",
        "description": "What this pass observed, by condition. Never a book quantity.",
        "additionalProperties": {"type": "integer"},
    },
    "acknowledged_scan_keys": {"type": "array", "items": {"type": "string"}},
    "observations": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "scan_key": {"type": "string"},
                "location_id": {"type": "string", "nullable": True},
                "location_name": {"type": "string"},
                "sku_id": {"type": "string", "nullable": True},
                "item": {"type": "string"},
                "description": {"type": "string"},
                "alias_value": {"type": "string", "nullable": True},
                "condition": {"type": "string", "enum": list(counts.CONDITION_VALUES)},
                "qty": {"type": "integer"},
                "correction_of_id": {"type": "string", "nullable": True},
                "actual_at": {"type": "string", "format": "date-time"},
                "recorded_at": {"type": "string", "format": "date-time"},
                "counted_by": PERSON,
            },
            "required": [
                "scan_key",
                "location_id",
                "location_name",
                "sku_id",
                "item",
                "description",
                "alias_value",
                "condition",
                "qty",
                "correction_of_id",
                "actual_at",
                "recorded_at",
                "counted_by",
            ],
        },
    },
}

PASS_DETAIL: dict[str, Any] = {
    "type": "object",
    "description": (
        "One blind counting pass (read by its counter, or a reviewer not counting here). "
        "It carries what was observed and never what the book expects."
    ),
    "properties": _PASS_PROPERTIES,
    "required": list(_PASS_PROPERTIES),
}

_VARIANCE_PROPERTIES: dict[str, Any] = {
    "stocktake_id": {"type": "string"},
    "revision": {"type": "integer"},
    "variance_hash": {
        "type": "string",
        "description": "What a zero-variance close names as `reviewed_hash`.",
    },
    "selected_pass_ids": {"type": "array", "items": {"type": "string"}},
    "complete": {"type": "boolean"},
    "matches_book": {"type": "boolean"},
    "issues": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    "uncovered_locations": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
            "required": ["id", "name"],
        },
    },
    "differing_lines": {"type": "integer"},
    "lines": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_key": {"type": "string"},
                        "sku_id": {"type": "string", "nullable": True},
                        "item": {"type": "string"},
                        "description": {"type": "string"},
                        "location_id": {"type": "string"},
                        "location_name": {"type": "string"},
                        "condition": {"type": "string"},
                        "book_qty": {"type": "integer"},
                        "observed_qty": {"type": "integer", "nullable": True},
                        "delta": {"type": "integer", "nullable": True},
                        "pass_id": {"type": "string", "nullable": True},
                    },
                    "required": [
                        "line_key",
                        "sku_id",
                        "item",
                        "description",
                        "location_id",
                        "location_name",
                        "condition",
                        "book_qty",
                        "observed_qty",
                        "delta",
                        "pass_id",
                    ],
                },
            },
            "next_cursor": {"type": "string", "nullable": True},
            "total": {"type": "integer"},
        },
        "required": ["items", "next_cursor", "total"],
    },
}

VARIANCE: dict[str, Any] = {
    "type": "object",
    "description": (
        "E163, quantities only: observed minus the frozen as-of book per item, location "
        "and condition, from the selected submitted passes; unscanned known stock is "
        "zero observed. Cost and value, delta review and approval are ticket 17A's."
    ),
    "properties": _VARIANCE_PROPERTIES,
    "required": list(_VARIANCE_PROPERTIES),
}

LOOKUP: dict[str, Any] = {
    "type": "object",
    "description": (
        "E211: what a scanned code is - identity only. No book, on-hand, held, "
        "reserved, cost, value or availability is looked up or returned."
    ),
    "properties": {
        "result": {"type": "string", "enum": ["resolved", "unknown", "ambiguous"]},
        "candidate_hash": {"type": "string"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku_id": {"type": "string"},
                    "brand": {"type": "string"},
                    "style": {"type": "string"},
                    "size": {"type": "string"},
                    "colour": {"type": "string"},
                    "grade": {"type": "string"},
                },
                "required": ["sku_id", "brand", "style", "size"],
            },
        },
        "chosen_sku_id": {"type": "string", "nullable": True},
        "issues": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    },
    "required": ["result", "candidate_hash", "candidates", "chosen_sku_id", "issues"],
}


def _request(
    properties: dict[str, Any], required: list[str], *, revision_bound: bool
) -> dict[str, Any]:
    return {
        "type": "object",
        "required": [*_required(revision_bound=revision_bound), *required],
        "properties": {**_meta(revision_bound=revision_bound), **properties},
        "additionalProperties": False,
    }


START_REQUEST = _request(
    {
        "site_id": {"type": "string"},
        "scope": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(counts.SCOPE_KINDS)},
                "location_id": {"type": "string", "format": "uuid", "nullable": True},
                "brand_id": {"type": "string", "nullable": True},
                "count_kind": {"type": "string", "enum": list(counts.COUNT_KINDS)},
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    ["site_id", "scope"],
    revision_bound=False,
)
OPEN_REQUEST = _request(
    {
        "location_id": {
            "type": "string",
            "format": "uuid",
            "nullable": True,
            "description": (
                "Assign this pass one location (and everything under it). Without it the "
                "pass is assigned everything the count covers."
            ),
        }
    },
    [],
    revision_bound=False,
)
SCAN_REQUEST = _request(
    {
        "observations": {
            "type": "array",
            "minItems": 1,
            "maxItems": counts.MAX_OBSERVATIONS,
            "items": {
                "type": "object",
                "required": ["scan_key", "location_id", "condition", "qty"],
                "properties": {
                    "scan_key": {"type": "string", "format": "uuid"},
                    "location_id": {"type": "string", "format": "uuid"},
                    "sku_id": {"type": "string", "format": "uuid", "nullable": True},
                    "description": {"type": "string", "maxLength": 240},
                    "alias_value": {"type": "string", "maxLength": 128, "nullable": True},
                    "condition": {"type": "string", "enum": list(counts.CONDITION_VALUES)},
                    "qty": {
                        "type": "integer",
                        "description": "1-999999; signed only on a correction of an earlier scan.",
                    },
                    "correction_of_id": {"type": "string", "format": "uuid", "nullable": True},
                    "actual_at": {"type": "string", "format": "date-time"},
                },
                "additionalProperties": False,
            },
        }
    },
    ["observations"],
    revision_bound=True,
)
SUBMIT_REQUEST = _request(
    {
        "reviewed_hash": {"type": "string", "minLength": 64, "maxLength": 64},
        "scope_complete": {
            "type": "boolean",
            "description": (
                'GSA-T17: "I have counted the whole assigned area, including empty '
                'locations." Must be true to submit.'
            ),
        },
    },
    ["reviewed_hash", "scope_complete"],
    revision_bound=True,
)
RESUME_REQUEST = _request({}, [], revision_bound=True)
RECOUNT_REQUEST = _request(
    {
        "line_keys": {
            "type": "array",
            "minItems": 1,
            "maxItems": counts.MAX_RECOUNT_LINES,
            "items": {"type": "string", "format": "uuid"},
        },
        "counter_id": {"type": "string", "format": "uuid"},
        "reason_code": {"type": "string", "minLength": 1, "maxLength": 60},
    },
    ["line_keys", "counter_id", "reason_code"],
    revision_bound=True,
)
CLOSE_REQUEST = _request(
    {
        "reviewed_hash": {"type": "string", "minLength": 64, "maxLength": 64},
        "selected_pass_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 200,
            "items": {"type": "string", "format": "uuid"},
        },
    },
    ["reviewed_hash", "selected_pass_ids"],
    revision_bound=True,
)
CANCEL_REQUEST = _request(
    {
        "reason_code": {"type": "string", "minLength": 1, "maxLength": 60},
        "note": {"type": "string", "maxLength": 500},
    },
    ["reason_code"],
    revision_bound=True,
)


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _stocktake_for(access: AccessContext, pk: uuid.UUID) -> GoodsStocktake:
    row = counts.stocktake_of(access.tenant_id, pk)
    if not reads.may_read(access, row):
        raise Refusal("NOT_FOUND", "That count was not found.")
    return row


def _pass_for(access: AccessContext, pk: uuid.UUID) -> GoodsCountPass:
    row = counts.pass_of(access.tenant_id, pk)
    if not reads.may_read_pass(access, row):
        raise Refusal("NOT_FOUND", "That count pass was not found.")
    return row


def _require_reviewer(access: AccessContext, stocktake: GoodsStocktake) -> None:
    if not reads.may_review(access, stocktake):
        raise Refusal("ACTION_DENIED", "Reviewing a count needs `count.review` at its site.")
    if reads.counting_now(access, stocktake):
        raise Refusal(
            "ACTION_DENIED",
            "You are counting in this count. The book stays closed to you until your pass "
            "is submitted.",
        )


def _detail(access: AccessContext, pk: uuid.UUID, history_cursor: str | None = None) -> Any:
    stocktake = counts.stocktake_of(access.tenant_id, pk)
    return reads.count_detail(access, stocktake, timezone.now(), history_cursor)


def _pass_body(access: AccessContext, pk: uuid.UUID) -> Any:
    row = counts.pass_of(access.tenant_id, pk)
    return reads.pass_detail(row, timezone.now(), access.tenant_id)


# ---------------------------------------------------------------------------
# E102 / E159: the counts, and starting one
# ---------------------------------------------------------------------------


class StocktakeListCreateView(GoodsAPIView):
    """The counts at this person's sites, and the route that starts one."""

    http_method_names = ["get", "head", "post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_stocktakes_list",
        description=(
            "E102. Counts at sites where the caller holds `count.run` or `count.review`, "
            "newest first. Query: `site_id`, `state`, `cursor`, `limit` (1-100)."
        ),
        parameters=[
            OpenApiParameter("site_id", str, required=False),
            OpenApiParameter("state", str, required=False, enum=list(GoodsStocktake.State.values)),
            OpenApiParameter("cursor", str, required=False),
            OpenApiParameter("limit", int, required=False),
        ],
        responses=_responses(
            200,
            {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": COUNT_SUMMARY},
                    "next_cursor": {"type": "string", "nullable": True},
                    "as_of": {"type": "string", "format": "date-time"},
                },
                "required": ["items", "next_cursor", "as_of"],
            },
            _READ_REFUSALS,
        ),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, {"site_id", "state", "cursor", "limit"})
        rows = GoodsStocktake.objects.select_related("document", "site").filter(
            tenant_id=access.tenant_id
        )
        if params.get("site_id"):
            rows = rows.filter(site_id=parse_int_id(params["site_id"], "site_id"))
        state = params.get("state") or None
        if state:
            if state not in GoodsStocktake.State.values:
                raise Refusal(
                    "INVALID_REQUEST", f"state is one of {', '.join(GoodsStocktake.State.values)}."
                )
            rows = rows.filter(state=state)
        visible = [
            row for row in rows.order_by("-created_at", "-pk") if reads.may_read(access, row)
        ]
        window, cursor = paginate(visible, params)
        return Response(page(reads.list_rows(access, window, timezone.now()), cursor))

    @extend_schema(
        operation_id="goods_v1_outbound_stocktakes_start",
        description=(
            "E159. Start a blind count at an affirmatively non-trading site (`count.run` "
            "there). In one commit: numbers it CNT, installs the site's count freeze and "
            "freezes the as-of book of every physical piece in scope. The freeze stops "
            "stock movements, dispatch, arrival and receipt dispositions at the site; it "
            "does not stop damage reporting (GSA-R02). Refusals: INVALID_REQUEST, "
            "ACTION_DENIED, NOT_FOUND (site, location or brand), CONTRACT_DISABLED, "
            "SITE_NOT_READY, UNDER_COUNT (another count already freezes the site), "
            "COUNT_ALREADY_OPEN, TRADING_NOT_EXCLUDED (no current approved non-trading "
            "declaration, sell-ready, tills declared or unknown, or trading history), "
            "SERIES_NOT_READY (no CNT numbering), COMMAND_CONFLICT."
        ),
        request={"application/json": START_REQUEST},
        responses=_responses(201, COUNT_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        from masters.models import Store

        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, counts.START_FIELDS, required=("site_id", "scope"))
        site_id, scope = counts.parse_start(body)
        access.require(counts.RUN_ACTION, site_id=site_id)
        site = Store.objects.select_related("gstin").filter(pk=site_id).first()
        if site is None:
            raise Refusal("NOT_FOUND", "That site was not found.")

        def handler(run: CommandRun) -> CommandResult:
            stocktake = counts.start_count(run, site, scope)
            return CommandResult(
                resource_type="stocktake",
                resource_id=str(stocktake.pk),
                status_code=201,
                revision=stocktake.revision,
                document_number=stocktake.document.official_number,
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.count.start",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=site_id,
            subject_key=f"site:{site_id}",
        )
        return Response(
            _detail(access, uuid.UUID(str(result.resource_id))), status=result.status_code
        )


class StocktakeDetailView(GoodsAPIView):
    """E103: one count, its passes, where it stands and its history."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_stocktakes_detail",
        description=(
            "E103. Read by `count.run` or `count.review` at the count's site; anyone else "
            "gets NOT_FOUND. Query: `history_cursor`."
        ),
        parameters=[OpenApiParameter("history_cursor", str, required=False)],
        responses=_responses(200, COUNT_DETAIL, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, {"history_cursor"})
        _stocktake_for(access, pk)
        return Response(_detail(access, pk, params.get("history_cursor") or None))


# ---------------------------------------------------------------------------
# E160-E162 and resume: the blind pass
# ---------------------------------------------------------------------------


class StocktakePassOpenView(GoodsAPIView):
    """E160: open a blind pass, or continue the caller's unfinished one."""

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_stocktakes_passes_open",
        description=(
            "E160. The caller's unfinished pass of this count (200) or a new blind pass "
            "(201) assigned the whole count or one location in it. `count.run` at the "
            "site. Refusals: INVALID_REQUEST, ACTION_DENIED, NOT_FOUND (count, or a "
            "location outside it), COUNT_NOT_OPEN, COMMAND_CONFLICT."
        ),
        request={"application/json": OPEN_REQUEST},
        responses={200: PASS_DETAIL, **_responses(201, PASS_DETAIL, _WRITE_REFUSALS)},
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, counts.OPEN_FIELDS)
        location_id = counts.parse_open(body)
        stocktake = _stocktake_for(access, pk)
        access.require(counts.RUN_ACTION, site_id=stocktake.site_id)

        def handler(run: CommandRun) -> CommandResult:
            row, created = counts.open_pass(run, stocktake.pk, location_id)
            return CommandResult(
                resource_type="count_pass",
                resource_id=str(row.pk),
                status_code=201 if created else 200,
                revision=row.revision,
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.count.pass.open",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=stocktake.site_id,
            subject_key=f"stocktake:{stocktake.pk}",
        )
        return Response(
            _pass_body(access, uuid.UUID(str(result.resource_id))), status=result.status_code
        )


class CountPassDetailView(GoodsAPIView):
    """One pass, for its counter or a reviewer who is not counting here."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_count_sessions_detail",
        description=(
            "A blind pass: its assigned scope and locations, every acknowledged scan with "
            "who made it and when, and its affirmation once submitted. Its own counter or "
            "a `count.review` holder not counting in this count reads it; anyone else "
            "gets NOT_FOUND."
        ),
        responses=_responses(200, PASS_DETAIL, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, frozenset())
        _pass_for(access, pk)
        return Response(_pass_body(access, pk))


class _PassCommandView(GoodsAPIView):
    http_method_names = ["post", "options"]

    def _counter_pass(self, access: AccessContext, pk: uuid.UUID) -> GoodsCountPass:
        row = counts.pass_of(access.tenant_id, pk)
        if row.counter_id != access.human_id or not access.can(
            counts.RUN_ACTION, site_id=row.stocktake.site_id
        ):
            if reads.may_read_pass(access, row):
                raise Refusal("ACTION_DENIED", "Only the person counting this pass may do that.")
            raise Refusal("NOT_FOUND", "That count pass was not found.")
        return row


class CountPassScanView(_PassCommandView):
    """E161: acknowledge scans into the caller's open pass. Nothing moves or is revealed."""

    @extend_schema(
        operation_id="goods_v1_outbound_count_sessions_scan",
        description=(
            "E161. Each observation names where it was counted, the item (a SKU the blind "
            "lookup resolved, or a description for goods nobody can identify), its "
            "condition and quantity, and may correct an earlier scan's quantity. All are "
            "checked before any is kept; a scan key already acknowledged with the same "
            "content counts once (a request of only such replays needs no current "
            "revision). Damaged pieces are recorded as counted damaged; reporting the "
            "damage is the ordinary damage report, which the freeze does not stop "
            "(GSA-R02). Refusals: INVALID_REQUEST, ACTION_DENIED (not this pass's "
            "counter), NOT_FOUND, COUNT_NOT_OPEN, COUNT_STALE (idle 24 hours - resume it "
            "first), REVISION_SUPERSEDED, OBSERVATION_INVALID (LOCATION_OUT_OF_SCOPE, "
            "OUT_OF_RECOUNT_SCOPE, BRAND_OUT_OF_SCOPE, UNKNOWN_SKU, DESCRIPTION_REQUIRED, "
            "UNIDENTIFIED_WITH_SKU, QTY_INVALID, CORRECTION_*), EVENT_TIME_INVALID, "
            "COMMAND_CONFLICT (SCAN_KEY_REUSED)."
        ),
        request={"application/json": SCAN_REQUEST},
        responses=_responses(200, PASS_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, counts.SCAN_FIELDS, required=("observations",))
        row = self._counter_pass(access, pk)

        def handler(run: CommandRun) -> CommandResult:
            observations = counts.parse_scans(body, run.now)
            done = counts.scan(run, row.pk, observations, meta.expected_revision)
            return CommandResult(
                resource_type="count_pass", resource_id=str(done.pk), revision=done.revision
            )

        self.run_command(
            request,
            access=access,
            action="stock.count.pass.scan",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=row.stocktake.site_id,
            subject_key=f"stocktake:{row.stocktake_id}",
        )
        return Response(_pass_body(access, pk))


class CountPassSubmitView(_PassCommandView):
    """E162: submit a pass with the GSA-T17 scope-complete affirmation."""

    @extend_schema(
        operation_id="goods_v1_outbound_count_sessions_submit",
        description=(
            "E162. `scope_complete: true` is the counter's affirmation that the whole "
            "assigned scope was counted, including empty locations (GSA-T17); it is "
            "appended bound to the pass, the scope hash and the exact observation hash "
            "and revision reviewed. False keeps the pass open to continue. Refusals: "
            "INVALID_REQUEST, ACTION_DENIED, NOT_FOUND, COUNT_NOT_OPEN, COUNT_STALE, "
            "REVISION_SUPERSEDED (scans changed since `reviewed_hash`), "
            "COUNT_SESSION_INVALID (SCOPE_NOT_AFFIRMED), COMMAND_CONFLICT."
        ),
        request={"application/json": SUBMIT_REQUEST},
        responses=_responses(200, PASS_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data, counts.SUBMIT_FIELDS, required=("reviewed_hash", "scope_complete")
        )
        reviewed, complete = counts.parse_submit(body)
        row = self._counter_pass(access, pk)

        def handler(run: CommandRun) -> CommandResult:
            done = counts.submit(
                run,
                row.pk,
                reviewed_hash=reviewed,
                scope_complete=complete,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(
                resource_type="count_pass", resource_id=str(done.pk), revision=done.revision
            )

        self.run_command(
            request,
            access=access,
            action="stock.count.pass.submit",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=row.stocktake.site_id,
            subject_key=f"stocktake:{row.stocktake_id}",
            reviewed_hash=reviewed,
        )
        return Response(_pass_body(access, pk))


class CountPassResumeView(GoodsAPIView):
    """Take up a stale pass again, explicitly. Its scans stay and its owned work resolves."""

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_count_sessions_resume",
        description=(
            "A pass idle for 24 hours is stale: it takes no scans and cannot be submitted "
            "until its counter, or a `count.review` holder at the site, resumes it. "
            "Resuming keeps every scan, resolves the pass's `count_session_stale` owned "
            "work and restarts its idle clock; it never submits or completes anything. "
            "Refusals: INVALID_REQUEST, ACTION_DENIED, NOT_FOUND, COUNT_NOT_OPEN, "
            "STATE_CONFLICT (not stale), REVISION_SUPERSEDED, COMMAND_CONFLICT."
        ),
        request={"application/json": RESUME_REQUEST},
        responses=_responses(200, PASS_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, frozenset())
        row = counts.pass_of(access.tenant_id, pk)
        site_id = row.stocktake.site_id
        own = row.counter_id == access.human_id and access.can(counts.RUN_ACTION, site_id=site_id)
        if not own and not reads.may_review(access, row.stocktake):
            if reads.may_read_pass(access, row):
                raise Refusal("ACTION_DENIED", "Only its counter or a reviewer may resume a pass.")
            raise Refusal("NOT_FOUND", "That count pass was not found.")

        def handler(run: CommandRun) -> CommandResult:
            done = counts.resume(run, row.pk, meta.expected_revision)
            return CommandResult(
                resource_type="count_pass", resource_id=str(done.pk), revision=done.revision
            )

        self.run_command(
            request,
            access=access,
            action="stock.count.pass.resume",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=site_id,
            subject_key=f"stocktake:{row.stocktake_id}",
        )
        return Response(_pass_body(access, pk))


# ---------------------------------------------------------------------------
# E163 / E164: the reviewer's variance and a scoped recount
# ---------------------------------------------------------------------------


class StocktakeVarianceView(GoodsAPIView):
    """E163 (quantities only): the submitted passes against the frozen as-of book."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_stocktakes_variance",
        description=(
            "E163 for a `count.review` holder at the site who is not counting in this "
            "count. `pass_ids` (comma-separated) selects submitted passes; by default "
            "every submitted pass. Counting passes must not overlap and must cover every "
            "location in scope, or the variance is incomplete; a selected recount stands "
            "for its own items and locations in place of the pass it replaces, never "
            "summed with it. Lines are paged (`cursor`, `limit` 1-500). Refusals: "
            "INVALID_REQUEST, ACTION_DENIED, NOT_FOUND, COUNT_SELECTION_INVALID."
        ),
        parameters=[
            OpenApiParameter("pass_ids", str, required=False),
            OpenApiParameter("cursor", str, required=False),
            OpenApiParameter("limit", int, required=False),
        ],
        responses=_responses(200, VARIANCE, (*_READ_REFUSALS, 422)),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, {"pass_ids", "cursor", "limit"})
        stocktake = _stocktake_for(access, pk)
        _require_reviewer(access, stocktake)
        raw = params.get("pass_ids") or ""
        selected = (
            [parse_uuid(part.strip(), "pass_ids") for part in raw.split(",") if part.strip()]
            if raw
            else None
        )
        report = counts.variance(stocktake, selected)
        body = reads.variance_detail(stocktake, report, access.tenant_id)
        window, cursor = paginate(body["lines"], params, default=500, maximum=500)
        body["lines"] = {"items": window, "next_cursor": cursor, "total": len(report.lines)}
        return Response(body)


class _ReviewCommandView(GoodsAPIView):
    http_method_names = ["post", "options"]


class StocktakeRecountView(_ReviewCommandView):
    """E164: assign a blind recount of exactly these lines' items and locations."""

    @extend_schema(
        operation_id="goods_v1_outbound_stocktakes_recount",
        description=(
            "E164. A `count.review` holder not counting here names variance `line_keys` "
            "counted in one pass, a counter holding `count.run` at the site with no "
            "unfinished pass here, and a reason. A new blind pass is assigned those "
            "items at those locations, replacing the earlier pass for them only when "
            "selected; every earlier observation stays. Refusals: INVALID_REQUEST "
            "(LINES_FROM_SEVERAL_PASSES), ACTION_DENIED, NOT_FOUND (count or line), "
            "COUNT_NOT_OPEN, REVISION_SUPERSEDED, COUNT_SELECTION_INVALID, "
            "COUNT_SESSION_INVALID (LINE_NOT_COUNTED, COUNTER_NOT_AUTHORISED, "
            "COUNTER_BUSY), COMMAND_CONFLICT."
        ),
        request={"application/json": RECOUNT_REQUEST},
        responses=_responses(201, PASS_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data, counts.RECOUNT_FIELDS, required=("line_keys", "counter_id", "reason_code")
        )
        keys, counter_id, reason = counts.parse_recount(body)
        stocktake = _stocktake_for(access, pk)
        _require_reviewer(access, stocktake)

        def handler(run: CommandRun) -> CommandResult:
            row = counts.request_recount(
                run,
                stocktake.pk,
                line_keys=keys,
                counter_id=counter_id,
                reason_code=reason,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(
                resource_type="count_pass",
                resource_id=str(row.pk),
                status_code=201,
                revision=row.revision,
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.count.recount",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=stocktake.site_id,
            subject_key=f"stocktake:{stocktake.pk}",
        )
        return Response(
            _pass_body(access, uuid.UUID(str(result.resource_id))), status=result.status_code
        )


class StocktakeCloseView(_ReviewCommandView):
    """The zero-variance ending: close and release the freeze, posting nothing (P18)."""

    @extend_schema(
        operation_id="goods_v1_outbound_stocktakes_close",
        description=(
            "Verified zero variance (P18). A `count.review` holder not counting here names "
            "the selected passes and the `variance_hash` reviewed. With no pass still "
            "being counted, a selection counting the whole scope exactly once and every "
            "line matching the frozen book, one commit records the decision and its "
            "selected passes, closes the count and releases the site's freeze - with no "
            "quantity or value posting. A nonzero difference is refused VARIANCE_PENDING "
            "and stays pending, frozen and visible for the difference review and the "
            "Owner's approval (ticket 17A); book stock is never set to a scan total. "
            "Refusals: INVALID_REQUEST, ACTION_DENIED, NOT_FOUND, COUNT_NOT_OPEN, "
            "REVISION_SUPERSEDED, COUNT_SESSION_INVALID (PASS_OPEN), "
            "COUNT_SELECTION_INVALID, VARIANCE_PENDING, COMMAND_CONFLICT."
        ),
        request={"application/json": CLOSE_REQUEST},
        responses=_responses(200, COUNT_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data, counts.CLOSE_FIELDS, required=("reviewed_hash", "selected_pass_ids")
        )
        reviewed, selected = counts.parse_close(body)
        stocktake = _stocktake_for(access, pk)
        _require_reviewer(access, stocktake)

        def handler(run: CommandRun) -> CommandResult:
            done = counts.close_zero(
                run,
                stocktake.pk,
                reviewed_hash=reviewed,
                selected=selected,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(
                resource_type="stocktake", resource_id=str(done.pk), revision=done.revision
            )

        self.run_command(
            request,
            access=access,
            action="stock.count.close",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=stocktake.site_id,
            subject_key=f"stocktake:{stocktake.pk}",
            reviewed_hash=reviewed,
        )
        return Response(_detail(access, pk))


class StocktakeCancelView(_ReviewCommandView):
    """E166: cancel an unfinished count, keeping every observation and lifting the freeze."""

    @extend_schema(
        operation_id="goods_v1_outbound_stocktakes_cancel",
        description=(
            "E166. `count.review` at the site, or `count.run` there for a cycle count. "
            "Appends the cancellation with its reason; every pass, scan and affirmation "
            "stays; no adjustment or posting is made; the site's freeze is released and "
            "stale-pass owned work resolves, all in one commit. Refusals: "
            "INVALID_REQUEST, ACTION_DENIED, NOT_FOUND, COUNT_NOT_OPEN (closed and "
            "cancelled counts are immutable), REVISION_SUPERSEDED, COMMAND_CONFLICT."
        ),
        request={"application/json": CANCEL_REQUEST},
        responses=_responses(200, COUNT_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, counts.CANCEL_FIELDS, required=("reason_code",))
        reason, note = counts.parse_cancel(body)
        stocktake = _stocktake_for(access, pk)
        cycle = (stocktake.scope or {}).get("count_kind") == "cycle"
        if not reads.may_review(access, stocktake) and not (
            cycle and access.can(counts.RUN_ACTION, site_id=stocktake.site_id)
        ):
            raise Refusal(
                "ACTION_DENIED",
                "Cancelling this count needs `count.review` at its site "
                "(or `count.run` for a cycle count).",
            )

        def handler(run: CommandRun) -> CommandResult:
            done = counts.cancel(
                run,
                stocktake.pk,
                reason_code=reason,
                note=note,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(
                resource_type="stocktake", resource_id=str(done.pk), revision=done.revision
            )

        self.run_command(
            request,
            access=access,
            action="stock.count.cancel",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=stocktake.site_id,
            subject_key=f"stocktake:{stocktake.pk}",
        )
        return Response(_detail(access, pk))


# ---------------------------------------------------------------------------
# E211: the blind lookup
# ---------------------------------------------------------------------------


class CountLookupView(GoodsAPIView):
    """E211: what a scanned code is, never how many of it there should be."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_count_lookup",
        description=(
            "E211. `count.run` at an open count's site. Resolves `alias_value` to the SKU "
            "candidates in force at the site - brand, style, size, colour, grade - and "
            "nothing else: no book, on-hand, held, reserved, cost, value or availability "
            "is queried or returned. Extra query parameters are refused. Refusals: "
            "INVALID_REQUEST, ACTION_DENIED, NOT_FOUND, COUNT_NOT_OPEN."
        ),
        parameters=[
            OpenApiParameter("stocktake_id", str, required=True),
            OpenApiParameter("alias_value", str, required=True),
        ],
        responses=_responses(200, LOOKUP, (*_READ_REFUSALS, 409)),
    )
    def get(self, request: Request) -> Response:
        from masters.goods_identity_services import resolve_alias

        access = self.access(request)
        params = check_query(request, {"stocktake_id", "alias_value"})
        stocktake = _stocktake_for(access, parse_uuid(params.get("stocktake_id"), "stocktake_id"))
        access.require(counts.RUN_ACTION, site_id=stocktake.site_id)
        if stocktake.state != GoodsStocktake.State.OPEN:
            raise Refusal("COUNT_NOT_OPEN", "This count is not open.")
        alias = (params.get("alias_value") or "").strip()
        if not 1 <= len(alias) <= 128:
            raise Refusal(
                "INVALID_REQUEST", "alias_value is the scanned code, 1 to 128 characters."
            )
        resolution = resolve_alias(
            access.tenant_id, value=alias, site_id=stocktake.site_id, as_of=timezone.now()
        )
        return Response(resolution.as_dto())
