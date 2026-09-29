"""The transfer lifecycle over HTTP (OPS-06), under ``/api/goods-v1/outbound/``.

One record, one URL, and every step an action on that record - which is what
the store-and-warehouse operations PRD §7 asks for when it says *New transfer*,
*Dispatch* and *Receive* are actions on the transfer and *In transit* is a
filter, not a screen of its own.

Each route checks three things for itself rather than trusting the screen: the
action at the site the step actually happens at (drafting and dispatching at
the source, counting and accepting at the destination), the state the transfer
or the shipment is in, and - for approval - that the person is not the one who
drafted or sent it. The legacy ``/api/outbound/transfers`` routes are untouched
and keep their own pre-goods-v1 contract.

Named ``goods_*`` like every other goods-v1 view module, because that is how a
reader - and ``tests/test_goods_v1_namespace.py`` - tells a goods-v1 view from a
legacy one that happens to answer inside the namespace (#303).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone
from drf_spectacular.utils import extend_schema
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
)
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from outbound import transfer_excess, transfer_returns, transfer_shortages, transfers
from outbound.goods_models import GapResolution, GoodsTransfer, TransferDispatch, TransferReturn

LIST_QUERY = frozenset({"site", "direction", "state", "cursor", "limit"})
REQUEST_QUERY = frozenset({"site", "state", "cursor", "limit"})
DIRECTIONS = ("in", "out")

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


def _command_request(
    properties: dict[str, Any], *, required: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Closed goods-v1 command metadata with the route's accepted body."""
    return {"application/json": {
        "type": "object", "additionalProperties": False,
        "required": ["command_id", "contract_version", *required],
        "properties": {
            "command_id": {"type": "string", "format": "uuid"},
            "contract_version": {"type": "string", "enum": ["goods-v1"]},
            "expected_revision": {"type": "integer", "minimum": 1},
            **properties,
        },
    }}


TRANSFER_REQUEST_CREATE = _command_request({
    "source_site_id": {"type": "integer"},
    "destination_site_id": {"type": "integer"},
    "note": {"type": "string", "maxLength": 500},
    "lines": {"type": "array", "minItems": 1, "maxItems": transfers.MAX_LINES,
        "items": {"type": "object", "additionalProperties": False,
            "required": ["line_key", "sku_id", "qty"],
            "properties": {
                "line_key": {"type": "string", "format": "uuid"},
                "sku_id": {"type": "string", "format": "uuid"},
                "qty": {"type": "integer", "minimum": 1},
                "note": {"type": "string", "maxLength": 240},
            }},
    },
}, required=("source_site_id", "destination_site_id", "lines"))
TRANSFER_SUBMIT_REQUEST = _command_request({})
TRANSFER_APPROVE_REQUEST = _command_request({"reason": {"type": "string"}})
TRANSFER_CANCEL_REQUEST = _command_request({
    "reason": {"type": "string", "minLength": 1, "maxLength": 500},
}, required=("reason",))


PERSON: dict[str, Any] = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
}

SITE_REF: dict[str, Any] = {
    "type": "object",
    "description": "One end of a transfer, by name and code only.",
    "properties": {
        "id": {"type": "string"},
        "code": {"type": "string"},
        "name": {"type": "string"},
    },
    "required": ["id", "code", "name"],
}

TRANSFER_SUMMARY: dict[str, Any] = {
    "type": "object",
    "description": (
        "One internal transfer as a list row. `state` is the movement's own "
        "lifecycle - draft, submitted, approved, dispatching (at least one "
        "shipment is out), completed or cancelled - and `in_transit_qty` is what "
        "is physically on the road right now, which is why In transit is a filter "
        "rather than a separate screen."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "number": {"type": "string", "nullable": True},
        "state": {"type": "string", "enum": list(GoodsTransfer.State.values)},
        "custody": {
            "type": "string",
            "enum": list(GoodsTransfer.Custody.values),
            "description": (
                "Which source pool the movement takes from. `ordinary`: accepted, good, "
                "unheld, unreserved stock. `quarantine`: the controlled custody transfer "
                "of overall PRD §15.2.1 rule 10 (goods ticket 13D) - recorded pieces held "
                "in the source's quarantine, which stay held at every site, keep every "
                "hold, condition, origin and value, and are never made available by "
                "moving or arriving. `pre_pt`: the same rule's transfer of damaged "
                "pre-PT custody between a store and a warehouse (goods ticket 13E) - "
                "goods a GRN counted and no PT covers, identified from their GRN, moved "
                "on the transfer's own document with no transfer PT, their value "
                "unknown at both sites and held the whole way."
            ),
        },
        "source_site_id": {"type": "string"},
        "destination_site_id": {"type": "string"},
        "source_site": SITE_REF,
        "destination_site": SITE_REF,
        "created_at": {"type": "string", "format": "date-time"},
        "dispatched_at": {"type": "string", "nullable": True},
        "arrived_at": {"type": "string", "nullable": True},
        "dispatch_count": {"type": "integer"},
        "in_transit_qty": {
            "type": "integer",
            "description": (
                "Everything still on the road: uncounted shipments, what a failed "
                "delivery has not brought back, and a counted shipment's missing "
                "pieces until an approved shortage correction resolves them - a "
                "pending proposal leaves them here (goods ticket 14)."
            ),
        },
        "short_qty": {
            "type": "integer",
            "description": "What the counts found missing, over every shipment. Never rewritten.",
        },
        "shortage_resolved_qty": {
            "type": "integer",
            "description": (
                "Of `short_qty`, what approved shortage corrections (GAP, P13) have "
                "taken out of transit."
            ),
        },
        "corrective_for_id": {
            "type": "string",
            "format": "uuid",
            "nullable": True,
            "description": (
                "Goods ticket 16: set on a corrective transfer - the movement whose "
                "observed excess it corrects. A corrective transfer is never shipped; "
                "it is confirmed once against that excess."
            ),
        },
    },
}

DOCUMENT_SUMMARY: dict[str, Any] = {
    "type": "object",
    "nullable": True,
    "description": (
        "The delivery challan or tax invoice this shipment left with (store "
        "operations ticket 36), or null: the sending site's switch was off. No "
        "money; read the document itself to print it."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "kind": {"type": "string", "enum": ["delivery_challan", "tax_invoice"]},
        "number": {"type": "string"},
        "issued_on": {"type": "string", "format": "date"},
    },
    "required": ["id", "kind", "number", "issued_on"],
}

EWAY: dict[str, Any] = {
    "type": "object",
    "description": (
        "The shipment's e-way evidence as separate facts (transfers PRD §8). "
        "`at_dispatch` is whether a reference left with the goods - fixed at "
        "dispatch, null only for a shipment recorded before this was kept. "
        "`reference` is the reference on file now: the latest attached, else the "
        "one that left with them. `verified` is true only when that same "
        "reference was verified. `missing_exception_open` says the owned "
        "`eway_missing` exception at the sending site is still open. None of "
        "this is a statement that the movement was lawful."
    ),
    "properties": {
        "at_dispatch": {"type": "string", "enum": ["present", "not_present"], "nullable": True},
        "reference": {"type": "string", "nullable": True},
        "attached_at": {"type": "string", "nullable": True},
        "attached_by": {"type": "string", "nullable": True},
        "verified": {"type": "boolean"},
        "verified_at": {"type": "string", "nullable": True},
        "verified_by": {"type": "string", "nullable": True},
        "missing_exception_open": {"type": "boolean"},
    },
}

RETURN_RECEIPT: dict[str, Any] = {
    "type": "object",
    "description": (
        "One actual receipt at the source of goods from a failed delivery: how "
        "many of each line came back good or damaged, where, who recorded it, "
        "when it happened (`returned_at`) and when it was recorded."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "receipt_key": {"type": "string", "format": "uuid"},
        "site_id": {"type": "string"},
        "returned_at": {"type": "string", "format": "date-time"},
        "recorded_at": {"type": "string", "format": "date-time"},
        "recorded_by": PERSON,
        "reason": {"type": "string"},
        "evidence_reference": {"type": "string", "nullable": True},
        "note": {"type": "string", "nullable": True},
        "quantity": {"type": "integer"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "good": {"type": "integer"},
                    "damaged": {"type": "integer"},
                },
            },
        },
    },
}

COUNT: dict[str, Any] = {
    "type": "object",
    "nullable": True,
    "description": (
        "What the destination found when it counted the whole shipment, written "
        "once and never rewritten. Per line: `dispatched`, the pieces found "
        "`good`, `damaged` and `unidentified`, `wrong` - expected pieces other "
        "goods came in place of - and `short`, the expected pieces that never "
        "arrived, `wrong` included (goods ticket 16). `excess` is goods nobody "
        "sent, and what came in place of wrong pieces: each its own unvalued held "
        "custody lot with its observation, never more of what was sent. A "
        "corrective transfer's confirmation writes the same shape, with "
        "`corrective` naming the decision it matched."
    ),
    "properties": {
        "counted_at": {"type": "string", "format": "date-time"},
        "note": {"type": "string", "nullable": True},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "dispatched": {"type": "integer"},
                    "good": {"type": "integer"},
                    "damaged": {"type": "integer"},
                    "wrong": {"type": "integer"},
                    "unidentified": {"type": "integer"},
                    "short": {"type": "integer"},
                },
            },
        },
        "good_total": {"type": "integer"},
        "held_total": {"type": "integer"},
        "short_total": {"type": "integer"},
        "wrong_total": {"type": "integer"},
        "excess": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "lot_id": {"type": "string", "format": "uuid"},
                    "observation_id": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer"},
                    "description": {"type": "string"},
                    "sku_id": {"type": "string", "nullable": True},
                    "alias_value": {"type": "string", "nullable": True},
                    "in_place_of_line_key": {"type": "string", "nullable": True},
                    "pairing_key": {"type": "string", "nullable": True},
                    "hold_key": {"type": "string"},
                    "condition": {"type": "string", "enum": ["good", "wrong"]},
                },
            },
        },
        "dispatch_sequence_no": {"type": "integer"},
        "corrective": {
            "type": "object",
            "nullable": True,
            "description": (
                "Only on a corrective transfer's shipment: the excess decision its "
                "confirmation matched and the source's evidence."
            ),
            "properties": {
                "gap_id": {"type": "string", "format": "uuid"},
                "lot_id": {"type": "string", "format": "uuid"},
                "observation_id": {"type": "string", "nullable": True},
                "source_evidence_reference": {"type": "string"},
            },
        },
    },
}

NULLABLE_PERSON: dict[str, Any] = {
    "type": "object",
    "nullable": True,
    "properties": PERSON["properties"],
}

SHORTAGE_RESOLUTION: dict[str, Any] = {
    "type": "object",
    "description": (
        "One proposed transit-shortage correction of a counted shipment and its "
        "decision (goods ticket 14, GSA-T14). `lines` freezes the exact ranges of "
        "the original shipment it names. `number` is the GAP number, given only "
        "on approval. `check_event_id` is the shipment's recorded destination "
        "check (its `check` event)."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "number": {"type": "string", "nullable": True},
        "state": {"type": "string", "enum": ["pending", "approved", "rejected"]},
        "kind": {"type": "string", "enum": ["short"]},
        "resolution": {"type": "string", "enum": ["transit_shortage"]},
        "quantity": {"type": "integer"},
        "check_event_id": {"type": "string", "format": "uuid"},
        "reason": {"type": "string"},
        "evidence_reference": {"type": "string"},
        "followup_note": {"type": "string"},
        "recount_note": {"type": "string", "nullable": True},
        "pairing_key": {
            "type": "string",
            "nullable": True,
            "description": (
                "Goods ticket 16: set when this is the short-expected half of a "
                "wrong-goods pair; the excess-observed half carries the same key."
            ),
        },
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer"},
                    "portions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "lot_id": {"type": "string", "format": "uuid"},
                                "lower": {"type": "integer"},
                                "upper": {"type": "integer"},
                                "origin_id": {"type": "string", "nullable": True},
                            },
                        },
                    },
                },
            },
        },
        "proposed_by": NULLABLE_PERSON,
        "proposed_at": {"type": "string", "nullable": True},
        "decided_by": NULLABLE_PERSON,
        "decided_at": {"type": "string", "nullable": True},
        "decision_reason": {"type": "string", "nullable": True},
    },
}

SHIPMENT_SHORTAGE: dict[str, Any] = {
    "type": "object",
    "description": (
        "The shipment's shortage: `short` is what its count found missing; "
        "`resolved` what approved corrections took out of transit; "
        "`pending_approval` what a proposal names and is still waiting; "
        "`unresolved` what is still in transit (short less resolved, pending "
        "included); `unclaimed` what no proposal names yet."
    ),
    "properties": {
        "short": {"type": "integer"},
        "resolved": {"type": "integer"},
        "pending_approval": {"type": "integer"},
        "unresolved": {"type": "integer"},
        "unclaimed": {"type": "integer"},
    },
}

SHIPMENT_RECONCILIATION: dict[str, Any] = {
    "type": "object",
    "description": (
        "The shipment's own conservation (goods ticket 14): `dispatched` = "
        "`received_good` + `received_held` + `returned` + `shortage_resolved` + "
        "`in_transit`, and `balanced` says it holds. `in_transit_pending_approval` "
        "is the part of `in_transit` a shortage proposal names. `excess` is "
        "beside it, never inside it."
    ),
    "properties": {
        "dispatched": {"type": "integer"},
        "received_good": {"type": "integer"},
        "received_held": {"type": "integer"},
        "returned": {"type": "integer"},
        "shortage_resolved": {"type": "integer"},
        "in_transit": {"type": "integer"},
        "in_transit_pending_approval": {"type": "integer"},
        "excess": {"type": "integer"},
        "balanced": {"type": "boolean"},
    },
}

EXCESS_DECISION: dict[str, Any] = {
    "type": "object",
    "description": (
        "Goods ticket 16: one excess-observed decision - a corrective transfer "
        "the source proposed for exact pieces of an observation, with its "
        "evidence. `number` is the GAP number, given when the Owner approves the "
        "corrective transfer. `state` is pending, approved, or withdrawn (the "
        "corrective transfer's balance was cancelled before its confirmation). "
        "`matched` says its confirmation has matched the pieces."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "number": {"type": "string", "nullable": True},
        "state": {"type": "string", "enum": list(GapResolution.State.values)},
        "quantity": {"type": "integer"},
        "lot_id": {"type": "string", "format": "uuid"},
        "pairing_key": {"type": "string", "nullable": True},
        "sku_id": {"type": "string", "format": "uuid"},
        "source_evidence_reference": {"type": "string"},
        "reason": {"type": "string"},
        "matched": {"type": "boolean"},
        "corrective_transfer": {
            "type": "object",
            "nullable": True,
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "number": {"type": "string", "nullable": True},
                "state": {"type": "string", "enum": list(GoodsTransfer.State.values)},
            },
        },
        "proposed_by": NULLABLE_PERSON,
        "proposed_at": {"type": "string", "nullable": True},
        "decided_by": NULLABLE_PERSON,
        "decided_at": {"type": "string", "nullable": True},
    },
}

EXCESS_OBSERVATION: dict[str, Any] = {
    "type": "object",
    "description": (
        "Goods ticket 16: goods the count found that nobody sent - or that came in "
        "place of expected pieces (`in_place_of_line_key`, with the `pairing_key` "
        "its short-expected half shares) - as its own unvalued custody lot "
        "(`lot_id`) in the destination's excess hold, with its observation "
        "(`observation_id`) and the identity evidence the counter had. "
        "`matched_qty` is what corrective transfers have matched; "
        "`claimed_qty` what an open corrective decision names and has not matched "
        "yet; `unresolved_qty` what is still held (qty less matched); "
        "`unclaimed_qty` what is held and named by no decision. It stays owned "
        "work (`transfer_discrepancy`, subject `transfer_excess:{lot_id}`) until "
        "wholly matched."
    ),
    "properties": {
        "lot_id": {"type": "string", "format": "uuid"},
        "observation_id": {"type": "string", "nullable": True},
        "description": {"type": "string"},
        "sku_id": {"type": "string", "nullable": True},
        "alias_value": {"type": "string", "nullable": True},
        "qty": {"type": "integer"},
        "in_place_of_line_key": {"type": "string", "nullable": True},
        "pairing_key": {"type": "string", "nullable": True},
        "matched_qty": {"type": "integer"},
        "claimed_qty": {"type": "integer"},
        "unresolved_qty": {"type": "integer"},
        "unclaimed_qty": {"type": "integer"},
        "decisions": {"type": "array", "items": EXCESS_DECISION},
    },
}

DISPATCH: dict[str, Any] = {
    "type": "object",
    "description": (
        "One shipment against the approved movement. `count` is what the "
        "destination actually found, whole-shipment: good, damaged, wrong, "
        "unidentified and what was missing. It is null until the shipment has "
        "been counted, and it is never rewritten afterwards."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "sequence_no": {"type": "integer"},
        "state": {"type": "string", "enum": list(TransferDispatch.State.values)},
        "source_site_id": {"type": "string"},
        "destination_site_id": {"type": "string"},
        "dispatched_at": {"type": "string", "format": "date-time"},
        "recorded_by": PERSON,
        "transport": {"type": "object", "additionalProperties": True},
        "preparation_id": {
            "type": "string",
            "format": "uuid",
            "nullable": True,
            "description": "The dispatch preparation whose scans this shipment carried.",
        },
        "arrived_at": {
            "type": "string",
            "nullable": True,
            "description": (
                "When the shipment physically arrived (E147), or when it was counted "
                "if no arrival was recorded first. Arrival releases nothing."
            ),
        },
        "arrival_recorded_by": {
            "type": "object",
            "nullable": True,
            "properties": PERSON["properties"],
        },
        "counted_at": {"type": "string", "nullable": True},
        "accepted_at": {"type": "string", "nullable": True},
        "returned_at": {
            "type": "string",
            "nullable": True,
            "description": "When the last piece of the shipment was back at the source.",
        },
        "return_reason": {
            "type": "string",
            "nullable": True,
            "description": "Why the delivery failed, as the first return receipt said it.",
        },
        "quantity": {"type": "integer"},
        "in_transit_qty": {
            "type": "integer",
            "description": (
                "What of this shipment is on the road now: all of it until it is "
                "counted or anything comes back; after a count only what never "
                "arrived; after a return, what the return receipts have not "
                "brought back - unresolved, never closed by the return."
            ),
        },
        "returned_qty": {
            "type": "integer",
            "description": "What the return receipts have brought back to the source so far.",
        },
        "returns": {
            "type": "array",
            "description": (
                "Every return receipt recorded at the source for this shipment "
                "(goods ticket 13C), oldest first. Never rewritten."
            ),
            "items": RETURN_RECEIPT,
        },
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "sku_id": {
                        "type": "string",
                        "format": "uuid",
                        "nullable": True,
                        "description": "Null on a pre-PT line whose count gave no SKU.",
                    },
                    "qty": {"type": "integer"},
                    "origins": {
                        "type": "array",
                        "description": "How many of the pieces came from each origin.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "origin_id": {"type": "string", "nullable": True},
                                "qty": {"type": "integer"},
                            },
                        },
                    },
                    "returned_qty": {
                        "type": "integer",
                        "description": "Pieces of this line recorded back at the source.",
                    },
                    "returned_awaiting_putaway": {
                        "type": "integer",
                        "description": (
                            "Returned good pieces of this line standing unaccepted in "
                            "the source's receiving: not sendable or sellable until "
                            "somebody at the source accepts and puts them away."
                        ),
                    },
                    "awaiting_putaway": {
                        "type": "integer",
                        "description": (
                            "Good pieces of this line the destination counted and has "
                            "not yet put away; 0 unless the shipment is `counted`. "
                            "Acceptance may take them in several goes."
                        ),
                    },
                },
            },
        },
        "count": COUNT,
        "eway": EWAY,
        "document": DOCUMENT_SUMMARY,
        "excess_observations": {"type": "array", "items": EXCESS_OBSERVATION},
        "shortage": SHIPMENT_SHORTAGE,
        "shortage_resolutions": {"type": "array", "items": SHORTAGE_RESOLUTION},
        "reconciliation": SHIPMENT_RECONCILIATION,
    },
}

TRANSFER_DOCUMENT_LINE: dict[str, Any] = {
    "type": "object",
    "description": (
        "One origin's pieces of one shipped line. Money (integer paise as text) "
        "and the rate only when `values_shown`: a reader with the `cost` field "
        "grant at the sending site for the line's brand."
    ),
    "properties": {
        "sku_id": {"type": "string", "nullable": True},
        "description": {"type": "string"},
        "hsn": {"type": "string"},
        "qty": {"type": "integer"},
        "values_shown": {"type": "boolean"},
        "unit_cost_paise": {"type": "string", "nullable": True},
        "taxable_paise": {"type": "string", "nullable": True},
        "rate": {"type": "string", "nullable": True},
        "cgst_paise": {"type": "string", "nullable": True},
        "sgst_paise": {"type": "string", "nullable": True},
        "igst_paise": {"type": "string", "nullable": True},
        "tax_paise": {"type": "string", "nullable": True},
    },
    "required": ["description", "hsn", "qty", "values_shown"],
}

TRANSFER_PARTY: dict[str, Any] = {
    "type": "object",
    "description": "One end as it stood when the goods left.",
    "properties": {
        "site_id": {"type": "string"},
        "code": {"type": "string"},
        "name": {"type": "string"},
        "city": {"type": "string"},
        "gstin": {"type": "string"},
        "state_code": {"type": "string"},
        "state_name": {"type": "string"},
        "legal_name": {"type": "string"},
    },
    "required": ["site_id", "code", "name", "gstin", "state_code", "state_name", "legal_name"],
}

TRANSFER_DOCUMENT: dict[str, Any] = {
    "type": "object",
    "description": (
        "The document one shipment left with (store operations ticket 36, "
        "ST-TRF-2): a delivery challan within one GSTIN, a tax invoice between "
        "two, worked out from the two registrations. Written once at dispatch "
        "and never changed. The totals only when every line's values are shown."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "kind": {"type": "string", "enum": ["delivery_challan", "tax_invoice"]},
        "number": {"type": "string"},
        "issued_on": {"type": "string", "format": "date"},
        "transfer_id": {"type": "string", "format": "uuid"},
        "dispatch_id": {"type": "string", "format": "uuid"},
        "sequence_no": {"type": "integer"},
        "dispatched_at": {"type": "string", "format": "date-time"},
        "transport": {"type": "object", "additionalProperties": True},
        "source": TRANSFER_PARTY,
        "destination": TRANSFER_PARTY,
        "tax_kind": {"type": "string", "enum": ["none", "cgst_sgst", "igst"]},
        "issued_by": {"type": "string"},
        "pieces": {"type": "integer"},
        "lines": {"type": "array", "items": TRANSFER_DOCUMENT_LINE},
        "values_shown": {"type": "boolean"},
        "taxable_paise": {"type": "string", "nullable": True},
        "tax_paise": {"type": "string", "nullable": True},
        "total_paise": {"type": "string", "nullable": True},
    },
    "required": [
        "id",
        "kind",
        "number",
        "issued_on",
        "transfer_id",
        "dispatch_id",
        "sequence_no",
        "dispatched_at",
        "transport",
        "source",
        "destination",
        "tax_kind",
        "issued_by",
        "pieces",
        "lines",
        "values_shown",
    ],
}

TRANSFER_DETAIL: dict[str, Any] = {
    "type": "object",
    "description": (
        "One transfer with its plan, its shipments and its history. No cost and "
        "no margin: a transfer creates no purchase value, and the frozen origin "
        "on each line is what carries the value the goods already had."
    ),
    "properties": {
        **TRANSFER_SUMMARY["properties"],
        "pt_id": {"type": "string", "format": "uuid", "nullable": True},
        "pt_number": {"type": "string", "nullable": True},
        "pt_state": {"type": "string", "nullable": True},
        "drafted_by": PERSON,
        "approved_by": {"type": "object", "nullable": True, "properties": PERSON["properties"]},
        "lines": {
            "type": "array",
            "description": (
                "What the movement is for: the official PT's lines once approved, "
                "else the frozen plan, else the draft. A frozen line carries "
                "`portions`, one per exact piece range, each with its `origin_id`; "
                "separate origins are never merged. For a reader holding the "
                "`cost` field grant at the source site for the line's brand, each "
                "portion also carries its origin's frozen `unit_cost_paise` and "
                "`mrp_paise` (integer paise as strings; null only where a portion "
                "has no origin). Without that grant both are absent, never zero. "
                "A pre-PT custody line (goods ticket 13E) names its source instead of "
                "an origin: `grn_id`, `grn_number` and `grn_line_key`, the identity "
                "the count gave (`sku_id` null when it gave none), `condition` "
                "(damaged), `counted_condition`, `discrepancy_remark`, `raw_alias`, "
                "`damage_report_ids`, `evidence_ids` and `value` (always `unknown`); "
                "its portions carry no origin and are never priced."
            ),
            "items": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "sku_id": {"type": "string", "format": "uuid", "nullable": True},
                    "grn_id": {"type": "string", "format": "uuid"},
                    "grn_number": {"type": "string", "nullable": True},
                    "grn_line_key": {"type": "string", "format": "uuid"},
                    "condition": {"type": "string"},
                    "counted_condition": {"type": "string", "nullable": True},
                    "discrepancy_remark": {"type": "string", "nullable": True},
                    "raw_alias": {"type": "string", "nullable": True},
                    "value": {"type": "string", "enum": ["unknown"]},
                    "damage_report_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "qty": {"type": "integer"},
                    "description": {"type": "string"},
                    "origin_ids": {"type": "array", "items": {"type": "string"}},
                    "portions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "lot_id": {"type": "string", "format": "uuid"},
                                "lower": {"type": "integer"},
                                "upper": {"type": "integer"},
                                "origin_id": {"type": "string", "nullable": True},
                                "source_location_id": {"type": "string", "nullable": True},
                                "unit_cost_paise": {"type": "string", "nullable": True},
                                "mrp_paise": {"type": "string", "nullable": True},
                            },
                        },
                    },
                },
            },
        },
        "reserved_qty": {"type": "integer"},
        "approved_qty": {
            "type": "integer",
            "description": "What the approval covers; 0 until it is approved.",
        },
        "dispatched_qty": {"type": "integer", "description": "What has left, over every shipment."},
        "cancelled_qty": {
            "type": "integer",
            "description": "What an explicit cancellation released from the reservation.",
        },
        "returned_qty": {
            "type": "integer",
            "description": (
                "What failed deliveries have brought back to the source, over every "
                "shipment. Never counted as delivered."
            ),
        },
        "reconciliation": {
            "type": "object",
            "description": (
                "The movement's undispatched balance, reconciled separately from "
                "each shipment (goods ticket 14): `approved` = `dispatched` + "
                "`reserved` (still reserved at the source) + `cancelled`, and "
                "`balanced` says it holds (true while nothing is approved)."
            ),
            "required": ["approved", "dispatched", "reserved", "cancelled", "balanced"],
            "properties": {
                "approved": {"type": "integer"},
                "dispatched": {"type": "integer"},
                "reserved": {"type": "integer"},
                "cancelled": {"type": "integer"},
                "balanced": {"type": "boolean"},
            },
        },
        "dispatch_preparation": {
            "type": "object",
            "nullable": True,
            "description": (
                "The shipment being scanned at the sending site right now, if any "
                "(E242-E244). Read it in full at E243."
            ),
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "revision": {"type": "integer"},
                "scanned_qty": {"type": "integer"},
                "opened_at": {"type": "string"},
                "last_activity_at": {"type": "string"},
            },
        },
        "excess_qty": {
            "type": "integer",
            "description": (
                "Goods ticket 16: pieces the counts observed that nobody sent, over "
                "every shipment - beside what was dispatched, never inside it."
            ),
        },
        "corrective_for": {
            "type": "object",
            "nullable": True,
            "description": "On a corrective transfer: the movement whose excess it corrects.",
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "number": {"type": "string", "nullable": True},
            },
        },
        "corrective_decision": {
            **EXCESS_DECISION,
            "nullable": True,
            "description": "On a corrective transfer: the excess decision it serves.",
        },
        "dispatches": {"type": "array", "items": DISPATCH},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "description": (
                            "What happened. `damage_rejected` records that a damage "
                            "report from this transfer's arrival count - or from a "
                            "failed delivery's return, at the source - was rejected "
                            "(ticket 12B); its details name the report, the release "
                            "and whether the shipment was opened for acceptance again. "
                            "`returned` is one return receipt at the source (ticket "
                            "13C): its quantity, good and damaged, and what is still "
                            "in transit. An `accept` with `returned_goods` true puts "
                            "returned pieces away at the source. `shortage_proposed`, "
                            "`shortage_resolved` and `shortage_rejected` (goods ticket "
                            "14) name the shipment, the GAP proposal, its quantity and "
                            "reason, and after a decision what is still in transit. "
                            "`corrective_proposed`, `corrective_matched` and "
                            "`corrective_withdrawn` (goods ticket 16) name an excess "
                            "observation, its corrective decision and transfer, and the "
                            "quantity; a corrective transfer's own `dispatch` and "
                            "`arrival` carry `corrective: true`."
                        ),
                    },
                    "site_id": {"type": "string"},
                    "actual_at": {"type": "string"},
                    "recorded_at": {"type": "string"},
                    "details": {"type": "object", "additionalProperties": True},
                },
            },
        },
        "allowed_actions": {"type": "array", "items": {"type": "string"}},
    },
}

TRANSFER_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "One site asking another for stock. Asking reserves nothing and moves "
        "nothing; `state` says whether the sending site has drafted a transfer "
        "for it yet."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "source_site_id": {"type": "string"},
        "destination_site_id": {"type": "string"},
        "requested_by": PERSON,
        "requested_at": {"type": "string", "format": "date-time"},
        "state": {"type": "string", "enum": ["open", "drafted", "closed"]},
        "note": {"type": "string", "nullable": True},
        "lines": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "transfer_id": {"type": "string", "format": "uuid", "nullable": True},
    },
}


TRANSFER_DRAFT_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "A draft movement from one site to another. Each line names a SKU and a "
        "quantity, and may name the eligible origins it must come from "
        "(`origin_ids`); the chosen origins are kept on the draft as evidence and "
        "stay separate rows when the plan is frozen at submission. Nothing is "
        "reserved by drafting. `custody` (default `ordinary`) picks the source "
        "pool: `quarantine` drafts the controlled custody transfer of held, "
        "recorded quarantined stock (goods ticket 13D); it cannot answer a request. "
        "`pre_pt` (goods ticket 13E) drafts the transfer of damaged pre-PT custody "
        "between a store and a warehouse: each line then names `grn_id` (a GRN "
        "issued at the source), `grn_line_key` and `qty` instead of a SKU and "
        "origins, and it cannot answer a request either."
    ),
    "required": [
        "command_id",
        "contract_version",
        "source_site_id",
        "destination_site_id",
        "lines",
    ],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "source_site_id": {"type": "string"},
        "destination_site_id": {"type": "string"},
        "request_id": {"type": "string", "format": "uuid", "nullable": True},
        "note": {"type": "string", "nullable": True, "maxLength": 500},
        "custody": {"type": "string", "enum": list(GoodsTransfer.Custody.values)},
        "lines": {
            "type": "array",
            "minItems": 1,
            "maxItems": transfers.MAX_LINES,
            "items": {
                "type": "object",
                "required": ["line_key", "qty"],
                "description": (
                    "`sku_id` (with optional `origin_ids`) on an ordinary or quarantine "
                    "line; `grn_id` and `grn_line_key` on a pre-PT line, never both."
                ),
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "sku_id": {"type": "string", "format": "uuid"},
                    "grn_id": {"type": "string", "format": "uuid"},
                    "grn_line_key": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer", "minimum": 1, "maximum": transfers.MAX_QTY},
                    "origin_ids": {
                        "type": "array",
                        "maxItems": 50,
                        "items": {"type": "string", "format": "uuid"},
                    },
                    "note": {"type": "string", "nullable": True, "maxLength": 240},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def _always_present(schema: dict[str, Any]) -> dict[str, Any]:
    """Mark every property of every object in ``schema`` as always sent.

    For the shapes goods ticket 13B added, where the server always sends every
    key (a nullable one as null), so the generated client types none of them
    as optional.
    """
    if schema.get("type") == "object" and "properties" in schema:
        schema["required"] = list(schema["properties"])
        for child in schema["properties"].values():
            _always_present(child)
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        _always_present(schema["items"])
    return schema


_always_present(EWAY)
_always_present(DISPATCH)
# Every top-level key of the transfer read is always sent (a nullable one as
# null). Not recursively: a frozen portion's cost and MRP are absent, not null,
# for a reader without the cost grant.
TRANSFER_DETAIL["required"] = list(TRANSFER_DETAIL["properties"])


def _page_of(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": item},
            "next_cursor": {"type": "string", "nullable": True},
            "as_of": {"type": "string", "format": "date-time"},
        },
    }


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _readable_sites(access: AccessContext) -> frozenset[int] | None:
    """Sites where any transfer-read action is granted; ``None`` means everywhere.

    A transfer carries no brand of its own, so a brand-limited grant reaches
    none of them: those grants are left out rather than read as "everywhere".
    """
    granted = access.all_actions()
    if not any(action in granted for action in transfers.READ_ACTIONS):
        raise Refusal("ACTION_DENIED", "You do not have permission to read transfers.")
    reach: set[int] = set()
    for action in transfers.READ_ACTIONS:
        sites = access.site_ids(action)
        if action in granted and sites is None:
            return None
        reach |= set(sites or ())
    return frozenset(reach)


def _wanted_site(
    access: AccessContext, params: dict[str, str]
) -> tuple[frozenset[int] | None, int | None]:
    sites = _readable_sites(access)
    wanted = parse_int_id(params["site"], "site") if params.get("site") else None
    if wanted is not None and sites is not None and wanted not in sites:
        raise Refusal("NOT_FOUND", "That site was not found.")
    return sites, wanted


def _transfer_for(access: AccessContext, transfer_id: uuid.UUID) -> GoodsTransfer:
    sites = _readable_sites(access)
    transfer = transfers.transfer_of(access.tenant_id, transfer_id)
    if sites is not None and not (
        transfer.source_site_id in sites or transfer.destination_site_id in sites
    ):
        raise Refusal("NOT_FOUND", "That transfer was not found.")
    return transfer


def _detail(access: AccessContext, transfer_id: uuid.UUID) -> dict[str, Any]:
    transfer = _transfer_for(access, transfer_id)
    records = list(TransferDispatch.objects.filter(transfer=transfer))
    names = transfers.people_names(
        [
            transfer.document.maker_id,
            *transfer_shortages.people_of(transfer),
            *[record.recorded_by_id for record in records],
            *[record.counted_by_id for record in records if record.counted_by_id],
            *[r.arrival_recorded_by_id for r in records if r.arrival_recorded_by_id],
            *TransferReturn.objects.filter(dispatch__transfer=transfer).values_list(
                "actor_id", flat=True
            ),
            *_approver_ids(transfer),
        ]
    )
    detail = transfers.detail_dto(
        transfer, names=names, allowed=transfers.allowed_actions(access, transfer)
    )
    detail["lines"] = transfers.priced_lines(access, transfer, detail["lines"])
    return detail


def _approver_ids(transfer: GoodsTransfer) -> list[uuid.UUID]:
    version = transfers._pt_version(transfer, required=False)
    return [version.approved_by_id] if version is not None else []


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TransferListCreateView(GoodsAPIView):
    """Every transfer touching this person's sites, and the route that drafts one."""

    http_method_names = ["get", "head", "post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_list",
        responses=_responses(200, _page_of(TRANSFER_SUMMARY), _READ_REFUSALS),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, LIST_QUERY)
        sites, wanted = _wanted_site(access, params)
        direction = params.get("direction") or None
        if direction and direction not in DIRECTIONS:
            raise Refusal("INVALID_REQUEST", "direction is in or out.")
        if direction and wanted is None:
            raise Refusal(
                "INVALID_REQUEST", "A direction is in or out of one site, so name the site."
            )
        state = params.get("state") or None
        if state and state not in GoodsTransfer.State.values:
            raise Refusal(
                "INVALID_REQUEST",
                f"state is one of {', '.join(GoodsTransfer.State.values)}.",
            )
        rows = transfers.visible_transfers(
            access.tenant_id, sites, site_id=wanted, direction=direction, state=state
        )
        records = list(TransferDispatch.objects.filter(transfer__in=rows))
        items = [transfers.summary_dto(row, records) for row in rows]
        window, cursor = paginate(items, params)
        return Response(page(window, cursor))

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_create",
        description=(
            "Draft a transfer at the sending site (`transfer.allocate` there). "
            "Refusals: INVALID_REQUEST (body), NOT_FOUND (a site or request out of "
            "scope), ACTION_DENIED, CONTRACT_DISABLED / SITE_NOT_READY / UNDER_COUNT "
            "(either site cannot move goods now), TRANSFER_POLICY_BLOCKED (the two "
            "sites belong to different legal entities: a commercial movement, not an "
            "internal transfer; a partner flag on a site changes nothing), "
            "SBU_RETIRED (a line's brand has a retired business unit at the source "
            "or the destination; nothing is written), STATE_CONFLICT (the request "
            "was already answered), TRANSFER_INVALID (a `quarantine` or `pre_pt` "
            "draft names a request, the two sites are the same, or a `pre_pt` draft "
            "is not between a store and a warehouse). A `pre_pt` line whose GRN was "
            "not issued at the source, or has no such line, is NOT_FOUND."
        ),
        request={"application/json": TRANSFER_DRAFT_REQUEST},
        responses=_responses(201, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, transfers.DRAFT_FIELDS)
        payload = transfers.parse_draft(body)
        # Drafting is the sending site's act, so the grant that matters is the
        # one at the source - not at the site that will receive the goods.
        access.require(transfers.ALLOCATE_ACTION, site_id=payload.source_site_id)

        def handler(run: CommandRun) -> CommandResult:
            transfer = transfers.create(run, payload)
            return CommandResult(
                resource_type="transfer", resource_id=str(transfer.pk), status_code=201, revision=1
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.transfer.create",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=payload.source_site_id,
        )
        return Response(
            _detail(access, uuid.UUID(str(result.resource_id))), status=result.status_code
        )


DESTINATION: dict[str, Any] = {
    "type": "object",
    "description": (
        "One site a transfer from the named source may be drafted to: its name, "
        "code and kind (`store` or `warehouse`), and nothing it holds."
    ),
    "properties": {
        "id": {"type": "string"},
        "code": {"type": "string"},
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": ["store", "warehouse"]},
    },
    "required": ["id", "code", "name", "kind"],
}


class TransferDestinationsView(GoodsAPIView):
    """Where the sending site may send to - so a site-scoped sender has somewhere.

    A store person's session names one site, their own, and a picker built from
    it offers nowhere to send. Drafting is gated on the source alone, so this is
    too: whoever may draft from ``source_site_id`` is told every site the draft
    would accept (store and warehouse operations PRD §7; Anand, 25 September 2026).
    """

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfer_destinations",
        description=(
            "Every other goods-v1 site of the source's legal entity that may receive "
            "a transfer, for somebody holding `transfer.allocate` at the source. "
            "Refusals: INVALID_REQUEST (no source named), ACTION_DENIED, NOT_FOUND "
            "(the source is outside what this person may send from)."
        ),
        responses=_responses(
            200,
            {"type": "object", "properties": {"items": {"type": "array", "items": DESTINATION}}},
            _READ_REFUSALS,
        ),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, frozenset({"source_site_id"}))
        if not params.get("source_site_id"):
            raise Refusal("INVALID_REQUEST", "Name the site the goods are sent from.")
        source = parse_int_id(params["source_site_id"], "source_site_id")
        access.require(transfers.ALLOCATE_ACTION, site_id=source)
        return Response({"items": transfers.destinations(access.tenant_id, source)})


QUARANTINE_STOCK_ROW: dict[str, Any] = {
    "type": "object",
    "description": (
        "Recorded pieces a quarantine transfer from the site could take (goods "
        "ticket 13D): held in the site's quarantine, with a resolved SKU and an "
        "origin, reserved to nobody. One row per SKU, origin and condition, with "
        "the hold kinds standing over it. No value."
    ),
    "properties": {
        "sku_id": {"type": "string", "format": "uuid"},
        "description": {"type": "string"},
        "origin_id": {"type": "string", "format": "uuid", "nullable": True},
        "condition": {"type": "string"},
        "qty": {"type": "integer"},
        "hold_kinds": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sku_id", "description", "origin_id", "condition", "qty", "hold_kinds"],
}


class TransferQuarantineStockView(GoodsAPIView):
    """What a quarantine transfer from one site could send (goods ticket 13D).

    The same pool submission freezes from, so the New transfer panel offers
    exactly what the server would take. Gated as drafting is (``transfer.allocate``
    at the source) and, because it reads stock held there, as reading that stock
    is: rows of a brand the person may not read at the site are left out.
    """

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfer_quarantine_stock",
        description=(
            "The quarantined recorded stock a quarantine transfer from "
            "`source_site_id` could take, for somebody holding `transfer.allocate` "
            "and `stock.view` at that site; rows of a brand outside their "
            "`stock.view` reach are left out. Refusals: INVALID_REQUEST (no source "
            "named), ACTION_DENIED, NOT_FOUND (the source is outside what this "
            "person may send from or read)."
        ),
        responses=_responses(
            200,
            {
                "type": "object",
                "properties": {"items": {"type": "array", "items": QUARANTINE_STOCK_ROW}},
                "required": ["items"],
            },
            _READ_REFUSALS,
        ),
    )
    def get(self, request: Request) -> Response:
        from masters.goods_identity_models import ProductSku

        access = self.access(request)
        params = check_query(request, frozenset({"source_site_id"}))
        if not params.get("source_site_id"):
            raise Refusal("INVALID_REQUEST", "Name the site the goods are sent from.")
        source = parse_int_id(params["source_site_id"], "source_site_id")
        access.require(transfers.ALLOCATE_ACTION, site_id=source)
        # Reading stock, as stock search does (``goods_reads.require_reader``):
        # the action somewhere, a grant that reaches this site, then each row
        # only where a grant covers its brand. A brand-limited reader gets
        # their own brand's rows, never a blanket refusal and never more.
        access.require_action(transfers.VALUE_READ)
        reach = access.site_reach(transfers.VALUE_READ)
        if reach is not None and source not in reach:
            raise Refusal("NOT_FOUND", "That site was not found.")
        rows = transfers.quarantine_stock(source)
        brands = dict(
            ProductSku.objects.filter(pk__in={row["sku_id"] for row in rows}).values_list(
                "pk", "style__brand_id"
            )
        )
        items = [
            row
            for row in rows
            if access.can(
                transfers.VALUE_READ,
                site_id=source,
                brand_id=brands.get(uuid.UUID(row["sku_id"])),
            )
        ]
        return Response({"items": items})


PRE_PT_CUSTODY_ROW: dict[str, Any] = {
    "type": "object",
    "description": (
        "Damaged pre-PT custody a pre-PT transfer from the site could take (goods "
        "ticket 13E), one row per GRN line: pieces that GRN counted, held in the "
        "site's quarantine under a damage hold, on no PT, with no value basis and "
        "reserved to nobody. `sku_id` only where the count gave one. No value: it "
        "is unknown."
    ),
    "properties": {
        "grn_id": {"type": "string", "format": "uuid"},
        "grn_number": {"type": "string", "nullable": True},
        "grn_line_key": {"type": "string", "format": "uuid"},
        "sku_id": {"type": "string", "format": "uuid", "nullable": True},
        "description": {"type": "string"},
        "condition": {"type": "string", "enum": ["damaged"]},
        "qty": {"type": "integer"},
        "received_at": {"type": "string", "format": "date-time"},
        "hold_kinds": {"type": "array", "items": {"type": "string"}},
        "damage_report_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "grn_id",
        "grn_number",
        "grn_line_key",
        "sku_id",
        "description",
        "condition",
        "qty",
        "received_at",
        "hold_kinds",
        "damage_report_ids",
    ],
}


class TransferPrePtCustodyView(GoodsAPIView):
    """What a pre-PT custody transfer from one site could send (goods ticket 13E).

    The same pool submission freezes from. Gated as drafting is
    (``transfer.allocate`` at the source) and, because each row is a GRN's
    goods, as reading that GRN is: a row whose GRN the person may not read
    (its site and brand) is left out, never refused wholesale.
    """

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfer_pre_pt_custody",
        description=(
            "The damaged pre-PT custody a pre-PT transfer from `source_site_id` "
            "could take, for somebody holding `transfer.allocate` at that site; a "
            "row whose GRN they may not read (receiving read at the site for the "
            "GRN's brand) is left out. Refusals: INVALID_REQUEST (no source named), "
            "ACTION_DENIED, NOT_FOUND."
        ),
        responses=_responses(
            200,
            {
                "type": "object",
                "properties": {"items": {"type": "array", "items": PRE_PT_CUSTODY_ROW}},
                "required": ["items"],
            },
            _READ_REFUSALS,
        ),
    )
    def get(self, request: Request) -> Response:
        from inbound.goods_services import can_read
        from outbound import pre_pt_custody

        access = self.access(request)
        params = check_query(request, frozenset({"source_site_id"}))
        if not params.get("source_site_id"):
            raise Refusal("INVALID_REQUEST", "Name the site the goods are sent from.")
        source = parse_int_id(params["source_site_id"], "source_site_id")
        access.require(transfers.ALLOCATE_ACTION, site_id=source)
        items = [
            {key: value for key, value in row.items() if key != "_brand_id"}
            for row in pre_pt_custody.rows(source)
            if can_read(access, source, row["_brand_id"])
        ]
        return Response({"items": items})


class TransferDetailView(GoodsAPIView):
    """One transfer: its plan, its shipments, its history and what you may do to it."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_detail",
        responses=_responses(200, TRANSFER_DETAIL, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, frozenset())
        return Response(_detail(access, pk))


class TransferRequestListCreateView(GoodsAPIView):
    """A site's requests for stock, and the route that raises one."""

    http_method_names = ["get", "head", "post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfer_requests_list",
        responses=_responses(200, _page_of(TRANSFER_REQUEST), _READ_REFUSALS),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, REQUEST_QUERY)
        sites, wanted = _wanted_site(access, params)
        rows = transfers.visible_requests(access.tenant_id, sites, site_id=wanted)
        state = params.get("state") or None
        if state:
            rows = [row for row in rows if row.state == state]
        names = transfers.people_names([row.requested_by_id for row in rows])
        window, cursor = paginate([transfers.request_dto(r, names) for r in rows], params)
        return Response(page(window, cursor))

    @extend_schema(
        operation_id="goods_v1_outbound_transfer_requests_create",
        request=TRANSFER_REQUEST_CREATE,
        responses=_responses(201, TRANSFER_REQUEST, _WRITE_REFUSALS),
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, transfers.REQUEST_FIELDS)
        parsed = transfers.parse_request(body)
        # The asking site is the one that wants the goods, so its own grant is
        # what authorises the asking.
        access.require(transfers.ALLOCATE_ACTION, site_id=parsed["destination_site_id"])

        def handler(run: CommandRun) -> CommandResult:
            record = transfers.create_request(run, body)
            return CommandResult(
                resource_type="transfer_request",
                resource_id=str(record.pk),
                status_code=201,
                revision=1,
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.transfer_request.create",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=parsed["destination_site_id"],
        )
        from outbound.goods_models import TransferRequest

        record = TransferRequest.objects.get(pk=uuid.UUID(str(result.resource_id)))
        names = transfers.people_names([record.requested_by_id])
        return Response(transfers.request_dto(record, names), status=result.status_code)


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


class _TransferCommandView(GoodsAPIView):
    """One step on one transfer: check the grant at the right site, then run it."""

    http_method_names = ["post", "options"]
    action_name = ""
    body_fields: frozenset[str] = frozenset()
    required_fields: tuple[str, ...] = ()

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        """Check this step's grant and answer the site the command is audited at."""
        raise NotImplementedError

    def run(self, run: CommandRun, transfer: GoodsTransfer, body: dict[str, Any]) -> CommandResult:
        raise NotImplementedError

    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, self.body_fields, required=self.required_fields)
        transfer = _transfer_for(access, pk)
        site_id = self.gate(access, transfer)

        def handler(run: CommandRun) -> CommandResult:
            return self.run(run, transfer, body)

        result = self.run_command(
            request,
            access=access,
            action=self.action_name,
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=site_id,
            subject_key=f"transfer:{pk}",
        )
        return Response(_detail(access, pk), status=result.status_code)


class TransferSubmitView(_TransferCommandView):
    """Freeze the exact source pieces and send the transfer PT for approval."""

    action_name = "stock.transfer.submit"

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfers.ALLOCATE_ACTION, site_id=transfer.source_site_id)
        return transfer.source_site_id

    def run(self, run: CommandRun, transfer: GoodsTransfer, body: dict[str, Any]) -> CommandResult:
        transfers.submit(run, transfer.pk)
        return CommandResult(resource_type="transfer", resource_id=str(transfer.pk))

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_submit",
        request=TRANSFER_SUBMIT_REQUEST,
        description=(
            "Freeze the exact source pieces into the transfer PT and send it for "
            "approval. Reserves nothing. Opens an `approval_pending` exception "
            "(`TRANSFER_APPROVAL`, subject `transfer:{id}`) at the sending site in "
            "the shared centre, which the approval resolves. An ordinary transfer "
            "freezes only accepted, good, unheld, unreserved pieces; a quarantine "
            "transfer only recorded pieces held in the source's quarantine and "
            "reserved to nobody - each pool is closed to the other. A pre-PT "
            "custody transfer (goods ticket 13E) freezes, per GRN line and oldest "
            "first, damaged pieces of that GRN held in the source's quarantine "
            "under a damage hold, with no PT coverage or value basis and reserved "
            "to nobody, onto the transfer's own document - no transfer PT is made - "
            "and each line keeps the GRN's evidence with its value `unknown`. "
            "Refusals: STATE_CONFLICT (not a draft), INSUFFICIENT_ELIGIBLE_STOCK "
            "(not enough eligible pieces in the transfer's pool), NOT_FOUND, "
            "ACTION_DENIED."
        ),
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        return super().post(request, pk)


class TransferApproveView(_TransferCommandView):
    """The second person approves the movement, which reserves the pieces at source.

    Three things have to hold together and all three are checked here rather
    than trusted: ``pt.approve.transfer`` at the source site, a fresh password
    confirmation, and a human who is neither the drafter nor the submitter.
    """

    action_name = "stock.transfer.approve"
    body_fields = frozenset({"reason"})

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfers.APPROVE_ACTION, site_id=transfer.source_site_id)
        access.require_step_up()
        return transfer.source_site_id

    def run(self, run: CommandRun, transfer: GoodsTransfer, body: dict[str, Any]) -> CommandResult:
        reason = body.get("reason")
        transfers.approve(run, transfer.pk, reason=str(reason) if reason else None)
        return CommandResult(resource_type="transfer", resource_id=str(transfer.pk))

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_approve",
        request=TRANSFER_APPROVE_REQUEST,
        description=(
            "A different authorised person approves exactly the pieces the PT "
            "froze, which officialises and numbers the PT and reserves them (P07) "
            "with no expiry. Approval never re-picks: a changed item, a bigger "
            "quantity or another destination is a new transfer with its own "
            "approval. Body: `{reason?}`. Needs a fresh password confirmation. "
            "Refusals: STEP_UP_REQUIRED, ACTION_DENIED, SELF_APPROVAL (the drafter "
            "or submitter), STATE_CONFLICT (not waiting for approval), "
            "INSUFFICIENT_ELIGIBLE_STOCK (a frozen piece was sold, moved, held or "
            "reserved meanwhile - for a quarantine transfer, moved out of the "
            "source's quarantine or no longer held; nothing is written and no "
            "number is used), ALLOCATED_TO_TILL (ordinary transfers only). A "
            "quarantine transfer's approval reserves the held pieces and lifts no "
            "hold. A pre-PT custody transfer's approval (goods ticket 13E) "
            "officialises the transfer's own document, unnumbered - it is not a PT "
            "and takes no transfer PT number - and otherwise behaves as a "
            "quarantine transfer's: the same authority, the same distinct person, "
            "the same recheck that every frozen piece is still held in the "
            "source's quarantine, and no value or identity added."
        ),
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        return super().post(request, pk)


class TransferCancelOutstandingView(_TransferCommandView):
    """Release the undispatched balance, and nothing that has already left."""

    action_name = "stock.transfer.cancel_outstanding"
    body_fields = frozenset({"reason"})
    required_fields = ("reason",)

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfers.ALLOCATE_ACTION, site_id=transfer.source_site_id)
        return transfer.source_site_id

    def run(self, run: CommandRun, transfer: GoodsTransfer, body: dict[str, Any]) -> CommandResult:
        reason = str(body["reason"]).strip()
        if not 1 <= len(reason) <= 500:
            raise Refusal("INVALID_REQUEST", "A reason of 1 to 500 characters is required.")
        transfers.cancel_outstanding(run, transfer.pk, reason=reason)
        return CommandResult(resource_type="transfer", resource_id=str(transfer.pk))

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_cancel_outstanding",
        request=TRANSFER_CANCEL_REQUEST,
        description=(
            "Release what is still reserved, with actor, time, quantity and "
            "reason (a `cancelled` event). A transfer nothing has left from is "
            "then `cancelled`; one with shipments stays open until they are "
            "accounted for. An independent hold over the pieces stays in force. "
            "Body: `{reason}` (1-500 characters). Refusals: INVALID_REQUEST, "
            "ACTION_DENIED, NOT_FOUND, STATE_CONFLICT (not approved, already "
            "cancelled, or nothing left reserved)."
        ),
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        return super().post(request, pk)


DISPATCH_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "Record one shipment leaving: exactly what its dispatch preparation "
        "scanned. `lines` must equal the preparation's scanned total per line, "
        "and the preparation must be the open one, at the revision and content "
        "hash you reviewed, for the live approval."
    ),
    "required": [
        "command_id",
        "contract_version",
        "lines",
        "dispatch_session_id",
        "dispatch_session_revision",
        "dispatch_session_hash",
    ],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "dispatched_at": {
            "type": "string",
            "format": "date-time",
            "description": "When it actually left; not later than now. Defaults to now.",
        },
        "transport": {
            "type": "object",
            "maxProperties": 20,
            "description": (
                "Free transport details. `eway_reference` (text, at most 100) is the "
                "e-way reference that left with the goods; with none, the shipment is "
                "still recorded and an owned `eway_missing` exception opens at the "
                "sending site."
            ),
            "properties": {"eway_reference": {"type": "string", "maxLength": 100}},
            "additionalProperties": True,
        },
        "dispatch_session_id": {"type": "string", "format": "uuid"},
        "dispatch_session_revision": {"type": "integer", "minimum": 1},
        "dispatch_session_hash": {"type": "string", "minLength": 64, "maxLength": 64},
        "lines": {
            "type": "array",
            "minItems": 1,
            "maxItems": transfers.MAX_LINES,
            "items": {
                "type": "object",
                "required": ["line_key", "qty"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer", "minimum": 1},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


class TransferDispatchView(_TransferCommandView):
    """One shipment leaves the source: exactly what its preparation scanned."""

    action_name = "stock.transfer.dispatch"
    body_fields = transfers.DISPATCH_FIELDS
    required_fields = transfers.DISPATCH_REQUIRED

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfers.MOVE_ACTION, site_id=transfer.source_site_id)
        return transfer.source_site_id

    def run(self, run: CommandRun, transfer: GoodsTransfer, body: dict[str, Any]) -> CommandResult:
        parsed = transfers.parse_dispatch(body, run.now)
        record = transfers.dispatch(run, transfer.pk, parsed)
        return CommandResult(
            resource_type="transfer_dispatch", resource_id=str(record.pk), status_code=201
        )

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_dispatch",
        description=(
            "E146, adapted to the selected shipment (goods ticket 13B). Rechecks, "
            "in one transaction, the live approval, the preparation's revision and "
            "content, the submitted totals against the scans, the holds and the "
            "reservations; then consumes exactly those reserved pieces into "
            "transit (P08), keeps the rest reserved, marks the preparation "
            "dispatched and records the departure. Refusals: INVALID_REQUEST, "
            "ACTION_DENIED (`transfer.move` at the sending site), NOT_FOUND, "
            "CONTRACT_DISABLED / SITE_NOT_READY / UNDER_COUNT, STATE_CONFLICT (not "
            "approved), REVISION_SUPERSEDED (the preparation changed after you "
            "reviewed it), DISPATCH_PLAN_MISMATCH (the totals are not exactly what "
            "was scanned, nothing was scanned, or the preparation is no longer open "
            "or not for the live approval), RESERVATION_BLOCKED (a hold now covers "
            "pieces the scanned shipment needs - on a quarantine transfer, a needed "
            "piece is no longer held in the source's quarantine; nothing moves), "
            "EVENT_TIME_INVALID. A quarantine transfer's pieces travel with every "
            "hold and condition they had. Store operations ticket 36: where the "
            "sending site's `transfer-documents` switch is on, the shipment gets "
            "its delivery challan (same GSTIN) or tax invoice (two GSTINs) in the "
            "same transaction; NUMBER_REFUSED (no prefix, series full - head "
            "office is alerted), TRANSFER_VALUE_UNKNOWN (a tax invoice would need "
            "a cost or ticket price that is not known), TRANSFER_HSN_MISSING or "
            "TRANSFER_TAX_RULE_MISSING (a piece with no HSN, or one the store's tax "
            "settings have no rule for) refuses the dispatch and nothing moves."
        ),
        request={"application/json": DISPATCH_REQUEST},
        responses=_responses(201, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        from masters.document_series import alert_on_refusal

        # Outside the command's transaction, so the alert outlives the refusal.
        with alert_on_refusal():
            return super().post(request, pk)


class DispatchDocumentView(GoodsAPIView):
    """The delivery challan or tax invoice one shipment left with, to print."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_dispatch_document",
        description=(
            "Store operations ticket 36 (ST-TRF-2). Anyone who may read the "
            "transfer may read and print its shipments' documents, switch on or "
            "off: the switch only decides whether a new shipment gets one. Values "
            "and tax only with the `cost` field grant at the sending site (per "
            "line brand). Refusals: ACTION_DENIED, NOT_FOUND (no such transfer), "
            "NO_TRANSFER_DOCUMENT (the shipment left with no document: the "
            "sending site's switch was off, or there is no such shipment)."
        ),
        responses=_responses(200, TRANSFER_DOCUMENT, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID, dispatch_id: uuid.UUID) -> Response:
        from outbound import transfer_documents
        from outbound.transfer_document_models import TransferDocument

        access = self.access(request)
        check_query(request, frozenset())
        transfer = _transfer_for(access, pk)
        document = (
            TransferDocument.objects.select_related("dispatch")
            .filter(dispatch_id=dispatch_id, dispatch__transfer=transfer)
            .first()
        )
        if document is None:
            # Its own code, so the screen can tell "none issued" from "not yours".
            raise Refusal(
                "NO_TRANSFER_DOCUMENT",
                "This shipment left with no transfer document from the system.",
                status=404,
            )
        names = transfers.people_names([document.issued_by_id])
        return Response(
            transfer_documents.document_dto(
                access, document, transfer, names.get(document.issued_by_id, "")
            )
        )


# ---------------------------------------------------------------------------
# Dispatch preparation (E242-E244)
# ---------------------------------------------------------------------------

PREPARATION: dict[str, Any] = {
    "type": "object",
    "description": (
        "One shipment being scanned at the sending site (design E242-E244, "
        "adapted by goods ticket 13B to the selected shipment). Preparing moves "
        "no stock and reserves nothing. `state` is open, dispatched (it became "
        "`dispatch_id`) or invalidated (`invalidated_reason`: REPLACED, "
        "BALANCE_CANCELLED or APPROVAL_CHANGED); a preparation is never deleted. "
        "A dispatch names `revision` and `content_hash` as it read them. Each line "
        "gives what is still reserved, what is scanned, the tags a piece of it may "
        "carry, and the same per origin."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "transfer_id": {"type": "string", "format": "uuid"},
        "source_site_id": {"type": "string"},
        "source_version_id": {"type": "string", "format": "uuid"},
        "state": {"type": "string", "enum": ["open", "dispatched", "invalidated"]},
        "revision": {"type": "integer"},
        "content_hash": {"type": "string"},
        "opened_by": PERSON,
        "opened_at": {"type": "string", "format": "date-time"},
        "last_activity_at": {"type": "string", "format": "date-time"},
        "closed_at": {"type": "string", "nullable": True},
        "invalidated_reason": {"type": "string", "nullable": True},
        "dispatch_id": {"type": "string", "format": "uuid", "nullable": True},
        "scanned_qty": {"type": "integer"},
        "acknowledged_scan_keys": {"type": "array", "items": {"type": "string"}},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "sku_id": {"type": "string", "format": "uuid", "nullable": True},
                    "description": {"type": "string"},
                    "approved_qty": {"type": "integer"},
                    "reserved_qty": {"type": "integer"},
                    "scanned_qty": {"type": "integer"},
                    "alias_values": {"type": "array", "items": {"type": "string"}},
                    "origins": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "origin_id": {"type": "string"},
                                "reserved_qty": {"type": "integer"},
                                "scanned_qty": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        },
        "scans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scan_key": {"type": "string", "format": "uuid"},
                    "line_key": {"type": "string", "format": "uuid"},
                    "origin_id": {"type": "string", "nullable": True},
                    "qty": {"type": "integer"},
                    "alias_value": {"type": "string"},
                    "actual_at": {"type": "string"},
                    "recorded_at": {"type": "string"},
                    "scanned_by": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "nullable": True},
                            "name": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}

_always_present(PREPARATION)

PREPARATION_OPEN_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": ["command_id", "contract_version"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "replace": {
            "type": "boolean",
            "description": (
                "Start the shipment again: the open preparation is invalidated "
                "(REPLACED) and kept, and a fresh one opens."
            ),
        },
    },
    "additionalProperties": False,
}

PREPARATION_SCAN_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": ["command_id", "contract_version", "expected_revision", "observations"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer", "minimum": 1},
        "observations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 200,
            "items": {
                "type": "object",
                "required": ["scan_key", "line_key", "qty", "alias_value"],
                "properties": {
                    "scan_key": {"type": "string", "format": "uuid"},
                    "line_key": {"type": "string", "format": "uuid"},
                    "origin_id": {
                        "type": "string",
                        "format": "uuid",
                        "nullable": True,
                        "description": (
                            "The origin the piece is from, when the scanner knows it. "
                            "Without it the dispatch takes the line's oldest reserved "
                            "pieces; the shipment still records every piece's origin."
                        ),
                    },
                    "qty": {"type": "integer", "minimum": 1},
                    "alias_value": {"type": "string", "minLength": 1, "maxLength": 128},
                    "actual_at": {"type": "string", "format": "date-time"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def _preparation_for(access: AccessContext, preparation_id: uuid.UUID) -> Any:
    """A preparation, visible only to the dispatch grant at its sending site."""
    from outbound import dispatch_preparation as preparing

    row = preparing.preparation_of(access.tenant_id, preparation_id)
    if not access.can(transfers.MOVE_ACTION, site_id=row.transfer.source_site_id):
        raise Refusal("NOT_FOUND", "That dispatch preparation was not found.")
    return row


class TransferPreparationOpenView(GoodsAPIView):
    """E242: open, resume or start again the shipment being scanned at the source."""

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_dispatch_sessions_open",
        description=(
            "E242. Resumes the transfer's open preparation (200) or opens one for "
            "its live approved version (201); with `replace: true` the open one is "
            "invalidated and kept and a fresh one opens. No reservation or stock "
            "changes. An open preparation bound to an approval that is no longer "
            "the live one is invalidated (APPROVAL_CHANGED) and a fresh one opens. "
            "Refusals: INVALID_REQUEST, ACTION_DENIED (`transfer.move` at the "
            "sending site), NOT_FOUND, CONTRACT_DISABLED / SITE_NOT_READY / "
            "UNDER_COUNT, STATE_CONFLICT (not approved, cancelled, or nothing left "
            "reserved)."
        ),
        request={"application/json": PREPARATION_OPEN_REQUEST},
        responses={
            200: PREPARATION,
            **_responses(201, PREPARATION, _WRITE_REFUSALS),
        },
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        from outbound import dispatch_preparation as preparing

        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, preparing.OPEN_FIELDS)
        replace = preparing.parse_open(body)
        transfer = _transfer_for(access, pk)
        access.require(transfers.MOVE_ACTION, site_id=transfer.source_site_id)

        def handler(run: CommandRun) -> CommandResult:
            row, created = preparing.open_preparation(run, transfer.pk, replace=replace)
            return CommandResult(
                resource_type="dispatch_preparation",
                resource_id=str(row.pk),
                status_code=201 if created else 200,
                revision=row.revision,
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.transfer.dispatch_session.open",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=transfer.source_site_id,
            subject_key=f"transfer:{pk}",
        )
        row = preparing.preparation_of(access.tenant_id, uuid.UUID(str(result.resource_id)))
        return Response(preparing.preparation_dto(row, timezone.now()), status=result.status_code)


class DispatchSessionDetailView(GoodsAPIView):
    """E243: the shipment being scanned, for anyone with the dispatch grant at the source."""

    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_dispatch_sessions_detail",
        description=(
            "E243. The preparation's binding, every acknowledged scan with who "
            "made it and when, and per line what is reserved and scanned. Only "
            "`transfer.move` at the sending site reads it; anyone else gets "
            "NOT_FOUND."
        ),
        responses=_responses(200, PREPARATION, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        from outbound import dispatch_preparation as preparing

        access = self.access(request)
        check_query(request, frozenset())
        row = _preparation_for(access, pk)
        return Response(preparing.preparation_dto(row, timezone.now()))


class DispatchSessionScanView(GoodsAPIView):
    """E244: acknowledge scans into the open preparation. Nothing moves."""

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_dispatch_sessions_scan",
        description=(
            "E244. Each observation names the approved line, the scanned tag and "
            "a quantity, and may name the origin. All are checked before any is "
            "kept. A scan key already acknowledged with the same content counts "
            "once (a request of only such replays needs no current revision); "
            "with different content it is COMMAND_CONFLICT. No stock moves and "
            "nothing is reserved. Refusals: INVALID_REQUEST, ACTION_DENIED, "
            "NOT_FOUND (the preparation, or a line not on this transfer), "
            "REVISION_SUPERSEDED (somebody scanned since you loaded it), "
            "TAG_MISMATCH (not this line's tag), DISPATCH_PLAN_MISMATCH (beyond "
            "what is still reserved on the line or from that origin, an origin "
            "not reserved on the line, or the preparation is no longer open or "
            "not for the live approval), EVENT_TIME_INVALID, COMMAND_CONFLICT, "
            "CONTRACT_DISABLED / SITE_NOT_READY / UNDER_COUNT."
        ),
        request={"application/json": PREPARATION_SCAN_REQUEST},
        responses=_responses(200, PREPARATION, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        from outbound import dispatch_preparation as preparing

        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, preparing.SCAN_FIELDS, required=("observations",))
        row = _preparation_for(access, pk)
        site_id = row.transfer.source_site_id

        def handler(run: CommandRun) -> CommandResult:
            observations = preparing.parse_scans(body, run.now)
            done = preparing.scan(run, row.pk, observations, meta.expected_revision)
            return CommandResult(
                resource_type="dispatch_preparation",
                resource_id=str(done.pk),
                revision=done.revision,
            )

        self.run_command(
            request,
            access=access,
            action="stock.transfer.dispatch_session.scan",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=site_id,
            subject_key=f"transfer:{row.transfer_id}",
        )
        fresh = preparing.preparation_of(access.tenant_id, pk)
        return Response(preparing.preparation_dto(fresh, timezone.now()))


class _DispatchCommandView(GoodsAPIView):
    """One step on one shipment of one transfer."""

    http_method_names = ["post", "options"]
    action_name = ""
    body_fields: frozenset[str] = frozenset()
    required_fields: tuple[str, ...] = ()

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        raise NotImplementedError

    def run(
        self,
        run: CommandRun,
        transfer: GoodsTransfer,
        dispatch_id: uuid.UUID,
        body: dict[str, Any],
    ) -> CommandResult:
        raise NotImplementedError

    def post(self, request: Request, pk: uuid.UUID, dispatch_id: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, self.body_fields, required=self.required_fields)
        transfer = _transfer_for(access, pk)
        site_id = self.gate(access, transfer)

        def handler(run: CommandRun) -> CommandResult:
            return self.run(run, transfer, dispatch_id, body)

        result = self.run_command(
            request,
            access=access,
            action=self.action_name,
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=site_id,
            subject_key=f"transfer_dispatch:{dispatch_id}",
        )
        return Response(_detail(access, pk), status=result.status_code)


COUNT_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": ["command_id", "contract_version", "lines"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "counted_at": {
            "type": "string",
            "format": "date-time",
            "description": "When it was counted; not later than now. Defaults to now.",
        },
        "note": {"type": "string", "maxLength": 500},
        "lines": {
            "type": "array",
            "minItems": 1,
            "maxItems": transfers.MAX_LINES,
            "description": "Every line the shipment carried, once each.",
            "items": {
                "type": "object",
                "required": ["line_key"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    **{
                        name: {"type": "integer", "minimum": 0, "maximum": transfers.MAX_QTY}
                        for name in transfers.COUNT_CONDITIONS
                    },
                },
                "additionalProperties": False,
            },
        },
        "excess": {
            "type": "array",
            "maxItems": transfer_excess.MAX_ENTRIES,
            "description": (
                "Goods nobody sent, and what came in place of each line's `wrong` "
                "pieces (goods ticket 16): the entries naming a line in "
                "`in_place_of_line_key` must add up to exactly that line's `wrong`."
            ),
            "items": {
                "type": "object",
                "required": ["description", "qty"],
                "properties": {
                    "description": {"type": "string", "minLength": 1, "maxLength": 240},
                    "qty": {"type": "integer", "minimum": 1, "maximum": transfers.MAX_QTY},
                    "sku_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "The item, when the counter could tell which it is.",
                    },
                    "alias_value": {
                        "type": "string",
                        "maxLength": 128,
                        "description": "The code read off the goods, as evidence.",
                    },
                    "in_place_of_line_key": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Wrong goods: the shipped line they came in place of.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

ACCEPT_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": ["command_id", "contract_version", "lines"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "lines": {
            "type": "array",
            "minItems": 1,
            "maxItems": transfers.MAX_LINES,
            "items": {
                "type": "object",
                "required": ["line_key", "qty", "destination_location_id"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer", "minimum": 1},
                    "destination_location_id": {"type": "string", "format": "uuid"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


class DispatchCountView(_DispatchCommandView):
    """The destination counts one whole shipment, exactly as it found it."""

    action_name = "stock.transfer.count"
    body_fields = transfers.COUNT_FIELDS

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfers.MOVE_ACTION, site_id=transfer.destination_site_id)
        return transfer.destination_site_id

    def run(
        self,
        run: CommandRun,
        transfer: GoodsTransfer,
        dispatch_id: uuid.UUID,
        body: dict[str, Any],
    ) -> CommandResult:
        parsed = transfers.parse_count(body, run.now)
        record = transfers.count(run, transfer.pk, dispatch_id, parsed)
        return CommandResult(resource_type="transfer_dispatch", resource_id=str(record.pk))

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_count",
        description=(
            "Account for one whole shipment at the destination, exactly as it was "
            "found (transfers PRD §5; R-INV-006). Every line the shipment carried "
            "is answered once, with how many pieces arrived `good`, `damaged` and "
            "`unidentified`, and how many were `wrong` - other goods came in their "
            "place; what is not answered, and every wrong piece, is short. Good "
            "pieces come ashore into the destination's receiving location, arrived "
            "and unaccepted (P09); damaged and unidentified ones go to its "
            "quarantine under a hold (P10), each piece keeping its origin and value. "
            "Wrong pieces never arrive as the expected item: they stay in transit, "
            "short-expected, and what came instead is an `excess` entry naming the "
            "line (`in_place_of_line_key`), paired with them by a `pairing_key` "
            "(goods ticket 16). "
            "Damage opens a pending damage report for a different person and one "
            "owned `stock_hold_active` per hold with its follow-up due date and no "
            "resolution action - never a pickup or disposal deadline. What is "
            "missing stays in transit as an owned `transfer_discrepancy` "
            "(`TRANSIT_SHORTAGE`) at the destination; absent goods never enter "
            "quarantine. Each `excess` entry is recorded as an observation bound to "
            "the shipment with its identity evidence and a separate unvalued custody "
            "lot in the excess hold under a `transfer_excess` hold, and opens an "
            "owned `transfer_discrepancy` (`TRANSFER_EXCESS`, or `WRONG_GOODS` for "
            "wrong goods; subject `transfer_excess:{lot}`) at the destination with "
            "its follow-up date; only a corrective transfer's confirmation closes "
            "it. A shipment is counted once: there is no staged partial "
            "receipt, and an unfinished scan never short-closes it. A shipment "
            "with nothing good to put away is `accepted` at once. On a `quarantine` "
            "transfer (goods ticket 13D) every piece comes ashore into the "
            "destination's quarantine: `good` means arrived as it left, keeping its "
            "condition and every hold; the shipment is `accepted` at once, and each "
            "open owned hold work (`hold:{key}`) opens at the destination "
            "(`QUARANTINE_TRANSFER_ARRIVAL`) and closes at the source "
            "(`MOVED_BY_TRANSFER`) once nothing under that hold is left there. "
            "Nothing is accepted, released or made available. No vendor GRN, "
            "inbound PT or purchase cost is created. The same `command_id` with "
            "the same body replays; with a different body it is COMMAND_CONFLICT. "
            "Refusals: INVALID_REQUEST, ACTION_DENIED (`transfer.move` at the "
            "destination), NOT_FOUND (shipment, or a line it did not carry), "
            "CONTRACT_DISABLED / SITE_NOT_READY / UNDER_COUNT (either site's "
            "fence), TRANSFER_INVALID (a line not answered, or more found than it "
            "carried), INVALID_REQUEST (`WRONG_GOODS_UNNAMED`: a line's `wrong` and "
            "the excess entries naming it differ), STATE_CONFLICT (already counted, "
            "or returned to source), COMMAND_CONFLICT."
        ),
        request={"application/json": COUNT_REQUEST},
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, dispatch_id: uuid.UUID) -> Response:
        return super().post(request, pk, dispatch_id)


class DispatchAcceptView(_DispatchCommandView):
    """Put the good arrived pieces away at the destination."""

    action_name = "stock.transfer.accept"
    body_fields = transfers.ACCEPT_FIELDS
    required_fields = ("lines",)

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfers.ACCEPT_ACTION, site_id=transfer.destination_site_id)
        return transfer.destination_site_id

    def run(
        self,
        run: CommandRun,
        transfer: GoodsTransfer,
        dispatch_id: uuid.UUID,
        body: dict[str, Any],
    ) -> CommandResult:
        lines = transfers.parse_accept(body)
        record = transfers.accept(run, transfer.pk, dispatch_id, lines)
        return CommandResult(resource_type="transfer_dispatch", resource_id=str(record.pk))

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_accept",
        description=(
            "Put good arrived pieces of one counted shipment away at the "
            "destination, with the destination's own acceptance evidence; only "
            "then are they sellable there. Acceptance may be partial and repeated: "
            "each line names how many of its `awaiting_putaway` pieces go to which "
            "ordinary storage location. The shipment becomes `accepted` when "
            "nothing good is left waiting. No transfer reservation is inherited; "
            "held pieces are never offered; origin and value are unchanged. "
            "Refusals: INVALID_REQUEST, ACTION_DENIED (`stock.accept` at the "
            "destination), NOT_FOUND, CONTRACT_DISABLED / SITE_NOT_READY / "
            "UNDER_COUNT, TRANSFER_INVALID (more than is waiting, or not an "
            "ordinary storage location at the destination), STATE_CONFLICT (not "
            "counted, or nothing left to accept), COMMAND_CONFLICT."
        ),
        request={"application/json": ACCEPT_REQUEST},
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, dispatch_id: uuid.UUID) -> Response:
        return super().post(request, pk, dispatch_id)


RETURN_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": ["command_id", "contract_version", "receipt_key", "site_id", "reason", "lines"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "receipt_key": {
            "type": "string",
            "format": "uuid",
            "description": (
                "Identifies this physical receipt. The same key with the same content "
                "has one effect however often it is sent; with different content it "
                "is COMMAND_CONFLICT."
            ),
        },
        "site_id": {
            "type": "string",
            "description": "Where the goods physically came back to. Must be the source.",
        },
        "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            "description": "Why the delivery failed, or why these came back.",
        },
        "returned_at": {
            "type": "string",
            "format": "date-time",
            "description": "When they were back: not before they left, not after now.",
        },
        "evidence_reference": {
            "type": "string",
            "maxLength": 100,
            "description": "The source's own receiving evidence, such as a gate entry.",
        },
        "note": {"type": "string", "maxLength": 500},
        "lines": {
            "type": "array",
            "minItems": 1,
            "maxItems": transfers.MAX_LINES,
            "items": {
                "type": "object",
                "required": ["line_key"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "good": {"type": "integer", "minimum": 0},
                    "damaged": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


class DispatchReturnView(_DispatchCommandView):
    """A failed delivery: record at the source what actually came back."""

    action_name = "stock.transfer.return_to_source"
    body_fields = transfer_returns.RETURN_FIELDS
    required_fields = transfer_returns.RETURN_REQUIRED

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfers.MOVE_ACTION, site_id=transfer.source_site_id)
        return transfer.source_site_id

    def run(
        self,
        run: CommandRun,
        transfer: GoodsTransfer,
        dispatch_id: uuid.UUID,
        body: dict[str, Any],
    ) -> CommandResult:
        receipt = transfer_returns.parse_return(body, run.now)
        record = transfer_returns.return_to_source(run, transfer.pk, dispatch_id, receipt)
        return CommandResult(resource_type="transfer_dispatch", resource_id=str(record.pk))

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_return_to_source",
        description=(
            "Goods ticket 13C (transfers PRD §6). One actual return receipt at the "
            "source for a shipment the destination has not counted: per line, how "
            "many pieces are physically back good and how many damaged. Good pieces "
            "go to the source's receiving, unaccepted; damaged ones to its "
            "quarantine under a damage hold with a pending damage report; a piece "
            "that travelled in any other condition keeps it and is quarantined. "
            "Each piece keeps its origin and value (P09's check leg, at the source). "
            "What the receipt does not name stays in transit, the shipment becomes "
            "`partly_returned` and an owned `transfer_discrepancy` "
            "(`RETURN_INCOMPLETE`) opens at the source; a later receipt that brings "
            "the rest makes it `returned_to_source` and resolves that work. Each "
            "receipt keeps its own reason; the shipment's `return_reason` is the "
            "first receipt's - why the delivery failed - and is never overwritten. The "
            "dispatch and its departure are never rewritten and no destination "
            "receipt is written. Refusals: INVALID_REQUEST, ACTION_DENIED "
            "(`transfer.move` at the source), NOT_FOUND (shipment, or a line it did "
            "not carry), CONTRACT_DISABLED / SITE_NOT_READY / UNDER_COUNT (the "
            "source's fence), TRANSFER_INVALID (`RETURN_NOT_AT_SOURCE`: `site_id` is "
            "not the source; `RETURN_EXCEEDS_TRANSIT`: more than is still "
            "unaccounted for), STATE_CONFLICT (counted at the destination, or "
            "everything already back), COMMAND_CONFLICT (`RECEIPT_KEY_REUSED`), "
            "EVENT_TIME_INVALID (before it left, or later than now)."
        ),
        request={"application/json": RETURN_REQUEST},
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, dispatch_id: uuid.UUID) -> Response:
        return super().post(request, pk, dispatch_id)


ACCEPT_RETURNED_REQUEST: dict[str, Any] = ACCEPT_REQUEST


class DispatchReturnAcceptView(_DispatchCommandView):
    """Put a failed delivery's returned good pieces away at the source."""

    action_name = "stock.transfer.accept_returned"
    body_fields = transfer_returns.ACCEPT_RETURNED_FIELDS
    required_fields = ("lines",)

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfers.ACCEPT_ACTION, site_id=transfer.source_site_id)
        return transfer.source_site_id

    def run(
        self,
        run: CommandRun,
        transfer: GoodsTransfer,
        dispatch_id: uuid.UUID,
        body: dict[str, Any],
    ) -> CommandResult:
        lines = transfers.parse_accept(body)
        record = transfer_returns.accept_returned(run, transfer.pk, dispatch_id, lines)
        return CommandResult(resource_type="transfer_dispatch", resource_id=str(record.pk))

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_accept_returned",
        description=(
            "Goods ticket 13C. Accepts returned good pieces standing unaccepted in "
            "the source's receiving and puts them in an ordinary storage location "
            "there, with the source's own acceptance evidence; only then may they "
            "be sent or sold again. Held and quarantined pieces are never offered. "
            "The shipment's return state is unchanged. Refusals: INVALID_REQUEST, "
            "ACTION_DENIED (`stock.accept` at the source), NOT_FOUND, "
            "CONTRACT_DISABLED / SITE_NOT_READY / UNDER_COUNT (the source's fence), "
            "TRANSFER_INVALID (more than is waiting, or not an ordinary storage "
            "location at the source), STATE_CONFLICT (nothing has come back)."
        ),
        request={"application/json": ACCEPT_RETURNED_REQUEST},
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, dispatch_id: uuid.UUID) -> Response:
        return super().post(request, pk, dispatch_id)


ARRIVAL_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": ["command_id", "contract_version"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "arrived_at": {
            "type": "string",
            "format": "date-time",
            "description": "When it physically arrived: not before it left, not after now.",
        },
        "packages_received": {"type": "integer", "minimum": 0, "maximum": 9999},
        "note": {"type": "string", "maxLength": 500},
    },
    "additionalProperties": False,
}


class DispatchArrivalView(_DispatchCommandView):
    """E147: the shipment is physically at the destination. Nothing is released."""

    action_name = "stock.transfer.arrival"
    body_fields = frozenset({"arrived_at", "packages_received", "note"})

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfers.MOVE_ACTION, site_id=transfer.destination_site_id)
        return transfer.destination_site_id

    def run(
        self,
        run: CommandRun,
        transfer: GoodsTransfer,
        dispatch_id: uuid.UUID,
        body: dict[str, Any],
    ) -> CommandResult:
        from outbound import transfer_shipments

        parsed = transfer_shipments.parse_arrival(body, run.now)
        record = transfer_shipments.record_arrival(run, transfer.pk, dispatch_id, parsed)
        return CommandResult(resource_type="transfer_dispatch", resource_id=str(record.pk))

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_arrival",
        description=(
            "E147, per shipment (goods ticket 13B). Records that the shipment is "
            "physically at the destination, with an `arrival` event. It releases "
            "nothing: the pieces stay in transit - not destination stock, not "
            "available - until the whole shipment is counted. Refusals: "
            "INVALID_REQUEST, ACTION_DENIED (`transfer.move` at the destination), "
            "NOT_FOUND, CONTRACT_DISABLED / SITE_NOT_READY / UNDER_COUNT, "
            "STATE_CONFLICT (already counted or returned, or arrival already "
            "recorded), EVENT_TIME_INVALID (before it left, or later than now)."
        ),
        request={"application/json": ARRIVAL_REQUEST},
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, dispatch_id: uuid.UUID) -> Response:
        return super().post(request, pk, dispatch_id)


EWAY_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": ["command_id", "contract_version", "action", "reference"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "action": {"type": "string", "enum": ["attach", "verify"]},
        "reference": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": (
                "attach: the e-way reference being attached. verify: the reference "
                "on file being verified - it must be that one."
            ),
        },
        "note": {"type": "string", "maxLength": 500},
    },
    "additionalProperties": False,
}


class DispatchEwayView(GoodsAPIView):
    """E150: attach a late e-way reference, or verify the one on file."""

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_eway",
        description=(
            "E150, per shipment (goods ticket 13B; transfers PRD §8). `attach` "
            "(`transfer.move` at the sending site - the dispatcher) appends an "
            "`eway_added` event and resolves the shipment's `eway_missing` "
            "exception; whether a reference left with the goods stays as it was "
            "recorded at dispatch. `verify` (`pt.approve.transfer` at the sending "
            "site - the inventory controller's authority, which the Owner carries "
            "in this increment) appends an `eway_verified` event for the reference "
            "on file; attaching is never verifying. Neither moves stock, and a "
            "count freeze does not stop either. Nothing physical - arrival, count, "
            "acceptance or completion - resolves a missing-evidence exception, "
            "and nothing here certifies that a movement was lawful. Refusals: "
            "INVALID_REQUEST, ACTION_DENIED, NOT_FOUND, EWAY_VERIFY_DENIED (no "
            "reference on file, or not the one on file), STATE_CONFLICT (already "
            "verified)."
        ),
        request={"application/json": EWAY_REQUEST},
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, dispatch_id: uuid.UUID) -> Response:
        from outbound import transfer_shipments

        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data, transfer_shipments.EWAY_FIELDS, required=("action", "reference")
        )
        parsed = transfer_shipments.parse_eway(body)
        transfer = _transfer_for(access, pk)
        grant = (
            transfer_shipments.VERIFY_ACTION
            if parsed["action"] == "verify"
            else transfer_shipments.ATTACH_ACTION
        )
        access.require(grant, site_id=transfer.source_site_id)

        def handler(run: CommandRun) -> CommandResult:
            record = transfer_shipments.record_eway(run, transfer.pk, dispatch_id, parsed)
            return CommandResult(resource_type="transfer_dispatch", resource_id=str(record.pk))

        result = self.run_command(
            request,
            access=access,
            action=f"stock.transfer.eway.{parsed['action']}",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=transfer.source_site_id,
            subject_key=f"transfer_dispatch:{dispatch_id}",
        )
        return Response(_detail(access, pk), status=result.status_code)


SHORTAGE_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "GSA-T14: name the exact missing pieces of one counted shipment as a "
        "transit shortage, with the reason, the supporting evidence and the "
        "follow-up recorded with the source or the transporter. The shipment's "
        "recorded destination check is referenced by the server."
    ),
    "required": [
        "command_id",
        "contract_version",
        "lines",
        "reason",
        "evidence_reference",
        "followup_note",
    ],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "lines": {
            "type": "array",
            "minItems": 1,
            "maxItems": transfers.MAX_LINES,
            "items": {
                "type": "object",
                "required": ["line_key", "qty"],
                "properties": {
                    "line_key": {"type": "string", "format": "uuid"},
                    "qty": {"type": "integer", "minimum": 1, "maximum": transfers.MAX_QTY},
                },
                "additionalProperties": False,
            },
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        "evidence_reference": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "The supporting evidence, such as a recount sheet or a photo reference.",
        },
        "followup_note": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": "What was asked of, or heard from, the source or the transporter.",
        },
        "recount_note": {
            "type": "string",
            "maxLength": 500,
            "description": "What the destination's recount found, if it recounted.",
        },
        "pairing_key": {
            "type": "string",
            "format": "uuid",
            "description": (
                "Goods ticket 16: propose the short-expected half of one wrong-goods "
                "pair - the count's `excess` entry with this key. The proposal then "
                "names exactly that entry's line and quantity, once."
            ),
        },
    },
    "additionalProperties": False,
}


class DispatchShortageView(_DispatchCommandView):
    """The destination proposes that named missing pieces of one shipment are a shortage."""

    action_name = "stock.transfer.shortage.propose"
    body_fields = transfer_shortages.PROPOSE_FIELDS
    required_fields = transfer_shortages.PROPOSE_REQUIRED

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfer_shortages.PROPOSE_ACTION, site_id=transfer.destination_site_id)
        return transfer.destination_site_id

    def run(
        self,
        run: CommandRun,
        transfer: GoodsTransfer,
        dispatch_id: uuid.UUID,
        body: dict[str, Any],
    ) -> CommandResult:
        proposal = transfer_shortages.parse_proposal(body)
        gap = transfer_shortages.propose(run, transfer.pk, dispatch_id, proposal)
        return CommandResult(
            resource_type="gap_resolution", resource_id=str(gap.pk), status_code=201
        )

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_shortage_propose",
        description=(
            "Goods ticket 14 (E149 for a transit shortage; GSA-T14). The "
            "destination names, per line, how many of a counted shipment's missing "
            "pieces are a transit shortage. The server freezes the exact ranges of "
            "the original shipment that are still in transit and named by no other "
            "proposal, opens an unnumbered GAP document at the destination for a "
            "different person to decide, and an `approval_pending` exception "
            "(`SHORTAGE_APPROVAL`, subject `gap:{id}`) at the destination. Nothing "
            "moves: the pieces stay visibly in transit until approval. No source "
            "sign-off is asked for. Answers 201 with the transfer. Refusals: "
            "INVALID_REQUEST, ACTION_DENIED (`transfer.move` at the destination), "
            "NOT_FOUND (shipment, or a line it did not carry), CONTRACT_DISABLED / "
            "SITE_NOT_READY / UNDER_COUNT (the destination's fence), STATE_CONFLICT "
            "(not counted yet), GAP_ALREADY_RESOLVED (more than is still unresolved "
            "and unnamed on that line, or the pair's short-expected half is already "
            "proposed or resolved), TRANSFER_INVALID (a `pairing_key` proposal that "
            "does not name exactly its pair's line and quantity), COMMAND_CONFLICT. "
            "With a `pairing_key` that is not a wrong-goods pair of this shipment, "
            "NOT_FOUND."
        ),
        request={"application/json": SHORTAGE_REQUEST},
        responses=_responses(201, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, dispatch_id: uuid.UUID) -> Response:
        return super().post(request, pk, dispatch_id)


SHORTAGE_DECISION_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": ["command_id", "contract_version", "decision", "reason"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "decision": {"type": "string", "enum": list(transfer_shortages.DECISIONS)},
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "additionalProperties": False,
}


class ShortageDecisionView(GoodsAPIView):
    """The Owner, a different person, approves or rejects one shortage proposal."""

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_shortage_decide",
        description=(
            "Goods ticket 14 (GSA-R01). A different person from the proposer - "
            "the Owner, holding `pt.approve.transfer` at the source, with a fresh "
            "password confirmation - decides one pending shortage proposal. "
            "*Approve* numbers the GAP document and posts P13: exactly the frozen "
            "ranges of the original shipment leave transit for the `consumed` "
            "boundary, once, at unchanged origin and with no value leg - never "
            "counted in transit again. When the shipment's whole shortage is "
            "resolved its `transfer_discrepancy` closes, and the movement "
            "completes once every shipment is accounted for. *Reject* keeps the "
            "proposal and its history (the document is `reversed`) and leaves the "
            "pieces in transit for a fresh proposal. Either way the "
            "`approval_pending` exception closes. Refusals: INVALID_REQUEST, "
            "STEP_UP_REQUIRED, ACTION_DENIED, NOT_FOUND, SELF_APPROVAL (the "
            "proposer), CONTRACT_DISABLED / SITE_NOT_READY / UNDER_COUNT (the "
            "destination's fence), STATE_CONFLICT (already decided, or a named "
            "piece is no longer in transit), SERIES_NOT_READY (no GAP series), "
            "COMMAND_CONFLICT."
        ),
        request={"application/json": SHORTAGE_DECISION_REQUEST},
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, gap_id: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            transfer_shortages.DECIDE_FIELDS,
            required=transfer_shortages.DECIDE_REQUIRED,
        )
        transfer = _transfer_for(access, pk)
        access.require(transfer_shortages.DECIDE_ACTION, site_id=transfer.source_site_id)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            decision, reason = transfer_shortages.parse_decision(body)
            gap = transfer_shortages.decide(run, transfer.pk, gap_id, decision, reason)
            return CommandResult(resource_type="gap_resolution", resource_id=str(gap.pk))

        result = self.run_command(
            request,
            access=access,
            action="stock.transfer.shortage.decide",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=transfer.destination_site_id,
            subject_key=f"gap:{gap_id}",
        )
        return Response(_detail(access, pk), status=result.status_code)


# ---------------------------------------------------------------------------
# Goods ticket 16: correcting observed excess
# ---------------------------------------------------------------------------

CORRECTIVE_PROPOSAL_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "The source's proposal of a corrective transfer for exact pieces of one "
        "excess observation: the item it says the goods are (its own, with its "
        "evidence), how many, why, and optionally which of its eligible origins."
    ),
    "required": [
        "command_id",
        "contract_version",
        "sku_id",
        "qty",
        "source_evidence_reference",
        "reason",
    ],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "sku_id": {"type": "string", "format": "uuid"},
        "qty": {"type": "integer", "minimum": 1, "maximum": transfers.MAX_QTY},
        "source_evidence_reference": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "What shows the goods are the source's, such as a packing check.",
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        "origin_ids": {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "string", "format": "uuid"},
        },
    },
    "additionalProperties": False,
}


class ExcessCorrectiveView(GoodsAPIView):
    """The source proposes a corrective transfer for exact pieces of one observation."""

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_excess_corrective",
        description=(
            "Goods ticket 16 (E232; design §7.3). The source - `transfer.allocate` "
            "at the source site, both sites' fences - proposes a corrective "
            "transfer for `qty` pieces of one excess observation on this transfer "
            "(`lot_id` from a shipment's `excess_observations`). The goods must be "
            "the item the destination recorded, when it recorded one, and the "
            "source must hold that many eligible pieces of it (accepted, good, "
            "unheld, unreserved; FIFO or the named `origin_ids`). One command "
            "drafts the corrective transfer (linked by `corrective_for_id`), "
            "freezes its pieces into its transfer PT and sends it for approval "
            "(`approval_pending`, `TRANSFER_APPROVAL`, at the source), and opens "
            "an unnumbered excess-observed GAP decision at the destination that "
            "claims exactly `qty` still-held pieces of the observation, carrying "
            "the wrong-goods `pairing_key` where there is one. Nothing is reserved "
            "and nothing moves. The Owner approves the corrective transfer through "
            "the ordinary approval - a different person from the proposer "
            "(GSA-R01) - which reserves the source pieces and numbers the GAP "
            "decision. Answers 201 with this transfer. Refusals: INVALID_REQUEST, "
            "ACTION_DENIED, NOT_FOUND (no such observation on this transfer), "
            "CONTRACT_DISABLED / SITE_NOT_READY / UNDER_COUNT (either site's fence), "
            "EXCESS_EVIDENCE_MISSING (a different item from the one observed, or "
            "the source cannot show that many eligible pieces - the excess stays "
            "held, unvalued and visible; its valuation route is ticket 16A's), "
            "GAP_ALREADY_RESOLVED (more than is still held and unclaimed), "
            "SBU_RETIRED, COMMAND_CONFLICT."
        ),
        request={"application/json": CORRECTIVE_PROPOSAL_REQUEST},
        responses=_responses(201, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID, lot_id: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            transfer_excess.PROPOSE_FIELDS,
            required=transfer_excess.PROPOSE_REQUIRED,
        )
        transfer = _transfer_for(access, pk)
        access.require(transfer_excess.PROPOSE_ACTION, site_id=transfer.source_site_id)

        def handler(run: CommandRun) -> CommandResult:
            proposal = transfer_excess.parse_proposal(body)
            gap = transfer_excess.propose(run, transfer.pk, lot_id, proposal)
            return CommandResult(
                resource_type="gap_resolution", resource_id=str(gap.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="stock.transfer.excess.propose_corrective",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=transfer.source_site_id,
            subject_key=transfer_excess.subject_of(lot_id),
        )
        return Response(_detail(access, pk), status=result.status_code)


CORRECTIVE_CONFIRMATION_REQUEST: dict[str, Any] = {
    "type": "object",
    "required": ["command_id", "contract_version", "source_evidence_reference"],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "source_evidence_reference": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "The source's evidence that these reserved pieces are the goods.",
        },
        "confirmed_at": {
            "type": "string",
            "format": "date-time",
            "description": "When it was confirmed; not later than now. Defaults to now.",
        },
        "note": {"type": "string", "maxLength": 500},
    },
    "additionalProperties": False,
}


class CorrectiveConfirmationView(_TransferCommandView):
    """The source confirms an approved corrective transfer: one dispatch/arrival pair, one match."""

    action_name = "stock.transfer.corrective_confirmation"
    body_fields = transfer_excess.CONFIRM_FIELDS
    required_fields = transfer_excess.CONFIRM_REQUIRED

    def gate(self, access: AccessContext, transfer: GoodsTransfer) -> int:
        access.require(transfer_excess.CONFIRM_ACTION, site_id=transfer.source_site_id)
        return transfer.source_site_id

    def run(self, run: CommandRun, transfer: GoodsTransfer, body: dict[str, Any]) -> CommandResult:
        parsed = transfer_excess.parse_confirmation(body, run.now)
        record = transfer_excess.confirm(run, transfer.pk, parsed)
        return CommandResult(resource_type="transfer_dispatch", resource_id=str(record.pk))

    @extend_schema(
        operation_id="goods_v1_outbound_transfers_corrective_confirmation",
        description=(
            "Goods ticket 16 (E233; design P17). `transfer.move` at the source, "
            "both sites' fences, on an approved corrective transfer not yet "
            "confirmed. One command, once: every reserved source piece - still "
            "free of holds, exactly as many as its excess decision names - is "
            "consumed from its reservation and recorded leaving the source and "
            "arriving in the destination's receiving, good and unaccepted, keeping "
            "its origin and value; the decision's claimed interval of the "
            "observation leaves custody for the `matched_observation` boundary, "
            "its `transfer_excess` hold released, with one `CustodyMatch` per "
            "matched piece. The destination's physical quantity does not rise a "
            "second time; no destination GRN, inbound PT or purchase value is "
            "made. The corrective transfer gets one shipment, `counted`, which the "
            "destination puts away through the ordinary acceptance; its dispatch "
            "and arrival events carry `corrective: true`, and the original "
            "transfer records `corrective_matched`. When the whole observation is "
            "matched its `transfer_discrepancy` closes (`CORRECTIVE_MATCHED`). "
            "Refusals: INVALID_REQUEST, ACTION_DENIED, NOT_FOUND, "
            "CONTRACT_DISABLED / SITE_NOT_READY / UNDER_COUNT, TRANSFER_INVALID "
            "(not a corrective transfer), STATE_CONFLICT (not approved, or "
            "already confirmed), RESERVATION_BLOCKED (a reserved piece is held), "
            "CUSTODY_MATCH_INVALID (the reserved and claimed quantities differ, "
            "or a claimed piece is no longer held or is already matched), "
            "EVENT_TIME_INVALID, COMMAND_CONFLICT."
        ),
        request={"application/json": CORRECTIVE_CONFIRMATION_REQUEST},
        responses=_responses(200, TRANSFER_DETAIL, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        return super().post(request, pk)
