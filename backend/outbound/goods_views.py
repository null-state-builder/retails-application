"""Goods-v1 movements, mark-damaged and stock search (E104, E105, E151, E155, E209, E210).

These views hand-build their responses, so the schemas below are what makes the
emitted OpenAPI operation describable. Every one of them answers only under
``/api/goods-v1/outbound/``; the legacy ``/api/outbound/...`` movement, damage and
search routes keep their own pre-goods-v1 contract (#303).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    LIST_QUERY_KEYS,
    GoodsAPIView,
    business_body,
    check_query,
    decode_cursor,
    page,
    page_limit,
    paginate,
    parse_int_id,
    parse_meta,
    resource_dto,
)
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.kernel_models import DocumentHead
from core.refusals import Refusal
from outbound import damage_review
from outbound import goods_adjustments as adjustments
from outbound import goods_disposal as disposal
from outbound import goods_movements as movements
from outbound import goods_rtv as rtv
from outbound import goods_rtv_shipments as rtv_shipments
from outbound import goods_writeoff as writeoff
from outbound.goods_models import DamageReport, GoodsMovement, RtvEvent
from stockledger import goods_reads as reads
from stockledger import goods_views as stock_views

#: Who may read a movement document: whoever may draft one, approve one, or read
#: the stock it moves. A movement is an inventory document, not a PT.
READ_ACTIONS = (movements.DRAFT_ACTION, movements.APPROVE_ACTION, reads.READ)
LINE_PAGE = 500
DETAIL_QUERY = frozenset({"version", "line_cursor", "history_cursor", "limit"})
#: E210's own query: ``ListQuery`` plus the two filters a stock search is run by,
#: plus ``basis`` - which the ticket's own criterion needs ("labelled quantity and
#: value bases"), and which is refused without the cost grant rather than silently
#: answered as quantity. Every other stock-read filter is deliberately absent:
#: extra query keys are refused (design §6.1).
SEARCH_QUERY = LIST_QUERY_KEYS | {"sku_id", "source_site_id", "basis"}
#: The damage-review list asks a simpler question than a movement list does -
#: one site, one state - and names its site parameter `site`, as the operations
#: contract and the receiving inbox (OPS-04) both write it.
DAMAGE_QUERY = frozenset({"site", "state", "cursor", "limit"})

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


MOVEMENT_LINE: dict[str, Any] = {
    "type": "object",
    "description": (
        "One frozen movement line: which lot, which exact portions of it moved, "
        "from where to where, in what condition, and which hold keys a release "
        "lifted. ``portions`` is what makes the source exact rather than a "
        "quantity someone can argue about."
    ),
    "properties": {
        "line_key": {"type": "string", "format": "uuid"},
        "lot_id": {"type": "string", "format": "uuid", "nullable": True},
        "qty": {"type": "integer"},
        "sku_id": {"type": "string", "nullable": True},
        "origin_id": {"type": "string", "nullable": True},
        "condition": {"type": "string"},
        "source_location_id": {
            "type": "string",
            "nullable": True,
            "description": "Null only on a new-found adjustment line: it came from nowhere.",
        },
        "destination_location_id": {"type": "string", "nullable": True},
        "hold_keys": {"type": "array", "items": {"type": "string"}},
        "source_pool": {
            "type": "string",
            "enum": [rtv.POOL_QUARANTINE, rtv.POOL_STOCK],
            "description": (
                "RTV only (goods ticket 15B): recorded pieces held in the site's quarantine, "
                "or accepted good stock from a storage location."
            ),
        },
        "portions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "lot_id": {"type": "string", "format": "uuid"},
                    "lower": {"type": "integer"},
                    "upper": {"type": "integer"},
                    "origin_id": {
                        "type": "string",
                        "nullable": True,
                        "description": (
                            "Adjustments, RTVs, write-offs and disposals: the layer whose "
                            "recorded cost values it (for a disposal, also its source reference)."
                        ),
                    },
                },
            },
        },
        "value_basis": {
            "type": "string",
            "enum": ["recorded_layer_cost", "origin_evidence", "unvalued", "written_off"],
            "description": (
                "Adjustments, RTVs, write-offs and disposals only (goods tickets 15A-15D). A "
                "removal, a returned or a written-off piece is valued at each portion's own "
                "recorded layer cost; a found line at its same-site origin evidence, or not "
                "at all. A disposal line is `recorded_layer_cost` when the disposal recognises "
                "the loss itself, or `written_off` when the write-off it names already did. No "
                "money amount is published here."
            ),
        },
        "description": {"type": "string", "description": "A found line: what was found."},
        "cost_evidence_origin_id": {"type": "string", "nullable": True},
        "found_lot_id": {
            "type": "string",
            "nullable": True,
            "description": "A found line, once approved: the custody lot it opened.",
        },
    },
}

DISPOSAL_FACTS: dict[str, Any] = {
    "type": "object",
    "nullable": True,
    "description": (
        "Disposal only (goods ticket 15D; quarantine outcomes PRD §7.2): what actually "
        "happened to the goods. `method` is `destruction` or `scrap_handover` (donation and "
        "sale as damaged merchandise are not offered); `disposed_at` is the actual event "
        "time, with its time zone, never later than the recording; `handed_over_to` names "
        "who took scrap/recycled goods (required for, and only for, a handover); "
        "`scrap_proceeds_paise` is what the scrap dealer paid, recorded separately from the "
        "loss and never posted; `evidence_reference` is the handover slip or destruction "
        "note reference."
    ),
    "required": ["method", "disposed_at"],
    "additionalProperties": False,
    "properties": {
        "method": {"type": "string", "enum": list(disposal.METHODS)},
        "disposed_at": {"type": "string", "format": "date-time"},
        "handed_over_to": {
            "type": "string",
            "nullable": True,
            "maxLength": disposal.MAX_RECIPIENT,
        },
        "scrap_proceeds_paise": {"type": "string", "nullable": True, "pattern": "^[0-9]+$"},
        "evidence_reference": {
            "type": "string",
            "nullable": True,
            "maxLength": disposal.MAX_REFERENCE,
        },
    },
}

MOVEMENT_HEADER: dict[str, Any] = {
    "type": "object",
    "description": "MovementPayload without its lines (design §5).",
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                *movements.ACTIVE_KINDS,
                *movements.ADJUSTMENT_KINDS,
                movements.RTV_KIND,
                movements.WRITEOFF_KIND,
                movements.DISPOSAL_KIND,
            ],
        },
        "vendor_id": {
            "type": "string",
            "nullable": True,
            "description": "RTV only: the vendor that agreed to take the goods back.",
        },
        "agreement_reference": {
            "type": "string",
            "nullable": True,
            "description": (
                "RTV only (GSA-R04): the reference of the vendor's agreement. The system "
                "records it and does not evaluate the return terms."
            ),
        },
        "site_id": {"type": "string"},
        "reason_code": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "evidence_note": {
            "type": "string",
            "nullable": True,
            "description": (
                "Adjustments and write-offs: a note that is (with or instead of a photo) "
                "evidence. RTVs: an optional note on the agreement."
            ),
        },
        "source_document_id": {
            "type": "string",
            "nullable": True,
            "description": (
                "Adjustments: the original GRN or PT. Disposals: the approved write-off whose "
                "pieces are disposed of, if any."
            ),
        },
        "count_id": {"type": "string", "nullable": True},
        "disposal": DISPOSAL_FACTS,
    },
}

MOVEMENT_AUTHORITY: dict[str, Any] = {
    "type": "object",
    "description": (
        "Who recorded the movement, and - for a release or an adjustment - the "
        "distinct person who approved it. A bin move and a hold carry the actor's own "
        "authority, so "
        "`kind` is `actor` and there is no approver to show."
    ),
    "properties": {
        "kind": {"type": "string", "enum": ["actor", "approval"]},
        "actor": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
        },
        "approved_by": {
            "type": "object",
            "nullable": True,
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
        },
        "approved_at": {"type": "string", "nullable": True},
        "approval_request_id": {"type": "string", "nullable": True},
        "approval_state": {"type": "string", "nullable": True},
    },
}

RTV_STATE: dict[str, Any] = {
    "type": "string",
    "nullable": True,
    "enum": [rtv.INITIATED, rtv.COMPLETED, rtv.CLOSED_PARTIALLY_RETURNED, rtv.CANCELLED],
    "description": "RTV only: null until the RTV is approved, and for other kinds.",
}
_QTY = {"type": "integer"}
_PERSON = {
    "type": "object",
    "nullable": True,
    "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
}
RTV_LEFT_BEHIND: dict[str, Any] = {
    "type": "object",
    "description": (
        "Why some pieces of a line were left behind at a pickup (quarantine outcomes §5). "
        "A reason never withdraws anything: the pieces stay reserved until another pickup "
        "or an explicit withdrawal."
    ),
    "required": ["line_key", "reason", "qty", "further_pickup_expected"],
    "properties": {
        "line_key": {"type": "string", "format": "uuid"},
        "reason": {"type": "string", "enum": list(rtv.LEFT_BEHIND_REASONS)},
        "qty": {"type": "integer", "minimum": 1},
        "remark": {"type": "string", "nullable": True, "maxLength": rtv.MAX_REMARK},
        "further_pickup_expected": {"type": "boolean"},
    },
}
RTV_LEFT_BEHIND_INPUT: dict[str, Any] = {
    **RTV_LEFT_BEHIND,
    "description": (
        "A reason for pieces of one line left behind at this pickup. "
        "`further_pickup_expected` is required only for `other` (with a `remark`); every "
        "other reason already says it, and a contradicting value is REASON_CONFLICT."
    ),
    "required": ["line_key", "reason", "qty"],
}
RTV_CLOSURE: dict[str, Any] = {
    "type": "object",
    "description": (
        "A shortfall closure of one shipment (goods ticket 15H, GSA-R07): the pieces the "
        "vendor never acknowledged, the reason and evidence, who prepared it and - once "
        "approved - the Owner who approved it."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "closed_at": {"type": "string", "nullable": True},
        "approved_by": _PERSON,
        "prepared_by": _PERSON,
        "quantity": _QTY,
        "reason": {"type": "string", "nullable": True},
        "evidence_reference": {"type": "string", "nullable": True},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "evidence_note": {"type": "string", "nullable": True},
        "value_basis": {"type": "string", "nullable": True},
        "value_paise": {"type": "string", "nullable": True},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"line_key": {"type": "string", "format": "uuid"}, "qty": _QTY},
            },
        },
    },
}
RTV_SHIPMENT: dict[str, Any] = {
    "type": "object",
    "description": (
        "One shipment of the RTV for delivery to the vendor (goods ticket 15F). `status`: "
        "`awaiting_receipt` (no acknowledgement yet, something still away), "
        "`short_acknowledged` (the latest acknowledgement leaves pieces neither acknowledged "
        "nor back - an owned `rtv_acknowledgement_discrepancy`), `delivered` (every piece "
        "acknowledged), `returned_to_source` (every piece came back), `partly_returned` (all "
        "accounted for, some acknowledged and some back) or `shortfall_closed` (goods ticket "
        "15H, GSA-R07: the Owner approved closing the pieces the vendor never acknowledged as "
        "a recognised shortfall). `acknowledged_qty` is the latest snapshot (null before "
        "any), never a sum of snapshots; `closed_qty` is the approved closed shortfall. "
        "`state_hash` is what an acknowledgement or a closure is recorded against. "
        "`awaiting_putaway_qty` is returned good pieces standing unaccepted in the source's "
        "receiving. `eway` keeps what left with the goods, what was attached later and "
        "whether that reference was verified as separate facts; completing the shipment "
        "closes none of them. `closures` are approved shortfall closures and "
        "`pending_closure` the prepared one waiting for the Owner (null if none), with the "
        "`approval_request_id`, `reviewed_hash` and `revision` the approvals decision quotes; "
        "`value_paise` (at recorded layer cost; null when unknown, never 0) is present only "
        "for a reader with the `cost` field grant over the stock read. `allowed_actions` are "
        "this reader's: `acknowledge`, `return_to_source`, `putaway_returned`, "
        "`eway_attach`, `eway_verify`, `propose_closure` (a short-acknowledged shipment, "
        "`movement.draft`) and `decide_closure` (a pending closure, `movement.approve`, "
        "never its preparer)."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "sequence_no": _QTY,
        "status": {"type": "string", "enum": list(rtv.SHIPMENT_STATUSES)},
        "shipped_at": {"type": "string", "format": "date-time"},
        "recorded_at": {"type": "string", "nullable": True},
        "recorded_by": _PERSON,
        "carrier": {"type": "string", "nullable": True},
        "evidence_reference": {"type": "string", "nullable": True},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "evidence_note": {"type": "string", "nullable": True},
        "closed_damage_report_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
        },
        "shipped_qty": _QTY,
        "acknowledged_qty": {"type": "integer", "nullable": True},
        "returned_qty": _QTY,
        "closed_qty": _QTY,
        "unaccounted_qty": _QTY,
        "awaiting_putaway_qty": _QTY,
        "state_hash": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "shipped_qty": _QTY,
                    "acknowledged_qty": {"type": "integer", "nullable": True},
                    "returned_qty": _QTY,
                    "closed_qty": _QTY,
                    "unaccounted_qty": _QTY,
                    "awaiting_putaway_qty": _QTY,
                },
            },
        },
        "acknowledgements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "acknowledged_at": {"type": "string", "format": "date-time"},
                    "recorded_at": {"type": "string", "nullable": True},
                    "recorded_by": _PERSON,
                    "quantity": _QTY,
                    "shortfall_qty": _QTY,
                    "recipient_reference": {"type": "string", "nullable": True},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence_note": {"type": "string", "nullable": True},
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "line_key": {"type": "string", "format": "uuid"},
                                "qty": _QTY,
                            },
                        },
                    },
                },
            },
        },
        "returns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "returned_at": {"type": "string", "format": "date-time"},
                    "recorded_at": {"type": "string", "nullable": True},
                    "recorded_by": _PERSON,
                    "quantity": _QTY,
                    "reason": {"type": "string", "nullable": True},
                    "evidence_reference": {"type": "string", "nullable": True},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence_note": {"type": "string", "nullable": True},
                    "damage_report_id": {"type": "string", "nullable": True},
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "line_key": {"type": "string", "format": "uuid"},
                                "qty": _QTY,
                                "good": _QTY,
                                "damaged": _QTY,
                            },
                        },
                    },
                },
            },
        },
        "putaways": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "recorded_at": {"type": "string", "nullable": True},
                    "recorded_by": _PERSON,
                    "quantity": _QTY,
                },
            },
        },
        "eway": {
            "type": "object",
            "properties": {
                "at_dispatch": {"type": "string", "enum": ["present", "not_present"]},
                "reference": {"type": "string", "nullable": True},
                "attached_at": {"type": "string", "nullable": True},
                "attached_by": {"type": "string", "nullable": True},
                "verified": {"type": "boolean"},
                "verified_at": {"type": "string", "nullable": True},
                "verified_by": {"type": "string", "nullable": True},
                "missing_exception_open": {"type": "boolean"},
            },
        },
        "closures": {"type": "array", "items": RTV_CLOSURE},
        "pending_closure": {
            **RTV_CLOSURE,
            "nullable": True,
            "properties": {
                **RTV_CLOSURE["properties"],
                "approval_request_id": {"type": "string", "format": "uuid"},
                "reviewed_hash": {"type": "string"},
                "revision": _QTY,
                "prepared_at": {"type": "string", "nullable": True},
            },
        },
        "allowed_actions": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "acknowledge",
                    "return_to_source",
                    "putaway_returned",
                    "eway_attach",
                    "eway_verify",
                    "propose_closure",
                    "decide_closure",
                ],
            },
        },
    },
}

RTV_DETAIL: dict[str, Any] = {
    "type": "object",
    "nullable": True,
    "description": (
        "RTV only (goods ticket 15B). `state` is null until the Owner approves it; then "
        "`initiated` while a balance waits to leave, `completed` when every approved piece "
        "was handed over, `closed_partially_returned` when part left and the rest was "
        "withdrawn, `cancelled` when all of it was withdrawn before anything left. "
        "`outstanding_qty` is what is still reserved to the RTV; `awaiting_withdrawal_qty` "
        "is the part of it the latest pickup said will not be collected (it stays reserved "
        "until somebody withdraws it). `events` are the confirmed pickups, withdrawals and "
        "shipments, each with its actual and recorded time and who recorded it. Goods ticket "
        "15F: a shipment for delivery to the vendor is accounted for only when every piece "
        "it carried is acknowledged by the vendor or recorded back at the source, so an RTV "
        "stays `initiated` while any shipment awaits receipt or carries an acknowledgement "
        "shortfall, even with nothing reserved. `shipped_qty`, `acknowledged_qty` (the "
        "latest acknowledgement of each shipment, summed), `returned_to_source_qty`, "
        "`shortfall_closed_qty` (goods ticket 15H: pieces whose shortfall the Owner closed) "
        "and `awaiting_receipt_qty` (shipped, neither acknowledged, back nor closed) are per "
        "RTV and per line; `shipments` lists each shipment with its own facts. `completed` "
        "needs every approved piece collected or acknowledged; a returned piece is never "
        "counted as delivered, an RTV none of whose goods reached the vendor (and with no "
        "closed shortfall) is `cancelled`, and one with a closed shortfall is "
        "`closed_partially_returned`."
    ),
    "properties": {
        "state": RTV_STATE,
        "vendor": {
            "type": "object",
            "nullable": True,
            "properties": {
                "id": {"type": "string"},
                "code": {"type": "string"},
                "name": {"type": "string"},
            },
        },
        "agreement_reference": {"type": "string", "nullable": True},
        "approved_qty": _QTY,
        "picked_up_qty": _QTY,
        "withdrawn_qty": _QTY,
        "outstanding_qty": _QTY,
        "awaiting_withdrawal_qty": _QTY,
        "shipped_qty": _QTY,
        "acknowledged_qty": _QTY,
        "returned_to_source_qty": _QTY,
        "shortfall_closed_qty": _QTY,
        "awaiting_receipt_qty": _QTY,
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "sku_id": {"type": "string", "nullable": True},
                    "source_pool": {"type": "string", "nullable": True},
                    "approved_qty": _QTY,
                    "picked_up_qty": _QTY,
                    "withdrawn_qty": _QTY,
                    "outstanding_qty": _QTY,
                    "awaiting_withdrawal_qty": _QTY,
                    "shipped_qty": _QTY,
                    "acknowledged_qty": _QTY,
                    "returned_to_source_qty": _QTY,
                    "shortfall_closed_qty": _QTY,
                    "awaiting_receipt_qty": _QTY,
                },
            },
        },
        "shipments": {"type": "array", "items": RTV_SHIPMENT},
        "left_behind": {"type": "array", "items": RTV_LEFT_BEHIND},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "kind": {"type": "string", "enum": ["pickup", "withdrawal", "shipment"]},
                    "sequence_no": _QTY,
                    "quantity": _QTY,
                    "event_at": {"type": "string", "format": "date-time"},
                    "recorded_at": {"type": "string", "nullable": True},
                    "actor": _PERSON,
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "line_key": {"type": "string", "format": "uuid"},
                                "qty": _QTY,
                            },
                        },
                    },
                    "details": {
                        "type": "object",
                        "description": (
                            "Pickup: collected_by, evidence_reference, evidence_ids, "
                            "evidence_note, left_behind, closed_damage_report_ids (pending "
                            "damage reports this pickup closed as returned to vendor, because "
                            "it took back the last piece they covered). Withdrawal: reason, "
                            "remark. Shipment (15F): carrier, evidence_reference, evidence_ids, "
                            "evidence_note, eway_reference, eway_at_dispatch, "
                            "closed_damage_report_ids (closed as shipped to vendor)."
                        ),
                        "properties": {
                            "collected_by": {"type": "string"},
                            "carrier": {"type": "string"},
                            "eway_reference": {"type": "string", "nullable": True},
                            "eway_at_dispatch": {
                                "type": "string",
                                "enum": ["present", "not_present"],
                            },
                            "evidence_reference": {"type": "string", "nullable": True},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "evidence_note": {"type": "string", "nullable": True},
                            "left_behind": {"type": "array", "items": RTV_LEFT_BEHIND},
                            "closed_damage_report_ids": {
                                "type": "array",
                                "items": {"type": "string", "format": "uuid"},
                            },
                            "reason": {"type": "string", "enum": list(rtv.WITHDRAWAL_REASONS)},
                            "remark": {"type": "string", "nullable": True},
                        },
                    },
                },
            },
        },
    },
}

WRITE_OFF_DETAIL: dict[str, Any] = {
    "type": "object",
    "nullable": True,
    "description": (
        "Write-off only (goods ticket 15C; quarantine outcomes PRD §7, GSA-R05). `state` is "
        "null until the Owner approves it, then `written_off`: the loss of exactly the frozen "
        "pieces was recognised once, at their recorded layer cost, and nothing moved. "
        "`physical_qty` and `quarantine_qty` say how many of those pieces still stand at a "
        "site and in quarantine (unchanged by the write-off; only a later disposal, 15D/15E, "
        "removes any), `disposed_qty` how many a disposal has removed, and `disposals` links "
        "every disposal naming this write-off (goods ticket 15D) with its state, number and "
        "quantity; `still_held_qty` how many the write-off's own hold (`hold_key`) still "
        "covers. `value_paise` (per line and in total) is present only to a reader with the "
        "`cost` field grant over the stock read at the site for the line's brand - absent "
        "otherwise, never zero. No GL, vendor, cash or tax entry exists for it."
    ),
    "properties": {
        "state": {"type": "string", "nullable": True, "enum": [writeoff.WRITTEN_OFF]},
        "value_basis": {"type": "string", "enum": [writeoff.VALUE_BASIS]},
        "qty": _QTY,
        "physical_qty": _QTY,
        "quarantine_qty": _QTY,
        "disposed_qty": _QTY,
        "still_held_qty": _QTY,
        "hold_key": {"type": "string", "format": "uuid", "nullable": True},
        "disposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "number": {"type": "string", "nullable": True},
                    "state": {"type": "string"},
                    "qty": _QTY,
                },
            },
        },
        "value_paise": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "qty": _QTY,
                    "value_paise": {"type": "string"},
                },
            },
        },
    },
}

DISPOSAL_DETAIL: dict[str, Any] = {
    "type": "object",
    "nullable": True,
    "description": (
        "Disposal only (goods ticket 15D; quarantine outcomes PRD §7, GSA-R05). `state` is "
        "null until the Owner approves it, then `disposed`: exactly the frozen pieces left "
        "custody for the disposed boundary once (`disposed_qty`), every hold over them ended "
        "and every other piece stayed. The physical fact and the value fact are kept apart: "
        "`value_basis` is `recorded_layer_cost` when this disposal recognised the loss itself "
        "(`recognised_here_qty`), or `written_off` when the write-off it names (`write_off`) "
        "already did (`written_off_earlier_qty`) - the same loss is never recognised twice. "
        "`value_paise` (per line and in total) is the loss this disposal recognised, present "
        "only to a reader with the `cost` field grant over the stock read at the site for "
        "the line's brand, and absent after a write-off (read the write-off). "
        "`scrap_proceeds_paise` is recorded separately and never posted. No GL, vendor, cash "
        "or tax entry exists for it."
    ),
    "properties": {
        "state": {"type": "string", "nullable": True, "enum": [disposal.DISPOSED]},
        "method": {"type": "string", "nullable": True, "enum": list(disposal.METHODS)},
        "disposed_at": {"type": "string", "format": "date-time", "nullable": True},
        "recorded_at": {
            "type": "string",
            "format": "date-time",
            "description": "When the disposal was recorded, beside when it actually happened.",
        },
        "qty": _QTY,
        "disposed_qty": _QTY,
        "value_basis": {
            "type": "string",
            "enum": [disposal.BASIS_RECORDED, disposal.BASIS_WRITTEN_OFF],
        },
        "recognised_here_qty": _QTY,
        "written_off_earlier_qty": _QTY,
        "write_off": {
            "type": "object",
            "nullable": True,
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "number": {"type": "string", "nullable": True},
            },
        },
        "scrap_proceeds_paise": {"type": "string", "nullable": True},
        "value_paise": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "qty": _QTY,
                    "value_basis": {
                        "type": "string",
                        "enum": [disposal.BASIS_RECORDED, disposal.BASIS_WRITTEN_OFF],
                    },
                    "value_paise": {"type": "string"},
                },
            },
        },
    },
}

MOVEMENT_DETAIL: dict[str, Any] = {
    "type": "object",
    "description": "MovementDetailDTO (design §6.2).",
    "properties": {
        "header": MOVEMENT_HEADER,
        "authority": MOVEMENT_AUTHORITY,
        "source_document": {
            "type": "object",
            "nullable": True,
            "description": (
                "Adjustments: the GRN or PT a removal corrects, by number. Null when "
                "none is named or the reader holds none of movement.draft, "
                "movement.approve or pt.view at the site."
            ),
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "kind": {"type": "string"},
                "number": {"type": "string", "nullable": True},
            },
        },
        "exceptions": {
            "type": "array",
            "nullable": True,
            "description": (
                "Adjustments, RTVs, write-offs and disposals: the owned work this document opened "
                "(approval_pending, acceptance_remaining, rtv_not_dispatched; for an RTV's "
                "shipments rtv_receipt_pending, rtv_acknowledgement_discrepancy and "
                "eway_missing). Null for a reader without "
                "exception.view or exception.manage at the site."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string"},
                    "subject_id": {"type": "string"},
                    "state": {"type": "string"},
                    "reason_code": {"type": "string"},
                    "due_at": {"type": "string", "nullable": True},
                    "allowed_resolution_actions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "rtv": RTV_DETAIL,
        "write_off": WRITE_OFF_DETAIL,
        "disposal": DISPOSAL_DETAIL,
        "lines": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": MOVEMENT_LINE},
                "next_cursor": {"type": "string", "nullable": True},
                "total": {"type": "integer"},
            },
        },
    },
}

MOVEMENT_RESOURCE: dict[str, Any] = {
    "type": "object",
    "description": "ResourceDTO<MovementDetailDTO>.",
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
        "data": MOVEMENT_DETAIL,
        "history_cursor": {"type": "string", "nullable": True},
        "as_of": {"type": "string", "format": "date-time"},
    },
}

MOVEMENT_SUMMARY: dict[str, Any] = {
    "type": "object",
    "description": "ResourceSummary (design §6.1). Restricted money is absent.",
    "properties": {
        "id": {"type": "string"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "kind": {"type": "string"},
        "purpose": {"type": "string", "nullable": True},
        "number": {"type": "string", "nullable": True},
        "state": {"type": "string"},
        "site_id": {"type": "string"},
        "brand_id": {"type": "string", "nullable": True},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
        "owner_role": {"type": "string", "nullable": True},
        "due_at": {"type": "string", "nullable": True},
        "rtv_state": RTV_STATE,
    },
}

MOVEMENT_PAGE: dict[str, Any] = {
    "type": "object",
    "description": "Page<ResourceSummary>, newest first.",
    "properties": {
        "items": {"type": "array", "items": MOVEMENT_SUMMARY},
        "next_cursor": {"type": "string", "nullable": True},
        "as_of": {"type": "string", "format": "date-time"},
    },
}

DAMAGE_REPORT_FIELDS = (
    "id",
    "record_contract",
    "site_id",
    "source",
    "movement_id",
    "disposition_id",
    "dispatch_id",
    "reported_by",
    "reported_at",
    "quantity",
    "reason_code",
    "evidence_id",
    "state",
    "reviewed_by",
    "reviewed_at",
    "review_reason",
    "release_movement_id",
    "lines",
    "can_reject",
)

DAMAGE_REPORT: dict[str, Any] = {
    "type": "object",
    "description": (
        "DamageReportDTO (design E252/E253; OPS-05; goods tickets 12A and 12B). "
        "Reporting already quarantined the quantity, so `state` says what a second "
        "person decided about it, never whether the goods are available: from the "
        "moment of the report they are not. Confirming is only that decision - it "
        "lifts no other hold, cancels no reservation, grants no acceptance and "
        "establishes no value (`value_damage` is a separate, separately approved "
        "route). `source` is `movement` for damage reported on the stock screen, "
        "`receiving` for damage counted at a GRN or found on its goods before their "
        "PT, `transfer_arrival` for "
        "damage found counting an arriving shipment and `transfer_return` for damage "
        "found as a failed delivery came back to its source (ticket 13C); exactly "
        "one of `movement_id`, "
        "`disposition_id` and `dispatch_id` is set to match; for a receiving report "
        "`disposition_id` is the receipt's own damage-hold record (ticket 05C). "
        "`can_reject` is false only where a rejection could not go ahead: a "
        "receiving report whose pieces were since valued as damaged, one recorded "
        "before ticket 05C without the hold it placed, or - while pending - one whose "
        "hold is no longer over every piece or whose pieces have left the site's "
        "quarantine (ticket 12B). "
        "No cost, value or valuation is ever carried here."
    ),
    "required": list(DAMAGE_REPORT_FIELDS),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "site_id": {"type": "string"},
        "source": {
            "type": "string",
            "enum": ["movement", "receiving", "transfer_arrival", "transfer_return"],
        },
        "movement_id": {"type": "string", "format": "uuid", "nullable": True},
        "disposition_id": {"type": "string", "format": "uuid", "nullable": True},
        "dispatch_id": {"type": "string", "format": "uuid", "nullable": True},
        "reported_by": {
            "type": "object",
            "required": ["id", "name"],
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
        },
        "reported_at": {"type": "string", "format": "date-time"},
        "quantity": {"type": "integer"},
        "reason_code": {"type": "string"},
        "evidence_id": {"type": "string", "nullable": True},
        "state": {
            "type": "string",
            "enum": list(DamageReport.State.values),
            "description": (
                "`closed` is not a review: every piece the report covered went back to the "
                "vendor on an RTV pickup (goods ticket 15B), `reviewed_by` is null and "
                "`review_reason` says `Returned to vendor on <RTV number>`."
            ),
        },
        "reviewed_by": {
            "type": "object",
            "nullable": True,
            "required": ["id", "name"],
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
        },
        "reviewed_at": {"type": "string", "nullable": True},
        "review_reason": {"type": "string", "nullable": True},
        "release_movement_id": {"type": "string", "format": "uuid", "nullable": True},
        "lines": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "can_reject": {"type": "boolean"},
    },
}

DAMAGE_REPORT_PAGE: dict[str, Any] = {
    "type": "object",
    "description": "Page<DamageReportDTO>, newest report first.",
    "required": ["items", "next_cursor", "as_of"],
    "properties": {
        "items": {"type": "array", "items": DAMAGE_REPORT},
        "next_cursor": {"type": "string", "nullable": True},
        "as_of": {"type": "string", "format": "date-time"},
    },
}

DAMAGE_DECISION_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "The second person's decision (E253). Not revision-bound: a report is "
        "decided once, and a second decision is STATE_CONFLICT."
    ),
    "required": ["command_id", "contract_version", "decision", "reason"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "decision": {"type": "string", "enum": list(damage_review.DECISIONS)},
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "additionalProperties": False,
}

STOCK_ROW: dict[str, Any] = {
    "type": "object",
    "description": (
        "StockDTO (E210). Every quantity carries its own name - physical, "
        "valued, accepted, held, reserved, available - and a money property "
        "appears only for a basis the caller's cost field grant allows."
    ),
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
        # The same R27 declaration every stock row carries. E210 takes no
        # ``as_of``, so it is always empty here - published rather than
        # omitted, because the rows come from ``reads.on_hand_rows`` and a
        # field the reader can see belongs in the reader's contract.
        "limitations": stock_views.STOCK_READ_LIMITATIONS,
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        # Present only on a basis the cost field grant allows, and null where the
        # pieces are unvalued: an unknown value is never reported as zero.
        "cost_value_paise": {"type": "string", "nullable": True},
        "ticket_value_paise": {"type": "string", "nullable": True},
    },
}

STOCK_SEARCH_PAGE: dict[str, Any] = {
    "type": "object",
    "description": (
        "Page<StockDTO> with the basis it was read on. ``basis`` labels what the "
        "money columns mean; a cost or ticket basis without the cost field grant "
        "is refused (FIELD_DENIED), never silently answered as quantity."
    ),
    "properties": {
        "items": {"type": "array", "items": STOCK_ROW},
        "next_cursor": {"type": "string", "nullable": True},
        "as_of": {"type": "string", "format": "date-time"},
        "basis": {"type": "string", "enum": list(reads.BASES)},
    },
}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _readable_sites(access: AccessContext) -> frozenset[int] | None:
    """Sites where any movement-read action is granted; ``None`` means everywhere.

    A movement carries no brand, so a brand-limited grant covers none of them:
    ``site_ids`` leaves those grants out, where ``site_reach`` would answer
    "everywhere" and hand a brand reader every site's movements.
    """
    granted = access.all_actions()
    if not any(action in granted for action in READ_ACTIONS):
        raise Refusal("ACTION_DENIED", "You do not have permission to read movements.")
    reach: set[int] = set()
    for action in READ_ACTIONS:
        sites = access.site_ids(action)
        if action in granted and sites is None:
            return None
        reach |= set(sites or ())
    return frozenset(reach)


def _movement_for(access: AccessContext, document_id: uuid.UUID) -> GoodsMovement:
    sites = _readable_sites(access)
    movement = (
        GoodsMovement.objects.select_related("document")
        .filter(tenant_id=access.tenant_id, document_id=document_id)
        .first()
    )
    if movement is None or (sites is not None and movement.document.site_id not in sites):
        raise Refusal("NOT_FOUND", "That movement was not found.")
    return movement


def _allowed(access: AccessContext, movement: GoodsMovement, head: DocumentHead) -> list[str]:
    if movement.kind == GoodsMovement.Kind.RTV:
        return rtv.allowed_actions(access, movement, head)
    site_id = movement.document.site_id
    actions: list[str] = []
    if (
        movement.kind
        in (
            GoodsMovement.Kind.RELEASE,
            GoodsMovement.Kind.WRITEOFF,
            GoodsMovement.Kind.DISPOSAL,
            *movements.ADJUSTMENT_KINDS,
        )
        and head.state == DocumentHead.State.DRAFT
        and access.can(movements.DRAFT_ACTION, site_id=site_id)
    ):
        actions.append("submit")
    if head.state == DocumentHead.State.SUBMITTED and access.can(
        movements.APPROVE_ACTION, site_id=site_id
    ):
        actions.append("decide")
    return actions


def movement_resource(
    access: AccessContext,
    movement: GoodsMovement,
    head: DocumentHead,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    params = params or {}
    offset = decode_cursor(params.get("line_cursor"))
    limit = page_limit(params, default=LINE_PAGE, maximum=LINE_PAGE)
    decode_cursor(params.get("history_cursor"))
    document = movement.document
    content = movements.reviewable_hash(head)
    data = movements.detail(head, offset=offset, limit=limit)
    if movement.kind == GoodsMovement.Kind.ADJUSTMENT_UP:
        opened = adjustments.found_lots(head.live_version)
        for row in data["lines"]["items"]:
            row["found_lot_id"] = opened.get(str(row["line_key"]))
    if movement.kind == GoodsMovement.Kind.WRITEOFF:
        data["write_off"] = writeoff.detail(access, movement, head)
    if movement.kind == GoodsMovement.Kind.DISPOSAL:
        data["disposal"] = disposal.detail(access, movement, head)
    if movement.kind in (
        GoodsMovement.Kind.RTV,
        GoodsMovement.Kind.WRITEOFF,
        GoodsMovement.Kind.DISPOSAL,
    ):
        if movement.kind == GoodsMovement.Kind.RTV:
            data["rtv"] = rtv.detail(movement, head)
            data["rtv"]["shipments"] = rtv_shipments.shipments(access, movement)
        data["exceptions"] = (
            adjustments.owned_work(document.pk)
            if any(
                access.can_at_store(action, document.site_id)
                for action in ("exception.view", "exception.manage")
            )
            else None
        )
    if movement.kind in movements.ADJUSTMENT_KINDS:
        site_id = document.site_id
        # The reference is named to whoever drafts, decides or reads PTs here;
        # the owned work only to someone who may read exceptions at the site.
        # Neither widens a read: both are the grants those records already use.
        data["source_document"] = (
            adjustments.named_reference(movement)
            if any(
                access.can_at_store(action, site_id)
                for action in (movements.DRAFT_ACTION, movements.APPROVE_ACTION, "pt.view")
            )
            else None
        )
        data["exceptions"] = (
            adjustments.owned_work(document.pk)
            if any(
                access.can_at_store(action, site_id)
                for action in ("exception.view", "exception.manage")
            )
            else None
        )
    resource = resource_dto(
        id=document.pk,
        data=data,
        revision=head.revision,
        state=head.state,
        context={"site_id": document.site_id, "entity_id": document.entity_id},
        number=document.official_number,
        version=head.live_version.version if head.live_version is not None else None,
        allowed_actions=_allowed(access, movement, head),
        content={"hash": content},
    )
    # The published hash is the *reviewable content's* own hash, not a hash of
    # this paged read: a submission quotes it back as `reviewed_hash`, so it must
    # not change when a caller turns to the second page of lines.
    resource["content_hash"] = content
    return resource


# ---------------------------------------------------------------------------
# E104 / E105: read movements
# ---------------------------------------------------------------------------


class MovementListCreateView(GoodsAPIView):
    """E104 list; E151 create. A bin move or hold posts here; a release is drafted."""

    http_method_names = ["get", "head", "post", "options"]

    @extend_schema(responses=_responses(200, MOVEMENT_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        # E104's declared input is ListQuery and nothing else, so `q` is the only
        # filter here. It is matched in the database, and the page is cut from the
        # whole authorised set rather than from a fixed window: a read that stops
        # at row 1000 with a null cursor is a silent truncation (E104 step 6).
        params = check_query(request, LIST_QUERY_KEYS)
        sites = _readable_sites(access)
        wanted = parse_int_id(params["site_id"], "site_id") if params.get("site_id") else None
        if wanted is not None:
            if sites is not None and wanted not in sites:
                raise Refusal("NOT_FOUND", "That site was not found.")
            sites = frozenset({wanted})
        queryset = movements.visible_movements(access.tenant_id, sites)
        text = (params.get("q") or "").strip()
        if text:
            queryset = queryset.filter(document__official_number__icontains=text)
        rows = list(queryset)
        heads = {
            head.document_id: head
            for head in DocumentHead.objects.select_related(
                "live_version", "document", "draft_revision"
            ).filter(document_id__in=[row.document_id for row in rows])
        }
        summaries = [movements.summary(row, heads.get(row.document_id)) for row in rows]
        window, cursor = paginate(summaries, params)
        return Response(page(window, cursor))

    @extend_schema(
        description=(
            "E151 bin move / hold / release, and E152 (goods ticket 15A) evidenced "
            "adjustments: `kind` `adjustment_down` or `shrinkage` (lines name "
            "`lot_id` or `sku_id`, `qty`, `source_location_id`, optional `condition`/"
            "`origin_id`) or `adjustment_up` (new-found lines: `sku_id`, `qty`, optional "
            "`description` and same-site `cost_evidence_origin_id`). Needs "
            "`movement.draft` at the site, a `reason_code` and evidence - "
            "`evidence_ids` and/or `evidence_note`. An adjustment is drafted with its "
            "exact portions frozen; nothing moves until a different person with "
            "`movement.approve` (the Owner, GSA-R01) approves it through E155 and the "
            "approvals decision, which posts P13 (removal at each portion's recorded "
            "layer cost) or P14 (a new held lot, valued only with origin evidence). No "
            "GL, vendor, cash or tax entry. Refusals: INVALID_REQUEST (schema); "
            "MOVEMENT_INVALID 422 with issue EVIDENCE_REQUIRED, "
            "INSUFFICIENT_ELIGIBLE_STOCK/RESERVED/HOLD_ACTIVE (reserved or held pieces "
            "are never taken), SOURCE_NOT_REFERENCED, REFERENCE_NOT_SUPPORTED, "
            "TRANSFER_EXCESS_ROUTE (ticket 16A), CONDITION_NOT_GOOD, EVIDENCE_NOT_FOUND, "
            "NOT_FOR_REMOVAL; SUPPLEMENT_ROUTE_REQUIRED 409 with issue RECEIPT_EXCESS "
            "(receipt excess never uses an adjustment), UNVALUED_CUSTODY or "
            "NO_DOWNSTREAM_USE (reverse the PT and correct the GRN instead); "
            "COST_EVIDENCE_INVALID 422; NOT_FOUND; ACTION_DENIED; CONTRACT_DISABLED, "
            "SITE_NOT_READY, UNDER_COUNT 409; COMMAND_CONFLICT on a changed replay. "
            "Goods ticket 15B, `kind` `rtv` (E154): header `vendor_id` (the vendor that agreed "
            "to take the goods back) and `agreement_reference` (its reference, GSA-R04; the "
            "system does not evaluate return terms), optional `evidence_ids`/`evidence_note`; "
            "lines name `lot_id` or `sku_id`, `qty`, `source_location_id` (the site's "
            "quarantine for recorded pieces under a hold, or a storage location for accepted "
            "good stock nobody holds) and optional `origin_id`/`condition`. The draft freezes "
            "the exact oldest eligible pieces; nothing is reserved or moved until the Owner "
            "approves it through E155 and the approvals decision, which reserves exactly those "
            "pieces (P15). Refusals as above plus MOVEMENT_INVALID issues AGREEMENT_REQUIRED, "
            "LOCATION_NOT_ELIGIBLE, WRONG_SITE, NOT_FOR_RTV, VENDOR_INACTIVE, "
            "INSUFFICIENT_ELIGIBLE_STOCK with RESERVED/HOLD_ACTIVE/NOT_HELD/UNVALUED_CUSTODY/"
            "NOT_ACCEPTED/CONDITION_NOT_GOOD/WRITTEN_OFF counts (pre-PT custody returns through "
            "its GRN, ticket 15G; a written-off piece waits for its disposal route); NOT_FOUND "
            "for a vendor or location outside the tenant. "
            "Goods ticket 15C, `kind` `writeoff` (E153): a reason and evidence - `evidence_ids` "
            "and/or `evidence_note` - and lines naming `lot_id` or `sku_id`, `qty`, "
            "`source_location_id` (the site's own quarantine) and optional `origin_id`/"
            "`condition`. The draft freezes the oldest company-owned recorded pieces standing "
            "there under a hold that nobody has reserved and no write-off has already taken. "
            "Nothing is recognised until the Owner - a different person - approves it through "
            "E155 and the approvals decision (policy purpose `writeoff`), which posts P20 once: "
            "value stock -> external at each portion's recorded layer cost and a `write_off` "
            "hold over the same pieces, with no quantity leg - the goods stay where they are, "
            "in quarantine, under every hold they had. No GL, vendor, cash or tax entry. "
            "Refusals as above plus MOVEMENT_INVALID issues EVIDENCE_REQUIRED, "
            "LOCATION_NOT_ELIGIBLE, WRONG_SITE, NOT_FOR_WRITEOFF, COUNT_OWNED, VENDOR_OWNED "
            "(vendor-owned goods leave through an RTV), and INSUFFICIENT_ELIGIBLE_STOCK with "
            "UNVALUED_CUSTODY (no recorded cost, e.g. pre-PT custody: value stays unknown, "
            "never zero)/MEMO_VALUE_ONLY (a value_damage memo is not recorded cost)/NOT_HELD/"
            "RESERVED/WRITTEN_OFF counts. "
            "Goods ticket 15D, `kind` `disposal`: the record of goods actually destroyed or "
            "handed over for scrap/recycling. Header `reason_code`; the `disposal` object "
            "(`method` `destruction`|`scrap_handover`, `disposed_at` - the actual time, not in "
            "the future - `handed_over_to` for a handover, optional `scrap_proceeds_paise` for "
            "a handover and `evidence_reference`); evidence of the destruction or handover - "
            "`evidence_ids`, `evidence_note` and/or `disposal.evidence_reference`; optional "
            "`source_document_id` naming an approved write-off at the site; lines as for a "
            "write-off. Without a write-off the draft freezes the oldest company-owned "
            "recorded pieces held in the site's quarantine that nobody has reserved and no "
            "write-off has touched; naming one, it freezes only that write-off's pieces still "
            "there and unreserved. Nothing leaves the books until the Owner - a different "
            "person - approves it through E155 and the approvals decision (policy purpose "
            "`disposal`), which numbers it DSP and posts P21 once: the frozen pieces go to the "
            "disposed boundary, every hold over exactly them ends (pending damage reports whose "
            "last piece went close as disposed of, their hold work resolves) and every other "
            "piece stays in quarantine under every hold it has; value stock -> external at "
            "recorded layer cost only for pieces no write-off covered. Scrap proceeds are "
            "recorded and never posted. No GL, vendor, cash or tax entry. Refusals as above "
            "plus INVALID_REQUEST for a malformed `disposal` object; MOVEMENT_INVALID issues "
            "EVIDENCE_REQUIRED, METHOD_NOT_SUPPORTED (donation, sale or any other method), "
            "RECIPIENT_REQUIRED, METHOD_FIELD_MISMATCH, EVENT_TIME_INVALID, "
            "LOCATION_NOT_ELIGIBLE, WRONG_SITE, NOT_FOR_DISPOSAL, COUNT_OWNED, VENDOR_OWNED, "
            "SOURCE_NOT_WRITE_OFF, WRITE_OFF_NOT_APPROVED, and INSUFFICIENT_ELIGIBLE_STOCK with "
            "UNVALUED_CUSTODY (pre-PT custody: ticket 15E)/MEMO_VALUE_ONLY/NOT_HELD/"
            "RTV_RESERVED (withdraw the pieces from the pending RTV first)/RESERVED/"
            "WRITTEN_OFF (name the write-off)/NOT_IN_WRITE_OFF counts; NOT_FOUND for a "
            "write-off or location outside the site."
        ),
        responses=_responses(201, MOVEMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        # The ``disposal`` facts object belongs to a disposal alone (goods ticket
        # 15D); every other kind still refuses it as an unknown field.
        allowed = set(movements.PAYLOAD_FIELDS)
        if request.data.get("kind") == movements.DISPOSAL_KIND:
            allowed.add(disposal.HEADER_FIELD)
        body = business_body(request.data, allowed)
        if body.get("kind") in adjustments.KINDS:
            return self._adjust(request, access, meta, body)
        if body.get("kind") == movements.RTV_KIND:
            return self._rtv(request, access, meta, body)
        if body.get("kind") == movements.WRITEOFF_KIND:
            return self._writeoff(request, access, meta, body)
        if body.get("kind") == movements.DISPOSAL_KIND:
            return self._dispose(request, access, meta, body)
        payload = movements.parse(body)
        access.require(movements.DRAFT_ACTION, site_id=payload.site_id)

        def handler(run: CommandRun) -> CommandResult:
            identity, state = movements.create(run, payload)
            return CommandResult(
                resource_type="movement",
                resource_id=str(identity.pk),
                status_code=201,
                revision=1,
                issue_codes=[state],
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.movement.create",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(line.lot_id) for line in payload.lines],
            site_id=payload.site_id,
        )
        return Response(_read_back(access, result), status=result.status_code)

    def _adjust(self, request: Request, access: AccessContext, meta: Any, body: Any) -> Response:
        """Goods ticket 15A (E152 on this route): draft an evidenced adjustment."""
        payload = adjustments.parse(body)
        access.require(movements.DRAFT_ACTION, site_id=payload.site_id)

        def handler(run: CommandRun) -> CommandResult:
            identity, state = adjustments.create(run, payload)
            return CommandResult(
                resource_type="movement",
                resource_id=str(identity.pk),
                status_code=201,
                revision=1,
                issue_codes=[state],
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.movement.create",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=sorted(
                {str(line.lot_id) for line in payload.removals if line.lot_id}
                | {str(line.sku_id) for line in payload.removals if line.sku_id}
                | {str(line.sku_id) for line in payload.found}
            ),
            site_id=payload.site_id,
        )
        return Response(_read_back(access, result), status=result.status_code)

    def _rtv(self, request: Request, access: AccessContext, meta: Any, body: Any) -> Response:
        """Goods ticket 15B (E154 on this route): draft a return to vendor."""
        payload = rtv.parse(body)
        access.require(movements.DRAFT_ACTION, site_id=payload.site_id)

        def handler(run: CommandRun) -> CommandResult:
            identity, state = rtv.create(run, payload)
            return CommandResult(
                resource_type="movement",
                resource_id=str(identity.pk),
                status_code=201,
                revision=1,
                issue_codes=[state],
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.movement.create",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=sorted(
                {str(line.lot_id) for line in payload.lines if line.lot_id}
                | {str(line.sku_id) for line in payload.lines if line.sku_id}
            ),
            site_id=payload.site_id,
        )
        return Response(_read_back(access, result), status=result.status_code)

    def _writeoff(self, request: Request, access: AccessContext, meta: Any, body: Any) -> Response:
        """Goods ticket 15C (E153 on this route): draft a write-off without disposal."""
        payload = writeoff.parse(body)
        access.require(movements.DRAFT_ACTION, site_id=payload.site_id)

        def handler(run: CommandRun) -> CommandResult:
            identity, state = writeoff.create(run, payload)
            return CommandResult(
                resource_type="movement",
                resource_id=str(identity.pk),
                status_code=201,
                revision=1,
                issue_codes=[state],
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.movement.create",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=sorted(
                {str(line.lot_id) for line in payload.lines if line.lot_id}
                | {str(line.sku_id) for line in payload.lines if line.sku_id}
            ),
            site_id=payload.site_id,
        )
        return Response(_read_back(access, result), status=result.status_code)

    def _dispose(self, request: Request, access: AccessContext, meta: Any, body: Any) -> Response:
        """Goods ticket 15D (on this route): record a disposal of recorded stock."""
        payload = disposal.parse(body)
        access.require(movements.DRAFT_ACTION, site_id=payload.site_id)

        def handler(run: CommandRun) -> CommandResult:
            identity, state = disposal.create(run, payload)
            return CommandResult(
                resource_type="movement",
                resource_id=str(identity.pk),
                status_code=201,
                revision=1,
                issue_codes=[state],
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.movement.create",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=sorted(
                {str(line.lot_id) for line in payload.lines if line.lot_id}
                | {str(line.sku_id) for line in payload.lines if line.sku_id}
            ),
            site_id=payload.site_id,
        )
        return Response(_read_back(access, result), status=result.status_code)


def _read_back(access: AccessContext, result: CommandResult) -> dict[str, Any]:
    """The authoritative result, re-read after the command committed."""
    document_id = uuid.UUID(str(result.resource_id))
    movement = _movement_for(access, document_id)
    head = DocumentHead.objects.select_related("live_version", "document", "draft_revision").get(
        document_id=document_id
    )
    return movement_resource(access, movement, head)


ADJUSTMENT_REFERENCES: dict[str, Any] = {
    "type": "object",
    "description": "The receipt documents behind one stock layer at the site.",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "kind": {"type": "string", "enum": ["RPT", "OPT", "GRN"]},
                    "number": {"type": "string"},
                },
            },
        }
    },
}


class AdjustmentReferencesView(GoodsAPIView):
    """Goods ticket 15A: the original PT and GRN an adjust-down may reference.

    `GET .../movements/adjustment-references?site_id=&origin_id=` answers, for
    one stock row's origin (layer) at the site, the PT that recorded it and that
    PT's GRN, by number. Needs `movement.draft` at the site; an origin of another
    site or tenant answers an empty list. Refusals: INVALID_REQUEST, ACTION_DENIED.
    """

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_movements_adjustment_references",
        parameters=[
            OpenApiParameter("site_id", int, required=True),
            OpenApiParameter("origin_id", str, required=True),
        ],
        responses=_responses(200, ADJUSTMENT_REFERENCES, _READ_REFUSALS),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, frozenset({"site_id", "origin_id"}))
        site_id = parse_int_id(params.get("site_id") or "", "site_id")
        try:
            origin_id = uuid.UUID(params.get("origin_id") or "")
        except ValueError:
            raise Refusal("INVALID_REQUEST", "origin_id must be an ID.") from None
        access.require(movements.DRAFT_ACTION, site_id=site_id)
        return Response(
            {"items": adjustments.references_for_origin(access.tenant_id, site_id, origin_id)}
        )


class MovementDetailView(GoodsAPIView):
    """E105: one movement, its frozen header and its independently paged lines."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        # The list sits at the same prefix, so drf-spectacular would break the tie
        # with a positional numeral and rename somebody else's client method the
        # next time a route is added here.
        operation_id="goods_v1_outbound_movements_detail",
        responses=_responses(200, MOVEMENT_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, DETAIL_QUERY)
        movement = _movement_for(access, pk)
        head = DocumentHead.objects.select_related(
            "live_version", "document", "draft_revision"
        ).get(document_id=pk)
        if params.get("version"):
            wanted = parse_int_id(params["version"], "version")
            live = head.live_version.version if head.live_version is not None else None
            if live != wanted:
                raise Refusal("VERSION_NOT_FOUND", "That version of this movement was not found.")
        return Response(movement_resource(access, movement, head, params))


class MovementSubmitView(GoodsAPIView):
    """E155: send a drafted release or adjustment to its distinct approval. Never automatic."""

    http_method_names = ["post", "options"]

    @extend_schema(
        description=(
            "Send a drafted release or adjustment (E196/E197) for a distinct "
            "`movement.approve` decision; body `{reviewed_hash}` plus MutationMeta "
            "with `expected_revision`. Nothing moves. An adjustment re-checks its "
            "frozen portions (still standing, unreserved, unheld, valued), pins the "
            "approval policy for its purpose with its quantity and recorded-layer "
            "value, and opens an owned `approval_pending` exception "
            "(ADJUSTMENT_APPROVAL). An RTV (goods ticket 15B) does the same with its own "
            "pools - quarantined pieces still under a hold, or good stock still unheld, and "
            "none reserved - pins the policy for purpose `rtv` and opens `approval_pending` "
            "(RTV_APPROVAL). A write-off (goods ticket 15C) rechecks its frozen pieces - still "
            "in quarantine under a hold, recorded at cost, company-owned, unreserved and not "
            "already written off - pins the policy for purpose `writeoff` with its quantity and "
            "recorded-layer value, and opens `approval_pending` (WRITE_OFF_APPROVAL). "
            "A disposal (goods ticket 15D) rechecks its frozen pieces - still in quarantine "
            "under a hold, recorded at cost, company-owned, not reserved (RTV_RESERVED names a "
            "pending RTV to withdraw them from first; RESERVED any other), and either untouched "
            "by any write-off or, when it names one, that write-off's pieces - pins the policy "
            "for purpose `disposal` with its quantity and the loss it recognises itself (none "
            "after a write-off), and opens `approval_pending` (DISPOSAL_APPROVAL). "
            "Refusals: REVISION_SUPERSEDED, MOVEMENT_INVALID, "
            "SUPPLEMENT_ROUTE_REQUIRED, APPROVAL_POLICY_BLOCKED, UNDER_COUNT, NOT_FOUND, "
            "ACTION_DENIED."
        ),
        responses=_responses(200, MOVEMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reviewed_hash"}, required=["reviewed_hash"])
        reviewed = str(body["reviewed_hash"])
        movement = _movement_for(access, pk)
        access.require(movements.DRAFT_ACTION, site_id=movement.document.site_id)

        def handler(run: CommandRun) -> CommandResult:
            _movement, request_id = movements.submit(
                run, pk, reviewed_hash=reviewed, expected_revision=meta.expected_revision
            )
            return CommandResult(
                resource_type="movement", resource_id=str(pk), event_ids=[str(request_id)]
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.movement.submit",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=movement.document.site_id,
            subject_key=f"movement:{pk}",
            reviewed_hash=reviewed,
        )
        return Response(_read_back(access, result), status=result.status_code)


RTV_PICKUP_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "One confirmed handover to the vendor or its pickup representative (goods ticket "
        "15B). `lines` are the pieces actually collected per RTV line; `left_behind` gives "
        "a reason for every piece of every line still waiting after this pickup, split "
        "across quantities as needed. Evidence is at least one of `evidence_reference`, "
        "`evidence_ids` or `evidence_note`. Not revision-bound: replaying the same "
        "`command_id` returns the first result."
    ),
    "required": ["command_id", "contract_version", "collected_by", "lines"],
    "additionalProperties": False,
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "picked_up_at": {
            "type": "string",
            "format": "date-time",
            "description": (
                "When the handover actually happened; not later than now. Defaults to now."
            ),
        },
        "collected_by": {"type": "string", "minLength": 1, "maxLength": rtv.MAX_COLLECTOR},
        "evidence_reference": {"type": "string", "maxLength": rtv.MAX_REFERENCE},
        "evidence_ids": {
            "type": "array",
            "maxItems": rtv.MAX_EVIDENCE,
            "items": {"type": "string", "format": "uuid"},
        },
        "evidence_note": {"type": "string", "maxLength": rtv.MAX_NOTE},
        "lines": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["line_key", "qty"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer", "minimum": 1},
                },
            },
        },
        "left_behind": {"type": "array", "items": RTV_LEFT_BEHIND_INPUT},
    },
}

RTV_WITHDRAWAL_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "Explicitly withdraw pieces from an RTV's pending balance (goods ticket 15B). "
        "Omit `lines` to withdraw the whole outstanding balance. `other` needs a remark."
    ),
    "required": ["command_id", "contract_version", "reason"],
    "additionalProperties": False,
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "reason": {"type": "string", "enum": list(rtv.WITHDRAWAL_REASONS)},
        "remark": {"type": "string", "maxLength": rtv.MAX_REMARK},
        "lines": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["line_key", "qty"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
}


class RtvPickupView(GoodsAPIView):
    """Goods ticket 15B: record one confirmed vendor pickup against an approved RTV."""

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_movements_rtv_pickups",
        description=(
            "Confirm a handover to the vendor or its representative (transfers PRD §7). "
            "Needs `rtv.execute` at the RTV's site (Store person or Warehouse, GSA-R01). "
            "Exactly the collected pieces leave, oldest reserved first, in one P15 posting: "
            "their reservation is consumed, they go to the `returned` boundary, value moves "
            "stock -> external at each portion's own recorded cost, and every hold over "
            "exactly those pieces ends (its `stock_hold_active` work closes once nothing "
            "stands under it). A pending damage report whose every piece has now gone back "
            "to the vendor is closed (`closed`, note `Returned to vendor on <RTV number>`, "
            "no reviewer; Anand's 15B decision 3) and named in the pickup event's "
            "`closed_damage_report_ids`; a report with pieces still here stays pending. "
            "Pieces left behind stay reserved to the RTV whatever the "
            "reason. No GL, vendor, cash, credit-note or tax entry. When nothing is reserved "
            "any more the RTV closes: `completed` if every approved piece was collected, "
            "otherwise `closed_partially_returned`, and its `rtv_not_dispatched` work "
            "closes. Refusals: INVALID_REQUEST (schema, unknown reason), NOT_FOUND (outside "
            "the caller's reach), ACTION_DENIED, EVENT_TIME_INVALID (a future time), "
            "RTV_STATE_CONFLICT 409 (not an approved RTV with a balance waiting, or reserved "
            "pieces no longer at the site), MOVEMENT_INVALID 422 with issue "
            "EVIDENCE_REQUIRED, UNKNOWN_LINE, EXCEEDS_OUTSTANDING (cumulative departure "
            "never exceeds the approval), REASONS_INCOMPLETE (every piece left behind needs "
            "a reason), REMARK_REQUIRED, REASON_CONFLICT or EVIDENCE_NOT_FOUND; "
            "CONTRACT_DISABLED, SITE_NOT_READY, UNDER_COUNT 409; COMMAND_CONFLICT on a "
            "changed replay."
        ),
        request={"application/json": RTV_PICKUP_REQUEST},
        responses=_responses(201, MOVEMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, rtv.PICKUP_FIELDS)
        movement = _movement_for(access, pk)
        site_id = movement.document.site_id
        access.require(rtv.EXECUTE_ACTION, site_id=site_id)

        def handler(run: CommandRun) -> CommandResult:
            event = rtv.pickup(run, pk, rtv.parse_pickup(body, run.now))
            return CommandResult(
                resource_type="movement",
                resource_id=str(pk),
                status_code=201,
                event_ids=[str(event.pk)],
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.rtv.pickup",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=site_id,
            subject_key=f"movement:{pk}",
        )
        return Response(_read_back(access, result), status=result.status_code)


class RtvWithdrawalView(GoodsAPIView):
    """Goods ticket 15B: explicitly withdraw pieces from an RTV's pending balance."""

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_movements_rtv_withdrawals",
        description=(
            "Withdraw outstanding pieces from an approved RTV (quarantine outcomes §5, "
            "transfers PRD §3). Needs `movement.draft` or `movement.approve` at the RTV's "
            "site. Only the reservation of the withdrawn pieces is released (P16): nothing "
            "moves, the pieces keep their location and condition, every hold over them "
            "stays in force (quarantined goods stay unavailable) and every completed pickup "
            "keeps its evidence. Omitting `lines` withdraws the whole balance: before any "
            "pickup that cancels the RTV (`cancelled`), after one it closes it as "
            "`closed_partially_returned`. A reason recorded at a pickup is never a "
            "withdrawal by itself. Refusals: INVALID_REQUEST, NOT_FOUND, ACTION_DENIED, "
            "RTV_STATE_CONFLICT 409 (not an approved RTV with a balance waiting), "
            "MOVEMENT_INVALID 422 with issue UNKNOWN_LINE, EXCEEDS_OUTSTANDING or "
            "REMARK_REQUIRED; CONTRACT_DISABLED, SITE_NOT_READY, UNDER_COUNT 409; "
            "COMMAND_CONFLICT on a changed replay."
        ),
        request={"application/json": RTV_WITHDRAWAL_REQUEST},
        responses=_responses(201, MOVEMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, rtv.WITHDRAW_FIELDS, required=["reason"])
        parsed = rtv.parse_withdrawal(body)
        movement = _movement_for(access, pk)
        site_id = movement.document.site_id
        if not any(access.can(action, site_id=site_id) for action in rtv.WITHDRAW_ACTIONS):
            raise Refusal(
                "ACTION_DENIED", "Withdrawing an RTV balance needs movement.draft or approve."
            )

        def handler(run: CommandRun) -> CommandResult:
            event = rtv.withdraw(run, pk, parsed)
            return CommandResult(
                resource_type="movement",
                resource_id=str(pk),
                status_code=201,
                event_ids=[str(event.pk)],
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.rtv.withdraw",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=site_id,
            subject_key=f"movement:{pk}",
        )
        return Response(_read_back(access, result), status=result.status_code)


_EVIDENCE_PROPERTIES: dict[str, Any] = {
    "evidence_ids": {
        "type": "array",
        "maxItems": rtv.MAX_EVIDENCE,
        "items": {"type": "string", "format": "uuid"},
    },
    "evidence_note": {"type": "string", "maxLength": rtv.MAX_NOTE},
}
_META_PROPERTIES: dict[str, Any] = {
    "command_id": {"type": "string", "format": "uuid"},
    "contract_version": {"type": "string", "enum": ["goods-v1"]},
}
_QTY_LINES: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "object",
        "required": ["line_key", "qty"],
        "properties": {
            "line_key": {"type": "string", "format": "uuid"},
            "qty": {"type": "integer", "minimum": 1},
        },
    },
}

RTV_SHIPMENT_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "One shipment of an approved RTV for delivery to the vendor (goods ticket 15F). "
        "`lines` are the pieces that actually left per RTV line; `carrier` names the "
        "transporter or courier. Evidence is at least one of `evidence_reference` "
        "(challan or docket), `evidence_ids` or `evidence_note`. `eway_reference` is "
        "optional: a shipment without one is recorded and opens `eway_missing` work. Not "
        "revision-bound: replaying the same `command_id` returns the first result."
    ),
    "required": ["command_id", "contract_version", "carrier", "lines"],
    "additionalProperties": False,
    "properties": {
        **_META_PROPERTIES,
        "shipped_at": {
            "type": "string",
            "format": "date-time",
            "description": "When the goods actually left; not later than now. Defaults to now.",
        },
        "carrier": {"type": "string", "minLength": 1, "maxLength": rtv_shipments.MAX_CARRIER},
        "evidence_reference": {"type": "string", "maxLength": rtv.MAX_REFERENCE},
        **_EVIDENCE_PROPERTIES,
        "eway_reference": {"type": "string", "maxLength": rtv.MAX_REFERENCE},
        "lines": _QTY_LINES,
    },
}

RTV_ACKNOWLEDGEMENT_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "What the vendor says it received of one shipment (goods ticket 15F, GSA-T15). "
        "`lines` names every line the shipment carried exactly once with the acknowledged "
        "quantity (0 allowed): a cumulative snapshot that replaces the previous one, never "
        "an addition. `reviewed_hash` is the shipment's `state_hash` as read. Evidence is "
        "at least one of `recipient_reference`, `evidence_ids` or `evidence_note`."
    ),
    "required": ["command_id", "contract_version", "reviewed_hash", "lines"],
    "additionalProperties": False,
    "properties": {
        **_META_PROPERTIES,
        "acknowledged_at": {
            "type": "string",
            "format": "date-time",
            "description": (
                "When the vendor received the goods (or said so); not before the shipment "
                "left and not later than now. Defaults to now."
            ),
        },
        "recipient_reference": {"type": "string", "maxLength": rtv.MAX_REFERENCE},
        **_EVIDENCE_PROPERTIES,
        "reviewed_hash": {"type": "string", "minLength": 64, "maxLength": 64},
        "lines": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["line_key", "qty"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer", "minimum": 0},
                },
            },
        },
    },
}

RTV_RETURN_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "Goods of one shipment actually back at the source after a failed delivery (goods "
        "ticket 15F, transfers PRD §6). Per shipped line, how many came back `good` (as they "
        "left) and how many were found `damaged`. `reason` says why the delivery failed; "
        "evidence is at least one of `evidence_reference`, `evidence_ids` or `evidence_note`."
    ),
    "required": ["command_id", "contract_version", "reason", "lines"],
    "additionalProperties": False,
    "properties": {
        **_META_PROPERTIES,
        "returned_at": {
            "type": "string",
            "format": "date-time",
            "description": "When the goods came back; not before they left, not later than now.",
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": rtv_shipments.MAX_REASON},
        "evidence_reference": {"type": "string", "maxLength": rtv.MAX_REFERENCE},
        **_EVIDENCE_PROPERTIES,
        "lines": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["line_key"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "good": {"type": "integer", "minimum": 0},
                    "damaged": {"type": "integer", "minimum": 0},
                },
            },
        },
    },
}

RTV_PUTAWAY_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "Put returned good pieces of one shipment away at the source (goods ticket 15F): "
        "per shipped line, how many and into which ordinary storage location."
    ),
    "required": ["command_id", "contract_version", "lines"],
    "additionalProperties": False,
    "properties": {
        **_META_PROPERTIES,
        "lines": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["line_key", "qty", "destination_location_id"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer", "minimum": 1},
                    "destination_location_id": {"type": "string", "format": "uuid"},
                },
            },
        },
    },
}

RTV_EWAY_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "Attach an e-way reference to one shipment, or verify the one on file (goods ticket "
        "15F, transfers PRD §8)."
    ),
    "required": ["command_id", "contract_version", "action", "reference"],
    "additionalProperties": False,
    "properties": {
        **_META_PROPERTIES,
        "action": {"type": "string", "enum": list(rtv_shipments.EWAY_ACTIONS)},
        "reference": {"type": "string", "minLength": 1, "maxLength": rtv.MAX_REFERENCE},
        "note": {"type": "string", "maxLength": rtv_shipments.MAX_REASON},
    },
}


RTV_CLOSURE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "Prepare the closure of one shipment's persistent vendor acknowledgement shortfall "
        "for the Owner (goods ticket 15H, goods PRD §14.10 GSA-R07). `reason` says why the "
        "difference is closed; evidence is at least one of `evidence_reference` (the "
        "vendor's letter), `evidence_ids` (a photo) or `evidence_note`. `reviewed_hash` is "
        "the shipment's `state_hash` as read. The closure covers exactly the pieces the "
        "latest acknowledgement leaves neither acknowledged nor back; it names no quantity."
    ),
    "required": ["command_id", "contract_version", "reason", "reviewed_hash"],
    "additionalProperties": False,
    "properties": {
        **_META_PROPERTIES,
        "reason": {"type": "string", "minLength": 1, "maxLength": rtv_shipments.MAX_REASON},
        "evidence_reference": {"type": "string", "maxLength": rtv.MAX_REFERENCE},
        **_EVIDENCE_PROPERTIES,
        "reviewed_hash": {"type": "string", "minLength": 64, "maxLength": 64},
    },
}


class _RtvShipmentCommand(GoodsAPIView):
    """Shared body of the five shipped-RTV commands: scope, grant, command, read-back."""

    http_method_names = ["post", "options"]

    def _run(
        self,
        request: Request,
        pk: uuid.UUID,
        *,
        fields: frozenset[str],
        required: list[str],
        allowed: tuple[str, ...],
        denied: str,
        action: str,
        command: Callable[[CommandRun, dict[str, Any]], RtvEvent],
    ) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, fields, required=required)
        movement = _movement_for(access, pk)
        site_id = movement.document.site_id
        if not any(access.can(grant, site_id=site_id) for grant in allowed):
            raise Refusal("ACTION_DENIED", denied)

        def handler(run: CommandRun) -> CommandResult:
            event = command(run, body)
            return CommandResult(
                resource_type="movement",
                resource_id=str(pk),
                status_code=201,
                event_ids=[str(event.pk)],
            )

        result = self.run_command(
            request,
            access=access,
            action=action,
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=site_id,
            subject_key=f"movement:{pk}",
        )
        return Response(_read_back(access, result), status=result.status_code)


class RtvShipmentView(_RtvShipmentCommand):
    """Goods ticket 15F: record one shipment of an approved RTV for delivery to the vendor."""

    @extend_schema(
        operation_id="goods_v1_outbound_movements_rtv_shipments",
        description=(
            "Record one bounded dispatch of an approved RTV for delivery to the vendor "
            "(transfers PRD §§4, 7; E156 adapted). Needs `rtv.execute` at the RTV's site "
            "(Store person or Warehouse, GSA-R01). Exactly the pieces sent leave, oldest "
            "reserved first, in one P15 posting - as a pickup: reservation consumed, pieces to "
            "the `returned` boundary, value stock -> external at each portion's own recorded "
            "cost, every hold over exactly those pieces ends. The shipment keeps, per piece, "
            "the condition, location and holds it left with. A pending damage report whose "
            "every piece has left is closed (note `Shipped to vendor on <RTV number>`). "
            "Departure is not vendor receipt: the rest of the approval stays reserved until "
            "another departure or an explicit withdrawal, the shipment opens "
            "`rtv_receipt_pending` work (14-day reminder, C-WHO) and, with no "
            "`eway_reference`, `eway_missing` work; the RTV stays `initiated` until the "
            "shipment is accounted for, even when nothing is reserved (its "
            "`rtv_not_dispatched` work then closes). No GL, vendor, cash, credit-note or tax "
            "entry. Refusals: INVALID_REQUEST, NOT_FOUND, ACTION_DENIED, EVENT_TIME_INVALID, "
            "RTV_STATE_CONFLICT 409 (not an approved RTV with a balance waiting, or reserved "
            "pieces no longer at the site), MOVEMENT_INVALID 422 with issue EVIDENCE_REQUIRED, "
            "UNKNOWN_LINE, EXCEEDS_OUTSTANDING or EVIDENCE_NOT_FOUND; CONTRACT_DISABLED, "
            "SITE_NOT_READY, UNDER_COUNT 409 (a count freezes the site); COMMAND_CONFLICT on a "
            "changed replay."
        ),
        request={"application/json": RTV_SHIPMENT_REQUEST},
        responses=_responses(201, MOVEMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        return self._run(
            request,
            pk,
            fields=rtv_shipments.SHIP_FIELDS,
            required=["carrier", "lines"],
            allowed=(rtv_shipments.EXECUTE_ACTION,),
            denied="Recording a shipment needs rtv.execute at the RTV's site.",
            action="stock.rtv.ship",
            command=lambda run, body: rtv_shipments.ship(
                run, pk, rtv_shipments.parse_shipment(body, run.now)
            ),
        )


class RtvAcknowledgementView(_RtvShipmentCommand):
    """Goods ticket 15F: record the vendor's acknowledgement of one shipment."""

    @extend_schema(
        operation_id="goods_v1_outbound_movements_rtv_shipment_acknowledgements",
        description=(
            "Record what the vendor acknowledged receiving of one shipment (GSA-T15; E157 "
            "adapted). Needs `rtv.execute` at the RTV's site. Posts nothing: no stock, value, "
            "books or credit effect. The snapshot replaces the previous one and supersedes a "
            "shortfall closure waiting for the Owner (15H); the shipment's "
            "`rtv_receipt_pending` work closes. If every piece still away is acknowledged the "
            "shipment is `delivered` (or `partly_returned`), and the RTV closes when nothing "
            "else is open. A shortfall keeps the departure, restores no stock, writes nothing "
            "off, raises no credit and confirms nothing: the shipment is `short_acknowledged` "
            "and owned `rtv_acknowledgement_discrepancy` work opens (C-OWN, 30-day follow-up, "
            "a reminder; the Owner closes it through `shortfall-closures`, 15H, GSA-R07). A "
            "count freeze does not stop it. Refusals: "
            "INVALID_REQUEST, NOT_FOUND (movement or shipment), ACTION_DENIED, "
            "EVENT_TIME_INVALID (future, or before the shipment left), REVISION_SUPERSEDED 409 "
            "(`reviewed_hash` is not the shipment's current `state_hash`), RTV_STATE_CONFLICT "
            "409 (the shipment is already accounted for), RTV_CONFIRM_INVALID 422 with issue "
            "UNKNOWN_LINE, MISSING_LINE or ACK_EXCEEDS_SHIPPED, MOVEMENT_INVALID 422 "
            "EVIDENCE_REQUIRED or EVIDENCE_NOT_FOUND; COMMAND_CONFLICT on a changed replay."
        ),
        request={"application/json": RTV_ACKNOWLEDGEMENT_REQUEST},
        responses=_responses(201, MOVEMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, shipment_id: uuid.UUID) -> Response:
        return self._run(
            request,
            pk,
            fields=rtv_shipments.ACK_FIELDS,
            required=["reviewed_hash", "lines"],
            allowed=(rtv_shipments.EXECUTE_ACTION,),
            denied="Recording a vendor acknowledgement needs rtv.execute at the RTV's site.",
            action="stock.rtv.acknowledge",
            command=lambda run, body: rtv_shipments.acknowledge(
                run, pk, shipment_id, rtv_shipments.parse_acknowledgement(body, run.now)
            ),
        )


class RtvSourceReturnView(_RtvShipmentCommand):
    """Goods ticket 15F: record goods of a shipment actually back at the source."""

    @extend_schema(
        operation_id="goods_v1_outbound_movements_rtv_shipment_returns",
        description=(
            "Record pieces of one shipment physically back at the source after a failed "
            "delivery (transfers PRD §6). Needs `rtv.execute` at the RTV's site. Only what "
            "came back comes ashore, in one P15 posting reversing the departure leg at "
            "unchanged value (external -> stock at each portion's recorded cost); the "
            "departure stays and no vendor receipt is recorded. Pieces that left under holds "
            "come back to quarantine under new holds of the same kinds (owned "
            "`stock_hold_active` work); pieces found `damaged` that left good go to quarantine "
            "under a damage hold with a pending damage report on the RTV "
            "(`RTV_RETURN_DAMAGE`); good pieces that left good wait unaccepted in the source's "
            "receiving for a putaway. The rest of the shipment stays unaccounted and visible, "
            "and a shortfall closure waiting for the Owner is superseded (15H). "
            "When every piece is accounted for the shipment's receipt and discrepancy work "
            "closes and the RTV may close. Refusals: INVALID_REQUEST, NOT_FOUND, "
            "ACTION_DENIED, EVENT_TIME_INVALID, RTV_STATE_CONFLICT 409 (pieces no longer "
            "away), MOVEMENT_INVALID 422 with issue UNKNOWN_LINE, RETURN_EXCEEDS_UNACCOUNTED, "
            "EVIDENCE_REQUIRED or EVIDENCE_NOT_FOUND; CONTRACT_DISABLED, SITE_NOT_READY, "
            "UNDER_COUNT 409; COMMAND_CONFLICT on a changed replay."
        ),
        request={"application/json": RTV_RETURN_REQUEST},
        responses=_responses(201, MOVEMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, shipment_id: uuid.UUID) -> Response:
        return self._run(
            request,
            pk,
            fields=rtv_shipments.RETURN_FIELDS,
            required=["reason", "lines"],
            allowed=(rtv_shipments.EXECUTE_ACTION,),
            denied="Recording goods back at the source needs rtv.execute at the RTV's site.",
            action="stock.rtv.return_to_source",
            command=lambda run, body: rtv_shipments.return_to_source(
                run, pk, shipment_id, rtv_shipments.parse_return(body, run.now)
            ),
        )


class RtvPutawayView(_RtvShipmentCommand):
    """Goods ticket 15F: put returned good pieces of a shipment away at the source."""

    @extend_schema(
        operation_id="goods_v1_outbound_movements_rtv_shipment_putaways",
        description=(
            "Accept returned good pieces of one shipment into an ordinary storage location at "
            "the source (P09 acceptance/putaway, with the source's own acceptance evidence), "
            "after which they are usable again. Needs `stock.accept` at the RTV's site. Works "
            "after the RTV has closed. Refusals: INVALID_REQUEST, NOT_FOUND, ACTION_DENIED, "
            "MOVEMENT_INVALID 422 with issue UNKNOWN_LINE, NOT_WAITING or a location refusal "
            "(WRONG_SITE, retired or system location); CONTRACT_DISABLED, SITE_NOT_READY, "
            "UNDER_COUNT 409; COMMAND_CONFLICT on a changed replay."
        ),
        request={"application/json": RTV_PUTAWAY_REQUEST},
        responses=_responses(201, MOVEMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, shipment_id: uuid.UUID) -> Response:
        return self._run(
            request,
            pk,
            fields=rtv_shipments.PUTAWAY_FIELDS,
            required=["lines"],
            allowed=(rtv_shipments.ACCEPT_ACTION,),
            denied="Putting returned goods away needs stock.accept at the RTV's site.",
            action="stock.rtv.putaway",
            command=lambda run, body: rtv_shipments.putaway(
                run, pk, shipment_id, rtv_shipments.parse_putaway(body)
            ),
        )


class RtvEwayView(_RtvShipmentCommand):
    """Goods ticket 15F: attach or verify a shipment's e-way reference."""

    @extend_schema(
        operation_id="goods_v1_outbound_movements_rtv_shipment_eway",
        description=(
            "Movement-document evidence for one shipment (transfers PRD §8). `attach` needs "
            "`rtv.execute` at the RTV's site: it records the reference and closes the "
            "shipment's `eway_missing` work. `verify` needs `movement.approve` there (the "
            "Owner): it records that the reference on file was checked. Missing-at-dispatch, "
            "later attachment and verification stay separate facts; nothing physical closes "
            "any of them, and they are recorded after the shipment or RTV completes too. A "
            "count freeze does not stop it. Refusals: INVALID_REQUEST, NOT_FOUND, "
            "ACTION_DENIED, EWAY_VERIFY_DENIED 403 (no reference on file, or a different "
            "one), RTV_STATE_CONFLICT 409 (already verified); COMMAND_CONFLICT on a changed "
            "replay."
        ),
        request={"application/json": RTV_EWAY_REQUEST},
        responses=_responses(201, MOVEMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, shipment_id: uuid.UUID) -> Response:
        parsed = rtv_shipments.parse_eway(
            business_body(request.data, rtv_shipments.EWAY_FIELDS, required=["action", "reference"])
        )
        grant = (
            rtv_shipments.VERIFY_ACTION
            if parsed.action == "verify"
            else rtv_shipments.EXECUTE_ACTION
        )
        return self._run(
            request,
            pk,
            fields=rtv_shipments.EWAY_FIELDS,
            required=["action", "reference"],
            allowed=(grant,),
            denied=f"This e-way step needs {grant} at the RTV's site.",
            action=f"stock.rtv.eway_{parsed.action}",
            command=lambda run, _body: rtv_shipments.record_eway(run, pk, shipment_id, parsed),
        )


class RtvShortfallClosureView(_RtvShipmentCommand):
    """Goods ticket 15H: prepare the closure of a shipment's acknowledgement shortfall."""

    @extend_schema(
        operation_id="goods_v1_outbound_movements_rtv_shipment_shortfall_closures",
        description=(
            "Prepare the closure of one short-acknowledged shipment's persistent difference "
            "(goods PRD §14.10 GSA-R07). Needs `movement.draft` at the RTV's site (Store "
            "person or Warehouse prepare, GSA-R01). Moves and posts nothing: it freezes "
            "exactly the pieces the latest acknowledgement leaves neither acknowledged nor "
            "back (oldest first, as a return takes them) and their value at recorded layer "
            "cost - unknown, never 0, for unvalued custody - pins the approval policy for "
            "`movement.approve` purpose `rtv` with that quantity and value, and opens an "
            "approval request (subject kind `rtv_shortfall`, subject the shipment) decided "
            "through the approvals decision by the Owner, a different person. A closure "
            "already waiting is superseded, as is one prepared before a later "
            "acknowledgement or return. On approval the frozen pieces move once from the "
            "`returned` boundary to `consumed` (P13, reason `rtv_shortfall`, no value leg - "
            "the value left stock at departure at this same recorded cost), the shipment "
            "becomes `shortfall_closed`, its `rtv_acknowledgement_discrepancy` work resolves "
            "(`SHORTFALL_CLOSED`) and the RTV closes `closed_partially_returned` when nothing "
            "else is open; a rejection keeps the difference open. The 30-day follow-up on the "
            "difference is a reminder and never closes it. No GL, vendor, payable, "
            "credit-note or tax entry. A count freeze does not stop it. Refusals: "
            "INVALID_REQUEST, NOT_FOUND (movement or shipment), ACTION_DENIED, "
            "REVISION_SUPERSEDED 409 (`reviewed_hash` is not the shipment's `state_hash`), "
            "RTV_STATE_CONFLICT 409 (not acknowledged yet, or already accounted for), "
            "MOVEMENT_INVALID 422 with issue EVIDENCE_REQUIRED or EVIDENCE_NOT_FOUND, "
            "APPROVAL_POLICY_BLOCKED 422 (no effective RTV policy, or the quantity/value "
            "outside it; an unknown value follows the policy's unknown-value rule); "
            "COMMAND_CONFLICT on a changed replay. The approvals decision refuses "
            "SELF_APPROVAL, ACTION_DENIED, REVISION_SUPERSEDED, STATE_CONFLICT (superseded or "
            "decided) and APPROVAL_REFUSED 422 with domain code RTV_STATE_CONFLICT (the "
            "pieces are no longer away)."
        ),
        request={"application/json": RTV_CLOSURE_REQUEST},
        responses=_responses(201, MOVEMENT_RESOURCE, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, shipment_id: uuid.UUID) -> Response:
        return self._run(
            request,
            pk,
            fields=rtv_shipments.CLOSURE_FIELDS,
            required=["reason", "reviewed_hash"],
            allowed=(rtv_shipments.PREPARE_CLOSURE_ACTION,),
            denied="Preparing a shortfall closure needs movement.draft at the RTV's site.",
            action="stock.rtv.shortfall_close",
            command=lambda run, body: rtv_shipments.propose_closure(
                run, pk, shipment_id, rtv_shipments.parse_closure(body)
            ),
        )


class MarkDamagedView(GoodsAPIView):
    """E209: record damage found on the stock screen, held at once in quarantine.

    Needs `movement.draft` at the site. The pieces move to the site's quarantine
    with condition `damaged` under a new damage hold in the same transaction, and
    a pending damage report (E252) opens for a different person to decide. A
    stock count freeze does not stop it (GSA-R02). Reserved pieces may be
    reported: the reservation stands, dispatching those pieces is blocked, and
    the transfer's owner gets a `transfer_discrepancy` (`RESERVED_STOCK_HELD`).
    Refusals: INVALID_REQUEST, NOT_FOUND, ACTION_DENIED, COMMAND_CONFLICT (a
    reused command id with different content), CONTRACT_DISABLED,
    SITE_NOT_READY, SERIES_NOT_READY, MOVEMENT_INVALID (pieces not where the
    line says, or a destination other than quarantine).
    """

    http_method_names = ["post", "options"]

    @extend_schema(responses=_responses(201, MOVEMENT_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, movements.PAYLOAD_FIELDS)
        payload = movements.parse(body, kind="hold")
        access.require(movements.DRAFT_ACTION, site_id=payload.site_id)

        def handler(run: CommandRun) -> CommandResult:
            identity = movements.mark_damaged(run, payload)
            return CommandResult(
                resource_type="movement",
                resource_id=str(identity.pk),
                status_code=201,
                revision=1,
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.movement.mark_damaged",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(line.lot_id) for line in payload.lines],
            site_id=payload.site_id,
        )
        return Response(_read_back(access, result), status=result.status_code)


class DamageReportListView(GoodsAPIView):
    """Damage reports at the caller's own sites, for the person who has to decide them.

    The same scope as the movements list: a report is the review side of a
    movement, so whoever may read the movement may read the report, and a site
    outside that reach is simply not there.
    """

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_damage_reports_list",
        description=(
            "E252: damage reports at the caller's own readable sites, newest first. "
            "Readable with `movement.draft`, `movement.approve` or `stock.view`. "
            "Refusals: INVALID_REQUEST (an unknown query key or state), ACTION_DENIED "
            "(no read grant), NOT_FOUND (a site outside the caller's reach)."
        ),
        parameters=[
            OpenApiParameter("site", str, description="Only this site's reports."),
            OpenApiParameter(
                "state",
                str,
                enum=list(DamageReport.State.values),
                description="Only reports in this state; `pending` is the review queue.",
            ),
            OpenApiParameter("cursor", str, description="Opaque next-page cursor."),
            OpenApiParameter("limit", int, description="Reports per page."),
        ],
        responses=_responses(200, DAMAGE_REPORT_PAGE, _READ_REFUSALS),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, DAMAGE_QUERY)
        sites = _readable_sites(access)
        wanted = parse_int_id(params["site"], "site") if params.get("site") else None
        if wanted is not None and sites is not None and wanted not in sites:
            raise Refusal("NOT_FOUND", "That site was not found.")
        state = params.get("state") or None
        if state and state not in DamageReport.State.values:
            raise Refusal("INVALID_REQUEST", "state is pending, confirmed, rejected or closed.")
        reports = damage_review.visible_reports(
            access.tenant_id, sites, site_id=wanted, state=state
        )
        names = damage_review.reporter_names(reports)
        window, cursor = paginate([damage_review.report_dto(r, names) for r in reports], params)
        return Response(page(window, cursor))


class DamageReportDecideView(GoodsAPIView):
    """The second person's decision: confirm the damage, or reject a mistaken report.

    Three things have to hold together, so all three are checked here rather
    than trusted: ``movement.approve`` at the report's own site, a fresh password
    confirmation, and a human who is not the reporter. Confirming moves nothing.
    Rejecting posts one linked release that gives the quantity back.
    """

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_damage_reports_decide",
        description=(
            "E253: confirm the damage, or reject a mistaken report. Needs "
            "`movement.approve` at the report's site (the Owner, GSA-R01), a fresh "
            "password confirmation and a named person who is not the reporter. "
            "Confirming posts nothing and changes no hold, reservation, acceptance or "
            "value. Rejecting posts one linked official release (`damage_rejected`, "
            "P12) that lifts only this report's damage hold and gives each portion "
            "back its earlier address and condition; a portion another hold still "
            "covers stays in quarantine, and any reservation over it is kept. During "
            "a count freeze both decisions are refused. A receiving report (ticket "
            "05C) is rejected the same way: its pieces go back to the site's receiving "
            "location as good goods, and the GRN's count is not changed. A rejection "
            "grants nothing else (ticket 12B): pieces never accepted go back unaccepted, "
            "and a transfer-arrival report's shipment is opened for acceptance again "
            "if its other pieces were already put away. The decision names no quantity: "
            "it is about the whole report. "
            "Refusals: INVALID_REQUEST, NOT_FOUND, ACTION_DENIED (no grant, or not a "
            "named person), STEP_UP_REQUIRED, SELF_APPROVAL (the reporter, whatever "
            "other role they hold), STATE_CONFLICT (already decided, or closed because its "
            "goods went back to the vendor), UNDER_COUNT "
            "(deciding during a count freeze), COMMAND_CONFLICT, CONTRACT_DISABLED, "
            "SITE_NOT_READY, MOVEMENT_INVALID (rejecting only: the report's hold is no "
            "longer over every piece, some pieces have left the site's quarantine, or a "
            "receiving report's pieces were since valued as damaged)."
        ),
        request={"application/json": DAMAGE_DECISION_REQUEST},
        responses=_responses(200, DAMAGE_REPORT, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, {"decision", "reason"}, required=["decision", "reason"])
        decision = str(body["decision"])
        reason = str(body["reason"]).strip()
        if not 1 <= len(reason) <= 500:
            raise Refusal("INVALID_REQUEST", "A reason of 1 to 500 characters is required.")
        report = _damage_report_for(access, pk)
        access.require(damage_review.REVIEW_ACTION, site_id=report.site_id)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up(run.now)
            decided = damage_review.decide(run, pk, decision=decision, reason=reason)
            return CommandResult(resource_type="damage_report", resource_id=str(decided.pk))

        result = self.run_command(
            request,
            access=access,
            action="stock.damage_report.decide",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=report.site_id,
            subject_key=f"damage_report:{pk}",
        )
        fresh = _damage_report_for(access, uuid.UUID(str(result.resource_id)))
        return Response(damage_review.report_dto(fresh, damage_review.reporter_names([fresh])))


def _damage_report_for(access: AccessContext, report_id: uuid.UUID) -> DamageReport:
    sites = _readable_sites(access)
    report = DamageReport.objects.filter(tenant_id=access.tenant_id, pk=report_id).first()
    if report is None or (sites is not None and report.site_id not in sites):
        raise Refusal("NOT_FOUND", "That damage report was not found.")
    return report


class StockSearchView(GoodsAPIView):
    """E210: find stock to move, inside the caller's own scope and nowhere wider."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(responses=_responses(200, STOCK_SEARCH_PAGE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, SEARCH_QUERY)
        # E210 names the site filter `source_site_id`: a search is run to find a
        # source to move or send from. It is the same site filter underneath, so
        # the one scope resolver still decides what the caller may see. Naming the
        # site twice, differently, is a confused request, not a choice to make for
        # the caller.
        source = params.pop("source_site_id", "")
        if source:
            if params.get("site_id") and params["site_id"] != source:
                raise Refusal("INVALID_REQUEST", "site_id and source_site_id name different sites.")
            params["site_id"] = source
        query = reads.resolve_query(access, params)
        answer = reads.rows_page(query, reads.on_hand_rows(query), params)
        return Response({**answer, "basis": query.basis})
