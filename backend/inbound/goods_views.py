"""Goods-v1 receiving endpoints (E094-E097, E113-E119, E167, E168, E236)."""

from __future__ import annotations

import uuid
from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    LIST_QUERY_KEYS,
    GoodsAPIView,
    business_body,
    check_query,
    page,
    paginate,
    parse_meta,
    resource_dto,
)
from accounts.principal import AccessContext
from alerts.goods_models import GoodsException
from approvals.goods_models import ActionDraft
from core.commands import CommandResult, CommandRun
from core.goods_documents import revision_lines
from core.kernel_models import DocumentHead, OfficialLine
from core.refusals import Refusal
from inbound import goods_input as inp
from inbound import sor_records, three_way_match
from inbound.goods_models import (
    Arrival,
    ArrivalHead,
    CounterGrnDraft,
    CountSession,
    Disposition,
    GoodsGrn,
    InvoiceClaimVersion,
)
from inbound.goods_services import (
    ARRIVAL_KEYS,
    CONDITIONS,
    DAMAGE_REPORT_ACTIONS,
    DAMAGE_REPORT_KEYS,
    DISPOSITION_KEYS,
    DISPOSITION_KINDS,
    INBOX_STEPS,
    NO_BOOKING_ACTION,
    READ_ACTIONS,
    RECEIVE_ACTION,
    arrival_data,
    arrival_hash,
    arrival_state,
    can_read,
    can_read_site,
    claim_data,
    confirm_no_booking,
    disposition_request_actions,
    disposition_request_state,
    duplicate_warning,
    goods_grn,
    grn_comparison,
    grn_count_history,
    grn_coverage,
    grn_damage_reports,
    grn_disposition_history,
    grn_hash,
    grn_invoice,
    hand_over_count,
    inbound_work,
    inbox_items,
    issue_grn,
    latest_claim,
    open_count_session,
    parse_arrival,
    parse_claim_lines,
    parse_counter_corrections,
    parse_damage_report,
    parse_disposition,
    parse_observations,
    record_arrival,
    record_disposition,
    record_invoice_claim,
    record_observations,
    report_receipt_damage,
    request_counter_grn,
    require_read,
    require_read_action,
    session_data,
    session_hash,
    session_observations,
)
from masters.store_features import is_feature_on, require_feature
from outbound import damage_review, pre_pt_custody
from outbound.goods_models import DamageReport
from outbound.goods_views import DAMAGE_REPORT

# ---------------------------------------------------------------------------
# Documented responses (ticket 05)
#
# These views hand-build dict responses rather than run through a serializer, so
# drf-spectacular sees nothing without the schemas below. Ticket 05 could not
# describe ``pending``, ``queue`` and the ``grns`` list/create: they shared their
# path templates with the legacy readers through the old contract dispatcher,
# and OpenAPI allows one operation per path and method (#303). All four answer
# under ``/api/goods-v1/inbound/`` alone now, so every operation in this module
# is described. The envelopes are repeated rather than imported so this module
# documents its own contract, as ``masters/goods_views.py`` and
# ``masters/goods_identity_views.py`` do.
# ---------------------------------------------------------------------------

REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}


def _dto_response(data_schema: dict[str, Any], description: str) -> dict[str, Any]:
    """The ``ResourceDTO<...>`` envelope of design §6.1 around one ``data`` schema."""
    return {
        "type": "object",
        "description": description,
        "properties": {
            "id": {"type": "string"},
            "record_contract": {"type": "string", "enum": ["goods-v1"]},
            "revision": {"type": "integer"},
            "content_hash": {"type": "string"},
            "state": {"type": "string"},
            "number": {"type": "string", "nullable": True},
            "version": {"type": "integer", "nullable": True},
            "context": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string", "nullable": True},
                    "entity_id": {"type": "string", "nullable": True},
                    "brand_id": {"type": "string", "nullable": True},
                },
            },
            "data": data_schema,
            "allowed_actions": {"type": "array", "items": {"type": "string"}},
        },
    }


def _page_response(item_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "description": "Page<T> (design §6.1).",
        "properties": {
            "items": {"type": "array", "items": item_schema},
            "next_cursor": {"type": "string", "nullable": True},
        },
    }


SUMMARY_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "ResourceSummary (design §6.1).",
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
    },
}

ARRIVAL_SUMMARY_ITEM: dict[str, Any] = {
    "type": "object",
    "description": (
        "ResourceSummary, plus the three fields an arrivals list is read by: the "
        "vendor and invoice each row carries, and the transporter reference "
        "deliveries that travelled together are grouped by. Grouping is "
        "presentation only; the arrivals stay separate records (GSA-T05). The "
        "duplicate-invoice warning is E250's, not this list's."
    ),
    "properties": {
        **SUMMARY_ITEM["properties"],
        "vendor_id": {"type": "string"},
        "transporter_ref": {"type": "string", "nullable": True},
        "invoice_number": {"type": "string", "nullable": True},
    },
}

DUPLICATE_ACKNOWLEDGEMENT: dict[str, Any] = {
    "type": "object",
    "nullable": True,
    "description": (
        "Why this arrival was recorded although the same vendor and invoice were "
        "already recorded in the receiving legal entity: which warning was "
        "answered, by whom, when and why. Null on an arrival that raised none. "
        "The matched arrivals are evidence, not part of this answer - a later "
        "reader of this arrival may reach fewer sites than the recorder did."
    ),
    "required": ["warning_hash", "reason", "actor_id", "recorded_at"],
    "properties": {
        "warning_hash": {"type": "string"},
        "reason": {"type": "string"},
        "actor_id": {"type": "string", "format": "uuid", "nullable": True},
        "recorded_at": {"type": "string", "format": "date-time"},
    },
}

DUPLICATE_WARNING_CANDIDATE: dict[str, Any] = {
    "type": "object",
    "description": (
        "One arrival already recorded for this vendor and invoice number that "
        "the caller may read for themselves."
    ),
    "required": ["id", "site_id", "state", "recorded_at", "transporter_ref"],
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "site_id": {"type": "string"},
        "state": {"type": "string"},
        "recorded_at": {"type": "string", "format": "date-time"},
        "transporter_ref": {"type": "string", "nullable": True},
    },
}

DUPLICATE_WARNING_DATA: dict[str, Any] = {
    "type": "object",
    "description": (
        "E250. The server's own answer to 'is this invoice already recorded "
        "here?'. `warning_hash` is opaque and covers exactly the candidates "
        "listed; E113 accepts it back with a reason and refuses a stale one. "
        "An empty `candidates` list comes with a null hash and nothing to "
        "acknowledge. Matches the caller cannot read are neither listed nor "
        "counted (design §5.8)."
    ),
    "required": ["warning_hash", "site_id", "vendor_id", "invoice_number", "candidates"],
    "properties": {
        "warning_hash": {"type": "string", "nullable": True},
        "site_id": {"type": "string"},
        "vendor_id": {"type": "string"},
        "invoice_number": {"type": "string"},
        "candidates": {"type": "array", "items": DUPLICATE_WARNING_CANDIDATE},
    },
}

ARRIVAL_DATA: dict[str, Any] = {
    "type": "object",
    "description": "ArrivalInput (design §5.3) as recorded.",
    "properties": {
        "site_id": {"type": "string"},
        "vendor_id": {"type": "string"},
        "brand_id": {"type": "string"},
        "subbrand_key": {"type": "string", "nullable": True},
        "actual_arrival_at": {"type": "string"},
        "transporter_ref": {"type": "string", "nullable": True},
        "booking_id": {"type": "string", "nullable": True},
        "invoice_number": {"type": "string", "nullable": True},
        "invoice_date": {"type": "string", "nullable": True},
        "invoice_evidence_id": {"type": "string", "nullable": True},
        "duplicate_acknowledgement": DUPLICATE_ACKNOWLEDGEMENT,
        "brand_dispatch_date": {
            "type": "string",
            "nullable": True,
            "description": "The brand's dispatch date standing for this delivery (ticket 24).",
        },
    },
}

CLAIM_LINE: dict[str, Any] = {
    "type": "object",
    "description": "ClaimLines entry (design §5.3). Money is integer paise as a string.",
    "properties": {
        "line_key": {"type": "string"},
        "style_code": {"type": "string", "nullable": True},
        "sku_id": {"type": "string", "nullable": True},
        "size_value_id": {"type": "string", "nullable": True},
        "description": {"type": "string"},
        "claimed_qty": {"type": "integer"},
        "invoice_basic_paise": {"type": "string", "nullable": True},
        "invoice_mrp_paise": {"type": "string", "nullable": True},
        "evidence_line_ref": {"type": "string", "nullable": True},
        "remark": {"type": "string", "nullable": True},
    },
}

CLAIM_DATA: dict[str, Any] = {
    "type": "object",
    "description": "One appended invoice claim version; a claim never becomes GRN cost.",
    "properties": {
        "arrival_id": {"type": "string"},
        "revision": {"type": "integer"},
        "evidence_id": {"type": "string", "nullable": True},
        "lines": {"type": "array", "items": CLAIM_LINE},
        "content_hash": {"type": "string"},
    },
}

THREE_WAY_ROW: dict[str, Any] = {
    "type": "object",
    "description": (
        "One three-way row (ticket 37): an invoice line, or a counted line on no invoice "
        "line. Cost keys are absent for a viewer without the cost field."
    ),
    "properties": {
        "row_key": {"type": "string"},
        "invoice_line_key": {"type": "string", "nullable": True},
        "grn_line_keys": {"type": "array", "items": {"type": "string"}},
        "booking_line_key": {"type": "string", "nullable": True},
        "booking": {"type": "string", "enum": ["linked", "not_booked", "several", "no_booking"]},
        "description": {"type": "string"},
        "booked_qty": {"type": "integer", "nullable": True},
        "invoiced_qty": {"type": "integer", "nullable": True},
        "counted_qty": {"type": "integer"},
        "booked_cost_paise": {"type": "string", "nullable": True},
        "invoiced_cost_paise": {"type": "string", "nullable": True},
        "cost_difference_paise": {"type": "string", "nullable": True},
        "results": {
            "type": "array",
            "items": {"type": "string", "enum": ["short", "excess", "cost_differs"]},
        },
        "status": {"type": "string", "enum": ["match", "mismatch"]},
    },
}

THREE_WAY_DATA: dict[str, Any] = {
    "type": "object",
    "description": "Booked, invoiced and counted per line for one arrival (ticket 37).",
    "properties": {
        "arrival_id": {"type": "string"},
        "grn_id": {"type": "string", "nullable": True},
        "has_booking": {"type": "boolean"},
        "has_invoice": {"type": "boolean"},
        "sees_cost": {"type": "boolean"},
        "qty_tolerance": {"type": "integer"},
        "cost_tolerance_paise": {"type": "string"},
        "exception_id": {"type": "string", "nullable": True},
        "rows": {"type": "array", "items": THREE_WAY_ROW},
    },
}

SESSION_OBSERVATION: dict[str, Any] = {
    "type": "object",
    "description": (
        "One durable observation exactly as it was recorded. Its `id` is what an "
        "E091 identity pick binds to when a scanned code matched several products."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "scan_key": {"type": "string", "format": "uuid"},
        "sku_id": {"type": "string", "format": "uuid", "nullable": True},
        "alias_value": {"type": "string", "nullable": True},
        "alias_context": {
            "type": "object",
            "nullable": True,
            "description": (
                "The scope the code was read under. Kept with the scan so the "
                "GRN judges the same code the same way the counter did."
            ),
            "properties": {
                "issuer_key": {"type": "string", "nullable": True},
                "alias_type": {"type": "string", "nullable": True},
                "profile_version_id": {"type": "string", "format": "uuid", "nullable": True},
            },
        },
        "description": {"type": "string"},
        "condition": {"type": "string", "enum": list(CONDITIONS)},
        "qty": {"type": "integer"},
        "correction_of_id": {"type": "string", "format": "uuid", "nullable": True},
    },
}

SESSION_HANDOVER: dict[str, Any] = {
    "type": "object",
    "description": (
        "One CountHandover: an unfinished count passed from the person holding it "
        "to another authorised person at the same site (E240). The session's "
        "counter and entry user are the current owner; this chain is who held it "
        "before, and why."
    ),
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "session_id": {"type": "string", "format": "uuid"},
        "from_human_id": {"type": "string", "format": "uuid"},
        "to_human_id": {"type": "string", "format": "uuid"},
        "actor_id": {"type": "string", "format": "uuid", "nullable": True},
        "recorded_at": {"type": "string", "format": "date-time"},
        "reason_code": {"type": "string"},
    },
}

SESSION_DATA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The count session, every scan key the server has acknowledged, the "
        "durable observations themselves, and who has held the count."
    ),
    "properties": {
        "arrival_id": {"type": "string"},
        "counter_id": {"type": "string"},
        "entry_user_id": {"type": "string"},
        "state": {"type": "string"},
        "revision": {"type": "integer"},
        "grn_id": {"type": "string", "nullable": True},
        "acknowledged_scan_keys": {"type": "array", "items": {"type": "string"}},
        "observations": {"type": "array", "items": SESSION_OBSERVATION},
        "handovers": {"type": "array", "items": SESSION_HANDOVER},
    },
}

OBSERVED_IDENTITY: dict[str, Any] = {
    "type": "object",
    "description": "ObservedIdentity (design §5.3); unidentified goods carry words, not a SKU.",
    "properties": {
        "sku_id": {"type": "string", "nullable": True},
        "description": {"type": "string"},
        "attributes": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "raw_alias": {"type": "string", "nullable": True},
    },
}

GRN_LINE_ITEM: dict[str, Any] = {
    "type": "object",
    "description": (
        "GrnCoverageDTO line. The physical quantities, plus what the pieces "
        "are and the condition they were counted in - a held quantity has to be "
        "able to say why it is held. `uncovered_qty` (no PT covers it yet) and "
        "`held_qty` (an actual hold is on it) are separate facts (GSA-T05)."
    ),
    "properties": {
        "line_key": {"type": "string"},
        "counted_qty": {"type": "integer"},
        "covered_qty": {"type": "integer"},
        "uncovered_qty": {"type": "integer"},
        "held_qty": {"type": "integer"},
        "unheld_uncovered_qty": {
            "type": "integer",
            "description": "Pieces no live PT covers and no hold is on (ticket 07B): on a good "
            "line, what a supplement PT may still cover, accepted excess included.",
        },
        "accepted_uncovered_qty": {
            "type": "integer",
            "description": "Ticket 05D: of a wrong or unidentified line, the pieces a "
            "different person accepted after their identity was resolved that no live PT "
            "covers and no hold is on - what a primary or supplement PT may cover. Zero on "
            "every other line.",
        },
        "disposed_qty": {"type": "integer"},
        "damage_held_qty": {
            "type": "integer",
            "description": "Of `held_qty`, the pieces held because damage was reported on "
            "them - each under a report a different person decides (ticket 05C).",
        },
        "damage_reportable_qty": {
            "type": "integer",
            "description": "Pieces on this line damage may still be reported on (E254): "
            "physically here, on no live PT, good and under no hold, or damaged and "
            "under no damage hold yet.",
        },
        "in_transit_qty": {
            "type": "integer",
            "description": (
                "Pieces of this line on the road between sites right now, on a pre-PT "
                "custody transfer (goods ticket 13E). Still this GRN's goods, uncovered "
                "and held; not in `uncovered_qty` or `held_qty` while they travel."
            ),
        },
        "elsewhere_qty": {
            "type": "integer",
            "description": (
                "Of `uncovered_qty`, the pieces standing at another site - moved there "
                "by a pre-PT custody transfer (goods ticket 13E), still held and "
                "still this GRN's goods. `counted_qty` = `covered_qty` + "
                "`uncovered_qty` + `in_transit_qty` + `disposed_qty` while nothing "
                "else has left."
            ),
        },
        "identity": OBSERVED_IDENTITY,
        "condition": {"type": "string", "enum": list(CONDITIONS)},
        "discrepancy_remark": {"type": "string", "nullable": True},
    },
}

GRN_COVERAGE_DATA: dict[str, Any] = {
    "type": "object",
    "description": "GrnCoverageDTO (design §6.1) with its invoice comparison and count history.",
    "properties": {
        "grn_header": {"type": "object", "additionalProperties": True},
        "lines": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": GRN_LINE_ITEM},
                "next_cursor": {"type": "string", "nullable": True},
                "total": {"type": "integer"},
            },
        },
        "invoice": {
            "type": "object",
            "nullable": True,
            "properties": {
                "claim_revision_id": {"type": "string"},
                "revision": {"type": "integer"},
                "invoice_number": {"type": "string", "nullable": True},
                "invoice_date": {"type": "string", "nullable": True},
                "evidence_id": {
                    "type": "string",
                    "nullable": True,
                    "description": "The confirmed evidence object the claim was entered from.",
                },
                "lines": {"type": "array", "items": CLAIM_LINE},
            },
        },
        "invoice_comparison": {
            "type": "array",
            "description": "Claimed versus counted per claim line, paired by the server.",
            "items": {
                "type": "object",
                "properties": {
                    "claim_line_key": {"type": "string"},
                    "claimed_qty": {"type": "integer"},
                    "counted_qty": {"type": "integer"},
                    "difference": {"type": "integer"},
                    "remaining_shortage_qty": {
                        "type": "integer", "minimum": 0,
                        "description": "Shortage still undecided by the canonical receipt writer; pending requests have not consumed it.",
                    },
                    "line_keys": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "count_history": {
            "type": "array",
            "description": "The issued count and every counter-GRN against it, oldest first.",
            "items": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "kind": {"type": "string", "enum": ["grn", "counter_grn"]},
                    "number": {"type": "string", "nullable": True},
                    "state": {"type": "string"},
                    "recorded_at": {"type": "string"},
                    "revision": {
                        "type": "integer",
                        "description": (
                            "A counter-GRN's own head revision — the one its approval "
                            "request is bound to. Absent on the GRN's own entry."
                        ),
                    },
                    "lines": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                    },
                },
            },
        },
        "custody_transfers": {
            "type": "array",
            "description": (
                "Every pre-PT custody transfer naming this GRN's damaged goods, oldest "
                "first (goods ticket 13E): where they went, in which state, and how many "
                "pieces of which lines. Open each at the transfers screen."
            ),
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "state",
                    "source_site_id",
                    "destination_site_id",
                    "qty",
                    "line_keys",
                ],
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "state": {"type": "string"},
                    "source_site_id": {"type": "string"},
                    "destination_site_id": {"type": "string"},
                    "qty": {"type": "integer"},
                    "line_keys": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "damage_reports": {
            "type": "array",
            "description": (
                "Every damage report over this GRN's goods, newest first (ticket 05C): "
                "damage the count found, damage reported before the PT (E254), and any "
                "stock-screen report on the same pre-PT goods. Each is decided in the "
                "common review (E252/E253); this names it and says which lines it covers."
            ),
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "source",
                    "state",
                    "quantity",
                    "reason_code",
                    "reported_by",
                    "reported_at",
                    "reviewed_by",
                    "reviewed_at",
                    "lines",
                ],
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "source": {
                        "type": "string",
                        "enum": ["movement", "receiving", "transfer_arrival", "transfer_return"],
                    },
                    "state": {"type": "string", "enum": list(DamageReport.State.values)},
                    "quantity": {"type": "integer"},
                    "reason_code": {"type": "string"},
                    "reported_by": {
                        "type": "object",
                        "required": ["id", "name"],
                        "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
                    },
                    "reported_at": {"type": "string", "format": "date-time"},
                    "reviewed_by": {
                        "type": "object",
                        "nullable": True,
                        "required": ["id", "name"],
                        "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
                    },
                    "reviewed_at": {"type": "string", "nullable": True},
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["line_key", "qty"],
                            "properties": {
                                "line_key": {"type": "string"},
                                "qty": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        },
        "dispositions": {
            "type": "array",
            "description": (
                "GRN disposition requests with damage evidence separate from source "
                "value and tax evidence, including approval state and frozen value."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "approval_request_id": {"type": "string", "nullable": True},
                    "kind": {"type": "string"},
                    "state": {"type": "string"},
                    "source_line_key": {"type": "string"},
                    "lot_id": {"type": "string", "nullable": True},
                    "qty": {"type": "integer"},
                    "reason_code": {"type": "string"},
                    "damage_description": {"type": "string", "nullable": True},
                    "damage_evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "resolved_sku_id": {"type": "string", "nullable": True},
                    "source_value_evidence": {
                        "type": "object",
                        "description": (
                            "Source value evidence, identified separately from the damage "
                            "evidence above. Null throughout for a reader without the cost "
                            "field."
                        ),
                        "properties": {
                            "evidence_id": {"type": "string", "nullable": True},
                            "cost_paise": {"type": "string", "nullable": True},
                            "mrp_paise": {"type": "string", "nullable": True},
                        },
                    },
                    "tax_basis_evidence_id": {"type": "string", "nullable": True},
                    "requested_value_paise": {"type": "string", "nullable": True},
                    "frozen_value_paise": {
                        "type": "string",
                        "nullable": True,
                        "description": (
                            "The approved origins' own value, present only once the "
                            "request is approved."
                        ),
                    },
                    "maker_id": {"type": "string"},
                },
            },
        },
    },
}

COUNTER_DATA: dict[str, Any] = {
    "type": "object",
    "description": "The counter-GRN draft: one correction per line, with its reason.",
    "additionalProperties": True,
}

DISPOSITION_DATA: dict[str, Any] = {
    "type": "object",
    "description": "DispositionPayload (design §5.3), or the parked request a checker decides.",
    "properties": {
        "kind": {"type": "string"},
        "source_document_id": {"type": "string", "format": "uuid"},
        "source_line_key": {"type": "string", "format": "uuid"},
        "lot_id": {"type": "string", "format": "uuid", "nullable": True},
        "qty": {"type": "integer", "minimum": 1},
        "reason_code": {"type": "string"},
        "evidence_ids": {
            "type": "array",
            "description": "Damage evidence, kept separate from value and tax evidence.",
            "items": {"type": "string", "format": "uuid"},
        },
        "damage_description": {"type": "string", "nullable": True},
        "resolved_sku_id": {"type": "string", "format": "uuid", "nullable": True},
        "approved_cost_evidence_id": {
            "type": "string",
            "format": "uuid",
            "nullable": True,
        },
        "approved_tax_evidence_id": {
            "type": "string",
            "format": "uuid",
            "nullable": True,
        },
        "approved_cost_paise": {
            "type": "string",
            "description": "Evidenced unit cost in integer paise.",
            "nullable": True,
        },
        "approved_mrp_paise": {
            "type": "string",
            "description": "Evidenced unit ticket MRP in integer paise.",
            "nullable": True,
        },
        "reviewed_grn_hash": {"type": "string", "minLength": 64, "maxLength": 64},
        "reviewed_pt_hash": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
            "nullable": True,
        },
        "followup_owner_id": {"type": "string", "format": "uuid", "nullable": True},
    },
}

#: What E117 answers when a GRN is issued: the official document itself - the
#: canonical header of its live version with that version's lines - not the
#: coverage read, which is a different question asked at the detail route.
GRN_PAYLOAD_DATA: dict[str, Any] = {
    "type": "object",
    "description": "The issued GRN's official payload (E117).",
    "additionalProperties": True,
    "properties": {
        "lines": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    },
}

#: One row of E167/E168's inbound worklist, and of E096's GRN list: the same
#: `_summary` shape `inbound.goods_services` builds for every kind of work.
WORK_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "DocumentSummaryDTO (E096, E167, E168).",
    "properties": {
        "id": {"type": "string"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "kind": {"type": "string", "enum": ["arrival", "grn"]},
        "purpose": {"type": "string", "nullable": True},
        "number": {"type": "string", "nullable": True},
        "state": {"type": "string"},
        "site_id": {"type": "string"},
        "brand_id": {"type": "string", "nullable": True},
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "owner_role": {"type": "string", "nullable": True},
        "due_at": {"type": "string", "format": "date-time", "nullable": True},
    },
}

#: One row of the receiving inbox (store and warehouse operations PRD §5.1): a
#: vendor delivery or an incoming transfer dispatch, with the step it is waiting
#: on, who owns that step, and the records it has reached so far. The links are
#: what the workflow screen opens; each is absent until its record exists.
INBOX_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "ReceivingInboxDTO: one delivery, with its next step and its records.",
    "properties": {
        "id": {"type": "string"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "kind": {
            "type": "string",
            "enum": ["vendor_delivery", "transfer_dispatch", "customer_return"],
        },
        "site_id": {"type": "string"},
        "brand_id": {"type": "string", "nullable": True},
        "reference": {
            "type": "string",
            "description": "What a person calls this delivery: vendor and invoice, or the "
            "dispatch reference.",
        },
        "arrived_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "next_step": {"type": "string", "enum": list(INBOX_STEPS)},
        "next_step_owner_role": {"type": "string", "nullable": True},
        "in_receiving_queue": {
            "type": "boolean",
            "description": "Whether the receiving queue (E167) also carries this delivery.",
        },
        "arrival_id": {"type": "string", "format": "uuid", "nullable": True},
        "grn_id": {"type": "string", "format": "uuid", "nullable": True},
        "grn_number": {"type": "string", "nullable": True},
        "pt_id": {
            "type": "string",
            "format": "uuid",
            "nullable": True,
            "description": "The PT this delivery is working on now: the primary, then each "
            "supplement in turn. Null before any PT, and while a new supplement is still to "
            "be started.",
        },
        "pt_number": {"type": "string", "nullable": True},
        "pt_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "description": "Every receipt PT this delivery has had, primary first, then its "
            "supplements (ticket 07B). Empty for the other kinds.",
        },
        "official_version_id": {"type": "string", "format": "uuid", "nullable": True},
        "acceptance_session_id": {"type": "string", "format": "uuid", "nullable": True},
        # An incoming transfer dispatch (OPS-06) names the movement it belongs
        # to and the shipment itself; a vendor delivery carries null for both.
        "transfer_id": {"type": "string", "format": "uuid", "nullable": True},
        "dispatch_id": {"type": "string", "format": "uuid", "nullable": True},
        # A customer return (OPS-09) names the bill line its pieces came back on;
        # the other two kinds carry null.
        "sale_line_id": {"type": "string", "nullable": True},
        # The booking a vendor delivery was received against, and how much of it has
        # been received so far (OPS-17, PRD §5.1: "Booking B-104: 80 of 100
        # received"). Worked out from the goods-v1 receipt links on every read, never
        # a stored counter. Null for an unbooked delivery, for the other kinds, and
        # for a booking this reader may not read.
        "booking_id": {"type": "string", "format": "uuid", "nullable": True},
        "booking_number": {"type": "string", "nullable": True},
        "booking_booked_qty": {
            "type": "integer",
            "nullable": True,
            "description": "Pieces on the booking's current lines.",
        },
        "booking_received_qty": {
            "type": "integer",
            "nullable": True,
            "description": "Pieces linked as received against the booking, less any a "
            "counter-GRN reversed.",
        },
    },
}

#: E168 is E167's page plus per-state counts over the identical scope, so the
#: two never disagree about how much work there is.
QUEUE_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "Page<DocumentSummaryDTO> with per-state counts (E168).",
    "properties": {
        "counts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "count": {"type": "integer"},
                },
            },
        },
        "items": {"type": "array", "items": WORK_ITEM},
        "next_cursor": {"type": "string", "nullable": True},
        "as_of": {"type": "string", "format": "date-time"},
    },
}

_READ_REFUSALS = (400, 401, 403, 404)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


_TEXT = {"type": "string"}
_UUID = {"type": "string", "format": "uuid"}
_LEGACY_ID = {"oneOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": "^[0-9]+$"}]}
_DATE = {"type": "string", "format": "date"}
_DATETIME = {"type": "string", "format": "date-time"}
_PAISE = {"type": "string", "pattern": "^[0-9]+$", "nullable": True}


def _mutation_request(
    fields: dict[str, Any], *, required: tuple[str, ...] = (), revision_bound: bool = True
) -> dict[str, Any]:
    required_fields = ["command_id", "contract_version", *required]
    if revision_bound:
        required_fields.append("expected_revision")
    return {
        "type": "object",
        "required": required_fields,
        "properties": {
            "command_id": _UUID,
            "contract_version": {"type": "string", "enum": ["goods-v1"]},
            "expected_revision": {"type": "integer", "minimum": 1},
            **fields,
        },
        "additionalProperties": False,
    }


ARRIVAL_CREATE_REQUEST = _mutation_request(
    {
        "site_id": _LEGACY_ID, "vendor_id": _LEGACY_ID, "brand_id": _LEGACY_ID,
        "subbrand_key": _TEXT, "actual_arrival_at": _DATETIME,
        "transporter_ref": _TEXT, "booking_id": _UUID, "invoice_number": _TEXT,
        "invoice_date": _DATE, "invoice_evidence_id": _UUID,
        "duplicate_warning_hash": _TEXT, "duplicate_reason": _TEXT,
        "brand_dispatch_date": _DATE,
    },
    required=("site_id", "vendor_id", "brand_id", "actual_arrival_at"),
    revision_bound=False,
)
_CLAIM_LINE_REQUEST = {
    "type": "object",
    "required": ["line_key", "description", "claimed_qty"],
    "properties": {
        "line_key": _UUID, "style_code": _TEXT, "sku_id": _UUID,
        "size_value_id": _UUID, "description": _TEXT,
        "claimed_qty": {"type": "integer", "minimum": 0},
        "invoice_basic_paise": _PAISE, "invoice_mrp_paise": _PAISE,
        "evidence_line_ref": _TEXT, "remark": _TEXT,
    },
    "additionalProperties": False,
}
ARRIVAL_INVOICE_REQUEST = _mutation_request(
    {
        "invoice_number": _TEXT, "invoice_date": _DATE, "evidence_id": _UUID,
        "lines": {"type": "array", "items": _CLAIM_LINE_REQUEST}, "reason_code": _TEXT,
    },
    required=("invoice_number", "invoice_date", "lines"),
)
COUNT_SESSION_REQUEST = _mutation_request(
    {"counter_id": _UUID, "entry_user_id": _UUID},
    required=("counter_id", "entry_user_id"), revision_bound=False,
)
NO_BOOKING_REQUEST = _mutation_request(
    {"reason_code": _TEXT, "evidence_id": _UUID}, required=("reason_code",)
)
_OBSERVATION_REQUEST = {
    "type": "object",
    "required": ["scan_key", "description", "condition", "qty"],
    "properties": {
        "scan_key": _UUID, "sku_id": _UUID, "description": _TEXT,
        "alias_value": _TEXT,
        "alias_context": {
            "type": "object",
            "properties": {"issuer_key": _TEXT, "alias_type": _TEXT, "profile_version_id": _UUID},
            "additionalProperties": False,
        },
        "attrs": {"type": "array", "items": {
            "type": "object",
            "required": ["field_id"],
            "properties": {"field_id": _UUID, "vocabulary_value_id": _UUID,
                           "supplied_text": _TEXT, "unknown": {"type": "boolean"}},
            "additionalProperties": False,
        }},
        "condition": {"type": "string", "enum": list(CONDITIONS)},
        "qty": {"type": "integer", "minimum": -999999, "maximum": 999999},
        "correction_of_id": _UUID,
    },
    "additionalProperties": False,
}
OBSERVATIONS_REQUEST = _mutation_request(
    {"observations": {"type": "array", "items": _OBSERVATION_REQUEST}},
    required=("observations",),
)
COUNT_HANDOVER_REQUEST = _mutation_request(
    {"to_human_id": _UUID, "reason_code": _TEXT, "reviewed_hash": _TEXT},
    required=("to_human_id", "reason_code", "reviewed_hash"),
)
GRN_ISSUE_REQUEST = _mutation_request(
    {"count_session_id": _UUID, "reviewed_hash": _TEXT,
     "remarks": {"type": "array", "items": {
         "type": "object", "required": ["claim_line_key", "remark"],
         "properties": {"claim_line_key": _UUID, "remark": _TEXT},
         "additionalProperties": False,
     }}},
    required=("count_session_id", "reviewed_hash"),
)
COUNTER_GRN_REQUEST = _mutation_request(
    {"corrections": {"type": "array", "minItems": 1, "items": {
        "type": "object",
        "required": ["line_key", "new_qty", "new_condition", "reason_code"],
        "properties": {"line_key": _UUID, "new_qty": {"type": "integer", "minimum": 0},
                       "new_condition": {"type": "string", "enum": list(CONDITIONS)},
                       "reason_code": _TEXT},
        "additionalProperties": False,
    }}, "evidence_ids": {"type": "array", "items": _UUID}},
    required=("corrections",),
)


DETAIL_QUERY_KEYS = frozenset({"version", "line_cursor", "history_cursor"})
DUPLICATE_QUERY_KEYS = frozenset({"site_id", "vendor_id", "invoice_number"})
QUEUE_QUERY_KEYS = LIST_QUERY_KEYS | {"sku_id", "origin_id", "condition", "state", "basis", "as_of"}
#: The inbox asks a simpler question than the queue does - one site, one tab -
#: and names its site parameter `site`, as the operations contract writes it.
#: `arrival`, `grn` and `pt` narrow it to the one delivery those records belong to,
#: which is how a link to a GRN or a PT opens its delivery at the right step (OPS-17).
INBOX_QUERY_KEYS = frozenset({"site", "view", "arrival", "grn", "pt", "cursor", "limit"})
#: The record filters, each matched against the row field of the same record.
INBOX_RECORD_FILTERS = {"arrival": "arrival_id", "grn": "grn_id", "pt": "pt_id"}


def _require_at_site(
    access: AccessContext, actions: tuple[str, ...], site_id: int, brand_id: int | None
) -> None:
    if not any(a in access.all_actions() for a in actions):
        raise Refusal("ACTION_DENIED", "You do not have permission for this action.")
    if not any(access.can(a, site_id=site_id, brand_id=brand_id) for a in actions):
        raise Refusal("NOT_FOUND", "That record was not found.")


def _arrival(pk: uuid.UUID) -> Arrival:
    arrival = Arrival.objects.filter(pk=pk).first()
    if arrival is None:
        raise Refusal("NOT_FOUND", "That arrival was not found.")
    return arrival


def _arrival_resource(arrival: Arrival) -> dict[str, Any]:
    head = ArrivalHead.objects.get(arrival_id=arrival.pk)
    body = resource_dto(
        id=arrival.pk,
        data=arrival_data(arrival),
        revision=head.revision,
        state=arrival_state(arrival, head),
        context={"site_id": arrival.site_id, "brand_id": arrival.brand_id},
        # Ticket 37: the screen asks for the three-way match only where the site's
        # switch is on, so a store with it off is never sent a refusal it did not ask for.
        allowed_actions=(
            [three_way_match.ALLOWED_ACTION]
            if is_feature_on(arrival.site_id, three_way_match.FEATURE_KEY)
            else []
        ),
    )
    # Ticket 24: the brand's dispatch date is its own record, changed apart from the
    # arrival, so it is shown here and kept out of the arrival's content hash.
    dispatch = sor_records.standing_dispatch(arrival.pk)
    body["data"]["brand_dispatch_date"] = dispatch.dispatch_date.isoformat() if dispatch else None
    body["content_hash"] = arrival_hash(arrival, head)
    return body


def _session_resource(session: CountSession) -> dict[str, Any]:
    arrival = Arrival.objects.get(pk=session.arrival_id)
    observations = session_observations(session.pk)
    body = resource_dto(
        id=session.pk,
        data=session_data(session, observations),
        revision=session.revision,
        state=session.state,
        context={"site_id": arrival.site_id, "brand_id": arrival.brand_id},
    )
    body["content_hash"] = session_hash(session, observations)
    return body


def _grn(pk: uuid.UUID) -> GoodsGrn:
    grn = goods_grn(pk)
    if grn is None:
        raise Refusal("NOT_FOUND", "That GRN was not found.")
    return grn


def _grn_payload_resource(grn: GoodsGrn) -> dict[str, Any]:
    head = DocumentHead.objects.select_related("live_version").get(document_id=grn.document_id)
    version = head.live_version
    lines = (
        [line.payload for line in OfficialLine.objects.filter(version=version).order_by("line_no")]
        if version is not None
        else []
    )
    header = dict(version.canonical_payload) if version is not None else {}
    body = resource_dto(
        id=grn.document_id,
        data={**header, "lines": lines},
        revision=head.revision,
        state="issued",
        context={
            "site_id": grn.document.site_id,
            "entity_id": grn.document.entity_id,
            "brand_id": grn.arrival.brand_id,
        },
        number=grn.document.official_number,
        version=version.version if version is not None else None,
    )
    body["content_hash"] = grn_hash(grn, head)
    return body


class GoodsArrivalListCreateView(GoodsAPIView):
    """E094 list and E113 record an arrival at the site where the goods are."""

    @extend_schema(responses=_responses(200, _page_response(ARRIVAL_SUMMARY_ITEM), _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        require_read_action(access)
        site_filter = inp.optional_legacy_id(params.get("site_id") or None, "site_id")
        if site_filter is not None and not can_read_site(access, site_filter):
            raise Refusal("NOT_FOUND", "That site was not found.")
        heads = {h.arrival_id: h for h in ArrivalHead.objects.all()}
        rows = [
            arrival
            for arrival in Arrival.objects.order_by("-recorded_at", "id")
            if can_read(access, arrival.site_id, arrival.brand_id)
            and (site_filter is None or arrival.site_id == site_filter)
            and arrival.pk in heads
        ]
        window, cursor = paginate(rows, params)
        items = [
            {
                "id": str(arrival.pk),
                "record_contract": "goods-v1",
                "kind": "arrival",
                "purpose": None,
                "number": None,
                "state": arrival_state(arrival, heads[arrival.pk]),
                "site_id": str(arrival.site_id),
                "brand_id": str(arrival.brand_id),
                "created_at": arrival.recorded_at.isoformat(),
                "updated_at": arrival.recorded_at.isoformat(),
                "owner_role": "M-STR",
                "due_at": None,
                # Beyond the shared summary shape, and deliberately: an arrivals
                # list is grouped by its transporter reference and shows which
                # vendor and invoice each row carries, none of which can be read
                # off a shape that omits them. The alternative was a detail read
                # per row on every load. The duplicate warning is E250's and is
                # not computed from this list.
                "vendor_id": str(arrival.vendor_id),
                "transporter_ref": arrival.transporter_ref,
                "invoice_number": arrival.invoice_number,
            }
            for arrival in window
        ]
        return Response(page(items, cursor))

    @extend_schema(
        request={"application/json": ARRIVAL_CREATE_REQUEST},
        responses=_responses(
            201, _dto_response(ARRIVAL_DATA, "ResourceDTO<ArrivalInput>."), _WRITE_REFUSALS
        )
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            ARRIVAL_KEYS,
            required=["site_id", "vendor_id", "brand_id", "actual_arrival_at"],
        )
        data = parse_arrival(body)
        from masters.models import Store

        if not Store.objects.filter(pk=data.site_id).exists():
            if RECEIVE_ACTION not in access.all_actions():
                raise Refusal("ACTION_DENIED", "You do not have permission to receive goods.")
            raise Refusal("NOT_FOUND", "That site was not found.")
        access.require(RECEIVE_ACTION, site_id=data.site_id, brand_id=data.brand_id)

        def handler(run: CommandRun) -> CommandResult:
            arrival = record_arrival(run, data, access=access)
            return CommandResult(
                resource_type="arrival", resource_id=str(arrival.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="inbound.arrivals.create",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=data.site_id,
            subject_key=f"site:{data.site_id}:arrival",
        )
        return Response(
            _arrival_resource(_arrival(uuid.UUID(str(result.resource_id)))),
            status=result.status_code,
        )


class GoodsArrivalDuplicateWarningView(GoodsAPIView):
    """E250: is this vendor and invoice already recorded at this legal entity?

    The screen recording an arrival has to be able to show the warning *before*
    the arrival exists, and the hash it must send back has to be minted by the
    server - a warning a client assembles for itself is neither scoped evidence
    nor something E113 can check an acknowledgement against.

    Gated on the receiving grant at the named site, not the wider receiving read
    set: it answers a question only somebody about to record an arrival there
    has. Each candidate is then filtered by the caller's own read scope, so this
    read opens no record they could not already open.
    """

    @extend_schema(
        operation_id="goods_v1_inbound_arrivals_duplicate_warning",
        responses=_responses(
            200,
            DUPLICATE_WARNING_DATA,
            _READ_REFUSALS,
        ),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, DUPLICATE_QUERY_KEYS)
        site_id = inp.legacy_id(params.get("site_id"), "site_id")
        vendor_id = inp.legacy_id(params.get("vendor_id"), "vendor_id")
        invoice_number = str(
            inp.text(params.get("invoice_number"), "invoice_number", 80, required=True)
        )
        if RECEIVE_ACTION not in access.all_actions():
            raise Refusal("ACTION_DENIED", "You do not have permission to receive goods.")
        if not access.can_reach_site(RECEIVE_ACTION, site_id):
            raise Refusal("NOT_FOUND", "That site was not found.")
        warning = duplicate_warning(
            access, site_id=site_id, vendor_id=vendor_id, invoice_number=invoice_number
        )
        candidates = [] if warning is None else warning.candidates
        # A plain answer rather than a ResourceDTO: this is the server's reading
        # of a question about an arrival that does not exist yet, so it has no
        # identity, no revision and no state of its own to report.
        return Response(
            {
                "warning_hash": warning.hash if warning is not None else None,
                "site_id": str(site_id),
                "vendor_id": str(vendor_id),
                "invoice_number": invoice_number.strip(),
                "candidates": [
                    {
                        "id": str(row.arrival.pk),
                        "site_id": str(row.arrival.site_id),
                        "state": row.state,
                        "recorded_at": row.arrival.recorded_at.isoformat(),
                        "transporter_ref": row.arrival.transporter_ref,
                    }
                    for row in candidates
                ],
            }
        )


class GoodsArrivalDetailView(GoodsAPIView):
    """E095."""

    @extend_schema(
        operation_id="goods_v1_inbound_arrivals_detail",
        responses=_responses(
            200, _dto_response(ARRIVAL_DATA, "ResourceDTO<ArrivalInput>."), _READ_REFUSALS
        ),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, DETAIL_QUERY_KEYS)
        arrival = _arrival(pk) if Arrival.objects.filter(pk=pk).exists() else None
        require_read(
            access, arrival.site_id if arrival else None, arrival.brand_id if arrival else None
        )
        if arrival is None:
            raise Refusal("NOT_FOUND", "That arrival was not found.")
        if params.get("version") and params["version"] != "1":
            raise Refusal("VERSION_NOT_FOUND", "That arrival version does not exist.", status=404)
        return Response(_arrival_resource(arrival))


class GoodsArrivalInvoiceView(GoodsAPIView):
    """E114: append an invoice claim version; claims never become GRN cost."""

    @extend_schema(
        request={"application/json": ARRIVAL_INVOICE_REQUEST},
        responses=_responses(
            200, _dto_response(CLAIM_DATA, "ResourceDTO<ClaimLines>."), _WRITE_REFUSALS
        )
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"invoice_number", "invoice_date", "evidence_id", "lines", "reason_code"},
            required=["invoice_number", "invoice_date", "lines"],
        )
        invoice_number = str(inp.text(body["invoice_number"], "invoice_number", 80, required=True))
        invoice_date = inp.day(body["invoice_date"], "invoice_date")
        evidence_id = inp.optional_uuid(body.get("evidence_id"), "evidence_id")
        lines = parse_claim_lines(body["lines"])
        reason_code = inp.text(body.get("reason_code"), "reason_code", 60)
        arrival = _arrival(pk)
        _require_at_site(access, (RECEIVE_ACTION, "pt.prepare"), arrival.site_id, arrival.brand_id)
        show_cost = three_way_match.sees_cost(access, arrival.site_id, arrival.brand_id)

        def handler(run: CommandRun) -> CommandResult:
            claim = record_invoice_claim(
                run,
                arrival,
                invoice_number=invoice_number,
                invoice_date=invoice_date,
                evidence_id=evidence_id,
                lines=lines,
                reason_code=reason_code,
                expected_revision=meta.expected_revision,
                sees_cost=show_cost,
            )
            return CommandResult(resource_type="invoice_claim", resource_id=str(claim.pk))

        result = self.run_command(
            request,
            access=access,
            action="inbound.arrivals.invoice",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=arrival.site_id,
            subject_key=f"arrival:{pk}",
        )
        claim = InvoiceClaimVersion.objects.get(pk=str(result.resource_id))
        body_out = resource_dto(
            id=claim.pk,
            data=claim_data(
                claim,
                show_cost=show_cost
                or not is_feature_on(arrival.site_id, three_way_match.FEATURE_KEY),
            ),
            revision=claim.revision,
            state="recorded",
            context={"site_id": arrival.site_id, "brand_id": arrival.brand_id},
        )
        body_out["content_hash"] = claim.content_hash
        return Response(body_out, status=result.status_code)


class GoodsArrivalThreeWayView(GoodsAPIView):
    """Ticket 37: booked, invoiced and counted per line of one arrival.

    Refused with FEATURE_OFF where the store's switch is off. Before its GRN is
    issued there are no rows yet; ``sees_cost`` tells the invoice step whether to
    ask for cost at all.
    """

    @extend_schema(
        operation_id="goods_v1_inbound_arrivals_three_way_match",
        responses=_responses(
            200, _dto_response(THREE_WAY_DATA, "ResourceDTO<ThreeWayMatch>."), _READ_REFUSALS
        ),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, frozenset())
        arrival = Arrival.objects.select_related("site").filter(pk=pk).first()
        require_read(
            access,
            arrival.site_id if arrival else None,
            arrival.brand_id if arrival else None,
        )
        if arrival is None:
            raise Refusal("NOT_FOUND", "That arrival was not found.")
        require_feature(arrival.site, three_way_match.FEATURE_KEY)
        show_cost = three_way_match.sees_cost(access, arrival.site_id, arrival.brand_id)
        grn = (
            GoodsGrn.objects.select_related("document", "arrival")
            .filter(arrival_id=arrival.pk, counter_of__isnull=True)
            .first()
        )
        rows = three_way_match.grn_rows(grn) if grn is not None else []
        exception = (
            GoodsException.objects.filter(
                tenant_id=access.tenant_id,
                kind=three_way_match.EXCEPTION_KIND,
                subject_key=f"grn:{grn.document_id}",
                state="open",
            ).first()
            if grn is not None
            else None
        )
        limits = three_way_match.tolerance()
        shown = three_way_match.for_viewer(rows, sees_cost=show_cost)
        # A store role whose lines all match on quantity is not told an exception is
        # open: that would say the cost differs.
        if not any(row["results"] for row in shown):
            exception = None
        has_booking = (
            three_way_match.booking_of(grn) is not None
            if grn is not None
            else arrival.booking_id is not None
        )
        data: dict[str, Any] = {
            "arrival_id": str(arrival.pk),
            "grn_id": str(grn.document_id) if grn is not None else None,
            "has_booking": has_booking,
            "has_invoice": latest_claim(arrival.pk) is not None,
            "sees_cost": show_cost,
            "qty_tolerance": limits.qty,
            "cost_tolerance_paise": str(limits.cost_paise),
            "exception_id": str(exception.pk) if exception is not None else None,
            "rows": shown,
        }
        if not show_cost:
            data = {k: v for k, v in data.items() if k not in three_way_match.COST_KEYS}
        head = ArrivalHead.objects.get(arrival_id=arrival.pk)
        return Response(
            resource_dto(
                id=arrival.pk,
                data=data,
                revision=head.revision,
                state="matched" if grn is not None else "awaiting_count",
                context={"site_id": arrival.site_id, "brand_id": arrival.brand_id},
            )
        )


class GoodsArrivalSessionView(GoodsAPIView):
    """E115: open a resumable count session with its counter and data-entry person."""

    @extend_schema(
        operation_id="goods_v1_inbound_arrivals_sessions_list",
        responses=_responses(
            200,
            _page_response(_dto_response(SESSION_DATA, "ResourceDTO<CountSession>.")),
            _READ_REFUSALS,
        ),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        """E249: this arrival's count sessions, newest first.

        A count that changed hands (E240) is continued, not restarted, so the
        person it was handed to has to be able to find it. Without this read the
        only door into a session is the response that created it, which the new
        owner never saw.

        Gated on the receiving grant alone rather than the wider receiving read
        set: this answers an unfinished count's rows, which is the counters'
        working paper, not the GRN a stock or PT reader is entitled to. Nobody
        who could not already receive here sees anything new.
        """
        access = self.access(request)
        params = check_query(request)
        arrival = _arrival(pk)
        _require_at_site(access, (RECEIVE_ACTION,), arrival.site_id, arrival.brand_id)
        rows = list(
            CountSession.objects.filter(arrival_id=arrival.pk).order_by("-created_at", "id")
        )
        window, cursor = paginate(rows, params)
        return Response(page([_session_resource(row) for row in window], cursor))

    @extend_schema(
        request={"application/json": COUNT_SESSION_REQUEST},
        responses=_responses(
            201, _dto_response(SESSION_DATA, "ResourceDTO<CountSession>."), _WRITE_REFUSALS
        )
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data, {"counter_id", "entry_user_id"}, required=["counter_id", "entry_user_id"]
        )
        counter_id = inp.uuid_value(body["counter_id"], "counter_id")
        entry_user_id = inp.uuid_value(body["entry_user_id"], "entry_user_id")
        arrival = _arrival(pk)
        _require_at_site(access, (RECEIVE_ACTION,), arrival.site_id, arrival.brand_id)

        def handler(run: CommandRun) -> CommandResult:
            session = open_count_session(
                run,
                arrival,
                counter_id=counter_id,
                entry_user_id=entry_user_id,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(
                resource_type="count_session", resource_id=str(session.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="inbound.arrivals.sessions",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=arrival.site_id,
            subject_key=f"arrival:{pk}",
        )
        session = CountSession.objects.get(pk=str(result.resource_id))
        return Response(_session_resource(session), status=result.status_code)


class GoodsArrivalNoBookingView(GoodsAPIView):
    """E236: C-BUY confirms the arrival has no booking; closes only that work item."""

    @extend_schema(
        request={"application/json": NO_BOOKING_REQUEST},
        responses=_responses(
            200, _dto_response(ARRIVAL_DATA, "ResourceDTO<ArrivalInput>."), _WRITE_REFUSALS
        )
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reason_code", "evidence_id"}, required=["reason_code"])
        reason_code = str(inp.text(body["reason_code"], "reason_code", 60, required=True))
        evidence_id = inp.optional_uuid(body.get("evidence_id"), "evidence_id")
        arrival = _arrival(pk)
        _require_at_site(access, (NO_BOOKING_ACTION,), arrival.site_id, arrival.brand_id)

        def handler(run: CommandRun) -> CommandResult:
            confirm_no_booking(
                run,
                arrival,
                reason_code=reason_code,
                evidence_id=evidence_id,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(resource_type="arrival", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action="inbound.arrivals.no_booking",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=arrival.site_id,
            subject_key=f"arrival:{pk}",
        )
        return Response(_arrival_resource(_arrival(pk)), status=result.status_code)


class GoodsObservationView(GoodsAPIView):
    """E116: durable, duplicate-safe scans; unidentified goods keep their description."""

    @extend_schema(
        request={"application/json": OBSERVATIONS_REQUEST},
        responses=_responses(
            200, _dto_response(SESSION_DATA, "ResourceDTO<CountSession>."), _WRITE_REFUSALS
        )
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"observations"}, required=["observations"])
        observations = parse_observations(body["observations"])
        session = CountSession.objects.select_related("arrival").filter(pk=pk).first()
        if session is None:
            raise Refusal("NOT_FOUND", "That count session was not found.")
        _require_at_site(
            access, (RECEIVE_ACTION,), session.arrival.site_id, session.arrival.brand_id
        )

        def handler(run: CommandRun) -> CommandResult:
            record_observations(run, pk, observations, expected_revision=meta.expected_revision)
            return CommandResult(resource_type="count_session", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action="inbound.count_sessions.observations",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=session.arrival.site_id,
            subject_key=f"count_session:{pk}",
        )
        return Response(
            _session_resource(CountSession.objects.get(pk=pk)), status=result.status_code
        )


class GoodsCountHandoverView(GoodsAPIView):
    """E240: the person holding an unfinished count passes it to somebody else."""

    @extend_schema(
        request={"application/json": COUNT_HANDOVER_REQUEST},
        responses=_responses(
            200, _dto_response(SESSION_DATA, "ResourceDTO<CountSession>."), _WRITE_REFUSALS
        )
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"to_human_id", "reason_code", "reviewed_hash"},
            required=["to_human_id", "reason_code", "reviewed_hash"],
        )
        to_human_id = inp.uuid_value(body["to_human_id"], "to_human_id")
        reason_code = str(inp.text(body["reason_code"], "reason_code", 60, required=True))
        reviewed_hash = str(inp.text(body["reviewed_hash"], "reviewed_hash", 64, required=True))
        session = CountSession.objects.select_related("arrival").filter(pk=pk).first()
        if session is None:
            raise Refusal("NOT_FOUND", "That count session was not found.")
        _require_at_site(
            access, (RECEIVE_ACTION,), session.arrival.site_id, session.arrival.brand_id
        )

        def handler(run: CommandRun) -> CommandResult:
            hand_over_count(
                run,
                pk,
                to_human_id=to_human_id,
                reason_code=reason_code,
                reviewed_hash=reviewed_hash,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(resource_type="count_session", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action="inbound.count_sessions.handover",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=session.arrival.site_id,
            subject_key=f"count_session:{pk}",
            reviewed_hash=reviewed_hash,
        )
        return Response(
            _session_resource(CountSession.objects.get(pk=pk)), status=result.status_code
        )


class GoodsGrnListCreateView(GoodsAPIView):
    """E096 list and E117 issue a quantity-only GRN once from a count session."""

    @extend_schema(responses=_responses(200, _page_response(WORK_ITEM), _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        require_read_action(access)
        site_filter = inp.optional_legacy_id(params.get("site_id") or None, "site_id")
        if site_filter is not None and not can_read_site(access, site_filter):
            raise Refusal("NOT_FOUND", "That site was not found.")
        rows = [
            grn
            for grn in GoodsGrn.objects.select_related("document", "arrival").order_by(
                "-created_at", "id"
            )
            if can_read(access, grn.document.site_id, grn.arrival.brand_id)
            and (site_filter is None or grn.document.site_id == site_filter)
        ]
        window, cursor = paginate(rows, params)
        items = [
            {
                "id": str(grn.document_id),
                "record_contract": "goods-v1",
                "kind": "grn",
                "purpose": "grn",
                "number": grn.document.official_number,
                "state": "issued",
                "site_id": str(grn.document.site_id),
                "brand_id": str(grn.arrival.brand_id),
                "created_at": grn.created_at.isoformat(),
                "updated_at": grn.created_at.isoformat(),
                "owner_role": "C-WHO",
                "due_at": None,
            }
            for grn in window
        ]
        return Response(page(items, cursor))

    @extend_schema(
        request={"application/json": GRN_ISSUE_REQUEST},
        responses=_responses(
            201, _dto_response(GRN_PAYLOAD_DATA, "ResourceDTO<GrnPayload>."), _WRITE_REFUSALS
        )
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"count_session_id", "reviewed_hash", "remarks"},
            required=["count_session_id", "reviewed_hash"],
        )
        session_id = inp.uuid_value(body["count_session_id"], "count_session_id")
        reviewed_hash = str(inp.text(body["reviewed_hash"], "reviewed_hash", 64, required=True))
        remarks: dict[str, str] = {}
        for index, raw in enumerate(
            inp.object_list(body.get("remarks") or [], "remarks", limit=50_000)
        ):
            field = f"remarks[{index}]"
            entry = inp.closed(
                raw, {"claim_line_key", "remark"}, field, required=["claim_line_key", "remark"]
            )
            key = str(inp.uuid_value(entry["claim_line_key"], f"{field}.claim_line_key"))
            remarks[key] = str(inp.text(entry["remark"], f"{field}.remark", 500, required=True))
        session = CountSession.objects.select_related("arrival").filter(pk=session_id).first()
        if session is None:
            raise Refusal("NOT_FOUND", "That count session was not found.")
        _require_at_site(
            access, (RECEIVE_ACTION,), session.arrival.site_id, session.arrival.brand_id
        )

        def handler(run: CommandRun) -> CommandResult:
            identity, number = issue_grn(
                run,
                session_id,
                reviewed_hash=reviewed_hash,
                remarks=remarks,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(
                resource_type="grn",
                resource_id=str(identity.pk),
                status_code=201,
                document_number=number,
                version=1,
            )

        result = self.run_command(
            request,
            access=access,
            action="inbound.grns.issue",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(session_id)],
            site_id=session.arrival.site_id,
            subject_key=f"count_session:{session_id}",
            reviewed_hash=reviewed_hash,
        )
        grn = _grn(uuid.UUID(str(result.resource_id)))
        return Response(_grn_payload_resource(grn), status=result.status_code)


class GoodsGrnDetailView(GoodsAPIView):
    """E097: counted, covered, held and disposed quantity per line."""

    @extend_schema(
        operation_id="goods_v1_inbound_grns_detail",
        responses=_responses(
            200, _dto_response(GRN_COVERAGE_DATA, "ResourceDTO<GrnCoverageDTO>."), _READ_REFUSALS
        ),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, DETAIL_QUERY_KEYS)
        grn = goods_grn(pk)
        require_read(
            access,
            grn.document.site_id if grn else None,
            grn.arrival.brand_id if grn else None,
        )
        if grn is None:
            raise Refusal("NOT_FOUND", "That GRN was not found.")
        head = DocumentHead.objects.select_related("live_version").get(document_id=grn.document_id)
        if params.get("version") and (
            head.live_version is None or str(head.live_version.version) != params["version"]
        ):
            raise Refusal("VERSION_NOT_FOUND", "That GRN version does not exist.", status=404)
        header, items = grn_coverage(grn)
        show_value = "cost" in access.field_grants(
            site_id=grn.document.site_id,
            brand_id=grn.arrival.brand_id,
            actions=set(READ_ACTIONS),
        )
        window, cursor = paginate(
            items,
            {"cursor": params.get("line_cursor", ""), "limit": "500"},
            default=500,
            maximum=500,
        )
        body = resource_dto(
            id=grn.document_id,
            data={
                "grn_header": header,
                "lines": {"items": window, "next_cursor": cursor, "total": len(items)},
                # The three things the discrepancy panel is: what the invoice
                # claimed against what was counted, how the count has been
                # corrected since, and the invoice itself.
                "invoice": grn_invoice(
                    grn,
                    show_cost=show_value
                    or not is_feature_on(grn.arrival.site_id, three_way_match.FEATURE_KEY),
                ),
                "invoice_comparison": grn_comparison(grn),
                "count_history": grn_count_history(grn),
                "dispositions": grn_disposition_history(grn, show_value=show_value),
                "damage_reports": grn_damage_reports(grn),
                "custody_transfers": pre_pt_custody.transfers_of_grn(grn),
            },
            revision=head.revision,
            state="issued",
            context={
                "site_id": grn.document.site_id,
                "entity_id": grn.document.entity_id,
                "brand_id": grn.arrival.brand_id,
            },
            number=grn.document.official_number,
            version=head.live_version.version if head.live_version is not None else None,
        )
        body["content_hash"] = grn_hash(grn, head)
        return Response(body)


class GoodsCounterGrnView(GoodsAPIView):
    """E118: draft a counter-GRN and ask a distinct C-INV to approve it."""

    @extend_schema(
        request={"application/json": COUNTER_GRN_REQUEST},
        responses=_responses(
            201, _dto_response(COUNTER_DATA, "ResourceDTO<CounterGrnDraft>."), _WRITE_REFUSALS
        )
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data, {"corrections", "evidence_ids"}, required=["corrections"]
        )
        corrections = parse_counter_corrections(body["corrections"])
        evidence_ids = inp.id_list(body.get("evidence_ids"), "evidence_ids")
        grn = _grn(pk)
        _require_at_site(access, (RECEIVE_ACTION,), grn.document.held_site_id, grn.arrival.brand_id)

        def handler(run: CommandRun) -> CommandResult:
            identity = request_counter_grn(
                run,
                grn,
                corrections=corrections,
                evidence_ids=evidence_ids,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(
                resource_type="counter_grn", resource_id=str(identity.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="inbound.grns.counter",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=grn.document.site_id,
            subject_key=f"grn:{pk}",
        )
        draft = CounterGrnDraft.objects.select_related("document").get(
            document_id=uuid.UUID(str(result.resource_id))
        )
        head = DocumentHead.objects.select_related("draft_revision", "live_version").get(
            document_id=draft.document_id
        )
        assert head.draft_revision is not None
        lines = [s.payload for s in revision_lines(draft.document_id, head.draft_revision.revision)]
        state = "official" if head.live_version_id else "pending_approval"
        response = resource_dto(
            id=draft.document_id,
            data={**head.draft_revision.payload, "lines": lines},
            revision=head.revision,
            state=state,
            context={"site_id": grn.document.site_id, "brand_id": grn.arrival.brand_id},
            number=draft.document.official_number,
        )
        response["content_hash"] = head.draft_revision.content_hash
        return Response(response, status=result.status_code)


DISPOSITION_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "E119 DispositionPayload + MutationMeta. Revision-bound to the GRN. "
        "`resolved_sku_id` is required for accept_excess, value_damage, "
        "resolve_identity and accept_wrong; `approved_cost_evidence_id` and "
        "`approved_tax_evidence_id` for accept_excess, value_damage and accept_wrong; "
        "value_damage also needs positive `approved_cost_paise`/`approved_mrp_paise`, "
        "a `damage_description` and damage `evidence_ids`."
    ),
    "required": [
        "command_id",
        "contract_version",
        "expected_revision",
        "kind",
        "source_document_id",
        "source_line_key",
        "qty",
        "reason_code",
        "reviewed_grn_hash",
    ],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer"},
        "kind": {"type": "string", "enum": list(DISPOSITION_KINDS)},
        "source_document_id": {"type": "string", "format": "uuid"},
        "source_line_key": {"type": "string", "format": "uuid"},
        "lot_id": {"type": "string", "format": "uuid", "nullable": True},
        "qty": {"type": "integer", "minimum": 1},
        "reason_code": {"type": "string", "minLength": 1, "maxLength": 60},
        "evidence_ids": {
            "type": "array",
            "description": "Damage evidence, kept separate from value and tax evidence.",
            "items": {"type": "string", "format": "uuid"},
        },
        "reviewed_grn_hash": {"type": "string", "minLength": 64, "maxLength": 64},
        "reviewed_pt_hash": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
            "nullable": True,
        },
        "followup_owner_id": {"type": "string", "format": "uuid", "nullable": True},
        "resolved_sku_id": {"type": "string", "format": "uuid", "nullable": True},
        "approved_cost_evidence_id": {"type": "string", "format": "uuid", "nullable": True},
        "approved_tax_evidence_id": {"type": "string", "format": "uuid", "nullable": True},
        "approved_cost_paise": {"type": "string", "nullable": True},
        "approved_mrp_paise": {"type": "string", "nullable": True},
        "damage_description": {"type": "string", "maxLength": 1000, "nullable": True},
    },
    "additionalProperties": False,
}


class GoodsDispositionView(GoodsAPIView):
    """E119: decide discrepant quantity, or request the distinct decision policy requires."""

    @extend_schema(
        operation_id="goods_v1_inbound_grns_dispositions_create",
        description=(
            "E119: decide what happens to discrepant goods on a GRN, or ask for the "
            "decision a different person approves. Needs `receipt.disposition.decide` at "
            "the GRN's site; `accept_wrong` may also be asked for with `movement.draft` "
            "(Warehouse, Store person, GSA-R01), as a named person. Kinds: "
            "accept_shortage, hold_excess, accept_excess, value_damage, return, dispose, "
            "resolve_identity, accept_wrong (`hold_damage` is refused: damage is reported "
            "on E254). `accept_wrong` (ticket 05D, overall PRD §15.2.1 rule 2) accepts "
            "wrong or unidentified pieces already named as `resolved_sku_id` (by the count "
            "or `resolve_identity`), with approved cost and tax evidence; it always goes to "
            "a different person (the approval policy's role - the Owner) and on approval "
            "the pieces become good goods an ordinary primary or supplement PT may cover. "
            "The GRN line, counted identity and earlier decisions are kept; no hold is "
            "released. Success: 200 with state `recorded` (decided now) or "
            "`approval_pending` (a request). Refusals: INVALID_REQUEST, NOT_FOUND, "
            "ACTION_DENIED, COMMAND_CONFLICT, CONTRACT_DISABLED, REVISION_SUPERSEDED, "
            "UNDER_COUNT, SERIES_NOT_READY, POSTING_EVENT_CONFLICT, APPROVAL_POLICY_BLOCKED, "
            "DISPOSITION_STALE (the GRN changed since it was reviewed, or too few pieces "
            "are left: `WRONG_GOODS_NOT_AVAILABLE`, `EXCESS_NOT_AVAILABLE` name how many), "
            "DISPOSITION_NOT_AUTHORISED (missing identity or evidence - IDENTITY_REQUIRED, "
            "COST_EVIDENCE_REQUIRED, TAX_BASIS_REQUIRED; for accept_wrong also "
            "IDENTITY_UNRESOLVED, SKU_MISMATCH, IDENTITY_NOT_SETTLED, "
            "DEFINING_ATTRIBUTE_UNKNOWN and DAMAGE_STAYS_HELD)."
        ),
        request={"application/json": DISPOSITION_REQUEST},
        responses=_responses(
            200,
            _dto_response(DISPOSITION_DATA, "ResourceDTO<DispositionPayload>."),
            _WRITE_REFUSALS,
        ),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            DISPOSITION_KEYS,
            required=[
                "kind",
                "source_document_id",
                "source_line_key",
                "qty",
                "reason_code",
                "reviewed_grn_hash",
            ],
        )
        payload = parse_disposition(body)
        grn = _grn(pk)
        _require_at_site(
            access,
            disposition_request_actions(payload["kind"]),
            grn.document.held_site_id,
            grn.arrival.brand_id,
        )

        def handler(run: CommandRun) -> CommandResult:
            kind, resource_id = record_disposition(
                run, grn, payload, expected_revision=meta.expected_revision
            )
            return CommandResult(resource_type=kind, resource_id=str(resource_id))

        result = self.run_command(
            request,
            access=access,
            action="inbound.grns.dispositions",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=grn.document.site_id,
            subject_key=f"grn:{pk}",
            reviewed_hash=payload["reviewed_grn_hash"],
        )
        if result.resource_type == "disposition_request":
            draft = ActionDraft.objects.get(pk=str(result.resource_id))
            data = dict(draft.payload["payload"])
            state = disposition_request_state(draft)
        else:
            row = Disposition.objects.get(pk=str(result.resource_id))
            data = dict(row.decision)
            state = "recorded"
        response = resource_dto(
            id=result.resource_id,
            data=data,
            revision=1,
            state=state,
            context={"site_id": grn.document.site_id, "brand_id": grn.arrival.brand_id},
        )
        return Response(response, status=result.status_code)


DAMAGE_REPORT_REQUEST: dict[str, Any] = {
    "type": "object",
    "description": (
        "E254 (ticket 05C): report damage found on a GRN's goods before their PT. "
        "Revision-bound to the GRN like a disposition."
    ),
    "required": [
        "command_id",
        "contract_version",
        "expected_revision",
        "source_line_key",
        "qty",
        "reason_code",
        "reviewed_grn_hash",
    ],
    "properties": {
        "command_id": {"type": "string", "format": "uuid"},
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer"},
        "source_line_key": {"type": "string", "format": "uuid"},
        "lot_id": {"type": "string", "format": "uuid", "nullable": True},
        "qty": {"type": "integer", "minimum": 1},
        "reason_code": {"type": "string", "minLength": 1, "maxLength": 60},
        "evidence_ids": {
            "type": "array",
            "description": "Damage evidence (a photo or note). Optional.",
            "items": {"type": "string", "format": "uuid"},
        },
        "reviewed_grn_hash": {"type": "string", "minLength": 64, "maxLength": 64},
    },
    "additionalProperties": False,
}


class GoodsGrnDamageReportView(GoodsAPIView):
    """E254: damage found on a GRN's goods before their PT, quarantined and reported at once."""

    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="goods_v1_inbound_grns_damage_reports_create",
        description=(
            "E254 (ticket 05C): report damage found on a GRN's goods before their PT. "
            "Needs `movement.draft` (Warehouse, Store person) or "
            "`receipt.disposition.decide` at the GRN's site, as a named person. In one "
            "command the chosen pieces move to the site's quarantine with condition "
            "`damaged` under the receipt's damage hold and a pending damage report "
            "(E252, source `receiving`) opens for a different authorised person to "
            "confirm or reject (E253). The count is not changed, and no SKU, cost, tax, "
            "layer or value is created. Reportable pieces are physically here, on no "
            "live PT and carry no covering decision: good and under no hold, or damaged "
            "and under no damage hold yet; wrong and unidentified goods keep their own "
            "reason. A count freeze does not stop it (GSA-R02). Pieces counted damaged "
            "are held and reported when the GRN is issued, without this route. "
            "Refusals: INVALID_REQUEST, NOT_FOUND, ACTION_DENIED (no grant, or not a "
            "named person), COMMAND_CONFLICT, CONTRACT_DISABLED, SITE_NOT_READY, "
            "REVISION_SUPERSEDED, DISPOSITION_STALE (the GRN changed since it was "
            "reviewed, or fewer reportable pieces than asked - `NOT_ENOUGH_TO_REPORT` "
            "names how many), POSTING_EVENT_CONFLICT."
        ),
        request={"application/json": DAMAGE_REPORT_REQUEST},
        responses=_responses(201, DAMAGE_REPORT, _WRITE_REFUSALS),
    )
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            DAMAGE_REPORT_KEYS,
            required=["source_line_key", "qty", "reason_code", "reviewed_grn_hash"],
        )
        payload = parse_damage_report(body)
        grn = _grn(pk)
        _require_at_site(
            access, DAMAGE_REPORT_ACTIONS, grn.document.held_site_id, grn.arrival.brand_id
        )

        def handler(run: CommandRun) -> CommandResult:
            report = report_receipt_damage(
                run, grn, payload, expected_revision=meta.expected_revision
            )
            return CommandResult(
                resource_type="damage_report", resource_id=str(report.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="inbound.grns.damage_reports",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=grn.document.site_id,
            subject_key=f"grn:{pk}",
            reviewed_hash=payload["reviewed_grn_hash"],
        )
        report = DamageReport.objects.get(pk=str(result.resource_id))
        return Response(
            damage_review.report_dto(report, damage_review.reporter_names([report])),
            status=result.status_code,
        )


def _work_items(
    request: Request, access: AccessContext
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    params = check_query(request, QUEUE_QUERY_KEYS)
    require_read_action(access)
    basis = params.get("basis") or "quantity"
    if basis not in ("quantity", "cost", "ticket"):
        raise inp.bad("basis must be quantity, cost or ticket.", "basis")
    if basis in ("cost", "ticket") and not any(
        {"cost", "layer_value"} & set(grant.fields) for grant in access.grants
    ):
        raise Refusal("FIELD_DENIED", "You are not entitled to see stock value.", status=403)
    if params.get("condition") and params["condition"] not in CONDITIONS:
        raise inp.bad("condition is not a condition.", "condition")
    if params.get("as_of"):
        inp.timestamp(params["as_of"], "as_of")
    for name in ("sku_id", "origin_id"):
        if params.get(name):
            inp.uuid_value(params[name], name)
    site_filter = inp.optional_legacy_id(params.get("site_id") or None, "site_id")
    brand_filter = inp.optional_legacy_id(params.get("brand_id") or None, "brand_id")
    items = _scoped_work(
        access,
        site_filter=site_filter,
        brand_filter=brand_filter,
        state=params.get("state") or None,
    )
    return items, params


def _scoped_work(
    access: AccessContext,
    *,
    site_filter: int | None,
    brand_filter: int | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """The receiving queue, filtered - the one scope rule E167, E168 and the inbox share.

    Pulled out of :func:`_work_items` when the inbox (OPS-04) needed the same
    answer under different query names. One rule, three readers: a site the
    caller cannot reach is "not found", and each row still had to pass
    ``can_read`` inside ``inbound_work`` before it got here.
    """
    if site_filter is not None and not can_read_site(access, site_filter):
        raise Refusal("NOT_FOUND", "That site was not found.")
    return [
        item
        for item in inbound_work(access)
        if (site_filter is None or item["site_id"] == str(site_filter))
        and (brand_filter is None or item["brand_id"] == str(brand_filter))
        and (not state or item["state"] == state)
    ]


class GoodsPendingView(GoodsAPIView):
    """E167: inbound work waiting for someone, in the caller's scope."""

    @extend_schema(responses=_responses(200, _page_response(WORK_ITEM), _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        items, params = _work_items(request, access)
        window, cursor = paginate(items, params)
        access.revalidate_delivery()
        return Response(page(window, cursor))


class GoodsInboxView(GoodsAPIView):
    """The receiving inbox: what is arriving at this site and what it waits on.

    Store and warehouse operations PRD §5.1. One list, two tabs: *Pending* is
    everything still to be done, *History* is what has been put away or closed.
    It lists vendor deliveries and incoming transfer dispatches together, and it
    is presentation over the existing records - arrival, count, GRN, discrepancy
    decisions, PT versions, approvals, print jobs and acceptance scans each keep
    their own document, approval and evidence exactly as before.
    """

    http_method_names = ["get", "head", "options"]

    @extend_schema(responses=_responses(200, _page_response(INBOX_ITEM), _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, INBOX_QUERY_KEYS)
        require_read_action(access)
        view = params.get("view") or "pending"
        if view not in ("pending", "history"):
            raise inp.bad("view must be pending or history.", "view")
        site_filter = inp.optional_legacy_id(params.get("site") or None, "site")
        records = {
            field: str(inp.optional_uuid(params.get(key) or None, key))
            for key, field in INBOX_RECORD_FILTERS.items()
            if params.get(key)
        }
        work = _scoped_work(access, site_filter=site_filter)
        items = inbox_items(access, site_id=site_filter, work=work)
        if records:
            items = [
                item
                for item in items
                if all(_matches(item, field, value) for field, value in records.items())
            ]
        if view == "history":
            # What is finished, newest first: the last thing done is the thing
            # somebody is most likely looking for.
            items = [item for item in items if item["next_step"] == "done"]
            items.reverse()
        else:
            items = [item for item in items if item["next_step"] != "done"]
        window, cursor = paginate(items, params)
        access.revalidate_delivery()
        return Response(page(window, cursor))


def _matches(item: dict[str, Any], field: str, value: str) -> bool:
    """A PT address finds its delivery whichever of the delivery's PTs it names:
    the primary, or any supplement (ticket 07B)."""
    if field == "pt_id":
        return item.get("pt_id") == value or value in (item.get("pt_ids") or [])
    return bool(item.get(field) == value)


class GoodsQueueView(GoodsAPIView):
    """E168: the same work with per-state counts over the identical scope."""

    @extend_schema(responses=_responses(200, QUEUE_RESPONSE, _READ_REFUSALS))
    def get(self, request: Request) -> Response:
        access = self.access(request)
        items, params = _work_items(request, access)
        counts: dict[str, int] = {}
        for item in items:
            counts[item["state"]] = counts.get(item["state"], 0) + 1
        window, cursor = paginate(items, params)
        body = page(window, cursor)
        return Response(
            {
                "counts": [{"state": state, "count": counts[state]} for state in sorted(counts)],
                "items": body["items"],
                "next_cursor": body["next_cursor"],
                "as_of": body["as_of"],
            }
        )
