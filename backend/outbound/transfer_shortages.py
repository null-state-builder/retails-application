"""Resolving a counted shipment's shortage, and reconciling each shipment (goods ticket 14).

Transfers PRD §5 and §7, goods PRD §14.9.2, GSA-T14 and GSA-R01. OPS-06 made the
destination count a whole shipment and leave what was missing in transit as an
explicit, owned shortage. What was still open, and lives here:

* **A shortage is proposed with its evidence, never assumed.** The destination
  names the exact missing pieces of one counted shipment - per line, how many -
  and gives a reason, supporting evidence and the follow-up it recorded with
  the source or the transporter (GSA-T14). The proposal references the
  shipment's own recorded check (its count). No sign-off from the source is
  asked for: the distinct checker is the only approval gate.
* **Pending approval, the pieces stay in transit.** A proposal freezes the
  exact ranges of the original shipment that it names, and nothing else; they
  remain visibly on the road, unresolved, until a decision.
* **The Owner decides, as a different person (GSA-R01).** Approval numbers the
  GAP document and posts P13 against exactly the frozen ranges - out of transit
  to the ``consumed`` boundary, once - so the same pieces can never also be
  counted as still in transit. No value leg is written and no loss posting is
  prescribed: this is a quantity correction, not a financial one (transfers PRD
  §5). Rejection keeps the history and leaves the pieces in transit; a fresh
  proposal may follow.
* **Each shipment reconciles on its own.** What arrived good, what arrived held,
  what came back, the explicitly resolved shortage and what is still in transit
  add up to what left; excess is beside it, never inside it. The movement's
  undispatched balance - reserved or cancelled - reconciles separately against
  what was approved; the old single reserved-quantity equation is not used.

Lock order, as in ``outbound.transfers``: SITE → DOCUMENT (transfer, dispatch,
gap row, GAP head) → LOT → SERIES.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from alerts.goods_services import open_exception, resolve_exceptions
from core.commands import CommandRun, LockRank
from core.goods_documents import append_revision, lock_heads, new_document, officialise, set_state
from core.goods_fields import bounds
from core.kernel_models import DocumentHead, DocumentIdentity
from core.numbering import allocate
from core.refusals import Refusal, issue
from outbound import transfers
from outbound.goods_models import GapResolution, GoodsTransfer, TransferDispatch, TransferEvent
from stockledger import goods_engine as engine
from stockledger import ranges

#: Proposing is the destination's physical-step grant, where the shipment was counted.
PROPOSE_ACTION = transfers.MOVE_ACTION
#: Deciding is the Owner's (GSA-R01): the same grant that approves the movement,
#: at its source, with a fresh password confirmation and never by the proposer.
DECIDE_ACTION = transfers.APPROVE_ACTION

GAP_DOC_TYPE = "GAP"
#: Design §7.2: transit shortage is P13 - named transfer transit to a documented
#: loss boundary, requiring the original dispatched allocation and only its
#: remaining range.
SHORTAGE_POSTING = "P13"
SHORTAGE_BOUNDARY = "consumed"
SHORTAGE_REASON = "transit_shortage"

DECISIONS = ("approve", "reject")
APPROVAL_REASON = "SHORTAGE_APPROVAL"

PROPOSE_FIELDS = frozenset(
    {"lines", "reason", "evidence_reference", "followup_note", "recount_note", "pairing_key"}
)
PROPOSE_REQUIRED = ("lines", "reason", "evidence_reference", "followup_note")
PROPOSE_LINE_FIELDS = frozenset({"line_key", "qty"})
DECIDE_FIELDS = frozenset({"decision", "reason"})
DECIDE_REQUIRED = ("decision", "reason")


@dataclass(frozen=True)
class Proposal:
    lines: tuple[tuple[uuid.UUID, int], ...]
    reason: str
    evidence_reference: str
    followup_note: str
    recount_note: str | None
    #: Goods ticket 16: the wrong-goods observation whose short-expected half
    #: this proposal is - its excess-observed half is a corrective decision.
    pairing_key: uuid.UUID | None = None


def parse_proposal(body: dict[str, Any]) -> Proposal:
    transfers._closed(body, PROPOSE_FIELDS)
    raw = body.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= transfers.MAX_LINES:
        raise transfers._invalid(f"A shortage names 1 to {transfers.MAX_LINES} lines.", "lines")
    lines: list[tuple[uuid.UUID, int]] = []
    seen: set[uuid.UUID] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise transfers._invalid("Every shortage line is an object.", "lines")
        transfers._closed(item, PROPOSE_LINE_FIELDS)
        key = transfers._uuid(item.get("line_key"), "line_key")
        if key in seen:
            raise transfers._invalid("Every shortage line names a different shipped line.", "lines")
        seen.add(key)
        lines.append((key, transfers._qty(item.get("qty"), "qty")))

    def required(name: str, limit: int) -> str:
        value = transfers._text(body.get(name), name, limit=limit)
        assert value is not None
        return value

    return Proposal(
        lines=tuple(lines),
        reason=required("reason", 500),
        evidence_reference=required("evidence_reference", 100),
        followup_note=required("followup_note", 1000),
        recount_note=transfers._text(
            body.get("recount_note"), "recount_note", limit=500, required=False
        ),
        pairing_key=transfers._optional_uuid(body.get("pairing_key"), "pairing_key"),
    )


def parse_decision(body: dict[str, Any]) -> tuple[str, str]:
    transfers._closed(body, DECIDE_FIELDS)
    decision = body.get("decision")
    if decision not in DECISIONS:
        raise transfers._invalid("decision is approve or reject.", "decision")
    reason = transfers._text(body.get("reason"), "reason", limit=500)
    assert reason is not None
    return str(decision), reason


# ---------------------------------------------------------------------------
# Reading a shipment's shortage
# ---------------------------------------------------------------------------


def _short(record: TransferDispatch) -> int:
    return int((record.count or {}).get("short_total") or 0)


#: Only shortage decisions resolve what never arrived; a corrective (excess)
#: decision on the same shipment is about goods nobody sent (goods ticket 16).
SHORT = GapResolution.Kind.SHORT


def _quantity(record: TransferDispatch, state: str) -> int:
    return sum(
        GapResolution.objects.filter(dispatch=record, kind=SHORT, state=state).values_list(
            "quantity", flat=True
        )
    )


def resolved_qty(record: TransferDispatch) -> int:
    """Missing pieces of this shipment an approved shortage has taken out of transit."""
    return _quantity(record, GapResolution.State.APPROVED)


def pending_qty(record: TransferDispatch) -> int:
    """Missing pieces named by a proposal still waiting for its decision."""
    return _quantity(record, GapResolution.State.PENDING)


def unresolved_qty(record: TransferDispatch) -> int:
    """Missing pieces still in transit: the count's shortage less what was resolved."""
    return max(0, _short(record) - resolved_qty(record))


def _claimed(record: TransferDispatch) -> dict[uuid.UUID, list[ranges.Interval]]:
    """The exact ranges an open or approved proposal already names, by lot."""
    out: dict[uuid.UUID, list[ranges.Interval]] = {}
    for payload in GapResolution.objects.filter(
        dispatch=record,
        kind=SHORT,
        state__in=(GapResolution.State.PENDING, GapResolution.State.APPROVED),
    ).values_list("payload", flat=True):
        for line in payload.get("lines", []):
            for piece in line["portions"]:
                out.setdefault(transfers._lot(piece), []).append(transfers._interval(piece))
    return out


def _unclaimed(
    record: TransferDispatch, line: dict[str, Any], claimed: dict[uuid.UUID, list[ranges.Interval]]
) -> list[transfers.FrozenPortion]:
    """This line's pieces still in transit under the transfer and named by no proposal."""
    out: list[transfers.FrozenPortion] = []
    for piece in transfers._transit_pieces(record, line):
        for left in ranges.subtract([piece.interval], claimed.get(piece.lot_id, [])):
            out.append(replace(piece, interval=left))
    return out


# ---------------------------------------------------------------------------
# Proposing a shortage
# ---------------------------------------------------------------------------


def _already_resolved(message: str, line_key: uuid.UUID, still: int) -> Refusal:
    return Refusal(
        "GAP_ALREADY_RESOLVED",
        message,
        status=409,
        issues=[issue("GAP_ALREADY_RESOLVED", message, line_key=line_key, quantity=still)],
    )


def propose(
    run: CommandRun, transfer_id: uuid.UUID, dispatch_id: uuid.UUID, proposal: Proposal
) -> GapResolution:
    """Name the missing pieces of one counted shipment as a transit shortage, for decision.

    Moves nothing: the pieces stay in transit, visibly, until a different
    person approves. Fenced by the destination only, where the check happened.
    """
    transfer, _head = transfers._locked(run, transfer_id, at="destination")
    record = transfers._locked_dispatch(run, transfer, dispatch_id)
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.destination_site_id
    if record.state not in (TransferDispatch.State.COUNTED, TransferDispatch.State.ACCEPTED):
        raise transfers._state_conflict(
            "Only a shipment the destination has counted can have a shortage resolved. "
            "Until then nothing is known to be missing."
        )
    check = (
        TransferEvent.objects.filter(
            transfer=transfer, kind=TransferEvent.Kind.CHECK, details__dispatch_id=str(record.pk)
        )
        .order_by("recorded_at")
        .first()
    )
    if check is None:
        raise transfers._state_conflict("This shipment has no recorded destination check.")
    by_key = {uuid.UUID(str(line["line_key"])): line for line in record.lines}
    if proposal.pairing_key is not None:
        _check_pairing(record, proposal)
    claimed = _claimed(record)
    bodies: list[tuple[uuid.UUID, Mapping[str, Any]]] = []
    for line_key, qty in proposal.lines:
        line = by_key.get(line_key)
        if line is None:
            raise Refusal("NOT_FOUND", "That line is not on this shipment.")
        pool = _unclaimed(record, line, claimed)
        still = sum(ranges.length(piece.interval) for piece in pool)
        if qty > still:
            raise _already_resolved(
                f"Only {still} missing piece(s) of that line are not already named by a "
                f"shortage proposal or resolution, and this names {qty}.",
                line_key,
                still,
            )
        taken = transfers._take(pool, qty)
        bodies.append(
            (
                line_key,
                {
                    "line_key": str(line_key),
                    "sku_id": line.get("sku_id"),
                    "qty": qty,
                    "portions": [
                        {
                            "lot_id": str(piece.lot_id),
                            "lower": piece.interval[0],
                            "upper": piece.interval[1],
                            "origin_id": str(piece.origin_id) if piece.origin_id else None,
                        }
                        for piece in taken
                    ],
                },
            )
        )
    identity, head = new_document(
        run,
        kind=GAP_DOC_TYPE,
        purpose=DocumentIdentity.Purpose.GAP_RESOLUTION,
        entity_id=transfer.document.entity_id,
        site_id=transfer.destination_site_id,
    )
    header = {
        "kind": GapResolution.Kind.SHORT.value,
        "resolution": SHORTAGE_REASON,
        "transfer_id": str(transfer.pk),
        "dispatch_id": str(record.pk),
        "check_event_id": str(check.pk),
        "reason": proposal.reason,
        "evidence_reference": proposal.evidence_reference,
        "followup_note": proposal.followup_note,
        "recount_note": proposal.recount_note,
        "pairing_key": str(proposal.pairing_key) if proposal.pairing_key else None,
    }
    append_revision(run, head, header=header, replace_lines=list(bodies))
    set_state(run, head, DocumentHead.State.SUBMITTED, event="submitted")
    assert run.principal.human_id is not None
    quantity = sum(qty for _key, qty in proposal.lines)
    gap = GapResolution.objects.create(
        tenant_id=run.tenant_id,
        document=identity,
        transfer=transfer,
        kind=GapResolution.Kind.SHORT,
        pairing_key=proposal.pairing_key,
        payload={**header, "lines": [dict(body) for _key, body in bodies]},
        dispatch=record,
        state=GapResolution.State.PENDING,
        quantity=quantity,
        proposed_by_id=run.principal.human_id,
        proposed_at=run.now,
    )
    open_exception(
        run,
        kind=transfers.APPROVAL_EXCEPTION,
        site_id=transfer.destination_site_id,
        subject_key=f"gap:{gap.pk}",
        reason_code=APPROVAL_REASON,
        source_event_key=engine.event_key("gap_approval", identity.pk),
        allowed_resolution_actions=transfers.RESOLUTION_ACTIONS,
        note=f"transfer:{transfer.pk}",
    )
    transfers._log(
        run,
        transfer,
        TransferEvent.Kind.SHORTAGE_PROPOSED,
        site_id=transfer.destination_site_id,
        details={
            "dispatch_id": str(record.pk),
            "sequence_no": record.sequence_no,
            "gap_id": str(gap.pk),
            "quantity": quantity,
            "reason": proposal.reason,
            "evidence_reference": proposal.evidence_reference,
            "followup_note": proposal.followup_note,
            "check_event_id": str(check.pk),
        },
    )
    run.audit_after = {"dispatch_id": str(record.pk), "gap_id": str(gap.pk), "quantity": quantity}
    return gap


def _check_pairing(record: TransferDispatch, proposal: Proposal) -> None:
    """The short-expected half of a wrong-goods pair names exactly its expected pieces.

    Goods ticket 16 (design §7.3): wrong goods record separate short-expected
    and excess-observed decisions sharing one pairing key. This half names the
    line the wrong goods came in place of, for exactly as many pieces, once.
    """
    from outbound.transfer_excess import entries_of

    pair = next(
        (e for e in entries_of(record) if e.get("pairing_key") == str(proposal.pairing_key)),
        None,
    )
    if pair is None:
        raise Refusal("NOT_FOUND", "That wrong-goods observation is not on this shipment.")
    wanted = (uuid.UUID(str(pair["in_place_of_line_key"])), int(pair["qty"]))
    if list(proposal.lines) != [wanted]:
        raise transfers._refuse(
            f"The wrong goods came in place of {wanted[1]} piece(s) of one line; the "
            "short-expected half of that pair names exactly those, on that line."
        )
    if GapResolution.objects.filter(
        dispatch=record,
        kind=SHORT,
        pairing_key=proposal.pairing_key,
        state__in=(GapResolution.State.PENDING, GapResolution.State.APPROVED),
    ).exists():
        raise _already_resolved(
            "The short-expected half of this wrong-goods pair is already proposed or resolved.",
            wanted[0],
            0,
        )


# ---------------------------------------------------------------------------
# Deciding a shortage
# ---------------------------------------------------------------------------


def decide(
    run: CommandRun, transfer_id: uuid.UUID, gap_id: uuid.UUID, decision: str, reason: str
) -> GapResolution:
    """Approve or reject one pending shortage proposal, as a different person."""
    known = GapResolution.objects.filter(
        tenant_id=run.tenant_id, pk=gap_id, transfer_id=transfer_id, kind=SHORT
    ).first()
    if known is None or known.dispatch_id is None:
        raise Refusal("NOT_FOUND", "That shortage proposal was not found.")
    transfer, _head = transfers._locked(run, transfer_id, at="destination")
    record = transfers._locked_dispatch(run, transfer, known.dispatch_id)
    rows: list[GapResolution] = run.lock(LockRank.DOCUMENT, GapResolution.objects.filter(pk=gap_id))
    gap = rows[0]
    head = lock_heads(run, [gap.document_id])[gap.document_id]
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.destination_site_id
    if gap.state != GapResolution.State.PENDING:
        raise transfers._state_conflict(f"This shortage proposal was already {gap.state}.")
    if run.principal.human_id is None or str(run.principal.human_id) == str(gap.proposed_by_id):
        raise Refusal(
            "SELF_APPROVAL",
            "Whoever proposed this shortage cannot also decide it. "
            "A different authorised person approves or rejects it.",
        )
    gap.decided_by_id = run.principal.human_id
    gap.decided_at = run.now
    gap.decision_reason = reason
    if decision == "reject":
        gap.state = GapResolution.State.REJECTED
        gap.save(update_fields=["state", "decided_by", "decided_at", "decision_reason"])
        set_state(run, head, DocumentHead.State.REVERSED, event="rejected", reason_code=reason[:60])
        kind = TransferEvent.Kind.SHORTAGE_REJECTED
    else:
        _post_shortage(run, transfer, record, gap, head)
        gap.state = GapResolution.State.APPROVED
        gap.save(update_fields=["state", "decided_by", "decided_at", "decision_reason"])
        kind = TransferEvent.Kind.SHORTAGE_RESOLVED
    resolve_exceptions(
        run,
        kind=transfers.APPROVAL_EXCEPTION,
        subject_key=f"gap:{gap.pk}",
        reason_code="SHORTAGE_APPROVED" if decision == "approve" else "SHORTAGE_REJECTED",
    )
    still = unresolved_qty(record)
    if decision == "approve" and still == 0:
        resolve_exceptions(
            run,
            kind=transfers.SHORTAGE_EXCEPTION,
            subject_key=f"transfer_dispatch:{record.pk}",
            reason_code="SHORTAGE_RESOLVED",
            source_event_key=engine.event_key("transfer_shortage", record.pk),
        )
    gap.document.refresh_from_db()
    transfers._log(
        run,
        transfer,
        kind,
        site_id=transfer.destination_site_id,
        details={
            "dispatch_id": str(record.pk),
            "sequence_no": record.sequence_no,
            "gap_id": str(gap.pk),
            "gap_number": gap.document.official_number,
            "quantity": gap.quantity,
            "reason": reason,
            "still_in_transit": still,
        },
    )
    if decision == "approve":
        transfers._close_if_settled(run, transfer, record)
    run.audit_after = {"gap_id": str(gap.pk), "state": gap.state, "still_in_transit": still}
    return gap


def _post_shortage(
    run: CommandRun,
    transfer: GoodsTransfer,
    record: TransferDispatch,
    gap: GapResolution,
    head: DocumentHead,
) -> None:
    """Number the GAP document and take exactly its frozen ranges out of transit (P13)."""
    lines = list(gap.payload.get("lines", []))
    pieces = [
        (transfers._lot(piece), transfers._interval(piece))
        for line in lines
        for piece in line["portions"]
    ]
    engine.lock_lots(run, sorted({lot for lot, _i in pieces}, key=str))
    for lot_id, interval in pieces:
        standing = [
            bounds(p.portion)
            for p in engine.positions_of(lot_id, interval)
            if p.boundary == "transit" and p.transfer_id == transfer.pk
        ]
        if ranges.total(ranges.intersect(standing, [interval])) != ranges.length(interval):
            raise transfers._state_conflict(
                "Some of the pieces this proposal names are no longer in transit under "
                "this transfer, so it cannot be approved. Reject it and propose again."
            )
    header = {key: value for key, value in gap.payload.items() if key != "lines"}
    assert run.principal.human_id is not None
    version, _official = officialise(
        run,
        head,
        approved_by_id=run.principal.human_id,
        canonical_header=header,
        lines=[(uuid.UUID(str(line["line_key"])), line) for line in lines],
        authority=run.authority,
        number=allocate(run, transfer.document.entity, GAP_DOC_TYPE),
    )
    plan = engine.Plan(SHORTAGE_POSTING, version.pk, engine.event_key(SHORTAGE_REASON, gap.pk))
    for lot_id, interval in pieces:
        engine.end_positions(run, plan, lot_id, interval, SHORTAGE_BOUNDARY, SHORTAGE_REASON)
    engine.post(run, version, plan)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def shortage_actions(
    access: Any, transfer: GoodsTransfer, records: list[TransferDispatch]
) -> list[str]:
    """Proposing at the destination while a counted shortage is unnamed; deciding, to the Owner."""
    out: list[str] = []
    counted = [
        r
        for r in records
        if r.state in (TransferDispatch.State.COUNTED, TransferDispatch.State.ACCEPTED)
        and _short(r) > 0
    ]
    if any(_short(r) - resolved_qty(r) - pending_qty(r) > 0 for r in counted) and access.can(
        PROPOSE_ACTION, site_id=transfer.destination_site_id
    ):
        out.append("propose_shortage")
    if GapResolution.objects.filter(
        transfer=transfer, kind=SHORT, state=GapResolution.State.PENDING
    ).exists() and access.can(DECIDE_ACTION, site_id=transfer.source_site_id):
        out.append("decide_shortage")
    return out


def people_of(transfer: GoodsTransfer) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for proposer, decider in GapResolution.objects.filter(transfer=transfer).values_list(
        "proposed_by_id", "decided_by_id"
    ):
        ids.extend(i for i in (proposer, decider) if i)
    return ids


def _person(pk: uuid.UUID | None, names: dict[uuid.UUID, str]) -> dict[str, str] | None:
    return {"id": str(pk), "name": names.get(pk, "")} if pk else None


def gap_dto(gap: GapResolution, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    payload = gap.payload
    return {
        "id": str(gap.pk),
        "number": gap.document.official_number,
        "state": gap.state,
        "kind": gap.kind,
        "resolution": payload.get("resolution"),
        "quantity": gap.quantity,
        "check_event_id": payload.get("check_event_id"),
        "reason": payload.get("reason"),
        "evidence_reference": payload.get("evidence_reference"),
        "followup_note": payload.get("followup_note"),
        "recount_note": payload.get("recount_note"),
        "pairing_key": str(gap.pairing_key) if gap.pairing_key else None,
        "lines": [
            {"line_key": line["line_key"], "qty": line["qty"], "portions": line["portions"]}
            for line in payload.get("lines", [])
        ],
        "proposed_by": _person(gap.proposed_by_id, names),
        "proposed_at": gap.proposed_at.isoformat() if gap.proposed_at else None,
        "decided_by": _person(gap.decided_by_id, names),
        "decided_at": gap.decided_at.isoformat() if gap.decided_at else None,
        "decision_reason": gap.decision_reason,
    }


def shipment_reading(record: TransferDispatch, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    """One shipment's shortage and its conservation, as the transfer read sends them."""
    count = record.count or {}
    short = _short(record)
    resolved = resolved_qty(record)
    pending = pending_qty(record)
    dispatched = transfers.shipped_qty(record)
    good = int(count.get("good_total") or 0)
    held = int(count.get("held_total") or 0)
    returned = int(record.returned_qty)
    in_transit = transfers.on_the_road(record)
    gaps = GapResolution.objects.select_related("document").filter(dispatch=record, kind=SHORT)
    return {
        "shortage": {
            "short": short,
            "resolved": resolved,
            "pending_approval": pending,
            "unresolved": max(0, short - resolved),
            "unclaimed": max(0, short - resolved - pending),
        },
        "shortage_resolutions": [gap_dto(gap, names) for gap in gaps.order_by("proposed_at", "id")],
        "reconciliation": {
            "dispatched": dispatched,
            "received_good": good,
            "received_held": held,
            "returned": returned,
            "shortage_resolved": resolved,
            "in_transit": in_transit,
            "in_transit_pending_approval": pending,
            "excess": sum(int(e.get("qty") or 0) for e in count.get("excess") or []),
            "balanced": dispatched == good + held + returned + resolved + in_transit,
        },
    }


def movement_reconciliation(
    *, approved: int, dispatched: int, reserved: int, cancelled: int
) -> dict[str, Any]:
    """The movement's undispatched balance: approved = dispatched + still reserved + cancelled."""
    return {
        "approved": approved,
        "dispatched": dispatched,
        "reserved": reserved,
        "cancelled": cancelled,
        "balanced": approved == 0 or approved == dispatched + reserved + cancelled,
    }
