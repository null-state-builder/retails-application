"""Goods-v1 return to vendor: approval, vendor pickups and balance withdrawal (goods ticket 15B).

Quarantine outcomes PRD §§4-5, transfers PRD §§3-4 and §7, goods PRD §14.10
GSA-R01 (the Owner approves, a different person from the preparer) and GSA-R04
(staff record that the vendor agreed to take the goods back, with the
agreement's reference; the system does not evaluate return terms).

An RTV rides the movement routes (E151 draft, E155 submit, the shared approvals
decision, E104/E105 reads) and adds two physical routes of its own. Read it as
four separate facts:

* **Approval reserves; it moves nothing.** The draft freezes exact recorded
  source portions - quarantined pieces under a hold, or accepted good stock
  nobody holds - and the Owner's approval reserves exactly those (P15, a
  reservation only). A reserved piece is not sellable, not transferable and
  not adjustable, and a competing transfer reservation over it refuses the
  approval. The goods stay where they are, in their condition, under every
  hold they had.
* **A pickup is one confirmed handover.** The person who handed the goods over
  records who collected them, when, how many per line and the evidence. Only
  those pieces leave (P15: reservation consumed, quantity to the ``returned``
  boundary, value stock -> external at each portion's own recorded cost, and
  the holds over exactly those pieces end, because the return is their exit
  route). A pending damage report whose every piece has now gone back is
  closed as returned to vendor (Anand's 15B decision 3). Several pickups may
  fulfil one RTV; cumulative departure never exceeds the approval.
* **Reasons are not withdrawal.** A partial pickup records reasons against
  every piece left behind (vendor rejected, will collect later, not ready,
  withdrawn by us, other with a remark). Those pieces stay reserved to the RTV
  until another pickup takes them or somebody explicitly withdraws them.
* **Withdrawal releases only the outstanding reservation** (P16). The pieces
  stay where they are, quarantined goods stay held and unavailable, and every
  completed pickup keeps its evidence. Withdrawing the whole balance before
  anything left cancels the RTV; after a pickup it closes the RTV as partially
  returned.

No RTV command writes a GL, vendor, cash, payable, credit-note or tax entry.
Credit or replacement is separate (a replacement is a linked inbound receipt).
Pre-PT custody returns through its GRN (ticket 15G), and the persistent
acknowledgement discrepancy is closed only by the Owner's approved closure
(ticket 15H, ``goods_rtv_shipments.propose_closure``). A shipment for delivery to
the vendor (ticket 15F, ``outbound.goods_rtv_shipments``) leaves through the
same departure as a pickup (``depart``), but is accounted for only by the
vendor's acknowledgement or by goods actually coming back; ``settle`` keeps
the RTV initiated until it is.

Lock order: SITE (the site guard) -> DOCUMENT (the movement head, then - for a
pickup - the pending damage reports over its lots) -> LOT -> SERIES.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from alerts.goods_services import open_exception, resolve_exceptions
from approvals.goods_policy import Amounts, pin
from approvals.goods_services import DecisionContext, create_request, supersede_pending
from core.commands import CommandRun
from core.goods_documents import (
    append_revision,
    lock_heads,
    new_document,
    officialise,
    record_event,
    revision_lines,
    set_state,
)
from core.goods_fields import bounds, portion
from core.kernel_models import DocumentHead, DocumentIdentity, OfficialVersion
from core.numbering import allocate
from core.operational import ValuePair
from core.refusals import Refusal, issue
from masters.goods_models import Location, SiteGuard
from outbound import damage_review
from outbound import goods_movements as movements
from outbound.goods_models import DamageReport, GoodsMovement, RtvEvent
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import ActiveHold, ActiveReservation, Origin

KIND = movements.RTV_KIND
DOC_TYPE = movements.RTV_DOC_TYPE
#: Approval (reservation) and pickup (departure) are P15; withdrawal is P16,
#: which releases a reservation and carries no value (design §7).
RTV_POSTING = "P15"
WITHDRAW_POSTING = "P16"
RETURNED_BOUNDARY = "returned"

DRAFT_ACTION = movements.DRAFT_ACTION
APPROVE_ACTION = movements.APPROVE_ACTION
#: Store person and Warehouse record the physical steps at their own site (GSA-R01).
EXECUTE_ACTION = "rtv.execute"
#: Whoever prepares an RTV, or the authority that approves one, may withdraw its
#: outstanding balance: it is a decision about the movement, not a physical step.
WITHDRAW_ACTIONS = (DRAFT_ACTION, APPROVE_ACTION)

APPROVAL_EXCEPTION = "approval_pending"
#: Owned work while an approved balance waits to leave. Its due date is a
#: follow-up, never a pickup deadline (goods PRD §14.9.2).
PENDING_EXCEPTION = "rtv_not_dispatched"
RESOLUTION_ACTIONS = movements.RESOLUTION_ACTIONS

#: Where a line's pieces are taken from, read off its source location.
POOL_QUARANTINE = "quarantine"
POOL_STOCK = "stock"

INITIATED = "initiated"
COMPLETED = "completed"
CLOSED_PARTIALLY_RETURNED = "closed_partially_returned"
CANCELLED = "cancelled"

#: Quarantine outcomes §5: why pieces were left behind, and whether another
#: pickup is expected under this RTV. ``other`` says so itself, with a remark.
VENDOR_REJECTED = "vendor_rejected"
COLLECT_LATER = "collect_later"
NOT_READY = "not_ready"
WITHDRAWN_BY_US = "withdrawn_by_us"
OTHER = "other"
FURTHER_PICKUP: dict[str, bool] = {
    VENDOR_REJECTED: False,
    COLLECT_LATER: True,
    NOT_READY: True,
    WITHDRAWN_BY_US: False,
}
LEFT_BEHIND_REASONS = (VENDOR_REJECTED, COLLECT_LATER, NOT_READY, WITHDRAWN_BY_US, OTHER)
WITHDRAWAL_REASONS = (VENDOR_REJECTED, WITHDRAWN_BY_US, OTHER)

MAX_NOTE = 1000
MAX_REMARK = 500
MAX_REFERENCE = 100
MAX_COLLECTOR = 120
MAX_EVIDENCE = 20

#: Line fields other movement kinds own. An RTV takes the pieces where they stand.
NOT_FOR_RTV = (
    "destination_location_id",
    "hold_keys",
    "cost_evidence_origin_id",
    "transfer_exception_id",
    "description",
)


def _invalid(message: str, field: str) -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field)])


def _refuse(message: str, code: str, **extra: Any) -> Refusal:
    return Refusal("MOVEMENT_INVALID", message, status=422, issues=[issue(code, message, **extra)])


def _state_conflict(message: str) -> Refusal:
    return Refusal("RTV_STATE_CONFLICT", message, status=409)


# ---------------------------------------------------------------------------
# Input: MovementPayload with kind "rtv"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Payload:
    site_id: int
    reason_code: str
    vendor_id: int
    agreement_reference: str
    evidence_ids: tuple[uuid.UUID, ...]
    evidence_note: str
    lines: tuple[movements.Line, ...]

    def as_header(self) -> dict[str, Any]:
        return {
            "kind": KIND,
            "site_id": str(self.site_id),
            "reason_code": self.reason_code,
            "vendor_id": str(self.vendor_id),
            "agreement_reference": self.agreement_reference,
            "evidence_ids": [str(e) for e in self.evidence_ids],
            "evidence_note": self.evidence_note or None,
            "source_document_id": None,
            "count_id": None,
        }

    def as_movement(self) -> movements.Payload:
        return movements.Payload(
            kind=KIND,
            site_id=self.site_id,
            reason_code=self.reason_code,
            evidence_ids=self.evidence_ids,
            source_document_id=None,
            lines=self.lines,
        )

    @property
    def quantity(self) -> int:
        return sum(line.qty for line in self.lines)


def parse(body: dict[str, Any]) -> Payload:
    """The closed MovementPayload for an RTV, refused before any scope lookup."""
    unknown = sorted(set(body) - movements.PAYLOAD_FIELDS)
    if unknown:
        raise _invalid(f"Unknown field(s): {', '.join(unknown)}.", unknown[0])
    if str(body.get("kind") or "") != KIND:
        raise _invalid("kind must be rtv.", "kind")
    if body.get("count_id") is not None:
        raise _refuse("A count_id belongs to a count's own delta, not to an RTV.", "COUNT_OWNED")
    if body.get("source_document_id") not in (None, ""):
        raise _refuse(
            "An RTV names its vendor and agreement, not a source document.",
            "NOT_FOR_RTV",
            field="source_document_id",
        )
    reason = body.get("reason_code")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 60 or len(reason) > 60:
        raise _invalid("reason_code is 1 to 60 characters.", "reason_code")
    agreement = _agreement(body.get("agreement_reference"))
    evidence = _evidence_ids(body.get("evidence_ids"))
    note = body.get("evidence_note")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        raise _invalid(f"evidence_note is text of at most {MAX_NOTE} characters.", "evidence_note")
    raw_lines = body.get("lines")
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= movements.MAX_LINES:
        raise _invalid(f"An RTV has 1 to {movements.MAX_LINES} lines.", "lines")
    lines = tuple(_line(item) for item in raw_lines)
    keys = [line.line_key for line in lines]
    if len(set(keys)) != len(keys):
        raise _invalid("Every line_key on an RTV is distinct.", "lines")
    return Payload(
        site_id=movements._int_id(body.get("site_id"), "site_id"),
        reason_code=reason,
        vendor_id=movements._int_id(body.get("vendor_id"), "vendor_id"),
        agreement_reference=agreement,
        evidence_ids=evidence,
        evidence_note=(note or "").strip(),
        lines=lines,
    )


def _agreement(raw: Any) -> str:
    """GSA-R04: the vendor's agreement is recorded with its reference, never judged."""
    if raw is not None and not isinstance(raw, str):
        raise _invalid("agreement_reference is text.", "agreement_reference")
    agreement = (raw or "").strip()
    if not agreement:
        raise _refuse(
            "Record the vendor's agreement to take these goods back, with its reference "
            "(a letter, an email or a return authorisation number).",
            "AGREEMENT_REQUIRED",
            field="agreement_reference",
        )
    if len(agreement) > MAX_REFERENCE:
        raise _invalid(
            f"agreement_reference is at most {MAX_REFERENCE} characters.", "agreement_reference"
        )
    return agreement


def _evidence_ids(raw: Any) -> tuple[uuid.UUID, ...]:
    raw = raw or []
    if not isinstance(raw, list) or len(raw) > MAX_EVIDENCE:
        raise _invalid(f"evidence_ids is a list of at most {MAX_EVIDENCE} IDs.", "evidence_ids")
    return tuple(movements._uuid(e, "evidence_ids") for e in raw)


def _line(item: Any) -> movements.Line:
    if not isinstance(item, dict):
        raise _invalid("Every RTV line is an object.", "lines")
    for field in NOT_FOR_RTV:
        if item.get(field) not in (None, [], ""):
            raise _refuse(
                f"{field} is not part of an RTV: the pieces are reserved where they stand "
                "and leave only when the vendor collects them.",
                "NOT_FOR_RTV",
                field=field,
            )
    trimmed = {k: v for k, v in item.items() if k not in NOT_FOR_RTV}
    return movements._parse_line(trimmed, KIND)


# ---------------------------------------------------------------------------
# Source portions: exact, recorded, and in their pool
# ---------------------------------------------------------------------------


def _pool_of(run: CommandRun, line: movements.Line, site_id: int) -> str:
    """Quarantined pieces come from the site's quarantine; good stock from a storage location."""
    location = Location.objects.filter(tenant_id=run.tenant_id, pk=line.source_location_id).first()
    if location is None:
        raise Refusal("NOT_FOUND", "That location was not found.")
    if location.site_id != site_id:
        raise _refuse(
            "That location is at another site. An RTV returns goods from its own site.",
            "WRONG_SITE",
            line_key=str(line.line_key),
        )
    if location.system and location.kind == "quarantine":
        return POOL_QUARANTINE
    if not location.system and location.kind in engine.TRANSFERABLE_KINDS:
        return POOL_STOCK
    raise _refuse(
        f"Goods cannot be returned from the site's {location.kind} location. Return quarantined "
        "goods from quarantine, or accepted good stock from where it is stored.",
        "LOCATION_NOT_ELIGIBLE",
        line_key=str(line.line_key),
    )


def _recorded(address: engine.Address) -> bool:
    """A PT registered it: a resolved SKU with an origin or value basis."""
    return address.sku_id is not None and (
        address.origin_id is not None or address.value_basis_origin_id is not None
    )


def _basis(address: engine.Address) -> str | None:
    found = address.origin_id or address.value_basis_origin_id
    return str(found) if found else None


def _free_slices(line: movements.Line, site_id: int, pool: str) -> list[movements.Slice]:
    """The oldest eligible pieces at the line's address, FIFO, never a reserved one.

    Quarantine: recorded pieces under at least one hold - only the held part.
    Stock: recorded, accepted, good pieces nobody holds. A reserved piece belongs
    to another movement in both pools; pre-PT custody has no recorded value and
    returns through its GRN (ticket 15G).
    """
    standing = movements._standing(line, site_id)
    holds, reservations = engine.active_encumbrances([p.lot_id for p in standing])
    # Goods ticket 15C: a written-off piece's value already left stock; a pickup
    # would take it out again. It waits for its disposal route (15D).
    gone = engine.written_off([p.lot_id for p in standing])
    free: list[movements.Slice] = []
    counts = {"RESERVED": 0, "HOLD_ACTIVE": 0, "NOT_HELD": 0, "UNVALUED_CUSTODY": 0}
    counts |= {"NOT_ACCEPTED": 0, "CONDITION_NOT_GOOD": 0, "WRITTEN_OFF": 0}
    for item in standing:
        size = ranges.length(item.interval)
        if not _recorded(item.address):
            counts["UNVALUED_CUSTODY"] += size
            continue
        lot_holds = holds.get(item.lot_id, [])
        lot_reserved = reservations.get(item.lot_id, [])
        if pool == POOL_QUARANTINE:
            held = ranges.intersect([item.interval], lot_holds)
            counts["NOT_HELD"] += size - ranges.total(held)
            lot_gone = gone.get(item.lot_id, [])
            counts["WRITTEN_OFF"] += ranges.total(ranges.intersect(held, lot_gone))
            held = ranges.subtract(held, lot_gone)
            counts["RESERVED"] += ranges.total(ranges.intersect(held, lot_reserved))
            pieces = ranges.subtract(held, lot_reserved)
        else:
            if item.address.accepted_event_id is None:
                counts["NOT_ACCEPTED"] += size
                continue
            if item.address.condition != "good":
                counts["CONDITION_NOT_GOOD"] += size
                continue
            blocked = ranges.intersect([item.interval], lot_reserved)
            counts["RESERVED"] += ranges.total(blocked)
            counts["HOLD_ACTIVE"] += ranges.total(
                ranges.subtract(ranges.intersect([item.interval], lot_holds), blocked)
            )
            pieces = ranges.subtract([item.interval], [*lot_holds, *lot_reserved])
        free.extend(movements.Slice(item.lot_id, piece, item.address) for piece in pieces)
    available = ranges.total([s.interval for s in free])
    if available < line.qty:
        raise Refusal(
            "MOVEMENT_INVALID",
            f"Only {available} of {line.qty} piece(s) there can be returned: an RTV takes "
            + (
                "recorded pieces held in quarantine that nobody has reserved."
                if pool == POOL_QUARANTINE
                else "accepted good stock that nobody holds or has reserved."
            ),
            status=422,
            issues=[
                issue(
                    "INSUFFICIENT_ELIGIBLE_STOCK",
                    f"only {available} eligible piece(s) stand there",
                    line_key=str(line.line_key),
                    quantity=line.qty,
                ),
                *(
                    issue(code, code.replace("_", " ").lower(), quantity=qty)
                    for code, qty in counts.items()
                    if qty
                ),
            ],
        )
    out: list[movements.Slice] = []
    remaining = line.qty
    for chosen in free:
        if remaining <= 0:
            break
        size = min(remaining, ranges.length(chosen.interval))
        lower = chosen.interval[0]
        out.append(replace(chosen, interval=(lower, lower + size)))
        remaining -= size
    return out


def _still_eligible(line: movements.Line, slices: Sequence[movements.Slice], pool: str) -> None:
    """A frozen portion that has left its pool since the draft is refused, nothing written."""
    for piece in slices:
        span = portion(*piece.interval)
        key = str(line.line_key)
        if not _recorded(piece.address):
            raise _refuse(
                "Some of these pieces have no recorded value. Pre-PT custody returns through "
                "its GRN.",
                "UNVALUED_CUSTODY",
                line_key=key,
            )
        if ActiveReservation.objects.filter(lot_id=piece.lot_id, portion__overlap=span).exists():
            raise _refuse(
                "Another movement has reserved some of these pieces since the RTV was "
                "prepared. A reserved piece is never taken twice.",
                "RESERVED",
                line_key=key,
            )
        if ranges.intersect(
            [piece.interval], engine.written_off([piece.lot_id]).get(piece.lot_id, [])
        ):
            raise _refuse(
                "Some of these pieces were written off since the RTV was prepared. Their value "
                "already left stock, so they wait for their disposal route.",
                "WRITTEN_OFF",
                line_key=key,
            )
        holds = [
            bounds(h.portion)
            for h in ActiveHold.objects.filter(lot_id=piece.lot_id, portion__overlap=span)
        ]
        if pool == POOL_QUARANTINE:
            if not ranges.contains(ranges.normalise(holds), [piece.interval]):
                raise _refuse(
                    "Some of these pieces are no longer held in quarantine. Return them as "
                    "good stock instead.",
                    "NOT_HELD",
                    line_key=key,
                )
            continue
        if holds:
            raise _refuse(
                "A hold covers some of these pieces since the RTV was prepared. Return held "
                "goods from quarantine.",
                "HOLD_ACTIVE",
                line_key=key,
            )
        if piece.address.accepted_event_id is None or piece.address.condition != "good":
            raise _refuse(
                "Some of these pieces are no longer accepted good stock.",
                "NOT_ACCEPTED",
                line_key=key,
            )


def _line_body(
    line: movements.Line, slices: Sequence[movements.Slice], pool: str
) -> dict[str, Any]:
    first = slices[0].address
    return {
        "line_key": str(line.line_key),
        "lot_id": str(line.lot_id) if line.lot_id else None,
        "qty": line.qty,
        "sku_id": str(first.sku_id) if first.sku_id else None,
        "origin_id": str(first.origin_id) if first.origin_id else None,
        "condition": first.condition,
        "source_location_id": str(line.source_location_id),
        # Nothing moves at approval: the pieces stay where they stand until the
        # vendor collects them, and then they leave the site.
        "destination_location_id": None,
        "hold_keys": [],
        "source_pool": pool,
        # Each portion names the layer whose recorded cost values it, so the
        # reviewed document says exactly which value leaves at pickup.
        "portions": [
            {
                "lot_id": str(piece.lot_id),
                "lower": piece.interval[0],
                "upper": piece.interval[1],
                "origin_id": _basis(piece.address),
            }
            for piece in slices
        ],
        "value_basis": "recorded_layer_cost",
    }


Pinned = movements.Pinned


def _resolve(
    run: CommandRun,
    payload: Payload,
    pinned: Pinned | None = None,
    pools: Mapping[str, str] | None = None,
) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    out: list[tuple[uuid.UUID, dict[str, Any]]] = []
    seen: dict[uuid.UUID, list[ranges.Interval]] = {}
    for line in payload.lines:
        pool = (pools or {}).get(str(line.line_key)) or _pool_of(run, line, payload.site_id)
        if pinned is not None:
            slices = movements.source_slices(line, payload.site_id, pinned)
        else:
            slices = _free_slices(line, payload.site_id, pool)
        for piece in slices:
            taken = seen.setdefault(piece.lot_id, [])
            if ranges.intersect([piece.interval], taken):
                raise _refuse("Two lines of this RTV claim the same pieces.", "OVERLAP")
            taken.append(piece.interval)
        _still_eligible(line, slices, pool)
        out.append((line.line_key, _line_body(line, slices, pool)))
    return out


def _vendor(run: CommandRun, vendor_id: int) -> Any:
    from vendors.models import Vendor

    vendor = Vendor.objects.filter(tenant_id=run.tenant_id, pk=vendor_id).first()
    if vendor is None:
        raise Refusal("NOT_FOUND", "That vendor was not found.")
    if not vendor.is_active:
        raise _refuse("That vendor is retired.", "VENDOR_INACTIVE", field="vendor_id")
    return vendor


def _evidence_exist(run: CommandRun, evidence_ids: Iterable[uuid.UUID]) -> None:
    from files.goods_models import EvidenceObject

    wanted = set(evidence_ids)
    found = set(
        EvidenceObject.objects.filter(tenant_id=run.tenant_id, pk__in=list(wanted)).values_list(
            "pk", flat=True
        )
    )
    if wanted - found:
        raise _refuse(
            "That evidence file was not found.", "EVIDENCE_NOT_FOUND", field="evidence_ids"
        )


# ---------------------------------------------------------------------------
# E154 draft (carried by E151)
# ---------------------------------------------------------------------------


def create(run: CommandRun, payload: Payload) -> tuple[DocumentIdentity, str]:
    """Draft the RTV with its exact pieces frozen. Nothing is reserved or moved yet."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "An RTV is prepared by a named person.")
    movements.require_movement_site(run, payload.site_id)
    site = movements.site_of(payload.site_id)
    _vendor(run, payload.vendor_id)
    identity, head = new_document(
        run,
        kind=DOC_TYPE,
        purpose=KIND,
        entity_id=site.gstin.legal_entity_id,
        site_id=payload.site_id,
    )
    engine.lock_lots(run, movements._candidate_lots(payload.as_movement()))
    _evidence_exist(run, payload.evidence_ids)
    lines = _resolve(run, payload)
    append_revision(run, head, header=payload.as_header(), replace_lines=list(lines))
    movements._record_movement(run, identity, KIND, None)
    run.audit_subject_key = f"movement:{identity.pk}"
    run.audit_site_id = payload.site_id
    run.audit_after = {"movement_id": str(identity.pk), "kind": KIND, "state": "draft"}
    return identity, "draft"


def draft_payload(head: DocumentHead) -> tuple[Payload, Pinned, dict[str, str]]:
    """The typed payload of the locked draft, its frozen portions and each line's pool."""
    revision = head.draft_revision
    assert revision is not None
    header = dict(revision.payload)
    stored = [dict(state.payload) for state in revision_lines(head.document_id, revision.revision)]
    lines = [
        {
            "line_key": row["line_key"],
            "lot_id": row.get("lot_id"),
            "qty": row["qty"],
            "sku_id": row.get("sku_id"),
            # The frozen portions pin the pieces exactly; the line's origin and
            # condition were read off its first portion only (see goods_adjustments).
            "origin_id": None,
            "condition": None,
            "source_location_id": row.get("source_location_id"),
        }
        for row in stored
    ]
    payload = parse({**header, "lines": lines})
    pinned: Pinned = {
        uuid.UUID(str(row["line_key"])): [
            (uuid.UUID(str(piece["lot_id"])), int(piece["lower"]), int(piece["upper"]))
            for piece in row.get("portions") or []
        ]
        for row in stored
    }
    pools = {str(row["line_key"]): str(row.get("source_pool") or "") for row in stored}
    return payload, pinned, pools


def _pinned_lots(pinned: Pinned) -> list[uuid.UUID]:
    return [lot_id for pieces in pinned.values() for lot_id, _l, _u in pieces]


def _costs(origin_ids: Iterable[str]) -> dict[str, int]:
    return {
        str(pk): int(cost)
        for pk, cost in Origin.objects.filter(pk__in=set(origin_ids)).values_list("pk", "unit_cost")
    }


def _amounts(payload: Payload, bodies: Sequence[tuple[uuid.UUID, Mapping[str, Any]]]) -> Amounts:
    pieces = [
        (int(piece["upper"]) - int(piece["lower"]), piece.get("origin_id"))
        for _key, body in bodies
        for piece in body["portions"]
    ]
    if any(not origin for _qty, origin in pieces):
        return Amounts(payload.quantity, None)
    costs = _costs(str(origin) for _qty, origin in pieces)
    return Amounts(payload.quantity, sum(qty * costs[str(origin)] for qty, origin in pieces))


# ---------------------------------------------------------------------------
# E155 submit
# ---------------------------------------------------------------------------


def submit(
    run: CommandRun,
    movement: GoodsMovement,
    *,
    reviewed_hash: str,
    expected_revision: int | None,
) -> tuple[GoodsMovement, uuid.UUID]:
    """Send the RTV for a distinct Owner approval (GSA-R01). Nothing is reserved here."""
    document_id = movement.document_id
    site_id = movement.document.held_site_id
    movements.require_movement_site(run, site_id)
    head = lock_heads(run, [document_id])[document_id]
    if expected_revision is not None and expected_revision != head.revision:
        raise Refusal("REVISION_SUPERSEDED", "This RTV changed after you loaded it. Reload it.")
    if reviewed_hash != movements.draft_hash(head):
        raise Refusal("REVISION_SUPERSEDED", "What you reviewed is no longer this RTV's content.")
    if head.state != DocumentHead.State.DRAFT:
        raise _refuse("This RTV is not a draft.", "NOT_DRAFT")
    supersede_pending(run, "document", str(document_id), APPROVE_ACTION)
    payload, pinned, pools = draft_payload(head)
    vendor = _vendor(run, payload.vendor_id)
    engine.lock_lots(run, _pinned_lots(pinned))
    bodies = _resolve(run, payload, pinned, pools)
    policy = pin(
        run,
        action=APPROVE_ACTION,
        purpose=KIND,
        site_id=site_id,
        brand_ids=[None],
        amounts=_amounts(payload, bodies),
    )
    request = create_request(
        run,
        subject_kind="document",
        subject_key=str(document_id),
        revision=head.revision,
        reviewed_hash=movements.draft_hash(head),
        requested_action=APPROVE_ACTION,
        site_id=site_id,
        title=f"Return {payload.quantity} piece(s) to {vendor.name}",
        policy=policy,
        new_subject=True,
    )
    set_state(run, head, DocumentHead.State.SUBMITTED, event="submitted")
    open_exception(
        run,
        kind=APPROVAL_EXCEPTION,
        site_id=site_id,
        subject_key=f"movement:{document_id}",
        reason_code="RTV_APPROVAL",
        source_event_key=engine.event_key("rtv_approval", document_id, request.pk),
        allowed_resolution_actions=RESOLUTION_ACTIONS,
        note=f"approval_request:{request.pk}",
    )
    run.audit_subject_key = f"movement:{document_id}"
    run.audit_site_id = site_id
    run.audit_after = {"approval_request_id": str(request.pk), "state": "submitted"}
    return movement, request.pk


# ---------------------------------------------------------------------------
# The decision (E234 -> goods_movements.decide_release -> here): P15 reservation
# ---------------------------------------------------------------------------


def decide(run: CommandRun, context: DecisionContext, movement: GoodsMovement) -> dict[str, Any]:
    request = context.request
    document_id = movement.document_id
    site_id = movement.document.held_site_id
    context.access.require(APPROVE_ACTION, site_id=site_id)
    movements.check_movement_site(SiteGuard.objects.filter(site_id=site_id).first())
    head = lock_heads(run, [document_id])[document_id]
    if str(context.checker_id) == str(head.document.maker_id):
        raise Refusal("SELF_APPROVAL", "Someone who prepared this RTV cannot also approve it.")
    context.enforce_policy(run)
    if head.state != DocumentHead.State.SUBMITTED:
        raise Refusal("STATE_CONFLICT", "This RTV is no longer waiting for approval.")
    if request.reviewed_hash != movements.draft_hash(head):
        raise Refusal("REVISION_SUPERSEDED", "The RTV changed after it was submitted.")
    exception_key = engine.event_key("rtv_approval", document_id, request.pk)
    if context.decision == "reject":
        set_state(
            run, head, DocumentHead.State.DRAFT, event="rejected", reason_code=context.reason_code
        )
        resolve_exceptions(
            run,
            kind=APPROVAL_EXCEPTION,
            subject_key=f"movement:{document_id}",
            reason_code="RTV_REJECTED",
            source_event_key=exception_key,
        )
        return {"state": "rejected"}
    payload, pinned, pools = draft_payload(head)
    engine.lock_lots(run, _pinned_lots(pinned))
    bodies = _resolve(run, payload, pinned, pools)
    if any(body["source_pool"] == POOL_STOCK for _key, body in bodies):
        # Good stock a counter took offline is not free to promise elsewhere
        # (PRD §10.2); quarantined goods were never on a counter's shelf. Imported
        # here: `sell.services` reaches into outbound at start-up.
        from sell.services.till_authority import refuse_if_allocated

        refuse_if_allocated(site_id)
    identity = head.document
    number = identity.official_number or allocate(run, identity.entity, DOC_TYPE)
    assert run.principal.human_id is not None
    version, _official = officialise(
        run,
        head,
        approved_by_id=run.principal.human_id,
        canonical_header=payload.as_header(),
        lines=[(key, body) for key, body in bodies],
        authority=run.authority,
        number=number,
    )
    plan = engine.Plan(RTV_POSTING, version.pk, engine.event_key("rtv_approve", version.pk))
    for _key, body in bodies:
        for piece in body["portions"]:
            engine.reserve(
                run,
                plan,
                lot_id=uuid.UUID(str(piece["lot_id"])),
                interval=(int(piece["lower"]), int(piece["upper"])),
                transfer_version_id=version.pk,
                site_id=site_id,
            )
    engine.post(run, version, plan)
    movement.rtv_state = INITIATED
    movement.save(update_fields=["rtv_state"])
    resolve_exceptions(
        run,
        kind=APPROVAL_EXCEPTION,
        subject_key=f"movement:{document_id}",
        reason_code="RTV_APPROVED",
        source_event_key=exception_key,
    )
    open_exception(
        run,
        kind=PENDING_EXCEPTION,
        site_id=site_id,
        subject_key=f"movement:{document_id}",
        reason_code="RTV_AWAITING_PICKUP",
        source_event_key=engine.event_key("rtv_pending", document_id),
        allowed_resolution_actions=RESOLUTION_ACTIONS,
        note=f"vendor:{payload.vendor_id}",
    )
    record_event(run, document_id, "rtv_approved", version_id=version.pk)
    return {"movement_id": str(document_id), "number": number, "rtv_state": INITIATED}


# ---------------------------------------------------------------------------
# Balances: what was approved, what left, what was withdrawn, what is reserved
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Piece:
    lot_id: uuid.UUID
    interval: ranges.Interval
    origin_id: str | None


def _official_lines(version: OfficialVersion) -> list[dict[str, Any]]:
    return [dict(line.payload) for line in version.lines.order_by("line_no")]


def _reserved_pieces(version_id: uuid.UUID, body: Mapping[str, Any]) -> list[Piece]:
    """The line's frozen portions still reserved to this RTV, in their frozen (FIFO) order."""
    frozen = [
        (uuid.UUID(str(p["lot_id"])), (int(p["lower"]), int(p["upper"])), p.get("origin_id"))
        for p in body.get("portions") or []
    ]
    live: dict[uuid.UUID, list[ranges.Interval]] = {}
    for row in ActiveReservation.objects.filter(
        transfer_version_id=version_id, lot_id__in={lot for lot, _i, _o in frozen}
    ):
        live.setdefault(row.lot_id, []).append(bounds(row.portion))
    out: list[Piece] = []
    for lot_id, interval, origin in frozen:
        for piece in ranges.intersect([interval], live.get(lot_id, [])):
            out.append(Piece(lot_id, piece, str(origin) if origin else None))
    return out


def _events(movement: GoodsMovement) -> list[RtvEvent]:
    return list(RtvEvent.objects.filter(movement=movement).order_by("sequence_no"))


def _moved(events: Iterable[RtvEvent], kind: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for event in events:
        if event.kind != kind:
            continue
        for line in event.lines:
            out[str(line["line_key"])] = out.get(str(line["line_key"]), 0) + int(line["qty"])
    return out


# ---------------------------------------------------------------------------
# Pickup (E156, adapted): P15 departure of exactly what was collected
# ---------------------------------------------------------------------------

PICKUP_FIELDS = frozenset(
    {
        "picked_up_at",
        "collected_by",
        "evidence_reference",
        "evidence_ids",
        "evidence_note",
        "lines",
        "left_behind",
    }
)
PICKUP_LINE_FIELDS = frozenset({"line_key", "qty"})
LEFT_BEHIND_FIELDS = frozenset({"line_key", "reason", "qty", "remark", "further_pickup_expected"})
WITHDRAW_FIELDS = frozenset({"reason", "remark", "lines"})


@dataclass(frozen=True)
class LeftBehind:
    line_key: uuid.UUID
    reason: str
    qty: int
    remark: str | None
    further_pickup_expected: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "line_key": str(self.line_key),
            "reason": self.reason,
            "qty": self.qty,
            "remark": self.remark,
            "further_pickup_expected": self.further_pickup_expected,
        }


@dataclass(frozen=True)
class Pickup:
    picked_up_at: datetime
    collected_by: str
    evidence_reference: str | None
    evidence_ids: tuple[uuid.UUID, ...]
    evidence_note: str | None
    lines: tuple[tuple[uuid.UUID, int], ...]
    left_behind: tuple[LeftBehind, ...]


def _closed(body: Mapping[str, Any], fields: frozenset[str]) -> None:
    unknown = sorted(set(body) - fields)
    if unknown:
        raise _invalid(f"Unknown field(s): {', '.join(unknown)}.", unknown[0])


def _qty(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= movements.MAX_QTY:
        raise _invalid(f"{field} is 1 to {movements.MAX_QTY} pieces.", field)
    return value


def _text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise _invalid(f"{field} is text of at most {limit} characters.", field)
    return value.strip() or None


def _quantities(raw: Any, field: str) -> tuple[tuple[uuid.UUID, int], ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= movements.MAX_LINES:
        raise _invalid(f"{field} has 1 to {movements.MAX_LINES} lines.", field)
    out: list[tuple[uuid.UUID, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid(f"Every {field} entry is an object.", field)
        _closed(item, PICKUP_LINE_FIELDS)
        out.append(
            (movements._uuid(item.get("line_key"), "line_key"), _qty(item.get("qty"), "qty"))
        )
    keys = [key for key, _qty in out]
    if len(set(keys)) != len(keys):
        raise _invalid(f"Every line_key in {field} is distinct.", field)
    return tuple(out)


def parse_pickup(body: dict[str, Any], now: datetime) -> Pickup:
    """The confirmed handover: who collected, when, how many per line, evidence, reasons."""
    from outbound.transfers import _moment

    _closed(body, PICKUP_FIELDS)
    collected_by = _text(body.get("collected_by"), "collected_by", MAX_COLLECTOR)
    if not collected_by:
        raise _invalid(
            "collected_by names the vendor or the representative who collected the goods.",
            "collected_by",
        )
    reference = _text(body.get("evidence_reference"), "evidence_reference", MAX_REFERENCE)
    note = _text(body.get("evidence_note"), "evidence_note", MAX_NOTE)
    evidence = _evidence_ids(body.get("evidence_ids"))
    if not (reference or note or evidence):
        raise _refuse(
            "A pickup needs its handover evidence: the vendor's slip or challan number, a "
            "photo, or a note of what was signed.",
            "EVIDENCE_REQUIRED",
            field="evidence_reference",
        )
    raw_reasons = body.get("left_behind") or []
    if not isinstance(raw_reasons, list) or len(raw_reasons) > 5 * movements.MAX_LINES:
        raise _invalid("left_behind is a list of reasons.", "left_behind")
    return Pickup(
        picked_up_at=_moment(body.get("picked_up_at"), "picked_up_at", now),
        collected_by=collected_by,
        evidence_reference=reference,
        evidence_ids=evidence,
        evidence_note=note,
        lines=_quantities(body.get("lines"), "lines"),
        left_behind=tuple(_left_behind(item) for item in raw_reasons),
    )


def _left_behind(item: Any) -> LeftBehind:
    if not isinstance(item, dict):
        raise _invalid("Every left_behind entry is an object.", "left_behind")
    _closed(item, LEFT_BEHIND_FIELDS)
    reason = item.get("reason")
    if reason not in LEFT_BEHIND_REASONS:
        raise _invalid(f"reason is one of {', '.join(LEFT_BEHIND_REASONS)}.", "left_behind.reason")
    remark = _text(item.get("remark"), "left_behind.remark", MAX_REMARK)
    further = item.get("further_pickup_expected")
    if further is not None and not isinstance(further, bool):
        raise _invalid("further_pickup_expected is true or false.", "further_pickup_expected")
    if reason == OTHER:
        if not remark or further is None:
            raise _refuse(
                "An 'other' reason needs a remark and says whether another pickup is expected.",
                "REMARK_REQUIRED",
                field="left_behind.remark",
            )
        expected = further
    else:
        expected = FURTHER_PICKUP[str(reason)]
        if further is not None and further != expected:
            raise _refuse(
                f"'{reason}' already says whether another pickup is expected.",
                "REASON_CONFLICT",
                field="further_pickup_expected",
            )
    return LeftBehind(
        line_key=movements._uuid(item.get("line_key"), "line_key"),
        reason=str(reason),
        qty=_qty(item.get("qty"), "left_behind.qty"),
        remark=remark,
        further_pickup_expected=expected,
    )


@dataclass(frozen=True)
class Withdrawal:
    reason: str
    remark: str | None
    #: ``None`` withdraws the whole outstanding balance.
    lines: tuple[tuple[uuid.UUID, int], ...] | None


def parse_withdrawal(body: dict[str, Any]) -> Withdrawal:
    _closed(body, WITHDRAW_FIELDS)
    reason = body.get("reason")
    if reason not in WITHDRAWAL_REASONS:
        raise _invalid(f"reason is one of {', '.join(WITHDRAWAL_REASONS)}.", "reason")
    remark = _text(body.get("remark"), "remark", MAX_REMARK)
    if reason == OTHER and not remark:
        raise _refuse("An 'other' withdrawal needs a remark.", "REMARK_REQUIRED", field="remark")
    raw = body.get("lines")
    return Withdrawal(
        reason=str(reason),
        remark=remark,
        lines=None if raw is None else _quantities(raw, "lines"),
    )


@dataclass
class _Locked:
    movement: GoodsMovement
    head: DocumentHead
    version: OfficialVersion
    bodies: dict[str, dict[str, Any]]
    reserved: dict[str, list[Piece]]
    #: Pending damage reports over the RTV's lots, locked for a pickup only.
    reports: list[DamageReport]


def _lock(run: CommandRun, document_id: uuid.UUID, *, reports: bool = False) -> _Locked:
    movement = movements.movement_of(document_id)
    if movement.kind != GoodsMovement.Kind.RTV:
        raise _state_conflict("This movement is not a return to vendor.")
    site_id = movement.document.held_site_id
    movements.require_movement_site(run, site_id)
    head = lock_heads(run, [document_id])[document_id]
    movement.refresh_from_db(fields=["rtv_state"])
    if movement.rtv_state != INITIATED or head.live_version is None:
        raise _state_conflict(
            "This RTV has no approved balance waiting: it is not approved yet, or it is "
            "already completed, closed or cancelled."
        )
    version = head.live_version
    bodies = {str(body["line_key"]): body for body in _official_lines(version)}
    lots = sorted(
        {uuid.UUID(str(p["lot_id"])) for body in bodies.values() for p in body["portions"]},
        key=str,
    )
    # DOCUMENT before LOT: a pickup may close a pending damage report, so it
    # waits behind a review decision already holding that report.
    pending = damage_review.lock_pending_over(run, lots) if reports else []
    engine.lock_lots(run, lots)
    reserved = {key: _reserved_pieces(version.pk, body) for key, body in bodies.items()}
    return _Locked(movement, head, version, bodies, reserved, pending)


def _outstanding(locked: _Locked) -> dict[str, int]:
    return {
        key: ranges.total([p.interval for p in pieces]) for key, pieces in locked.reserved.items()
    }


def _check_lines(locked: _Locked, lines: Sequence[tuple[uuid.UUID, int]]) -> None:
    outstanding = _outstanding(locked)
    for line_key, qty in lines:
        key = str(line_key)
        if key not in locked.bodies:
            raise _refuse("That line is not on this RTV.", "UNKNOWN_LINE", line_key=key)
        if qty > outstanding[key]:
            raise _refuse(
                f"Only {outstanding[key]} piece(s) of this line are still waiting to leave; "
                "cumulative departure never exceeds the approval.",
                "EXCEEDS_OUTSTANDING",
                line_key=key,
                quantity=qty,
            )


def _take(pieces: Sequence[Piece], qty: int, *, from_end: bool = False) -> list[Piece]:
    out: list[Piece] = []
    remaining = qty
    ordered = list(reversed(pieces)) if from_end else list(pieces)
    for piece in ordered:
        if remaining <= 0:
            break
        size = min(remaining, ranges.length(piece.interval))
        lower, upper = piece.interval
        span = (upper - size, upper) if from_end else (lower, lower + size)
        out.append(Piece(piece.lot_id, span, piece.origin_id))
        remaining -= size
    return out


def _check_reasons(
    locked: _Locked, lines: Sequence[tuple[uuid.UUID, int]], reasons: Sequence[LeftBehind]
) -> None:
    """Every piece left behind has a reason, and no reason names a piece that left."""
    taken = {str(key): qty for key, qty in lines}
    left = {key: qty - taken.get(key, 0) for key, qty in _outstanding(locked).items()}
    given: dict[str, int] = {}
    for reason in reasons:
        key = str(reason.line_key)
        if key not in locked.bodies:
            raise _refuse("That line is not on this RTV.", "UNKNOWN_LINE", line_key=key)
        given[key] = given.get(key, 0) + reason.qty
    for key, qty in left.items():
        if given.get(key, 0) != qty:
            raise _refuse(
                f"{qty} piece(s) of this line stay behind; record why for exactly that many "
                "(reasons can be split across quantities).",
                "REASONS_INCOMPLETE",
                line_key=key,
                quantity=qty,
            )


@dataclass(frozen=True)
class EndedHold:
    """One hold that ended over part of a departing piece, as the departure saw it."""

    hold_key: uuid.UUID
    kind: str
    interval: ranges.Interval


def _end_holds(
    run: CommandRun, plan: engine.Plan, piece: Piece, site_id: int, version_id: uuid.UUID
) -> list[EndedHold]:
    """End every hold over exactly this piece: the return is the goods' exit route.

    Overall PRD §15.2.1 rule 3: held goods leave quarantine through their
    approved return. A hold over goods no longer in custody would be work that
    nobody can finish. What ended is returned, so a shipment can put the same
    kinds of hold back on pieces that come back (goods ticket 15F).
    """
    ended: list[EndedHold] = []
    for hold in list(
        ActiveHold.objects.filter(lot_id=piece.lot_id, portion__overlap=portion(*piece.interval))
    ):
        for sub in ranges.intersect([bounds(hold.portion)], [piece.interval]):
            engine.release_hold(
                run,
                plan,
                lot_id=piece.lot_id,
                interval=sub,
                hold_key=hold.hold_key,
                site_id=site_id,
                source_version_id=version_id,
            )
            ended.append(EndedHold(hold.hold_key, hold.kind, sub))
    return ended


def _at_site(piece: Piece, site_id: int) -> None:
    standing = [
        bounds(p.portion)
        for p in engine.positions_of(piece.lot_id, piece.interval)
        if p.boundary == "physical" and p.site_id == site_id
    ]
    if ranges.total(ranges.intersect(standing, [piece.interval])) != ranges.length(piece.interval):
        raise _state_conflict("Some of the reserved pieces are no longer at this site.")


def _next_sequence(movement: GoodsMovement) -> int:
    last = (
        RtvEvent.objects.filter(movement=movement)
        .order_by("-sequence_no")
        .values_list("sequence_no", flat=True)
        .first()
    )
    return int(last or 0) + 1


@dataclass(frozen=True)
class Departed:
    """One departed slice of a reserved piece, with the address it left from."""

    lot_id: uuid.UUID
    interval: ranges.Interval
    origin_id: str | None
    condition: str
    location_id: str | None
    holds: tuple[EndedHold, ...]


@dataclass
class Departure:
    """What one pickup or shipment took: pieces per line, the holds it ended, its batch."""

    left: list[tuple[str, list[Piece]]]
    slices: dict[str, list[Departed]]
    ended: list[EndedHold]
    batch_id: uuid.UUID | None


def depart(
    run: CommandRun,
    locked: _Locked,
    lines: Sequence[tuple[uuid.UUID, int]],
    *,
    event_name: str,
) -> Departure:
    """P15 departure of exactly ``lines``, oldest reserved pieces first.

    Shared by a pickup (15B) and a shipment (15F): the reservation is consumed,
    the pieces go to the ``returned`` boundary, value leaves stock for external
    at each portion's own recorded cost, and every hold over exactly those
    pieces ends. A shipment keeps, per departed slice, the condition, location
    and holds it left with, so pieces that come back are received truthfully.
    """
    site_id = locked.movement.document.held_site_id
    version = locked.version
    plan = engine.Plan(
        RTV_POSTING, version.pk, engine.event_key(event_name, version.pk, run.key_id)
    )
    left: list[tuple[str, list[Piece]]] = [
        (str(key), _take(locked.reserved[str(key)], qty)) for key, qty in lines
    ]
    costs = _costs(p.origin_id for _key, pieces in left for p in pieces if p.origin_id)
    ended: list[EndedHold] = []
    slices: dict[str, list[Departed]] = {}
    for key, pieces in left:
        for piece in pieces:
            _at_site(piece, site_id)
            engine.end_reservation(
                run,
                plan,
                lot_id=piece.lot_id,
                interval=piece.interval,
                transfer_version_id=version.pk,
                effect="consume",
                site_id=site_id,
            )
            piece_holds = _end_holds(run, plan, piece, site_id, version.pk)
            ended.extend(piece_holds)
            for sub, old in engine.end_positions(
                run, plan, piece.lot_id, piece.interval, RETURNED_BOUNDARY, KIND
            ):
                slices.setdefault(key, []).append(
                    Departed(
                        lot_id=piece.lot_id,
                        interval=sub,
                        origin_id=piece.origin_id,
                        condition=old.condition,
                        location_id=str(old.location_id) if old.location_id else None,
                        holds=tuple(
                            EndedHold(hold.hold_key, hold.kind, part)
                            for hold in piece_holds
                            for part in ranges.intersect([hold.interval], [sub])
                        ),
                    )
                )
            if piece.origin_id is None:
                continue
            plan.value.append(
                ValuePair(
                    origin_id=uuid.UUID(piece.origin_id),
                    amount=ranges.length(piece.interval) * costs[piece.origin_id],
                    source_bucket="stock",
                    destination_bucket="external",
                    source_site_id=site_id,
                    destination_site_id=None,
                    lot_id=piece.lot_id,
                    lower=piece.interval[0],
                    upper=piece.interval[1],
                )
            )
    batch_id = engine.post(run, version, plan)
    return Departure(left=left, slices=slices, ended=ended, batch_id=batch_id)


def pickup(run: CommandRun, document_id: uuid.UUID, parsed: Pickup) -> RtvEvent:
    """Record one confirmed handover to the vendor: exactly what was collected leaves."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A pickup is confirmed by a named person.")
    locked = _lock(run, document_id, reports=True)
    site_id = locked.movement.document.held_site_id
    _check_lines(locked, parsed.lines)
    _check_reasons(locked, parsed.lines, parsed.left_behind)
    _evidence_exist(run, parsed.evidence_ids)
    version = locked.version
    departure = depart(run, locked, parsed.lines, event_name="rtv_pickup")
    left = departure.left
    closed = _close_returned_reports(
        run, locked, [piece for _key, pieces in left for piece in pieces]
    )
    quantity = sum(qty for _key, qty in parsed.lines)
    event: RtvEvent = run.record(
        RtvEvent(
            movement=locked.movement,
            kind=RtvEvent.Kind.PICKUP,
            sequence_no=_next_sequence(locked.movement),
            quantity=quantity,
            lines=[_event_line(key, pieces) for key, pieces in left],
            details={
                "collected_by": parsed.collected_by,
                "evidence_reference": parsed.evidence_reference,
                "evidence_ids": [str(e) for e in parsed.evidence_ids],
                "evidence_note": parsed.evidence_note,
                "left_behind": [reason.as_json() for reason in parsed.left_behind],
                "closed_damage_report_ids": [str(report.pk) for report in closed],
            },
            journal_batch_id=departure.batch_id,
        ),
        event_at=parsed.picked_up_at,
    )
    _close_hold_work(run, {hold.hold_key for hold in departure.ended})
    movements.settle_reservation_blocks(run, [version.pk])
    settle(run, locked.movement, version, locked.bodies, [event])
    run.audit_subject_key = f"movement:{document_id}"
    run.audit_site_id = site_id
    run.audit_after = {
        "movement_id": str(document_id),
        "rtv_event_id": str(event.pk),
        "picked_up_qty": quantity,
        "rtv_state": locked.movement.rtv_state,
    }
    return event


def _event_line(key: str, pieces: Sequence[Piece]) -> dict[str, Any]:
    return {
        "line_key": key,
        "qty": ranges.total([p.interval for p in pieces]),
        "portions": [
            {
                "lot_id": str(p.lot_id),
                "lower": p.interval[0],
                "upper": p.interval[1],
                "origin_id": p.origin_id,
            }
            for p in pieces
        ],
    }


def _close_returned_reports(
    run: CommandRun,
    locked: _Locked,
    collected: Sequence[Piece],
    *,
    note: str = "Returned to vendor on",
    reason_code: str = "RETURNED_TO_VENDOR",
) -> list[DamageReport]:
    """Close every pending damage report this pickup took the last of its pieces back on.

    Anand's 15B decision 3. Only a report over pieces this pickup collected is
    touched, and only once *every* piece it covers has gone back to the vendor:
    a report over collected and uncollected pieces stays pending while any of
    them is still here - those are damaged goods in quarantine that a different
    person may still confirm - and closes at the pickup that takes the last.
    Each closure is one ``damage_report_closed`` event in the RTV's history.
    """
    taken: dict[uuid.UUID, list[ranges.Interval]] = {}
    for piece in collected:
        taken.setdefault(piece.lot_id, []).append(piece.interval)
    number = locked.head.document.official_number
    closed: list[DamageReport] = []
    for report in locked.reports:
        covered = damage_review.report_pieces(report)
        if not any(
            ranges.intersect(spans, taken.get(lot_id, [])) for lot_id, spans in covered.items()
        ):
            continue
        if not damage_review.all_at_boundary(report, RETURNED_BOUNDARY):
            continue
        damage_review.close_departed(run, report, note=f"{note} {number}")
        record_event(
            run,
            locked.movement.document_id,
            "damage_report_closed",
            version_id=locked.version.pk,
            reason_code=reason_code,
            payload={"details": [], "damage_report_id": str(report.pk)},
        )
        closed.append(report)
    return closed


def _close_hold_work(run: CommandRun, keys: Iterable[uuid.UUID]) -> None:
    """A hold's owned work ends once nothing stands under its key any more."""
    for key in sorted(set(keys), key=str):
        if ActiveHold.objects.filter(hold_key=key).exists():
            continue
        resolve_exceptions(
            run,
            kind=movements.HOLD_EXCEPTION,
            subject_key=f"hold:{key}",
            reason_code="RETURNED_TO_VENDOR",
        )


# ---------------------------------------------------------------------------
# Withdrawal (E158, adapted): P16 releases only the outstanding reservation
# ---------------------------------------------------------------------------


def withdraw(run: CommandRun, document_id: uuid.UUID, parsed: Withdrawal) -> RtvEvent:
    """Explicitly take pieces off the pending balance. Nothing moves; holds stay."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A withdrawal is recorded by a named person.")
    locked = _lock(run, document_id)
    site_id = locked.movement.document.held_site_id
    outstanding = _outstanding(locked)
    if parsed.lines is None:
        lines = [(uuid.UUID(key), qty) for key, qty in outstanding.items() if qty]
    else:
        _check_lines(locked, parsed.lines)
        lines = list(parsed.lines)
    if not lines:
        raise _state_conflict("Nothing on this RTV is still waiting to leave.")
    version = locked.version
    plan = engine.Plan(
        WITHDRAW_POSTING, version.pk, engine.event_key("rtv_withdraw", version.pk, run.key_id)
    )
    released: list[tuple[str, list[Piece]]] = [
        # The last pieces go back first, so the oldest stay offered to a pickup.
        (str(key), _take(locked.reserved[str(key)], qty, from_end=True))
        for key, qty in lines
    ]
    for _key, pieces in released:
        for piece in pieces:
            engine.end_reservation(
                run,
                plan,
                lot_id=piece.lot_id,
                interval=piece.interval,
                transfer_version_id=version.pk,
                effect="release",
                site_id=site_id,
            )
    batch_id = engine.post(run, version, plan)
    quantity = sum(qty for _key, qty in lines)
    event: RtvEvent = run.record(
        RtvEvent(
            movement=locked.movement,
            kind=RtvEvent.Kind.WITHDRAWAL,
            sequence_no=_next_sequence(locked.movement),
            quantity=quantity,
            lines=[_event_line(key, pieces) for key, pieces in released],
            details={"reason": parsed.reason, "remark": parsed.remark},
            journal_batch_id=batch_id,
        )
    )
    # A hold over the withdrawn pieces stays in force; only the "reserved stock
    # is held" work is finished once there is no reservation left for it to block.
    movements.settle_reservation_blocks(run, [version.pk])
    settle(run, locked.movement, version, locked.bodies, [event])
    run.audit_subject_key = f"movement:{document_id}"
    run.audit_site_id = site_id
    run.audit_after = {
        "movement_id": str(document_id),
        "rtv_event_id": str(event.pk),
        "withdrawn_qty": quantity,
        "rtv_state": locked.movement.rtv_state,
    }
    return event


def settle(
    run: CommandRun,
    movement: GoodsMovement,
    version: OfficialVersion,
    bodies: Mapping[str, Mapping[str, Any]],
    latest: Sequence[RtvEvent],
) -> None:
    """Close the RTV once nothing is reserved and every departure is accounted for.

    Quarantine outcomes §4 and transfers PRD §7. A pickup is accounted for at
    handover. A shipment (goods ticket 15F) is accounted for only when every
    piece it carried is acknowledged by the vendor or recorded back at the
    source: while one awaits vendor receipt, or carries an acknowledgement
    shortfall (an owned discrepancy that only the Owner's approved closure
    ends, goods ticket 15H), the RTV stays ``initiated`` even with no balance
    reserved. A closed shortfall accounts for its pieces but never counts them
    as delivered: an RTV with one closes ``closed_partially_returned``, never
    ``completed`` or ``cancelled``. ``latest`` is this command's
    own events, still buffered with the rest of its evidence.

    Once nothing is reserved, nothing waits to be dispatched: the
    ``rtv_not_dispatched`` work closes then, whether or not a shipment is still
    on its way.
    """
    if ActiveReservation.objects.filter(transfer_version_id=version.pk).exists():
        return
    work_key = engine.event_key("rtv_pending", movement.document_id)
    subject = f"movement:{movement.document_id}"
    events = [*_events(movement), *latest]
    accounts = shipment_accounts(events)
    if any(not account.accounted for account in accounts):
        resolve_exceptions(
            run,
            kind=PENDING_EXCEPTION,
            subject_key=subject,
            reason_code="RTV_ALL_DISPATCHED",
            source_event_key=work_key,
        )
        return
    approved = sum(int(body["qty"]) for body in bodies.values())
    delivered = sum(_moved(events, RtvEvent.Kind.PICKUP).values()) + sum(
        account.acknowledged_qty for account in accounts
    )
    # Goods ticket 15H: pieces whose shortfall the Owner closed left for good
    # without reaching the vendor. They are neither delivered nor "nothing left".
    short_closed = sum(account.closed_qty for account in accounts)
    if delivered == approved:
        state = COMPLETED
    elif delivered == 0 and short_closed == 0:
        # Nothing reached the vendor: the whole balance was withdrawn, and any
        # shipment came back to the source. Each shipment keeps its own
        # ``returned_to_source`` outcome; the RTV is not labelled delivered.
        state = CANCELLED
    else:
        state = CLOSED_PARTIALLY_RETURNED
    movement.rtv_state = state
    movement.save(update_fields=["rtv_state"])
    resolve_exceptions(
        run,
        kind=PENDING_EXCEPTION,
        subject_key=subject,
        reason_code=f"RTV_{state.upper()}",
        source_event_key=work_key,
    )
    record_event(run, movement.document_id, f"rtv_{state}", version_id=version.pk)


# ---------------------------------------------------------------------------
# Shipments (goods ticket 15F): what each one carried and how it is accounted for
# ---------------------------------------------------------------------------

AWAITING_RECEIPT = "awaiting_receipt"
SHORT_ACKNOWLEDGED = "short_acknowledged"
DELIVERED = "delivered"
PARTLY_RETURNED = "partly_returned"
RETURNED_TO_SOURCE = "returned_to_source"
#: Goods ticket 15H: accounted for, with the Owner's approved closure of the
#: pieces the vendor never acknowledged (a recognised shortfall, GSA-R07).
SHORTFALL_CLOSED = "shortfall_closed"
SHIPMENT_STATUSES = (
    AWAITING_RECEIPT,
    SHORT_ACKNOWLEDGED,
    DELIVERED,
    PARTLY_RETURNED,
    RETURNED_TO_SOURCE,
    SHORTFALL_CLOSED,
)


@dataclass
class ShipmentAccount:
    """One shipment and every later fact about it, read off the RTV's events.

    ``acknowledged`` is the latest acknowledgement snapshot per line (a
    snapshot, never a sum), ``None`` until the vendor has acknowledged
    anything. ``returned`` sums the pieces recorded back at the source, and
    ``closed`` the pieces whose shortfall the Owner's approved closure
    recognised (goods ticket 15H). The rest of what left - neither acknowledged,
    back nor closed - is ``unaccounted``. ``proposals`` are prepared closures;
    they account for nothing until approved.
    """

    shipment: RtvEvent
    shipped: dict[str, int]
    acknowledged: dict[str, int] | None
    returned: dict[str, int]
    acknowledgements: list[RtvEvent]
    returns: list[RtvEvent]
    putaways: list[RtvEvent]
    eway: list[RtvEvent]
    closed: dict[str, int] = dataclasses.field(default_factory=dict)
    closures: list[RtvEvent] = dataclasses.field(default_factory=list)
    proposals: list[RtvEvent] = dataclasses.field(default_factory=list)

    @property
    def shipped_qty(self) -> int:
        return sum(self.shipped.values())

    @property
    def acknowledged_qty(self) -> int:
        return sum((self.acknowledged or {}).values())

    @property
    def returned_qty(self) -> int:
        return sum(self.returned.values())

    @property
    def closed_qty(self) -> int:
        return sum(self.closed.values())

    def unaccounted(self, key: str) -> int:
        return (
            self.shipped.get(key, 0)
            - (self.acknowledged or {}).get(key, 0)
            - self.returned.get(key, 0)
            - self.closed.get(key, 0)
        )

    @property
    def unaccounted_qty(self) -> int:
        return sum(self.unaccounted(key) for key in self.shipped)

    @property
    def accounted(self) -> bool:
        return self.unaccounted_qty == 0

    @property
    def status(self) -> str:
        if not self.accounted:
            return AWAITING_RECEIPT if self.acknowledged is None else SHORT_ACKNOWLEDGED
        if self.closed_qty:
            return SHORTFALL_CLOSED
        if self.acknowledged_qty == self.shipped_qty:
            return DELIVERED
        if self.returned_qty == self.shipped_qty:
            return RETURNED_TO_SOURCE
        return PARTLY_RETURNED

    def take(self, event: RtvEvent) -> None:
        """Add one later fact about this shipment, in recording order."""
        if event.kind == RtvEvent.Kind.ACKNOWLEDGEMENT:
            self.acknowledgements.append(event)
            self.acknowledged = {str(line["line_key"]): int(line["qty"]) for line in event.lines}
        elif event.kind == RtvEvent.Kind.SOURCE_RETURN:
            self.returns.append(event)
            _add_lines(self.returned, event)
        elif event.kind == RtvEvent.Kind.PUTAWAY:
            self.putaways.append(event)
        elif event.kind in (RtvEvent.Kind.EWAY_ATTACHED, RtvEvent.Kind.EWAY_VERIFIED):
            self.eway.append(event)
        elif event.kind == RtvEvent.Kind.SHORTFALL_PROPOSAL:
            self.proposals.append(event)
        elif event.kind == RtvEvent.Kind.SHORTFALL_CLOSURE:
            self.closures.append(event)
            _add_lines(self.closed, event)

    def state_hash(self) -> str:
        """What an acknowledgement or a shortfall closure is recorded against.

        The shipment as the reader saw it: every acknowledgement, return and
        approved closure so far. A prepared closure is not in it - it changes
        nothing about what is accounted for.
        """
        from core.canonical import content_hash

        facts: dict[str, Any] = {
            "shipment": str(self.shipment.pk),
            "acknowledgements": [str(e.pk) for e in self.acknowledgements],
            "returns": [str(e.pk) for e in self.returns],
        }
        if self.closures:
            facts["closures"] = [str(e.pk) for e in self.closures]
        return content_hash(facts)


def _add_lines(totals: dict[str, int], event: RtvEvent) -> None:
    for line in event.lines:
        key = str(line["line_key"])
        totals[key] = totals.get(key, 0) + int(line["qty"])


def shipment_accounts(events: Sequence[RtvEvent]) -> list[ShipmentAccount]:
    """Every shipment of an RTV with its acknowledgements, returns, put-aways and e-way facts."""
    accounts: dict[str, ShipmentAccount] = {}
    for event in events:
        if event.kind == RtvEvent.Kind.SHIPMENT:
            accounts[str(event.pk)] = ShipmentAccount(
                shipment=event,
                shipped={str(line["line_key"]): int(line["qty"]) for line in event.lines},
                acknowledged=None,
                returned={},
                acknowledgements=[],
                returns=[],
                putaways=[],
                eway=[],
            )
    for event in events:
        account = accounts.get(str(event.shipment_id)) if event.shipment_id else None
        if account is not None:
            account.take(event)
    return list(accounts.values())


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def detail(movement: GoodsMovement, head: DocumentHead) -> dict[str, Any]:
    """The RTV's status, balances per line, reasons left behind and its physical history."""
    from vendors.models import Vendor

    header: dict[str, Any] = {}
    if head.live_version is not None:
        header = dict(head.live_version.canonical_payload)
        bodies = _official_lines(head.live_version)
    elif head.draft_revision is not None:
        header = dict(head.draft_revision.payload)
        bodies = [
            dict(state.payload)
            for state in revision_lines(head.document_id, head.draft_revision.revision)
        ]
    else:  # pragma: no cover - a movement always has a draft
        bodies = []
    events = _events(movement)
    picked = _moved(events, RtvEvent.Kind.PICKUP)
    withdrawn = _moved(events, RtvEvent.Kind.WITHDRAWAL)
    shipped = _moved(events, RtvEvent.Kind.SHIPMENT)
    accounts = shipment_accounts(events)
    acknowledged: dict[str, int] = {}
    back: dict[str, int] = {}
    closed: dict[str, int] = {}
    unaccounted: dict[str, int] = {}
    for account in accounts:
        for key in account.shipped:
            acknowledged[key] = acknowledged.get(key, 0) + (account.acknowledged or {}).get(key, 0)
            back[key] = back.get(key, 0) + account.returned.get(key, 0)
            closed[key] = closed.get(key, 0) + account.closed.get(key, 0)
            unaccounted[key] = unaccounted.get(key, 0) + account.unaccounted(key)
    approved = head.live_version is not None
    awaiting = _awaiting_withdrawal(events)
    lines = []
    for body in bodies:
        key = str(body["line_key"])
        outstanding = (
            ranges.total([p.interval for p in _reserved_pieces(head.live_version.pk, body)])
            if head.live_version is not None
            else 0
        )
        lines.append(
            {
                "line_key": key,
                "sku_id": body.get("sku_id"),
                "source_pool": body.get("source_pool"),
                "approved_qty": int(body["qty"]) if approved else 0,
                "picked_up_qty": picked.get(key, 0),
                "withdrawn_qty": withdrawn.get(key, 0),
                "outstanding_qty": outstanding,
                "awaiting_withdrawal_qty": min(outstanding, awaiting.get(key, 0)),
                "shipped_qty": shipped.get(key, 0),
                "acknowledged_qty": acknowledged.get(key, 0),
                "returned_to_source_qty": back.get(key, 0),
                "shortfall_closed_qty": closed.get(key, 0),
                "awaiting_receipt_qty": unaccounted.get(key, 0),
            }
        )
    vendor_id = header.get("vendor_id")
    vendor = Vendor.objects.filter(pk=vendor_id).first() if vendor_id else None
    names = _names(e.actor_id for e in events)
    latest = next((e for e in reversed(events) if e.kind == RtvEvent.Kind.PICKUP), None)
    totals = (
        "picked_up_qty",
        "withdrawn_qty",
        "outstanding_qty",
        "awaiting_withdrawal_qty",
        "shipped_qty",
        "acknowledged_qty",
        "returned_to_source_qty",
        "shortfall_closed_qty",
        "awaiting_receipt_qty",
    )
    return {
        "state": movement.rtv_state,
        "vendor": (
            {"id": str(vendor.pk), "code": vendor.code, "name": vendor.name}
            if vendor is not None
            else None
        ),
        "agreement_reference": header.get("agreement_reference"),
        "approved_qty": sum(line["approved_qty"] for line in lines),
        **{total: sum(line[total] for line in lines) for total in totals},
        "lines": lines,
        "left_behind": list(latest.details.get("left_behind") or []) if latest else [],
        # The RTV's own physical and balance facts. What happened to a shipment
        # afterwards is read under that shipment (``shipments``).
        "events": [_event_dto(event, names) for event in events if event.kind in RTV_LEVEL_KINDS],
    }


#: The events an RTV lists as its own; later facts about a shipment list under it.
RTV_LEVEL_KINDS = (RtvEvent.Kind.PICKUP, RtvEvent.Kind.WITHDRAWAL, RtvEvent.Kind.SHIPMENT)


def _awaiting_withdrawal(events: Sequence[RtvEvent]) -> dict[str, int]:
    """Pieces the latest pickup said will not be collected, less what was withdrawn since."""
    latest = next(
        (i for i in range(len(events) - 1, -1, -1) if events[i].kind == RtvEvent.Kind.PICKUP),
        None,
    )
    if latest is None:
        return {}
    out: dict[str, int] = {}
    for reason in events[latest].details.get("left_behind") or []:
        if not reason.get("further_pickup_expected"):
            key = str(reason["line_key"])
            out[key] = out.get(key, 0) + int(reason["qty"])
    for key, qty in _moved(events[latest + 1 :], RtvEvent.Kind.WITHDRAWAL).items():
        out[key] = max(0, out.get(key, 0) - qty)
    return out


def _names(ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    from accounts.goods_models import HumanIdentity

    wanted = sorted({i for i in ids if i}, key=str)
    return dict(HumanIdentity.objects.filter(pk__in=wanted).values_list("pk", "display_name"))


def _event_dto(event: RtvEvent, names: Mapping[uuid.UUID, str]) -> dict[str, Any]:
    return {
        "id": str(event.pk),
        "kind": event.kind,
        "sequence_no": event.sequence_no,
        "quantity": event.quantity,
        "event_at": event.event_at.isoformat(),
        "recorded_at": event.recorded_at.isoformat() if event.recorded_at else None,
        "actor": {"id": str(event.actor_id), "name": names.get(event.actor_id, "")}
        if event.actor_id
        else None,
        "lines": [{"line_key": line["line_key"], "qty": line["qty"]} for line in event.lines],
        "details": dict(event.details),
    }


def allowed_actions(access: Any, movement: GoodsMovement, head: DocumentHead) -> list[str]:
    """What this reader may do to this RTV now. No action is gained through a link."""
    site_id = movement.document.site_id
    actions: list[str] = []
    if head.state == DocumentHead.State.DRAFT and access.can(DRAFT_ACTION, site_id=site_id):
        actions.append("submit")
    if head.state == DocumentHead.State.SUBMITTED and access.can(APPROVE_ACTION, site_id=site_id):
        actions.append("decide")
    if (
        movement.rtv_state == INITIATED
        and head.live_version is not None
        # A balance still waits to leave. An RTV kept initiated only by a
        # shipment awaiting vendor receipt has nothing left to collect, send or
        # withdraw; its shipment's own actions are offered under it (15F).
        and ActiveReservation.objects.filter(transfer_version_id=head.live_version.pk).exists()
    ):
        if access.can(EXECUTE_ACTION, site_id=site_id):
            actions.extend(["pickup", "ship"])
        if any(access.can(action, site_id=site_id) for action in WITHDRAW_ACTIONS):
            actions.append("withdraw")
    return actions
