"""Goods-v1 PT endpoints (E098, E099, E122-E133)."""

from __future__ import annotations

import uuid
from typing import Any, NoReturn

from django.db.models import QuerySet
from django.http import HttpResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    MutationMeta,
    business_body,
    check_query,
    decode_cursor,
    encode_cursor,
    page,
    paginate,
    parse_int_id,
    parse_meta,
    parse_uuid,
    resource_dto,
)
from accounts.principal import AccessContext
from approvals.goods_models import ApprovalRequest
from approvals.goods_services import decide
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.goods_documents import MAX_LINES, lock_heads, revision_lines
from core.goods_history import document_history
from core.kernel_models import DocumentHead, OfficialLine, OfficialVersion
from core.refusals import Refusal
from files.goods_models import EvidenceObject
from files.goods_services import readable_by
from inbound.goods_models import GoodsGrn
from masters.goods_identity_models import GovernanceState, ProductSku
from masters.goods_models import Sbu, SiteGuard
from masters.models import Brand
from ptmapper import goods_manifest_services as manifest_services
from ptmapper import goods_pt_services as pts
from ptmapper.goods_models import GoodsPt
from ptmapper.goods_paste import CELL_TEXT, PASTE_COLUMNS
from ptmapper.goods_pt_export import CanonicalCells, export_columns
from ptmapper.goods_workbook import Amount, workbook_bytes

LINE_PAGE = 500
READ_ACTIONS = (
    "pt.view",
    "pt.prepare",
    "pt.prepare.opening",
    "pt.approve.receipt",
    "pt.reversal.request",
    "pt.reversal.approve",
)
COST_SUPPLIED = frozenset({"basic_paise", "check_p_rate_paise"})
COST_CALCULATED = frozenset(
    {"p_rate_paise", "basic_paise", "margin_pct", "pricing_margin_pct", "transport_pct"}
)
ALLOWED_ACTIONS = {
    "draft": ["rows", "price", "rerun", "send"],
    "submitted": ["rows", "price", "rerun", "recall", "post"],
    "official": ["reverse", "export"],
    "reversed": ["reissue", "export"],
}
#: The two correction actions above are narrowed to the ones this caller could
#: actually run, so no screen offers a *correction* control that is known to answer
#: ``ACTION_DENIED`` (GSA-T06). ``reissue`` follows the PT's own purpose, exactly as
#: the reissue command does, so an opening PT offers it to an opening preparer only.
#: The draft and submitted entries are still answered on state alone; narrowing them
#: belongs to tickets 06/06A, not here.
GATED_ACTIONS = frozenset({"reverse", "reissue"})
#: What ``GoodsPtReverseView`` requires, named once so ``_gate`` cannot drift from it.
REVERSAL_REQUEST = "pt.reversal.request"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ---------------------------------------------------------------------------
# Scope and field grants
# ---------------------------------------------------------------------------


def _brand_id(goods_pt: GoodsPt) -> int | None:
    return goods_pt.grn.arrival.brand_id if goods_pt.grn is not None else None


def _has_read_action(access: AccessContext) -> bool:
    actions = access.all_actions()
    return any(action in actions for action in READ_ACTIONS)


def _can_read(access: AccessContext, site_id: int | None, brand_id: int | None = None) -> bool:
    return any(access.can(a, site_id=site_id, brand_id=brand_id) for a in READ_ACTIONS)


def _can_read_pt(access: AccessContext, goods_pt: GoodsPt) -> bool:
    return bool(access.covers_all(READ_ACTIONS, pts.pt_cells(goods_pt)))


def _prepare_action(goods_pt: GoodsPt) -> str:
    """Preparer authority follows the PT's own purpose: opening drafts need
    ``pt.prepare.opening``, receipt drafts ``pt.prepare``. The one statement of the
    rule — the preparer commands take it through ``_ByPurpose``, and ``_gate`` takes
    it for ``reissue``, so a screen can never offer what a command would refuse."""
    return "pt.prepare.opening" if _is_opening(goods_pt) else "pt.prepare"


def _gate(goods_pt: GoodsPt, action: str) -> str:
    """The authority ``action`` needs over this PT, taken from what its command requires."""
    return REVERSAL_REQUEST if action == "reverse" else _prepare_action(goods_pt)


def _offered(
    access: AccessContext,
    goods_pt: GoodsPt,
    state: str,
    cells: frozenset[tuple[int | None, int | None]],
) -> list[str]:
    """This state's actions, less the correction ones this caller cannot run (GSA-T06).

    The test is ``require_complete_scope``'s own: the action over every cell of the
    PT, so a partially authorised person is offered nothing rather than a control
    that refuses when pressed.
    """

    def runnable(action: str) -> bool:
        if action not in GATED_ACTIONS:
            return True
        needed = _gate(goods_pt, action)
        return bool(access.holds(needed) and access.covers_all({needed}, cells))

    return [action for action in ALLOWED_ACTIONS.get(state, []) if runnable(action)]


def _load(access: AccessContext, pk: uuid.UUID) -> GoodsPt:
    if not _has_read_action(access):
        raise Refusal("ACTION_DENIED", "You do not have permission to read PTs.")
    goods_pt = (
        GoodsPt.objects.select_related("document", "grn", "grn__document", "grn__arrival")
        .filter(tenant_id=access.tenant_id, document_id=pk)
        .first()
    )
    if goods_pt is None or not _can_read_pt(access, goods_pt):
        raise Refusal("NOT_FOUND", "That PT was not found.")
    return goods_pt


def _shows_cost(
    access: AccessContext,
    goods_pt: GoodsPt,
    cells: frozenset[tuple[int | None, int | None]] | None = None,
) -> bool:
    """Cost on every row, or on none: each cell needs a reading grant carrying the field.

    A caller that already has the PT's cells passes them; ``pt_cells`` is three
    queries, and a detail read needs the same answer for cost and for the actions
    it may offer."""
    cells = pts.pt_cells(goods_pt) if cells is None else cells
    if any(access.covers_all(READ_ACTIONS, cells, {name}) for name in ("cost", "margin")):
        return True
    return goods_pt.preparer_id == access.human_id and bool(
        access.covers_all(READ_ACTIONS, cells, {"cost_own_pt"})
    )


def _redact(line: dict[str, Any], shows_cost: bool) -> dict[str, Any]:
    """Cost and margin cells are omitted, never null-filled, without the field grant."""
    if shows_cost:
        return dict(line)
    out = dict(line)
    supplied = line.get("supplied") or {}
    calculated = line.get("calculated") or {}
    out["supplied"] = {k: v for k, v in supplied.items() if k not in COST_SUPPLIED}
    out["calculated"] = {k: v for k, v in calculated.items() if k not in COST_CALCULATED}
    return out


def _positive_int(params: dict[str, str], key: str) -> int | None:
    raw = params.get(key)
    if raw in (None, ""):
        return None
    try:
        value = int(str(raw))
    except ValueError:
        raise Refusal("INVALID_REQUEST", f"{key} must be a positive integer.") from None
    if value < 1:
        raise Refusal("INVALID_REQUEST", f"{key} must be a positive integer.")
    return value


# ---------------------------------------------------------------------------
# ResourceDTO<PtDetailDTO>
# ---------------------------------------------------------------------------


def _content(
    goods_pt: GoodsPt, head: DocumentHead, version_no: int | None
) -> tuple[dict[str, Any], list[dict[str, Any]], str, int | None]:
    if version_no is not None:
        version = OfficialVersion.objects.filter(
            document_id=goods_pt.document_id, version=version_no
        ).first()
        if version is None:
            raise Refusal("VERSION_NOT_FOUND", "That version does not exist.", status=404)
        lines = [
            # `official_line_id` (ticket 11): the row's own id, never part of the
            # frozen payload itself - a print job needs exactly this to name the
            # line it printed (E182 `lines[].official_line_id`), and nothing else
            # this detail read returns carries it.
            {**line.payload, "official_line_id": str(line.pk)}
            for line in OfficialLine.objects.filter(version=version).order_by("line_no")
        ]
        return dict(version.canonical_payload), lines, version.content_hash, version.version
    live = head.live_version.version if head.live_version is not None else None
    revision = head.draft_revision
    if revision is None:
        return {}, [], "", live
    lines = [
        dict(state.payload) for state in revision_lines(goods_pt.document_id, revision.revision)
    ]
    return dict(revision.payload), lines, revision.content_hash, live


def _line_item(
    line: dict[str, Any],
    reviews: dict[str, Any],
    shows_cost: bool,
    grid: _GridRead | None = None,
) -> dict[str, Any]:
    item = _redact(line, shows_cost)
    digest = pts.row_hash(line)
    item["reviewed"] = reviews.get(str(line.get("line_key")), {}).get("row_hash") == digest
    if shows_cost:
        item["row_hash"] = digest
    if grid is not None:
        grid.add(item, line)
    return item


class _GridRead:
    """What the PT grid shows beside a line's own values (OPS-16), read once per page.

    ``cells`` are the line's twenty-two KDPS values as the canonical export resolves
    them - describing values from the line, else its item - so the grid never works
    one out itself; cost cells are left out as the export leaves them out.
    ``item_pending`` marks a line whose item is a proposal still waiting for the
    product-master owner (store and warehouse operations PRD §5.4); ``item_rejected``
    carries the owner's reason when they rejected it (OPS-17A), and the row stays
    blocked until the preparer gives it another item.
    """

    def __init__(self, tenant_id: uuid.UUID, lines: list[dict[str, Any]], shows_cost: bool):
        self.cells = CanonicalCells(tenant_id, lines)
        self.keys = [column["key"] for column in export_columns(shows_cost)]
        states = dict(
            ProductSku.objects.filter(
                tenant_id=tenant_id,
                pk__in=sorted({str(line["sku_id"]) for line in lines if line.get("sku_id")}),
            )
            .exclude(governance_state=GovernanceState.EFFECTIVE)
            .exclude(originating_revision__isnull=True)
            .values_list("pk", "governance_state")
        )
        self.pending = {str(pk) for pk, state in states.items() if state == GovernanceState.PENDING}
        self.rejected = rejected_proposals(
            tenant_id, [pk for pk, state in states.items() if state == GovernanceState.RETIRED]
        )

    def add(self, item: dict[str, Any], line: dict[str, Any]) -> None:
        resolved = self.cells.cells(line)
        item["cells"] = {key: resolved.get(key) for key in self.keys}
        item["item_pending"] = str(line.get("sku_id")) in self.pending
        rejected = self.rejected.get(str(line.get("sku_id")))
        item["item_rejected"] = {"reason_code": rejected} if rejected is not None else None


def rejected_proposals(tenant_id: uuid.UUID, sku_ids: list[Any]) -> dict[str, str]:
    """Each proposed SKU the product-master owner rejected, with the reason they gave."""
    from approvals.goods_models import ApprovalDecision

    if not sku_ids:
        return {}
    keys = {f"sku:{pk}": str(pk) for pk in sku_ids}
    out: dict[str, str] = {}
    for key, reason in (
        ApprovalDecision.objects.filter(
            request__tenant_id=tenant_id,
            request__subject_kind="master_proposal",
            request__subject_key__in=sorted(keys),
            outcome=ApprovalDecision.Outcome.REJECTED,
        )
        .order_by("recorded_at")
        .values_list("request__subject_key", "reason_code")
    ):
        out[keys[key]] = reason or "REJECTED"
    return out


#: OPS-15 (store and warehouse operations PRD §5.4): what the grid reads beside a
#: line's values. Present on lines a brand file filled or a person edited.
LINE_ORIGINS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Where each KDPS column's value came from, keyed by the column's PRD name "
        "(SEASON, BRAND, COLOR, ... P RATE): `file` (the brand file's own value), `rule` "
        "(a confirmed rule or the product's clean-up), `suggestion` (close matches wait in "
        "`suggestions`; no value is applied), `person` (a person's edit) or `none`."
    ),
    "additionalProperties": {
        "type": "string",
        "enum": ["file", "rule", "suggestion", "person", "none"],
    },
}
LINE_SUGGESTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Per KDPS column whose origin is `suggestion`: the file's own text and the close "
        "matches offered for it. Never applied until a person picks one through the row edit."
    ),
    "additionalProperties": {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "choices": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "value_id": {"type": "string"},
                        "value": {"type": "string"},
                        "label": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    },
}
LINE_DESCRIBING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "The row's brand and design, as stated (a drafted new item has no SKU).",
    "properties": {
        "brand_id": {"type": "integer", "nullable": True},
        "design": {"type": "string", "nullable": True},
    },
}
LINE_MATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "How the row's item was found - by `barcode`, by `describing_values`, by a "
        "`person`, `ambiguous` (candidates listed, none chosen) or `new_item` (no SKU; "
        "propose it) - and the server's notes, which also appear in `issues`."
    ),
    "properties": {
        "by": {
            "type": "string",
            "nullable": True,
            "enum": ["barcode", "describing_values", "new_item", "ambiguous", "person", None],
        },
        "candidates": {"type": "array", "items": {"type": "string", "format": "uuid"}},
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "field": {"type": "string"},
                },
            },
        },
    },
}


LINE_CELLS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The line's KDPS values as the canonical export resolves them (OPS-16), keyed as "
        "the export's columns are (season, brand, colour, ... suggested_type). Money is "
        "integer paise text; a blank is null. Cost keys appear only to a caller who "
        "sees cost."
    ),
    "additionalProperties": {"nullable": True},
}


#: E099's answer (#303). The PT detail is a hand-written closed DTO rather than a
#: serializer, so its shape is stated here rather than introspected. It used to
#: share its path template with a legacy integer route (deleted in OPS-18), so
#: drf-spectacular emitted only the legacy description; the goods route now has the
#: namespace to itself and this block is the emitted contract.
PT_DETAIL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "ResourceDTO<PtDetailDTO> with independently paged lines and history.",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer"},
        "content_hash": {"type": "string"},
        "state": {"type": "string"},
        "number": {"type": "string", "nullable": True},
        "version": {"type": "integer", "nullable": True},
        "allowed_actions": {"type": "array", "items": {"type": "string"}},
        "context": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "draft_revision_id": {
                    "type": "string",
                    "format": "uuid",
                    "nullable": True,
                    "description": (
                        "The current draft revision: what an item proposal made while "
                        "preparing this PT cites as `originating_revision_id` (OPS-15)."
                    ),
                },
            },
        },
        "history_cursor": {
            "type": "string",
            "nullable": True,
            "description": "Cursor for the next history page; null on the last page.",
        },
        "as_of": {"type": "string", "format": "date-time"},
        "data": {
            "type": "object",
            "properties": {
                "header": {"type": "object", "additionalProperties": True},
                "lines": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "official_line_id": {
                                        "type": "string",
                                        "format": "uuid",
                                        "description": (
                                            "The official line row's own id, returned only "
                                            "for an explicit `?version=` read (design §5.8). "
                                            "Never part of the frozen canonical payload; a "
                                            "print job names the line it prints by this id."
                                        ),
                                    },
                                    "origins": LINE_ORIGINS_SCHEMA,
                                    "suggestions": LINE_SUGGESTIONS_SCHEMA,
                                    "describing": LINE_DESCRIBING_SCHEMA,
                                    "match": LINE_MATCH_SCHEMA,
                                    "cells": LINE_CELLS_SCHEMA,
                                    "item_pending": {
                                        "type": "boolean",
                                        "description": (
                                            "The line's item is a proposal still waiting for "
                                            "the product-master owner's confirmation (OPS-16)."
                                        ),
                                    },
                                    "item_rejected": {
                                        "type": "object",
                                        "nullable": True,
                                        "description": (
                                            "The line's item was a proposal the product-master "
                                            "owner rejected, with their reason (OPS-17A)."
                                        ),
                                        "properties": {"reason_code": {"type": "string"}},
                                    },
                                },
                                # The rest of a line is its frozen payload, whose
                                # shape is the PT's own vocabulary, not a fixed DTO.
                                "additionalProperties": True,
                            },
                        },
                        "next_cursor": {"type": "string", "nullable": True},
                        "total": {"type": "integer"},
                    },
                },
                "history": {
                    "type": "object",
                    "description": "At most 50 events, newest first (design §5.8).",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "format": "uuid"},
                                    "kind": {"type": "string"},
                                    "actor_id": {"type": "string", "nullable": True},
                                    "recorded_at": {"type": "string", "format": "date-time"},
                                    "revision": {"type": "integer", "nullable": True},
                                    "outcome": {"type": "string", "nullable": True},
                                    "reason_code": {"type": "string", "nullable": True},
                                    "evidence_ids": {
                                        "type": "array",
                                        "items": {"type": "string", "format": "uuid"},
                                    },
                                    "related_document_id": {"type": "string", "nullable": True},
                                },
                            },
                        },
                        "next_history_cursor": {"type": "string", "nullable": True},
                    },
                },
            },
        },
    },
}


#: The one refusal envelope every goods-v1 route answers with (design §6.1).
REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
    },
}


_READ_REFUSALS = (400, 401, 403, 404)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


_UUID_FIELD: dict[str, Any] = {"type": "string", "format": "uuid"}
_TEXT_FIELD: dict[str, Any] = {"type": "string"}


def _mutation_request(
    fields: dict[str, Any], *, required: tuple[str, ...] = (), revision_bound: bool = True
) -> dict[str, Any]:
    """The exact meta keys plus a route's admitted business fields."""
    required_keys = ["command_id", "contract_version"]
    if revision_bound:
        required_keys.append("expected_revision")
    required_keys.extend(required)
    return {
        "type": "object",
        "required": required_keys,
        "additionalProperties": False,
        "properties": {
            "command_id": _UUID_FIELD,
            "contract_version": {"type": "string", "enum": ["goods-v1"]},
            "expected_revision": {"type": "integer", "minimum": 1},
            **fields,
        },
    }


#: One row of E098's PT worklist: the document's identity and where it has got
#: to, never its lines or its money.
PT_LIST_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "PtListItemDTO (E098).",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "kind": {"type": "string"},
        "purpose": {"type": "string", "nullable": True},
        "number": {"type": "string", "nullable": True},
        "state": {"type": "string"},
        "site_id": {"type": "string"},
        "brand_id": {"type": "string", "nullable": True},
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "owner_role": {"type": "string", "nullable": True},
        "due_at": {"type": "string", "nullable": True},
    },
}

PT_LIST_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "Page<PtListItemDTO>, newest first.",
    "properties": {
        "items": {"type": "array", "items": PT_LIST_ITEM},
        "next_cursor": {"type": "string", "nullable": True},
        "as_of": {"type": "string", "format": "date-time"},
    },
}


def pt_resource(
    access: AccessContext, goods_pt: GoodsPt, params: dict[str, str] | None = None
) -> dict[str, Any]:
    params = params or {}
    head = DocumentHead.objects.select_related("draft_revision", "live_version").get(
        document_id=goods_pt.document_id
    )
    version_no = _positive_int(params, "version")
    header, all_lines, content, version = _content(goods_pt, head, version_no)
    offset = decode_cursor(params.get("line_cursor"))
    window = all_lines[offset : offset + LINE_PAGE]
    reviews = pts.reviewed_map(head) if version_no is None else {}
    cells = pts.pt_cells(goods_pt)
    shows_cost = _shows_cost(access, goods_pt, cells)
    more = offset + LINE_PAGE < len(all_lines)
    history = document_history(goods_pt.document_id, params.get("history_cursor"))
    grid = _GridRead(access.tenant_id, window, shows_cost)
    data = {
        "header": header,
        "lines": {
            "items": [_line_item(line, reviews, shows_cost, grid) for line in window],
            "next_cursor": encode_cursor(offset + LINE_PAGE) if more else None,
            "total": len(all_lines),
        },
        "history": history.as_dto(),
    }
    resource = resource_dto(
        id=goods_pt.document_id,
        data=data,
        revision=head.revision,
        state=head.state,
        number=goods_pt.document.official_number,
        version=version,
        context={
            "site_id": goods_pt.document.site_id,
            "entity_id": goods_pt.document.entity_id,
            "brand_id": _brand_id(goods_pt),
            # GSA-T07: the live OfficialVersion id — what E139 needs as
            # `source_version_id` to open an acceptance session. The document
            # detail otherwise names only the version *number* (`version`
            # above), never this id, and a receiver (`stock.accept`) holds no
            # `pt.view` to read anything else that would carry it.
            "official_version_id": str(head.live_version_id) if head.live_version_id else None,
        },
        allowed_actions=_offered(access, goods_pt, head.state, cells),
        history_cursor=history.next_cursor,
        content={"hash": content},
    )
    # OPS-15: the draft revision a preparer's item proposal cites as its
    # `originating_revision_id` (decision I2: later revisions of this draft may use
    # the proposal; no other document may).
    resource["context"]["draft_revision_id"] = (
        str(head.draft_revision_id) if head.draft_revision_id else None
    )
    if content:
        resource["content_hash"] = content
    return resource


# ---------------------------------------------------------------------------
# E098 list, E122 create, E123 prefill, E099 detail
# ---------------------------------------------------------------------------


def _filtered(
    access: AccessContext, queryset: QuerySet[GoodsPt], params: dict[str, str]
) -> QuerySet[GoodsPt]:
    site_id = parse_int_id(params["site_id"], "site_id") if params.get("site_id") else None
    brand_id = parse_int_id(params["brand_id"], "brand_id") if params.get("brand_id") else None
    if site_id is not None:
        known = SiteGuard.objects.filter(tenant_id=access.tenant_id, site_id=site_id).exists()
        if not known or not any(access.can_reach_site(a, site_id) for a in READ_ACTIONS):
            raise Refusal("NOT_FOUND", "That site was not found.")
        queryset = queryset.filter(document__site_id=site_id)
    if brand_id is not None:
        # An explicit filter outside scope, or naming nothing, is hidden the same way.
        # One grant must reach the brand (and the filtered site, when there is one).
        reaches = any(
            grant.actions & set(READ_ACTIONS)
            and access.reaches_brand(grant, brand_id)
            and (site_id is None or access.reaches_site(grant, site_id))
            for grant in access.grants
        )
        if not Brand.objects.filter(pk=brand_id).exists() or not reaches:
            raise Refusal("NOT_FOUND", "That brand was not found.")
        queryset = queryset.filter(grn__arrival__brand_id=brand_id)
    if params.get("sbu_id"):
        sbu = Sbu.objects.filter(
            tenant_id=access.tenant_id, pk=parse_uuid(params["sbu_id"], "sbu_id")
        ).first()
        if sbu is None or not _can_read(access, sbu.site_id, sbu.brand_id):
            raise Refusal("NOT_FOUND", "That SBU was not found.")
        queryset = queryset.filter(document__site_id=sbu.site_id)
        if sbu.brand_id is not None:
            queryset = queryset.filter(grn__arrival__brand_id=sbu.brand_id)
    query = params.get("q") or ""
    if len(query) > 100:
        raise Refusal("INVALID_REQUEST", "q is at most 100 characters.")
    if query:
        queryset = queryset.filter(document__official_number__icontains=query)
    return queryset


def _summary(goods_pt: GoodsPt) -> dict[str, Any]:
    head = goods_pt.document.head
    brand_id = _brand_id(goods_pt)
    return {
        "id": str(goods_pt.document_id),
        "record_contract": "goods-v1",
        "kind": goods_pt.document.kind,
        "purpose": goods_pt.document.purpose,
        "number": goods_pt.document.official_number,
        "state": head.state,
        "site_id": str(goods_pt.document.site_id),
        "brand_id": str(brand_id) if brand_id is not None else None,
        "created_at": goods_pt.created_at.isoformat(),
        "updated_at": head.updated_at.isoformat(),
        "owner_role": "C-INV" if head.state == DocumentHead.State.SUBMITTED else "C-WHO",
        "due_at": None,
    }


class GoodsPtListView(GoodsAPIView):
    """E098 list and E122 create."""

    @extend_schema(responses=_responses(200, PT_LIST_RESPONSE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        if not _has_read_action(access):
            raise Refusal("ACTION_DENIED", "You do not have permission to read PTs.")
        queryset = GoodsPt.objects.select_related(
            "document", "document__head", "grn", "grn__arrival"
        ).filter(tenant_id=access.tenant_id)
        rows = [
            goods_pt
            for goods_pt in _filtered(access, queryset, params).order_by("-created_at", "pk")
            if _can_read_pt(access, goods_pt)
        ]
        window, cursor = paginate(rows, params)
        return Response(page([_summary(goods_pt) for goods_pt in window], cursor))

    @extend_schema(
        request={"application/json": _mutation_request(
            {
                "purpose": {"type": "string", "enum": ["receipt", "opening"]},
                "receipt_kind": {"type": "string", "enum": ["primary", "supplement"]},
                "grn_id": _UUID_FIELD,
                "manifest_version_id": _UUID_FIELD,
                "profile_version_id": _UUID_FIELD,
                "direction": _TEXT_FIELD,
                "source": {"type": "string", "enum": ["typed", "canonical_upload", "brand_upload"]},
                "evidence_id": _UUID_FIELD,
                "lines": {"type": "array", "maxItems": MAX_LINES, "items": {"type": "object"}},
            },
            required=("purpose", "profile_version_id", "direction", "source"),
            revision_bound=False,
        )},
        responses=_responses(201, PT_DETAIL_RESPONSE, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {
                "purpose",
                "receipt_kind",
                "grn_id",
                "manifest_version_id",
                "profile_version_id",
                "direction",
                "source",
                "evidence_id",
                "lines",
            },
            required=["purpose", "profile_version_id", "direction", "source"],
        )
        if body["purpose"] not in ("receipt", "opening") or body["source"] not in (
            "typed",
            "canonical_upload",
            "brand_upload",
        ):
            raise Refusal("INVALID_REQUEST", "purpose or source is not a supported value.")
        if body["purpose"] == "opening":
            _refuse_opening(access, body)
        grn_id = parse_uuid(body.get("grn_id"), "grn_id")
        return _create(self, request, access, meta, body, grn_id, prefill=False)


#: Actions that let a person see a GRN or opening parent before the prepare grant is checked.
RECEIPT_PARENT_READERS = ("pt.prepare", "receive.arrival", *READ_ACTIONS)
OPENING_PARENT_READERS = ("pt.prepare.opening", *READ_ACTIONS)


def _grn_in_scope(access: AccessContext, grn_id: uuid.UUID, readers: tuple[str, ...]) -> GoodsGrn:
    """E122/E123 step 4: a missing or out-of-scope GRN is hidden the same way."""
    grn = (
        GoodsGrn.objects.select_related("document", "arrival")
        .filter(tenant_id=access.tenant_id, document_id=grn_id)
        .first()
    )
    if grn is None or not any(
        access.can(a, site_id=grn.document.site_id, brand_id=grn.arrival.brand_id) for a in readers
    ):
        if not any(a in access.all_actions() for a in readers):
            raise Refusal("ACTION_DENIED", "You do not have permission to prepare PTs.")
        raise Refusal("NOT_FOUND", "That GRN was not found.")
    return grn


def _refuse_opening(access: AccessContext, body: dict[str, Any]) -> NoReturn:
    """Opening PTs are prepared from an approved opening manifest, which this route cannot use.

    The refusal keeps the shared order: parents resolve in scope (NOT_FOUND), the
    opening prepare grant is enforced (ACTION_DENIED), and only then is the parent
    itself refused (PT_PARENT_INVALID).
    """
    if not any(action in access.all_actions() for action in OPENING_PARENT_READERS):
        raise Refusal("ACTION_DENIED", "You do not have permission to prepare opening PTs.")
    site_id: int | None = None
    brand_id: int | None = None
    if body.get("grn_id"):
        grn = _grn_in_scope(access, parse_uuid(body["grn_id"], "grn_id"), OPENING_PARENT_READERS)
        site_id, brand_id = grn.document.site_id, grn.arrival.brand_id
    if body.get("manifest_version_id"):
        manifest_id = parse_uuid(body["manifest_version_id"], "manifest_version_id")
        manifest = (
            OfficialVersion.objects.select_related("document")
            .filter(tenant_id=access.tenant_id, pk=manifest_id)
            .first()
        )
        if manifest is None or not any(
            access.can(a, site_id=manifest.document.site_id) for a in OPENING_PARENT_READERS
        ):
            raise Refusal("NOT_FOUND", "That opening manifest was not found.")
        site_id = site_id or manifest.document.site_id
    access.require("pt.prepare.opening", site_id=site_id, brand_id=brand_id)
    raise Refusal(
        "PT_PARENT_INVALID",
        "An opening PT is prepared from its approved opening manifest, not from this route.",
        status=422,
    )


def _source_evidence(
    access: AccessContext, body: dict[str, Any], grn: GoodsGrn
) -> uuid.UUID | None:
    """Only real evidence the caller may read, declared for the GRN's site, becomes PT source."""
    if not body.get("evidence_id"):
        return None
    evidence_id = parse_uuid(body["evidence_id"], "evidence_id")
    evidence = EvidenceObject.objects.filter(tenant_id=access.tenant_id, pk=evidence_id).first()
    declared = (
        {int(site) for site in (evidence.scope or {}).get("site_ids") or []}
        if evidence is not None
        else set()
    )
    if (
        evidence is None
        or not readable_by(access, evidence)
        or (declared and grn.document.site_id not in declared)
    ):
        raise Refusal("NOT_FOUND", "That evidence file was not found.")
    return evidence_id


def _source_lines(
    access: AccessContext, body: dict[str, Any], grn: GoodsGrn, *, prefill: bool, receipt_kind: str
) -> list[Any] | None:
    if prefill:
        return None
    lines = body.get("lines")
    if body.get("source") == "canonical_upload":
        from ptmapper.goods_canonical import lines_from_evidence

        if lines is not None:
            raise Refusal("INVALID_REQUEST", "A canonical upload takes its lines from the file.")
        if not body.get("evidence_id"):
            raise Refusal("PT_FILE_INVALID", "A canonical upload needs its workbook.", status=422)
        return lines_from_evidence(
            access,
            parse_uuid(body["evidence_id"], "evidence_id"),
            grn,
            receipt_kind,
            body.get("profile_version_id"),
        )
    if body.get("source") == "brand_upload":
        from ptmapper.goods_brand_intake import lines_from_brand_file

        if lines is not None:
            raise Refusal("INVALID_REQUEST", "A brand file upload takes its lines from the file.")
        if not body.get("evidence_id"):
            raise Refusal("PT_FILE_INVALID", "A brand file upload needs its file.", status=422)
        return lines_from_brand_file(
            access,
            parse_uuid(body["evidence_id"], "evidence_id"),
            grn,
            receipt_kind,
            body.get("profile_version_id"),
        )
    if lines is None:
        return []
    if not isinstance(lines, list) or len(lines) > MAX_LINES:
        raise Refusal("INVALID_REQUEST", "lines must be a list of at most 50,000 rows.")
    # Where a value came from, the waiting suggestions and how the item was found are
    # the server's to record; typed lines never supply them (OPS-15).
    return [
        {k: v for k, v in line.items() if k not in pts.INTAKE_KEYS}
        if isinstance(line, dict)
        else line
        for line in lines
    ]


def _create(
    view: GoodsAPIView,
    request: Request,
    access: AccessContext,
    meta: MutationMeta,
    body: dict[str, Any],
    grn_id: uuid.UUID,
    *,
    prefill: bool,
) -> Response:
    receipt_kind = str(body.get("receipt_kind") or "primary")
    if receipt_kind not in ("primary", "supplement"):
        raise Refusal("INVALID_REQUEST", "receipt_kind must be primary or supplement.")
    grn = _grn_in_scope(access, grn_id, RECEIPT_PARENT_READERS)
    evidence_id = _source_evidence(access, body, grn)
    access.require("pt.prepare", site_id=grn.document.site_id, brand_id=grn.arrival.brand_id)
    if body.get("manifest_version_id"):
        raise Refusal(
            "PT_PARENT_INVALID",
            "A receipt PT has its GRN as parent, never an opening manifest.",
            status=422,
        )
    lines = _source_lines(access, body, grn, prefill=prefill, receipt_kind=receipt_kind)

    def handler(run: CommandRun) -> CommandResult:
        profile = pts.load_profile(run.tenant_id, body["profile_version_id"], pts.pt_target(grn))
        identity, _head, _goods_pt = pts.create_receipt_draft(
            run,
            grn=grn,
            receipt_kind=receipt_kind,
            profile=profile,
            direction=str(body["direction"]),
            lines=lines,
            source=str(body.get("source") or "typed"),
            evidence_id=evidence_id,
            duplicate_code="PT_PARENT_INVALID" if prefill else "PRIMARY_EXISTS",
        )
        return CommandResult(resource_type="pt", resource_id=str(identity.pk), status_code=201)

    result = view.run_command(
        request,
        access=access,
        action="pt.prefill" if prefill else "pt.create",
        meta=meta,
        business_input=body,
        handler=handler,
        resource_ids=[str(grn_id)],
        site_id=grn.document.site_id,
        subject_key=f"grn:{grn_id}",
    )
    goods_pt = _load(access, uuid.UUID(str(result.resource_id)))
    return Response(pt_resource(access, goods_pt), status=result.status_code)


class GoodsPtFromGrnView(GoodsAPIView):
    """E123: the same draft command, prefilled from the GRN's counted lots."""

    @extend_schema(
        request={"application/json": _mutation_request(
            {
                "receipt_kind": {"type": "string", "enum": ["primary", "supplement"]},
                "profile_version_id": _UUID_FIELD,
                "direction": _TEXT_FIELD,
            },
            required=("profile_version_id", "direction"),
            revision_bound=False,
        )},
        responses=_responses(201, PT_DETAIL_RESPONSE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, grn_id: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"receipt_kind", "profile_version_id", "direction"},
            required=["profile_version_id", "direction"],
        )
        return _create(self, request, access, meta, body, grn_id, prefill=True)


class GoodsPtFromManifestView(GoodsAPIView):
    """The from-manifest analogue of E123: an opening PT draft prefilled 1:1 from
    every accepted row of an approved opening manifest. Opening purpose is
    refused everywhere on the general create route (R20); this is its only
    route into existence."""

    @extend_schema(
        request={"application/json": _mutation_request(
            {"profile_version_id": _UUID_FIELD, "evidence_id": _UUID_FIELD},
            required=("profile_version_id",),
            revision_bound=False,
        )},
        responses=_responses(201, PT_DETAIL_RESPONSE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, manifest_id: uuid.UUID) -> Response:
        from ptmapper.goods_models import OpeningManifest

        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data, {"profile_version_id", "evidence_id"}, required=["profile_version_id"]
        )
        manifest = OpeningManifest.objects.filter(
            tenant_id=access.tenant_id, pk=manifest_id
        ).first()
        if manifest is None or not any(
            access.can(a, site_id=manifest.site_id) for a in OPENING_PARENT_READERS
        ):
            if not any(a in access.all_actions() for a in OPENING_PARENT_READERS):
                raise Refusal("ACTION_DENIED", "You do not have permission to prepare opening PTs.")
            raise Refusal("NOT_FOUND", "That opening manifest was not found.")
        access.require("pt.prepare.opening", site_id=manifest.site_id)
        evidence_id = _source_evidence_id(access, body)

        def handler(run: CommandRun) -> CommandResult:
            identity, _head, _goods_pt = manifest_services.create_opening_draft(
                run,
                manifest=manifest,
                profile_version_id=body["profile_version_id"],
                evidence_id=evidence_id,
            )
            return CommandResult(resource_type="pt", resource_id=str(identity.pk), status_code=201)

        result = self.run_command(
            request,
            access=access,
            action="pt.prepare.opening",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(manifest_id)],
            site_id=manifest.site_id,
            subject_key=f"manifest:{manifest_id}",
        )
        goods_pt = _load(access, uuid.UUID(str(result.resource_id)))
        return Response(pt_resource(access, goods_pt), status=result.status_code)


def _source_evidence_id(access: AccessContext, body: dict[str, Any]) -> uuid.UUID | None:
    if not body.get("evidence_id"):
        return None
    evidence_id = parse_uuid(body["evidence_id"], "evidence_id")
    evidence = EvidenceObject.objects.filter(tenant_id=access.tenant_id, pk=evidence_id).first()
    if evidence is None or not readable_by(access, evidence):
        raise Refusal("NOT_FOUND", "That evidence file was not found.")
    return evidence_id


class GoodsPtDetailView(GoodsAPIView):
    """E099."""

    @extend_schema(
        operation_id="goods_v1_ptmapper_files_detail",
        parameters=[
            OpenApiParameter("version", int, description="An official version number."),
            OpenApiParameter("line_cursor", str, description="Opaque cursor for the next lines."),
            OpenApiParameter(
                "history_cursor",
                str,
                description="Opaque cursor for the next history page. Independent of line_cursor.",
            ),
        ],
        responses={
            200: PT_DETAIL_RESPONSE,
            400: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, {"version", "line_cursor", "history_cursor"})
        if len(params.get("history_cursor") or "") > 200:
            raise Refusal("INVALID_REQUEST", "history_cursor is not valid.")
        return Response(pt_resource(access, _load(access, pk), params))


# ---------------------------------------------------------------------------
# Revision-bound PT commands (E124-E128, E130, E131)
# ---------------------------------------------------------------------------


def _is_opening(goods_pt: GoodsPt) -> bool:
    """Whether this PT is an opening PT rather than a receipt one. Purpose lives on
    the document (``manifest_version``), never on the caller's chosen route."""
    return goods_pt.manifest_version_id is not None


class _ByPurpose:
    """Mixed into every preparer command so ``_prepare_action`` — the one statement
    of "opening drafts need ``pt.prepare.opening``, receipt drafts ``pt.prepare``" —
    decides each one's authority."""

    def required_action_for(self, goods_pt: GoodsPt) -> str:
        return _prepare_action(goods_pt)


class _PtCommandView(GoodsAPIView):
    action = ""
    required_action = "pt.prepare"
    allowed: frozenset[str] = frozenset()
    required: tuple[str, ...] = ()
    status_code = 200
    http_method_names = ["post", "options"]

    @extend_schema(responses=_responses(200, PT_DETAIL_RESPONSE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        return self.handle(request, pk)

    def required_action_for(self, goods_pt: GoodsPt) -> str:
        return self.required_action

    def handle(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, self.allowed, required=self.required)
        goods_pt = _load(access, pk)
        site_id = goods_pt.document.site_id
        pts.require_complete_scope(access, goods_pt.document_id, self.required_action_for(goods_pt))
        self.validate(access, body)

        def handler(run: CommandRun) -> CommandResult:
            head = lock_heads(run, [goods_pt.document_id]).get(goods_pt.document_id)
            if head is None:
                raise Refusal("NOT_FOUND", "That PT was not found.")
            if meta.expected_revision != head.revision:
                raise Refusal("REVISION_SUPERSEDED", "The PT changed after you loaded it.")
            self.execute(run, head, goods_pt, body)
            # The change itself (new lines of another brand) must stay inside authority.
            pts.require_complete_scope(
                access, goods_pt.document_id, self.required_action_for(goods_pt)
            )
            return CommandResult(
                resource_type="pt", resource_id=str(pk), status_code=self.status_code
            )

        reviewed = body.get("reviewed_hash")
        result = self.run_command(
            request,
            access=access,
            action=self.action,
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=site_id,
            subject_key=f"document:{pk}",
            reviewed_hash=str(reviewed) if reviewed else None,
        )
        return self.respond(access, _load(access, pk), result)

    def validate(self, access: AccessContext, body: dict[str, Any]) -> None:
        for key in ("reason_code",):
            value = body.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 60):
                raise Refusal("INVALID_REQUEST", f"{key} is at most 60 characters.")

    def execute(
        self, run: CommandRun, head: DocumentHead, goods_pt: GoodsPt, body: dict[str, Any]
    ) -> None:
        raise NotImplementedError

    def respond(self, access: AccessContext, goods_pt: GoodsPt, result: CommandResult) -> Response:
        return Response(pt_resource(access, goods_pt), status=result.status_code)


#: E124's request (design §6, PtRowEdit; ticket 06A adds `canonical`).
PT_ROWS_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": ["command_id", "contract_version", "expected_revision"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer", "minimum": 1},
        "updates": {
            "type": "array",
            "maxItems": MAX_LINES,
            "description": (
                "One edit per row, applied in order. A row edit carries typed `fields`, "
                "or pasted `canonical` cells, or `delete`."
            ),
            "items": {
                "type": "object",
                "required": ["line_key"],
                "additionalProperties": False,
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "fields": {
                        "type": "array",
                        "description": "Typed cells: money as integer-paise text, null to clear.",
                        "items": {
                            "type": "object",
                            "required": ["column_key"],
                            "additionalProperties": False,
                            "properties": {
                                "column_key": {"type": "string"},
                                "value": {"nullable": True},
                            },
                        },
                    },
                    "canonical": {
                        "type": "object",
                        "description": (
                            "Pasted cells (ticket 06A), keyed by KDPS column, as a canonical "
                            "workbook holds them: money in rupees with at most two decimals, "
                            "blank or null for unknown. Read by the canonical upload's own "
                            "reader, and only into a row already on the receipt PT: a BARCODE "
                            "must name the row's own item, a QTY stays on the row's one counted "
                            "lot within what that lot has free for this PT. P RATE is a check "
                            "value. Any refused cell refuses the whole edit with ROW_INVALID, "
                            "one issue per cell (field = the KDPS column, line_key = the row)."
                        ),
                        "additionalProperties": False,
                        "minProperties": 1,
                        "properties": {
                            name: {"type": "string", "nullable": True, "maxLength": CELL_TEXT}
                            for name in PASTE_COLUMNS
                        },
                    },
                    "delete": {"type": "boolean"},
                    "source_evidence_id": {"type": "string", "format": "uuid"},
                },
            },
        },
        "reviewed_rows": {
            "type": "array",
            "maxItems": MAX_LINES,
            "description": "Review marks for rows the preparer explicitly selected.",
            "items": {
                "type": "object",
                "required": ["line_key", "row_hash"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "row_hash": {"type": "string"},
                },
            },
        },
    },
    "additionalProperties": False,
}

PT_PRICE_REQUEST = _mutation_request(
    {
        "profile_version_id": _UUID_FIELD,
        "direction": _TEXT_FIELD,
        "reason_code": {"type": "string", "maxLength": 60},
    },
    required=("profile_version_id", "direction"),
)
PT_SEND_REQUEST = _mutation_request({"reviewed_hash": _TEXT_FIELD}, required=("reviewed_hash",))
PT_RECALL_REQUEST = _mutation_request(
    {"reason_code": {"type": "string", "maxLength": 60}, "note": {"type": "string", "maxLength": 500}},
    required=("reason_code",),
)
PT_REVERSE_REQUEST = _mutation_request(
    {
        "reason_code": {"type": "string", "maxLength": 60},
        "evidence_ids": {"type": "array", "maxItems": 100, "items": _UUID_FIELD},
    },
    required=("reason_code",),
)
PT_REISSUE_REQUEST = _mutation_request(
    {
        "corrected": {"type": "object", "description": "Corrected PT header and rows."},
        "reason_code": {"type": "string", "maxLength": 60},
    },
    required=("corrected", "reason_code"),
)
PT_SEND_RESPONSE: dict[str, Any] = {
    "type": "object",
    "required": ["document", "reconciliation", "approval_request_id"],
    "properties": {
        "document": PT_DETAIL_RESPONSE,
        "reconciliation": {"type": "object", "nullable": True},
        "approval_request_id": {"type": "string", "format": "uuid", "nullable": True},
    },
}
PT_REVERSE_RESPONSE: dict[str, Any] = {
    **PT_DETAIL_RESPONSE,
    "properties": {
        **PT_DETAIL_RESPONSE["properties"],
        "data": {
            "type": "object",
            "required": ["approval_request_id", "document_id", "version", "state"],
            "properties": {
                "approval_request_id": _UUID_FIELD,
                "document_id": _UUID_FIELD,
                "version": {"type": "integer", "nullable": True},
                "state": _TEXT_FIELD,
            },
        },
    },
}


class GoodsPtRowsView(_ByPurpose, _PtCommandView):
    """E124: edit supplied cells - typed or pasted (ticket 06A) - and record row review marks.

    Refusals: ACTION_DENIED, NOT_FOUND, INVALID_REQUEST, REVISION_SUPERSEDED (stale
    expected_revision, or a review mark for a row that changed), OFFICIAL_IMMUTABLE,
    STATE_CONFLICT, ROW_INVALID (a row edit or pasted cell that cannot be used;
    ``details.issues`` names each row and column), COMMAND_CONFLICT.
    """

    action = "pt.rows"
    allowed = frozenset({"updates", "reviewed_rows"})
    http_method_names = ["patch", "options"]

    @extend_schema(
        request={"application/json": PT_ROWS_REQUEST},
        responses=_responses(200, PT_DETAIL_RESPONSE, _WRITE_REFUSALS),
    )
    def patch(self, request: Request, pk: uuid.UUID) -> Response:
        return self.handle(request, pk)

    def validate(self, access: AccessContext, body: dict[str, Any]) -> None:
        updates = body.get("updates")
        reviews = body.get("reviewed_rows")
        if updates is not None and (not isinstance(updates, list) or len(updates) > MAX_LINES):
            raise Refusal("INVALID_REQUEST", "updates must be a list of at most 50,000 edits.")
        if reviews is not None and (not isinstance(reviews, list) or len(reviews) > MAX_LINES):
            raise Refusal("INVALID_REQUEST", "reviewed_rows must be a list.")

    def execute(
        self, run: CommandRun, head: DocumentHead, goods_pt: GoodsPt, body: dict[str, Any]
    ) -> None:
        pts.edit_rows(
            run, head, updates=body.get("updates") or [], reviews=body.get("reviewed_rows") or []
        )


@extend_schema_view(post=extend_schema(request={"application/json": PT_PRICE_REQUEST}))
class GoodsPtPriceView(_ByPurpose, _PtCommandView):
    """E125: recalculate every row under an approved profile and direction."""

    action = "pt.price"
    allowed = frozenset({"profile_version_id", "direction", "reason_code"})
    required = ("profile_version_id", "direction")

    def execute(
        self, run: CommandRun, head: DocumentHead, goods_pt: GoodsPt, body: dict[str, Any]
    ) -> None:
        pts.reprice(
            run,
            head,
            profile_version_id=body["profile_version_id"],
            direction=str(body["direction"]),
        )


@extend_schema_view(post=extend_schema(request={"application/json": PT_PRICE_REQUEST}))
class GoodsPtRerunView(GoodsPtPriceView):
    """E126: the same recalculation, recorded as a rerun."""

    action = "pt.rerun"


@extend_schema_view(
    post=extend_schema(
        request={"application/json": PT_SEND_REQUEST},
        responses=_responses(200, PT_SEND_RESPONSE, _WRITE_REFUSALS),
    )
)
class GoodsPtSendView(_ByPurpose, _PtCommandView):
    """E127: submit the exact reviewed revision for a distinct checker.

    Dispatches by purpose: a receipt PT reconciles against its GRN's counted
    lots (``pts.submit``); an opening PT reconciles against its approved
    manifest's accepted quantities (``manifest_services.submit_opening``).
    """

    action = "pt.send"
    allowed = frozenset({"reviewed_hash"})
    required = ("reviewed_hash",)

    def execute(
        self, run: CommandRun, head: DocumentHead, goods_pt: GoodsPt, body: dict[str, Any]
    ) -> None:
        if goods_pt.manifest_version_id is not None:
            manifest_services.submit_opening(
                run, head, reviewed_hash=str(body["reviewed_hash"]), goods_pt=goods_pt
            )
        else:
            pts.submit(run, head, reviewed_hash=str(body["reviewed_hash"]), goods_pt=goods_pt)

    def respond(self, access: AccessContext, goods_pt: GoodsPt, result: CommandResult) -> Response:
        action = manifest_services.APPROVE_OPENING if _is_opening(goods_pt) else pts.APPROVE_RECEIPT
        requests = ApprovalRequest.objects.filter(
            tenant_id=access.tenant_id,
            subject_kind="document",
            subject_key=str(goods_pt.document_id),
            requested_action=action,
        )
        request = (
            requests.filter(state="pending").first() or requests.order_by("-decided_at").first()
        )
        return Response(
            {
                "document": pt_resource(access, goods_pt),
                "reconciliation": request.reconciliation if request is not None else None,
                "approval_request_id": str(request.pk) if request is not None else None,
            },
            status=result.status_code,
        )


@extend_schema_view(post=extend_schema(request={"application/json": PT_RECALL_REQUEST}))
class GoodsPtRecallView(_ByPurpose, _PtCommandView):
    """E128: the preparer withdraws a submitted draft."""

    action = "pt.recall"
    allowed = frozenset({"reason_code", "note"})
    required = ("reason_code",)

    def validate(self, access: AccessContext, body: dict[str, Any]) -> None:
        super().validate(access, body)
        note = body.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            raise Refusal("INVALID_REQUEST", "note is at most 500 characters.")

    def execute(
        self, run: CommandRun, head: DocumentHead, goods_pt: GoodsPt, body: dict[str, Any]
    ) -> None:
        pts.recall(run, head, reason_code=str(body["reason_code"]), goods_pt=goods_pt)


def _evidence_ids(access: AccessContext, raw: Any) -> list[uuid.UUID]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 100:
        raise Refusal("INVALID_REQUEST", "evidence_ids must be a list of at most 100 IDs.")
    ids = sorted({parse_uuid(value, "evidence_ids") for value in raw}, key=str)
    found = list(EvidenceObject.objects.filter(tenant_id=access.tenant_id, pk__in=ids))
    if len(found) != len(ids) or not all(readable_by(access, evidence) for evidence in found):
        raise Refusal("NOT_FOUND", "That evidence was not found.")
    return ids


@extend_schema_view(
    post=extend_schema(
        request={"application/json": PT_REVERSE_REQUEST},
        responses=_responses(201, PT_REVERSE_RESPONSE, _WRITE_REFUSALS),
    )
)
class GoodsPtReverseView(_PtCommandView):
    """E130: request a reversal; a distinct checker posts P06 through E234."""

    action = "pt.reverse.request"
    required_action = REVERSAL_REQUEST
    allowed = frozenset({"reason_code", "evidence_ids"})
    required = ("reason_code",)
    status_code = 201

    def validate(self, access: AccessContext, body: dict[str, Any]) -> None:
        super().validate(access, body)
        _evidence_ids(access, body.get("evidence_ids"))

    def execute(
        self, run: CommandRun, head: DocumentHead, goods_pt: GoodsPt, body: dict[str, Any]
    ) -> None:
        evidence = [uuid.UUID(str(value)) for value in body.get("evidence_ids") or []]
        pts.request_reversal(
            run,
            head,
            reason_code=str(body["reason_code"]),
            evidence_ids=sorted(set(evidence), key=str),
            brand_id=_brand_id(goods_pt),
        )

    def respond(self, access: AccessContext, goods_pt: GoodsPt, result: CommandResult) -> Response:
        requests = ApprovalRequest.objects.filter(
            tenant_id=access.tenant_id,
            subject_kind="document",
            subject_key=str(goods_pt.document_id),
            requested_action=pts.APPROVE_REVERSAL,
        )
        request = (
            requests.filter(state="pending").first() or requests.order_by("-decided_at").first()
        )
        if request is None:
            raise Refusal("NOT_FOUND", "That reversal request was not found.")
        version = OfficialVersion.objects.filter(
            document_id=goods_pt.document_id, content_hash=request.reviewed_hash
        ).first()
        version_no = version.version if version is not None else None
        data = {
            "approval_request_id": str(request.pk),
            "document_id": str(goods_pt.document_id),
            "version": version_no,
            "state": request.state,
        }
        return Response(
            resource_dto(
                id=request.pk,
                data=data,
                revision=request.revision,
                state=request.state,
                number=goods_pt.document.official_number,
                version=version_no,
                context={"site_id": goods_pt.document.site_id, "brand_id": _brand_id(goods_pt)},
            ),
            status=result.status_code,
        )


@extend_schema_view(
    post=extend_schema(
        request={"application/json": PT_REISSUE_REQUEST},
        responses=_responses(201, PT_DETAIL_RESPONSE, _WRITE_REFUSALS),
    )
)
class GoodsPtReissueView(_ByPurpose, _PtCommandView):
    """E131: a corrected draft under the same number after safe reversal."""

    action = "pt.reissue"
    allowed = frozenset({"corrected", "reason_code"})
    required = ("corrected", "reason_code")
    status_code = 201

    def validate(self, access: AccessContext, body: dict[str, Any]) -> None:
        super().validate(access, body)
        if not isinstance(body.get("corrected"), dict):
            raise Refusal("INVALID_REQUEST", "corrected must be a PT payload.")

    def execute(
        self, run: CommandRun, head: DocumentHead, goods_pt: GoodsPt, body: dict[str, Any]
    ) -> None:
        pts.reissue(run, head, corrected=body["corrected"], reason_code=str(body["reason_code"]))


# ---------------------------------------------------------------------------
# E129 compatibility adapter onto E234
# ---------------------------------------------------------------------------


def _domain_refusal(refusal: Refusal) -> Refusal | None:
    """E129 answers with the domain code E234 wraps as APPROVAL_REFUSED."""
    if refusal.code != "APPROVAL_REFUSED" or not refusal.domain_code:
        return None
    status = 422 if refusal.domain_code == "PT_ROWS_INVALID" else None
    return Refusal(refusal.domain_code, refusal.message, status=status, issues=refusal.issues)


class GoodsPtPostView(GoodsAPIView):
    """E129: delegates to the one approval decision (E234); no second posting path."""

    @extend_schema(
        request={"application/json": _mutation_request(
            {"reviewed_hash": _TEXT_FIELD}, required=("reviewed_hash",)
        )},
        responses=_responses(200, PT_DETAIL_RESPONSE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reviewed_hash"}, required=["reviewed_hash"])
        goods_pt = _load(access, pk)
        site_id = goods_pt.document.site_id
        pts.require_complete_scope(access, goods_pt.document_id, pts.APPROVE_RECEIPT)
        reviewed_hash = str(body["reviewed_hash"])

        def handler(run: CommandRun) -> CommandResult:
            # Read, not lock: the decision takes the site guard before any document lock.
            head = DocumentHead.objects.get(document_id=pk)
            if meta.expected_revision != head.revision:
                raise Refusal("REVISION_SUPERSEDED", "The PT changed after you loaded it.")
            pending = ApprovalRequest.objects.filter(
                tenant_id=run.tenant_id,
                subject_kind="document",
                subject_key=str(pk),
                requested_action=pts.APPROVE_RECEIPT,
                state=ApprovalRequest.State.PENDING,
                reviewed_hash=reviewed_hash,
            ).first()
            if pending is None:
                raise Refusal("STATE_CONFLICT", "There is no pending approval for this revision.")
            access.require_step_up()
            try:
                decide(
                    run,
                    access=access,
                    request_id=pending.pk,
                    decision="approve",
                    reviewed_hash=reviewed_hash,
                    reason_code=None,
                )
            except Refusal as refusal:
                unwrapped = _domain_refusal(refusal)
                if unwrapped is None:
                    raise
                raise unwrapped from refusal
            return CommandResult(resource_type="pt", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action=f"approvals.decide.{pts.APPROVE_RECEIPT}",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=site_id,
            subject_key=str(pk),
            reviewed_hash=reviewed_hash,
        )
        return Response(pt_resource(access, _load(access, pk)), status=result.status_code)


# ---------------------------------------------------------------------------
# E132 / E133 frozen export
# ---------------------------------------------------------------------------


#: E132: the frozen official version and the columns that describe it. `document`
#: is the same ResourceDTO shape the detail answers, with its lines unpaged -
#: an export is the whole version or it is not an export.
PT_EXPORT_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": (
        "The exact official version, frozen (or with `?draft=true` the current draft as a "
        "labelled preview), with its column definitions: the twenty-two KDPS columns in "
        "the PRD's order, cost columns only for a caller entitled to them. Each line "
        "carries its resolved `cells` keyed by column key (E132)."
    ),
    "properties": {
        "document": {
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
                        "header": {"type": "object", "additionalProperties": True},
                        "lines": {
                            "type": "object",
                            "properties": {
                                "items": {
                                    "type": "array",
                                    "items": {"type": "object", "additionalProperties": True},
                                },
                                "next_cursor": {"type": "string", "nullable": True},
                                "total": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        },
        "columns": {
            "type": "array",
            "description": "Cost columns appear only for a caller entitled to them.",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
}


class GoodsPtExportView(GoodsAPIView):
    """E132 (JSON) and E133 (XLSX): the exact official version, frozen - or, asked for with
    ``?draft=true``, the current draft revision as an explicitly labelled preview with no
    number (E132 step 5). Either way the canonical KDPS workbook: the twenty-two columns
    in the PRD's order (store and warehouse operations PRD §5.4; OPS-15)."""

    as_file = False

    @extend_schema(
        parameters=[
            OpenApiParameter("version", int, description="An official version number."),
            OpenApiParameter(
                "draft",
                str,
                enum=["true", "false"],
                description="`true` previews the current draft revision, labelled as a draft.",
            ),
        ],
        responses=_responses(200, PT_EXPORT_RESPONSE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response | HttpResponse:
        access = self.access(request)
        params = check_query(request, {"version", "draft"})
        if params.get("draft") not in (None, "", "true", "false"):
            raise Refusal("INVALID_REQUEST", "draft must be true or false.")
        preview = params.get("draft") == "true"
        if preview and params.get("version"):
            raise Refusal("INVALID_REQUEST", "Ask for a draft preview or a version, not both.")
        goods_pt = _load(access, pk)
        head = DocumentHead.objects.select_related("live_version", "draft_revision").get(
            document_id=pk
        )
        shows_cost = _shows_cost(access, goods_pt)
        if preview:
            revision = head.draft_revision
            if revision is None or head.state not in (
                DocumentHead.State.DRAFT,
                DocumentHead.State.SUBMITTED,
            ):
                raise Refusal("VERSION_NOT_FOUND", "This PT has no draft to preview.", status=404)
            raw = [dict(state.payload) for state in revision_lines(pk, revision.revision)]
            header = {**revision.payload, "draft_preview": True}
            number, version_no, digest = None, None, revision.content_hash
            label = f"draft-r{revision.revision}"
        else:
            version = _export_version(pk, head, _positive_int(params, "version"))
            raw = [
                dict(line.payload)
                for line in OfficialLine.objects.filter(version=version).order_by("line_no")
            ]
            header = dict(version.canonical_payload)
            number, version_no = goods_pt.document.official_number, version.version
            digest, label = version.content_hash, f"v{version.version}"
        cells = CanonicalCells(access.tenant_id, raw)
        columns = export_columns(shows_cost)
        keys = [column["key"] for column in columns]
        lines = []
        for line in raw:
            resolved = cells.cells(line)
            lines.append({**_redact(line, shows_cost), "cells": {k: resolved[k] for k in keys}})
        _audit_export(
            access,
            goods_pt,
            version_no,
            draft_revision=head.draft_revision.revision
            if preview and head.draft_revision
            else None,
            shows_cost=shows_cost,
            as_file=self.as_file,
        )
        if self.as_file:
            return xlsx_response(number or str(pk), label, columns, lines, draft=preview)
        document = resource_dto(
            id=pk,
            data={
                "header": header,
                "lines": {"items": lines, "next_cursor": None, "total": len(lines)},
            },
            revision=head.revision,
            state=head.state,
            number=number,
            version=version_no,
            context={"site_id": goods_pt.document.site_id, "brand_id": _brand_id(goods_pt)},
            content={"hash": digest},
        )
        document["content_hash"] = digest
        return Response({"document": document, "columns": columns})


@extend_schema_view(
    get=extend_schema(
        responses={
            (
                200,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ): {
                "type": "string",
                "format": "binary",
                "description": "The same frozen official version as a spreadsheet (E133).",
            },
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            404: REFUSAL_RESPONSE,
        }
    )
)
class GoodsPtExportXlsxView(GoodsPtExportView):
    as_file = True


def _audit_export(
    access: AccessContext,
    goods_pt: GoodsPt,
    version: int | None,
    *,
    draft_revision: int | None = None,
    shows_cost: bool,
    as_file: bool,
) -> None:
    """E132/E133 step 6: who read which official version (or draft preview), and whether
    cost was included.

    The read changes no goods fact; the command boundary only seals its audit row.
    Failing to write that row fails the export, so nothing leaves unaudited.
    """
    action = "pt.export.xlsx" if as_file else "pt.export"
    detail: dict[str, Any] = {
        "version": version,
        "cost_fields": shows_cost,
        "format": "xlsx" if as_file else "json",
    }
    if draft_revision is not None:
        detail["draft_revision"] = draft_revision

    def handler(run: CommandRun) -> CommandResult:
        run.audit_after = detail
        return CommandResult(resource_type="pt_export", resource_id=str(goods_pt.document_id))

    execute_command(
        access.principal(),
        CommandSpec(
            action,
            uuid.uuid4(),
            {"document": str(goods_pt.document_id), **detail},
            subject_key=f"document:{goods_pt.document_id}",
            site_id=goods_pt.document.site_id,
        ),
        handler,
    )


def _export_version(pk: uuid.UUID, head: DocumentHead, number: int | None) -> OfficialVersion:
    versions = OfficialVersion.objects.filter(document_id=pk)
    if number is not None:
        version = versions.filter(version=number).first()
    else:
        version = head.live_version or versions.order_by("-version").first()
    if version is None:
        raise Refusal("VERSION_NOT_FOUND", "This PT has no official version.", status=404)
    return version


def _cell(line: dict[str, Any], key: str) -> Any:
    value = (line.get("cells") or {}).get(key)
    if value in (None, ""):
        return None
    if key.endswith("_paise"):
        return Amount(int(str(value)))
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def xlsx_response(
    number: str,
    label: str,
    columns: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    *,
    draft: bool = False,
) -> HttpResponse:
    rows = [[column["label"] for column in columns]]
    rows += [[_cell(line, column["key"]) for column in columns] for line in lines]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in number)
    title = "PT draft preview" if draft else "PT"
    response = HttpResponse(workbook_bytes(title, rows), content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="{safe}-{label}.xlsx"'
    return response
