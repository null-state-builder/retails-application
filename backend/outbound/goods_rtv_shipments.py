"""Goods-v1 shipped RTV and vendor receipt (goods ticket 15F).

Transfers PRD §§4, 6-8; quarantine outcomes PRD §§4-5; goods PRD §14.9.2 and
§14.10 GSA-R01 (the site records the physical steps; the Owner verifies) and
GSA-R04; GSA-T15 (partial vendor acknowledgement). A **shipment** is one
dispatch of an approved RTV for delivery to the vendor. Read it as five
separate facts, each kept apart from the others:

* **Departure is not vendor receipt.** A shipment leaves exactly as a pickup
  does (``goods_rtv.depart``: the reservation is consumed, the pieces go to the
  ``returned`` boundary, value leaves stock for external at each portion's own
  recorded cost, and the holds over exactly those pieces end). The rest of the
  approval stays reserved until another departure or an explicit withdrawal.
  Until the shipment is accounted for, the RTV stays ``initiated`` - even when
  no balance is reserved any more - and owned ``rtv_receipt_pending`` work
  follows it up (a 14-day reminder, never a deadline).
* **An acknowledgement posts nothing.** It is one evidence-backed, cumulative
  snapshot of what the vendor says it received, per line, recorded against the
  shipment as the recorder saw it (``reviewed_hash``). The latest snapshot
  counts; repeating one never adds. A shortfall keeps the original departure,
  restores no stock, writes nothing off, raises no credit and leaves the
  shipment and the RTV unconfirmed: it is an owned
  ``rtv_acknowledgement_discrepancy`` (the Owner's, 30-day follow-up - a
  reminder, never an automatic close).
* **A persistent shortfall is closed only by the Owner** (goods ticket 15H,
  goods PRD §14.10 GSA-R07). The site prepares the closure of one
  short-acknowledged shipment with a reason and evidence (a vendor letter, a
  photo or a note); it freezes exactly the unacknowledged pieces and their
  value at recorded layer cost - unknown for unvalued custody, never zero -
  and moves nothing. The Owner, a different person, approves it under the
  RTV approval policy: exactly those pieces leave the ``returned`` boundary
  for ``consumed`` as a recognised shortfall (P13, once), the difference's
  owned work closes and the RTV may close. The value already left stock at
  departure at that same recorded cost, so no second value leg is written.
  Reasons for pieces left behind, a withdrawal of goods that never left, a
  return without receipt evidence, or the follow-up date passing settle
  nothing.
* **Goods that actually come back are received.** After a failed delivery the
  source records what is physically back, per line and condition, with its
  evidence. Only those pieces come ashore (P15's departure leg in reverse, at
  unchanged value); the departure and the failed intended delivery stay.
  Pieces that left held come back under the same kinds of hold, in
  quarantine; pieces found damaged are quarantined under a damage hold with a
  pending damage report; good pieces that left good wait in the source's
  receiving until somebody puts them away (``putaway``). Whatever did not come
  back stays unaccounted and visible.
* **Documentation is not physical completion.** Whether an e-way reference
  left with the shipment is fixed at departure; one missing opens owned
  ``eway_missing`` work. Attaching one later and verifying it are separate
  facts; completing the shipment closes none of them.
* **No books effect.** No command here writes a GL, vendor, cash, payable,
  credit-note or tax entry.

Lock order: SITE (the site guard, for the stock-moving steps only) ->
DOCUMENT (the movement head, then any pending damage report over its lots) ->
LOT.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from alerts.goods_services import open_exception, resolve_exceptions
from approvals.goods_policy import Amounts, pin
from approvals.goods_services import (
    DecisionContext,
    create_request,
    register_subject_handler,
    supersede_pending,
)
from core.canonical import content_hash
from core.commands import CommandRun
from core.goods_documents import lock_heads, record_event
from core.goods_fields import bounds, portion
from core.kernel_models import DocumentHead, OfficialVersion
from core.operational import ValuePair
from core.refusals import Refusal, issue
from outbound import damage_review
from outbound import goods_adjustments as adjustments
from outbound import goods_movements as movements
from outbound import goods_rtv as rtv
from outbound.goods_models import GoodsMovement, RtvEvent
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import AcceptanceEvent, AcceptanceSession, Position

SHIP_FIELDS = frozenset(
    {
        "shipped_at",
        "carrier",
        "evidence_reference",
        "evidence_ids",
        "evidence_note",
        "eway_reference",
        "lines",
    }
)
ACK_FIELDS = frozenset(
    {
        "acknowledged_at",
        "recipient_reference",
        "evidence_ids",
        "evidence_note",
        "reviewed_hash",
        "lines",
    }
)
ACK_LINE_FIELDS = frozenset({"line_key", "qty"})
RETURN_FIELDS = frozenset(
    {"returned_at", "reason", "evidence_reference", "evidence_ids", "evidence_note", "lines"}
)
RETURN_LINE_FIELDS = frozenset({"line_key", "good", "damaged"})
PUTAWAY_FIELDS = frozenset({"lines"})
PUTAWAY_LINE_FIELDS = frozenset({"line_key", "qty", "destination_location_id"})
EWAY_FIELDS = frozenset({"action", "reference", "note"})
CLOSURE_FIELDS = frozenset(
    {"reason", "evidence_reference", "evidence_ids", "evidence_note", "reviewed_hash"}
)
EWAY_ACTIONS = ("attach", "verify")

MAX_CARRIER = 120
MAX_REASON = 500

#: The site records every physical step and the vendor's paperwork (GSA-R01).
EXECUTE_ACTION = rtv.EXECUTE_ACTION
#: Putting returned goods away is acceptance at the source (as a transfer's
#: returned goods are, goods ticket 13C).
ACCEPT_ACTION = "stock.accept"
#: Verifying movement-document evidence is the higher authority's: the Owner
#: approves RTVs (GSA-R01), as the transfer approver verifies a transfer's.
VERIFY_ACTION = movements.APPROVE_ACTION

#: Preparing a shortfall closure is the site's, as drafting any movement for the
#: Owner's approval is (GSA-R01); approving it is the Owner's, under the RTV
#: approval policy (purpose ``rtv``, Anand's 15B decision 1).
PREPARE_CLOSURE_ACTION = movements.DRAFT_ACTION
CLOSURE_APPROVE_ACTION = rtv.APPROVE_ACTION
CLOSURE_PURPOSE = rtv.KIND
#: The approval subject: one shipment's shortfall (subject key: the shipment id).
CLOSURE_SUBJECT = "rtv_shortfall"
#: Design §7.2 P13: a documented loss boundary, as an adjustment or a transit
#: shortage. Quantity only - the value left stock when the goods departed.
CLOSURE_POSTING = "P13"
SHORTFALL_BOUNDARY = "consumed"
SHORTFALL_REASON = "rtv_shortfall"
VALUE_BASIS = "recorded_layer_cost"
#: The field grant that shows money over the stock read (the stock-value rule).
VALUE_READ = "stock.view"

RECEIPT_EXCEPTION = "rtv_receipt_pending"
DISCREPANCY_EXCEPTION = "rtv_acknowledgement_discrepancy"
EWAY_EXCEPTION = "eway_missing"
RESOLUTION_ACTIONS = rtv.RESOLUTION_ACTIONS
#: Acceptance and putaway of returned goods (design §7 P09).
PUTAWAY_POSTING = "P09"
RETURN_DAMAGE_REASON = "RTV_RETURN_DAMAGE"
DAMAGE_HOLD = "damage"


def _invalid(message: str, field: str) -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field)])


def _refuse(message: str, code: str, **extra: Any) -> Refusal:
    return Refusal("MOVEMENT_INVALID", message, status=422, issues=[issue(code, message, **extra)])


def _confirm_invalid(message: str, code: str, **extra: Any) -> Refusal:
    return Refusal(
        "RTV_CONFIRM_INVALID", message, status=422, issues=[issue(code, message, **extra)]
    )


def _count(value: Any, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= movements.MAX_QTY:
        raise _invalid(f"{field} is a whole number from 0 to {movements.MAX_QTY}.", field)
    return value


def _evidence(
    reference: str | None, ids: Sequence[uuid.UUID], note: str | None, field: str
) -> None:
    """Anand's 15B decision 5, applied to every shipped-RTV fact: a reference, a file or a note."""
    if not (reference or note or ids):
        raise _refuse(
            "Record the evidence: a reference (challan, docket or the vendor's receipt), a "
            "photo, or a note.",
            "EVIDENCE_REQUIRED",
            field=field,
        )


def _subject(movement: GoodsMovement) -> str:
    return f"movement:{movement.document_id}"


def _note(shipment: RtvEvent) -> str:
    return f"rtv_shipment:{shipment.pk}"


# ---------------------------------------------------------------------------
# Shipment (E156, adapted): one bounded dispatch for delivery to the vendor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shipment:
    shipped_at: datetime
    carrier: str
    evidence_reference: str | None
    evidence_ids: tuple[uuid.UUID, ...]
    evidence_note: str | None
    eway_reference: str | None
    lines: tuple[tuple[uuid.UUID, int], ...]


def parse_shipment(body: dict[str, Any], now: datetime) -> Shipment:
    from outbound.transfers import _moment

    rtv._closed(body, SHIP_FIELDS)
    carrier = rtv._text(body.get("carrier"), "carrier", MAX_CARRIER)
    if not carrier:
        raise _invalid(
            "carrier names the transporter or courier the goods were handed to.", "carrier"
        )
    reference = rtv._text(body.get("evidence_reference"), "evidence_reference", rtv.MAX_REFERENCE)
    note = rtv._text(body.get("evidence_note"), "evidence_note", rtv.MAX_NOTE)
    evidence = rtv._evidence_ids(body.get("evidence_ids"))
    _evidence(reference, evidence, note, "evidence_reference")
    return Shipment(
        shipped_at=_moment(body.get("shipped_at"), "shipped_at", now),
        carrier=carrier,
        evidence_reference=reference,
        evidence_ids=evidence,
        evidence_note=note,
        eway_reference=rtv._text(body.get("eway_reference"), "eway_reference", rtv.MAX_REFERENCE),
        lines=rtv._quantities(body.get("lines"), "lines"),
    )


def ship(run: CommandRun, document_id: uuid.UUID, parsed: Shipment) -> RtvEvent:
    """One shipment leaves for the vendor: exactly its pieces depart; nothing is received yet."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A shipment is recorded by a named person.")
    locked = rtv._lock(run, document_id, reports=True)
    site_id = locked.movement.document.held_site_id
    rtv._check_lines(locked, parsed.lines)
    rtv._evidence_exist(run, parsed.evidence_ids)
    departure = rtv.depart(run, locked, parsed.lines, event_name="rtv_shipment")
    # Anand's 15B decision 3, applied to a shipment: the goods have left for the
    # vendor, so a pending report whose every piece went with them is closed.
    closed = rtv._close_returned_reports(
        run,
        locked,
        [piece for _key, pieces in departure.left for piece in pieces],
        note="Shipped to vendor on",
        reason_code="SHIPPED_TO_VENDOR",
    )
    eway = "present" if parsed.eway_reference else "not_present"
    event: RtvEvent = run.record(
        RtvEvent(
            movement=locked.movement,
            kind=RtvEvent.Kind.SHIPMENT,
            sequence_no=rtv._next_sequence(locked.movement),
            quantity=sum(qty for _key, qty in parsed.lines),
            lines=[
                _shipment_line(key, departure.slices.get(key, []))
                for key, _pieces in departure.left
            ],
            details={
                "carrier": parsed.carrier,
                "evidence_reference": parsed.evidence_reference,
                "evidence_ids": [str(e) for e in parsed.evidence_ids],
                "evidence_note": parsed.evidence_note,
                "eway_reference": parsed.eway_reference,
                "eway_at_dispatch": eway,
                "closed_damage_report_ids": [str(report.pk) for report in closed],
            },
            journal_batch_id=departure.batch_id,
        ),
        event_at=parsed.shipped_at,
    )
    rtv._close_hold_work(run, {hold.hold_key for hold in departure.ended})
    movements.settle_reservation_blocks(run, [locked.version.pk])
    open_exception(
        run,
        kind=RECEIPT_EXCEPTION,
        site_id=site_id,
        subject_key=_subject(locked.movement),
        reason_code="RTV_AWAITING_VENDOR_RECEIPT",
        source_event_key=engine.event_key("rtv_receipt", event.pk),
        allowed_resolution_actions=RESOLUTION_ACTIONS,
        note=_note(event),
    )
    if eway == "not_present":
        # Transfers PRD §8: missing movement-document evidence is owned work and
        # never blocks the physical recording.
        open_exception(
            run,
            kind=EWAY_EXCEPTION,
            site_id=site_id,
            subject_key=_subject(locked.movement),
            reason_code="EWAY_NOT_PRESENT",
            source_event_key=engine.event_key("rtv_eway_missing", event.pk),
            allowed_resolution_actions=RESOLUTION_ACTIONS,
            note=_note(event),
        )
    rtv.settle(run, locked.movement, locked.version, locked.bodies, [event])
    run.audit_subject_key = _subject(locked.movement)
    run.audit_site_id = site_id
    run.audit_after = {
        "movement_id": str(document_id),
        "rtv_event_id": str(event.pk),
        "shipped_qty": event.quantity,
        "eway_at_dispatch": eway,
        "rtv_state": locked.movement.rtv_state,
    }
    return event


def _shipment_line(key: str, slices: Sequence[rtv.Departed]) -> dict[str, Any]:
    return {
        "line_key": key,
        "qty": sum(ranges.length(s.interval) for s in slices),
        "portions": [
            {
                "lot_id": str(s.lot_id),
                "lower": s.interval[0],
                "upper": s.interval[1],
                "origin_id": s.origin_id,
                "condition": s.condition,
                "source_location_id": s.location_id,
                "holds": [
                    {
                        "hold_key": str(h.hold_key),
                        "kind": h.kind,
                        "lower": h.interval[0],
                        "upper": h.interval[1],
                    }
                    for h in s.holds
                ],
            }
            for s in slices
        ],
    }


# ---------------------------------------------------------------------------
# The shipment, locked, with everything recorded about it so far
# ---------------------------------------------------------------------------


@dataclass
class _Shipped:
    movement: GoodsMovement
    head: DocumentHead
    version: OfficialVersion
    bodies: dict[str, dict[str, Any]]
    events: list[RtvEvent]
    account: rtv.ShipmentAccount

    @property
    def site_id(self) -> int:
        return int(self.movement.document.held_site_id)

    @property
    def shipment(self) -> RtvEvent:
        return self.account.shipment


def _lock_shipment(
    run: CommandRun, document_id: uuid.UUID, shipment_id: uuid.UUID, *, fence: bool
) -> _Shipped:
    """The RTV's head, locked, and one of its shipments with every later fact.

    ``fence`` is for the steps that move stock (a source return, a putaway): the
    site's goods fence and count freeze apply, as for a pickup (Anand's 15B
    decision 7). Paperwork - an acknowledgement, e-way evidence - moves nothing,
    so neither a count nor the site's state stops it (transfers PRD §8).
    """
    movement = movements.movement_of(document_id)
    if movement.kind != GoodsMovement.Kind.RTV:
        raise rtv._state_conflict("This movement is not a return to vendor.")
    if fence:
        movements.require_movement_site(run, movement.document.held_site_id)
    head = lock_heads(run, [document_id])[document_id]
    if head.live_version is None:
        raise rtv._state_conflict("This RTV is not approved.")
    events = rtv._events(movement)
    account = next((a for a in rtv.shipment_accounts(events) if a.shipment.pk == shipment_id), None)
    if account is None:
        raise Refusal("NOT_FOUND", "That shipment was not found on this RTV.")
    version = head.live_version
    bodies = {str(body["line_key"]): body for body in rtv._official_lines(version)}
    return _Shipped(movement, head, version, bodies, events, account)


def _recount(shipped: _Shipped, latest: RtvEvent) -> rtv.ShipmentAccount:
    accounts = rtv.shipment_accounts([*shipped.events, latest])
    return next(a for a in accounts if a.shipment.pk == shipped.shipment.pk)


def _settle_shipment(
    run: CommandRun, shipped: _Shipped, account: rtv.ShipmentAccount, latest: RtvEvent, reason: str
) -> None:
    """A shipment every piece of which is acknowledged or back ends its owned work."""
    shipment = shipped.shipment
    if account.acknowledged is not None or account.accounted:
        # The vendor has answered, or nothing is on its way any more: the
        # shipment no longer awaits receipt (a shortfall is its own work below).
        resolve_exceptions(
            run,
            kind=RECEIPT_EXCEPTION,
            subject_key=_subject(shipped.movement),
            reason_code=reason,
            source_event_key=engine.event_key("rtv_receipt", shipment.pk),
        )
    if not account.accounted:
        return
    resolve_exceptions(
        run,
        kind=DISCREPANCY_EXCEPTION,
        subject_key=_subject(shipped.movement),
        reason_code=reason,
        source_event_key=engine.event_key("rtv_ack_short", shipment.pk),
    )
    rtv.settle(run, shipped.movement, shipped.version, shipped.bodies, [latest])


# ---------------------------------------------------------------------------
# Vendor acknowledgement (E157, adapted): a cumulative snapshot, no posting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Acknowledgement:
    acknowledged_at: datetime
    recipient_reference: str | None
    evidence_ids: tuple[uuid.UUID, ...]
    evidence_note: str | None
    reviewed_hash: str
    lines: tuple[tuple[uuid.UUID, int], ...]


def parse_acknowledgement(body: dict[str, Any], now: datetime) -> Acknowledgement:
    from outbound.transfers import _moment

    rtv._closed(body, ACK_FIELDS)
    reference = rtv._text(body.get("recipient_reference"), "recipient_reference", rtv.MAX_REFERENCE)
    note = rtv._text(body.get("evidence_note"), "evidence_note", rtv.MAX_NOTE)
    evidence = rtv._evidence_ids(body.get("evidence_ids"))
    _evidence(reference, evidence, note, "recipient_reference")
    reviewed = body.get("reviewed_hash")
    if not isinstance(reviewed, str) or len(reviewed) != 64:
        raise _invalid(
            "reviewed_hash is the shipment's state_hash as you read it.", "reviewed_hash"
        )
    raw = body.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= movements.MAX_LINES:
        raise _invalid(f"lines has 1 to {movements.MAX_LINES} entries.", "lines")
    lines: list[tuple[uuid.UUID, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("Every acknowledged line is an object.", "lines")
        rtv._closed(item, ACK_LINE_FIELDS)
        if item.get("qty") is None:
            raise _invalid("qty is required on every acknowledged line (0 is allowed).", "qty")
        lines.append(
            (movements._uuid(item.get("line_key"), "line_key"), _count(item.get("qty"), "qty"))
        )
    keys = [key for key, _qty in lines]
    if len(set(keys)) != len(keys):
        raise _invalid("Every line_key in lines is distinct.", "lines")
    return Acknowledgement(
        acknowledged_at=_moment(body.get("acknowledged_at"), "acknowledged_at", now),
        recipient_reference=reference,
        evidence_ids=evidence,
        evidence_note=note,
        reviewed_hash=reviewed,
        lines=tuple(lines),
    )


def _acknowledged_lines(
    account: rtv.ShipmentAccount, lines: Sequence[tuple[uuid.UUID, int]]
) -> dict[str, int]:
    """Every shipped line exactly once, and never more than left and did not come back."""
    given = {str(key): qty for key, qty in lines}
    for key in given:
        if key not in account.shipped:
            raise _confirm_invalid(
                "That line did not travel on this shipment.", "UNKNOWN_LINE", line_key=key
            )
    for key in account.shipped:
        if key not in given:
            raise _confirm_invalid(
                "Say how many the vendor acknowledged for every line of the shipment, 0 included.",
                "MISSING_LINE",
                line_key=key,
            )
        most = account.shipped[key] - account.returned.get(key, 0)
        if given[key] > most:
            raise _confirm_invalid(
                f"Only {most} piece(s) of this line left and did not come back; the vendor "
                "cannot have received more.",
                "ACK_EXCEEDS_SHIPPED",
                line_key=key,
                quantity=given[key],
            )
    return given


def acknowledge(
    run: CommandRun, document_id: uuid.UUID, shipment_id: uuid.UUID, parsed: Acknowledgement
) -> RtvEvent:
    """Record what the vendor says it received of one shipment. Nothing is posted."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "An acknowledgement is recorded by a named person.")
    shipped = _lock_shipment(run, document_id, shipment_id, fence=False)
    account = shipped.account
    if parsed.reviewed_hash != account.state_hash():
        raise Refusal(
            "REVISION_SUPERSEDED",
            "Something was recorded against this shipment after you read it. Reload it.",
        )
    if account.accounted:
        raise rtv._state_conflict("Every piece of this shipment is already accounted for.")
    # A new snapshot changes what is missing: a closure prepared against the old
    # one is no longer what the Owner would be approving (goods ticket 15H).
    supersede_pending(run, CLOSURE_SUBJECT, str(shipment_id))
    if parsed.acknowledged_at < account.shipment.event_at:
        raise Refusal(
            "EVENT_TIME_INVALID",
            "The vendor cannot acknowledge goods before they left.",
            status=422,
        )
    given = _acknowledged_lines(account, parsed.lines)
    rtv._evidence_exist(run, parsed.evidence_ids)
    shortfall = sum(
        account.shipped[key] - account.returned.get(key, 0) - given[key] for key in account.shipped
    )
    event: RtvEvent = run.record(
        RtvEvent(
            movement=shipped.movement,
            kind=RtvEvent.Kind.ACKNOWLEDGEMENT,
            shipment=account.shipment,
            sequence_no=rtv._next_sequence(shipped.movement),
            quantity=sum(given.values()),
            lines=[{"line_key": key, "qty": given[key]} for key in account.shipped],
            details={
                "recipient_reference": parsed.recipient_reference,
                "evidence_ids": [str(e) for e in parsed.evidence_ids],
                "evidence_note": parsed.evidence_note,
                "shortfall_qty": shortfall,
            },
        ),
        event_at=parsed.acknowledged_at,
    )
    after = _recount(shipped, event)
    if not after.accounted:
        # GSA-T15: the difference is owned and visible; nothing is restored,
        # written off or credited, and nothing is confirmed. Only the Owner's
        # approved closure on the RTV's shipment ends it (15H, GSA-R07).
        open_exception(
            run,
            kind=DISCREPANCY_EXCEPTION,
            site_id=shipped.site_id,
            subject_key=_subject(shipped.movement),
            reason_code="VENDOR_ACKNOWLEDGED_SHORT",
            source_event_key=engine.event_key("rtv_ack_short", account.shipment.pk),
            allowed_resolution_actions=RESOLUTION_ACTIONS,
            note=_note(account.shipment),
        )
    _settle_shipment(run, shipped, after, event, "VENDOR_ACKNOWLEDGED")
    run.audit_subject_key = _subject(shipped.movement)
    run.audit_site_id = shipped.site_id
    run.audit_after = {
        "movement_id": str(document_id),
        "shipment_id": str(shipment_id),
        "rtv_event_id": str(event.pk),
        "acknowledged_qty": event.quantity,
        "shortfall_qty": shortfall,
        "shipment_status": after.status,
    }
    return event


# ---------------------------------------------------------------------------
# Actual return to source after a failed delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReturnLine:
    line_key: uuid.UUID
    good: int
    damaged: int

    @property
    def qty(self) -> int:
        return self.good + self.damaged


@dataclass(frozen=True)
class SourceReturn:
    returned_at: datetime
    reason: str
    evidence_reference: str | None
    evidence_ids: tuple[uuid.UUID, ...]
    evidence_note: str | None
    lines: tuple[ReturnLine, ...]


def parse_return(body: dict[str, Any], now: datetime) -> SourceReturn:
    from outbound.transfers import _moment

    rtv._closed(body, RETURN_FIELDS)
    reason = rtv._text(body.get("reason"), "reason", MAX_REASON)
    if not reason:
        raise _invalid("reason says why the delivery failed and the goods came back.", "reason")
    reference = rtv._text(body.get("evidence_reference"), "evidence_reference", rtv.MAX_REFERENCE)
    note = rtv._text(body.get("evidence_note"), "evidence_note", rtv.MAX_NOTE)
    evidence = rtv._evidence_ids(body.get("evidence_ids"))
    _evidence(reference, evidence, note, "evidence_reference")
    raw = body.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= movements.MAX_LINES:
        raise _invalid(f"A return has 1 to {movements.MAX_LINES} lines.", "lines")
    lines: list[ReturnLine] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("Every return line is an object.", "lines")
        rtv._closed(item, RETURN_LINE_FIELDS)
        lines.append(
            ReturnLine(
                line_key=movements._uuid(item.get("line_key"), "line_key"),
                good=_count(item.get("good"), "good"),
                damaged=_count(item.get("damaged"), "damaged"),
            )
        )
    keys = [line.line_key for line in lines]
    if len(set(keys)) != len(keys):
        raise _invalid("Every return line names a different shipped line.", "lines")
    if sum(line.qty for line in lines) == 0:
        raise _invalid("A return records at least one piece that is physically back.", "lines")
    return SourceReturn(
        returned_at=_moment(body.get("returned_at"), "returned_at", now),
        reason=reason,
        evidence_reference=reference,
        evidence_ids=evidence,
        evidence_note=note,
        lines=tuple(lines),
    )


@dataclass(frozen=True)
class _Away:
    """One slice of a shipped line still away from the source, as it left."""

    lot_id: uuid.UUID
    interval: ranges.Interval
    origin_id: str | None
    condition: str
    holds: tuple[tuple[uuid.UUID, str, ranges.Interval], ...]


def _away(account: rtv.ShipmentAccount, key: str) -> list[_Away]:
    """The line's shipped slices less every slice an earlier return brought back, FIFO."""
    back: dict[uuid.UUID, list[ranges.Interval]] = {}
    for event in account.returns:
        for line in event.lines:
            if str(line["line_key"]) != key:
                continue
            for piece in line["portions"]:
                back.setdefault(uuid.UUID(str(piece["lot_id"])), []).append(
                    (int(piece["lower"]), int(piece["upper"]))
                )
    out: list[_Away] = []
    for line in account.shipment.lines:
        if str(line["line_key"]) != key:
            continue
        for piece in line["portions"]:
            lot_id = uuid.UUID(str(piece["lot_id"]))
            holds = tuple(
                (uuid.UUID(str(h["hold_key"])), str(h["kind"]), (int(h["lower"]), int(h["upper"])))
                for h in piece.get("holds") or []
            )
            for part in ranges.subtract(
                [(int(piece["lower"]), int(piece["upper"]))], back.get(lot_id, [])
            ):
                out.append(
                    _Away(
                        lot_id=lot_id,
                        interval=part,
                        origin_id=piece.get("origin_id"),
                        condition=str(piece.get("condition") or "good"),
                        holds=holds,
                    )
                )
    return out


def _cut(pool: list[_Away], qty: int) -> tuple[list[_Away], list[_Away]]:
    """Take ``qty`` pieces off the front of ``pool``; return (taken, rest)."""
    taken: list[_Away] = []
    rest: list[_Away] = []
    remaining = qty
    for piece in pool:
        if remaining <= 0:
            rest.append(piece)
            continue
        size = ranges.length(piece.interval)
        lower, upper = piece.interval
        if size <= remaining:
            taken.append(piece)
            remaining -= size
            continue
        taken.append(replace(piece, interval=(lower, lower + remaining)))
        rest.append(replace(piece, interval=(lower + remaining, upper)))
        remaining = 0
    return taken, rest


def _still_away(piece: _Away) -> None:
    there = [
        bounds(p.portion)
        for p in engine.positions_of(piece.lot_id, piece.interval)
        if p.boundary == rtv.RETURNED_BOUNDARY
    ]
    if ranges.total(ranges.intersect(there, [piece.interval])) != ranges.length(piece.interval):
        raise rtv._state_conflict(
            "Some of these pieces are no longer recorded as away with the vendor."
        )


def _plan_return(
    account: rtv.ShipmentAccount, lines: Sequence[ReturnLine]
) -> list[tuple[str, _Away, str]]:
    """Cut each named line's pieces still away into what came back, by reported condition."""
    moves: list[tuple[str, _Away, str]] = []
    for item in lines:
        key = str(item.line_key)
        if key not in account.shipped:
            raise _refuse(
                "That line did not travel on this shipment.", "UNKNOWN_LINE", line_key=key
            )
        still = account.unaccounted(key)
        if item.qty > still:
            raise _refuse(
                f"Only {still} piece(s) of this line are neither acknowledged by the vendor nor "
                f"back already, and the return says {item.qty} came back. A return records only "
                "what is physically back.",
                "RETURN_EXCEEDS_UNACCOUNTED",
                line_key=key,
                quantity=still,
            )
        good, rest = _cut(_away(account, key), item.good)
        damaged, _rest = _cut(rest, item.damaged)
        moves += [(key, piece, "good") for piece in good]
        moves += [(key, piece, "damaged") for piece in damaged]
    return moves


def return_to_source(
    run: CommandRun, document_id: uuid.UUID, shipment_id: uuid.UUID, parsed: SourceReturn
) -> RtvEvent:
    """Record what of one shipment is physically back at the source, and nothing more."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A return is recorded by a named person.")
    shipped = _lock_shipment(run, document_id, shipment_id, fence=True)
    account = shipped.account
    site_id = shipped.site_id
    if parsed.returned_at < account.shipment.event_at:
        raise Refusal("EVENT_TIME_INVALID", "Goods cannot come back before they left.", status=422)
    moves = _plan_return(account, parsed.lines)
    # Goods that came back are no longer missing: a closure prepared before is
    # superseded (goods ticket 15H). Taken before the lots, in lock order.
    supersede_pending(run, CLOSURE_SUBJECT, str(shipment_id))
    rtv._evidence_exist(run, parsed.evidence_ids)
    engine.lock_lots(run, sorted({piece.lot_id for _k, piece, _c in moves}, key=str))
    receiving = engine.system_location(site_id, "receiving")
    quarantine = engine.system_location(site_id, "quarantine")
    plan = engine.Plan(
        rtv.RTV_POSTING,
        shipped.version.pk,
        engine.event_key("rtv_source_return", account.shipment.pk, run.key_id),
    )
    costs = rtv._costs(piece.origin_id for _k, piece, _c in moves if piece.origin_id)
    placed: dict[uuid.UUID, str] = {}
    damage_lines: list[dict[str, Any]] = []
    by_line: dict[str, dict[str, Any]] = {}
    for key, piece, reported in moves:
        _still_away(piece)
        was_good = piece.condition == "good"
        newly_damaged = was_good and reported == "damaged"
        condition = "damaged" if newly_damaged else piece.condition
        # Every hold the piece left under comes back with it, as the same kind:
        # quarantined goods stay quarantined (transfers PRD §6).
        again = [
            (uuid.uuid5(run.key_id, str(hold_key)), kind, part)
            for hold_key, kind, span in piece.holds
            for part in ranges.intersect([span], [piece.interval])
        ]
        held = bool(again) or newly_damaged or condition != "good"
        location = quarantine.pk if held else receiving.pk
        engine.change_address(
            run,
            plan,
            piece.lot_id,
            piece.interval,
            lambda old, loc=location, cond=condition: replace(
                old,
                boundary="physical",
                site_id=site_id,
                location_id=loc,
                transfer_id=None,
                condition=cond,
                # Back at the source but not accepted there: acceptance belongs
                # to whoever puts the goods away, and nobody has yet.
                accepted_event_id=None,
                reason=None,
            ),
        )
        keys: list[str] = []
        for new_key, kind, part in again:
            engine.place_hold(
                run,
                plan,
                lot_id=piece.lot_id,
                interval=part,
                hold_key=new_key,
                kind=kind,
                site_id=site_id,
                source_version_id=shipped.version.pk,
            )
            placed[new_key] = kind
            keys.append(str(new_key))
        if newly_damaged:
            damage_key = uuid.uuid5(run.key_id, f"damage:{piece.lot_id}:{piece.interval[0]}")
            engine.place_hold(
                run,
                plan,
                lot_id=piece.lot_id,
                interval=piece.interval,
                hold_key=damage_key,
                kind=DAMAGE_HOLD,
                site_id=site_id,
                source_version_id=shipped.version.pk,
            )
            placed[damage_key] = DAMAGE_HOLD
            keys.append(str(damage_key))
            damage_lines.append(_damage_line(key, piece, receiving.pk, damage_key))
        if piece.origin_id:
            plan.value.append(
                ValuePair(
                    origin_id=uuid.UUID(piece.origin_id),
                    amount=ranges.length(piece.interval) * costs[piece.origin_id],
                    source_bucket="external",
                    destination_bucket="stock",
                    source_site_id=None,
                    destination_site_id=site_id,
                    lot_id=piece.lot_id,
                    lower=piece.interval[0],
                    upper=piece.interval[1],
                )
            )
        line = by_line.setdefault(
            key, {"line_key": key, "qty": 0, "good": 0, "damaged": 0, "portions": []}
        )
        size = ranges.length(piece.interval)
        line["qty"] += size
        line["damaged" if reported == "damaged" else "good"] += size
        line["portions"].append(
            {
                "lot_id": str(piece.lot_id),
                "lower": piece.interval[0],
                "upper": piece.interval[1],
                "origin_id": piece.origin_id,
                "reported": reported,
                "condition": condition,
                "location_id": str(location),
                "hold_keys": keys,
            }
        )
    batch_id = engine.post(run, shipped.version, plan)
    report = None
    if damage_lines:
        report = damage_review.open_for_rtv_return(
            run,
            document=shipped.movement.document,
            site_id=site_id,
            reason_code=RETURN_DAMAGE_REASON,
            lines=damage_lines,
        )
    event: RtvEvent = run.record(
        RtvEvent(
            movement=shipped.movement,
            kind=RtvEvent.Kind.SOURCE_RETURN,
            shipment=account.shipment,
            sequence_no=rtv._next_sequence(shipped.movement),
            quantity=sum(item.qty for item in parsed.lines),
            lines=list(by_line.values()),
            details={
                "reason": parsed.reason,
                "evidence_reference": parsed.evidence_reference,
                "evidence_ids": [str(e) for e in parsed.evidence_ids],
                "evidence_note": parsed.evidence_note,
                "damage_report_id": str(report.pk) if report is not None else None,
            },
            journal_batch_id=batch_id,
        ),
        event_at=parsed.returned_at,
    )
    _open_hold_work(run, placed, event, shipped)
    after = _recount(shipped, event)
    _settle_shipment(run, shipped, after, event, "RETURNED_TO_SOURCE")
    run.audit_subject_key = _subject(shipped.movement)
    run.audit_site_id = site_id
    run.audit_after = {
        "movement_id": str(document_id),
        "shipment_id": str(shipment_id),
        "rtv_event_id": str(event.pk),
        "returned_qty": event.quantity,
        "still_away": after.unaccounted_qty,
        "shipment_status": after.status,
    }
    return event


def _open_hold_work(
    run: CommandRun, placed: Mapping[uuid.UUID, str], event: RtvEvent, shipped: _Shipped
) -> None:
    site_id = shipped.site_id
    for hold_key, kind in sorted(placed.items(), key=lambda item: str(item[0])):
        # Goods on hold are owned work until the hold is lifted or the goods
        # leave by their own route (GSA-T12). A damage hold is never lifted
        # from the movements screen, so it names no route there.
        open_exception(
            run,
            kind=movements.HOLD_EXCEPTION,
            site_id=site_id,
            subject_key=f"hold:{hold_key}",
            reason_code=RETURN_DAMAGE_REASON if kind == DAMAGE_HOLD else "RTV_RETURNED_HELD",
            source_event_key=engine.event_key("rtv_return_hold", event.pk, hold_key),
            allowed_resolution_actions=[] if kind == DAMAGE_HOLD else movements.RESOLUTION_ACTIONS,
            note=_subject(shipped.movement),
        )


def _damage_line(key: str, piece: _Away, receiving: uuid.UUID, hold_key: uuid.UUID) -> Any:
    return {
        "line_key": key,
        "lot_id": str(piece.lot_id),
        "sku_id": None,
        "origin_id": piece.origin_id,
        "qty": ranges.length(piece.interval),
        # Where a rejected report puts the pieces back: the source's receiving,
        # still to be put away - where they would stand had nobody called them
        # damaged.
        "source_location_id": str(receiving),
        "hold_keys": [str(hold_key)],
        "portions": [
            {
                "lot_id": str(piece.lot_id),
                "lower": piece.interval[0],
                "upper": piece.interval[1],
                "condition": "good",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Putting returned good goods away at the source
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PutawayLine:
    line_key: uuid.UUID
    qty: int
    destination_location_id: uuid.UUID


def parse_putaway(body: dict[str, Any]) -> tuple[PutawayLine, ...]:
    rtv._closed(body, PUTAWAY_FIELDS)
    raw = body.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= movements.MAX_LINES:
        raise _invalid(f"A putaway has 1 to {movements.MAX_LINES} lines.", "lines")
    lines: list[PutawayLine] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("Every putaway line is an object.", "lines")
        rtv._closed(item, PUTAWAY_LINE_FIELDS)
        lines.append(
            PutawayLine(
                line_key=movements._uuid(item.get("line_key"), "line_key"),
                qty=rtv._qty(item.get("qty"), "qty"),
                destination_location_id=movements._uuid(
                    item.get("destination_location_id"), "destination_location_id"
                ),
            )
        )
    keys = [line.line_key for line in lines]
    if len(set(keys)) != len(keys):
        raise _invalid("Every putaway line names a different shipped line.", "lines")
    return tuple(lines)


def _waiting_by_line(
    account: rtv.ShipmentAccount, site_id: int, receiving_id: uuid.UUID
) -> dict[str, list[rtv.Piece]]:
    """Returned pieces of this shipment standing good and unaccepted in the source's receiving.

    One positions read per shipment, whatever its lines: the reads list every
    shipment of an RTV, so nothing here is asked line by line.
    """
    wanted: list[tuple[str, uuid.UUID, ranges.Interval, str | None]] = [
        (
            str(line["line_key"]),
            uuid.UUID(str(piece["lot_id"])),
            (int(piece["lower"]), int(piece["upper"])),
            piece.get("origin_id"),
        )
        for event in account.returns
        for line in event.lines
        for piece in line["portions"]
    ]
    out: dict[str, list[rtv.Piece]] = {}
    if not wanted:
        return out
    standing: dict[uuid.UUID, list[ranges.Interval]] = {}
    for position in Position.objects.filter(
        lot_id__in={lot for _k, lot, _i, _o in wanted},
        boundary="physical",
        site_id=site_id,
        location_id=receiving_id,
        condition="good",
        accepted_event_id__isnull=True,
    ):
        standing.setdefault(position.lot_id, []).append(bounds(position.portion))
    for key, lot_id, interval, origin in wanted:
        for part in ranges.intersect(standing.get(lot_id, []), [interval]):
            out.setdefault(key, []).append(rtv.Piece(lot_id, part, origin))
    return out


def _waiting(account: rtv.ShipmentAccount, key: str | None, site_id: int) -> list[rtv.Piece]:
    """The returned pieces of one line (or of every line) waiting to be put away."""
    receiving = engine.system_location(site_id, "receiving")
    by_line = _waiting_by_line(account, site_id, receiving.pk)
    if key is not None:
        return by_line.get(key, [])
    return [piece for pieces in by_line.values() for piece in pieces]


def putaway(
    run: CommandRun,
    document_id: uuid.UUID,
    shipment_id: uuid.UUID,
    lines: Sequence[PutawayLine],
) -> RtvEvent:
    """Accept returned good pieces into ordinary storage, where they are usable again.

    The same acceptance evidence a transfer's returned goods get (goods ticket
    13C): somebody at the source physically put these pieces here. Nothing is
    revalued - the return already brought their value back.
    """
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A putaway is recorded by a named person.")
    shipped = _lock_shipment(run, document_id, shipment_id, fence=True)
    site_id = shipped.site_id
    chosen: list[tuple[PutawayLine, list[rtv.Piece]]] = []
    for line in lines:
        key = str(line.line_key)
        if key not in shipped.account.shipped:
            raise _refuse(
                "That line did not travel on this shipment.", "UNKNOWN_LINE", line_key=key
            )
        destination = movements.storage_location(
            run.tenant_id, line.destination_location_id, site_id
        )
        pool = _waiting(shipped.account, key, site_id)
        available = ranges.total([piece.interval for piece in pool])
        if line.qty > available:
            raise _refuse(
                f"Only {available} returned good piece(s) of this line wait to be put away.",
                "NOT_WAITING",
                line_key=key,
                quantity=available,
            )
        chosen.append(
            (replace(line, destination_location_id=destination.pk), rtv._take(pool, line.qty))
        )
    engine.lock_lots(run, sorted({p.lot_id for _l, pieces in chosen for p in pieces}, key=str))
    session = _acceptance_session(run, site_id, shipped.version)
    plan = engine.Plan(
        PUTAWAY_POSTING,
        shipped.version.pk,
        engine.event_key("rtv_putaway", shipped.shipment.pk, run.key_id),
    )
    recorded: list[dict[str, Any]] = []
    for line, pieces in chosen:
        for piece in pieces:
            accepted = _acceptance_event(run, session, site_id, piece, line.destination_location_id)
            engine.change_address(
                run,
                plan,
                piece.lot_id,
                piece.interval,
                lambda old, loc=line.destination_location_id, ev=accepted.pk: replace(
                    old, location_id=loc, accepted_event_id=ev
                ),
            )
        recorded.append(
            {
                **rtv._event_line(str(line.line_key), pieces),
                "destination_location_id": str(line.destination_location_id),
            }
        )
    batch_id = engine.post(run, shipped.version, plan)
    event: RtvEvent = run.record(
        RtvEvent(
            movement=shipped.movement,
            kind=RtvEvent.Kind.PUTAWAY,
            shipment=shipped.shipment,
            sequence_no=rtv._next_sequence(shipped.movement),
            quantity=sum(line.qty for line, _pieces in chosen),
            lines=recorded,
            details={},
            journal_batch_id=batch_id,
        )
    )
    session.last_activity_at = run.now
    if not any(
        _waiting(account, None, site_id) for account in rtv.shipment_accounts(shipped.events)
    ):
        # Nothing of this RTV waits in receiving any more: the acceptance is
        # finished, not left open as a closure residual.
        session.state = AcceptanceSession.State.COMPLETED
    session.save(update_fields=["state", "last_activity_at"])
    run.audit_subject_key = _subject(shipped.movement)
    run.audit_site_id = site_id
    run.audit_after = {
        "movement_id": str(document_id),
        "shipment_id": str(shipment_id),
        "rtv_event_id": str(event.pk),
        "putaway_qty": event.quantity,
    }
    return event


def _acceptance_session(
    run: CommandRun, site_id: int, version: OfficialVersion
) -> AcceptanceSession:
    """One open acceptance session per RTV version at the source, while goods wait."""
    assert run.principal.human_id is not None
    existing = AcceptanceSession.objects.filter(
        site_id=site_id, source_version_id=version.pk, state=AcceptanceSession.State.OPEN
    ).first()
    if existing is not None:
        return existing
    return AcceptanceSession.objects.create(
        tenant_id=run.tenant_id,
        site_id=site_id,
        source_version_id=version.pk,
        opened_by_id=run.principal.human_id,
        last_activity_at=run.now,
    )


def _acceptance_event(
    run: CommandRun,
    session: AcceptanceSession,
    site_id: int,
    piece: rtv.Piece,
    location_id: uuid.UUID,
) -> AcceptanceEvent:
    event: AcceptanceEvent = run.record(
        AcceptanceEvent(
            session_id=session.pk,
            site_id=site_id,
            scan_key=uuid.uuid5(session.pk, f"{piece.lot_id}:{piece.interval[0]}:{run.key_id}"),
            official_line=None,
            lot_id=piece.lot_id,
            portion=portion(*piece.interval),
            destination_location_id=location_id,
            observed_alias="rtv return",
            observed_ticket_mrp=None,
            label_evidence_ref=None,
            tag_verdict="matched",
            outcome=AcceptanceEvent.Outcome.ACCEPTED_GOOD,
        )
    )
    return event


# ---------------------------------------------------------------------------
# E-way evidence: missing at dispatch, attached later, verified - three facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Eway:
    action: str
    reference: str
    note: str | None


def parse_eway(body: dict[str, Any]) -> Eway:
    rtv._closed(body, EWAY_FIELDS)
    action = body.get("action")
    if action not in EWAY_ACTIONS:
        raise _invalid("action is attach or verify.", "action")
    reference = rtv._text(body.get("reference"), "reference", rtv.MAX_REFERENCE)
    if not reference:
        raise _invalid("reference is the e-way bill number.", "reference")
    return Eway(
        action=str(action),
        reference=reference,
        note=rtv._text(body.get("note"), "note", MAX_REASON),
    )


def eway_state(account: rtv.ShipmentAccount) -> dict[str, Any]:
    """The shipment's e-way evidence as three separate facts, never merged.

    ``at_dispatch`` is what left with the goods and never changes. ``reference``
    is the reference on file now - the latest attached, else the one that left
    with them. ``verified`` is true only when that same reference was verified.
    """
    details = account.shipment.details or {}
    reference: str | None = details.get("eway_reference") or None
    attached_at = attached_by = verified_at = verified_by = None
    verified_reference: str | None = None
    for event in account.eway:
        if event.kind == RtvEvent.Kind.EWAY_ATTACHED:
            reference = str(event.details.get("reference"))
            attached_at, attached_by = event.recorded_at, event.actor_id
            verified_reference = None
            verified_at = verified_by = None
        else:
            verified_reference = str(event.details.get("reference"))
            verified_at, verified_by = event.recorded_at, event.actor_id
    verified = reference is not None and verified_reference == reference
    return {
        "at_dispatch": details.get("eway_at_dispatch") or "not_present",
        "reference": reference,
        "attached_at": attached_at.isoformat() if attached_at else None,
        "attached_by": str(attached_by) if attached_by else None,
        "verified": verified,
        "verified_at": verified_at.isoformat() if verified and verified_at else None,
        "verified_by": str(verified_by) if verified and verified_by else None,
    }


def record_eway(
    run: CommandRun, document_id: uuid.UUID, shipment_id: uuid.UUID, parsed: Eway
) -> RtvEvent:
    """Attach or verify the shipment's e-way reference. Paperwork: nothing physical changes."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "E-way evidence is recorded by a named person.")
    shipped = _lock_shipment(run, document_id, shipment_id, fence=False)
    state = eway_state(shipped.account)
    if parsed.action == "verify":
        if state["reference"] is None:
            raise Refusal(
                "EWAY_VERIFY_DENIED",
                "There is no e-way reference on this shipment to verify. Attach one first.",
                status=403,
            )
        if parsed.reference != state["reference"]:
            raise Refusal(
                "EWAY_VERIFY_DENIED",
                "That is not the e-way reference on file for this shipment.",
                status=403,
            )
        if state["verified"]:
            raise rtv._state_conflict("This e-way reference is already verified.")
    kind = RtvEvent.Kind.EWAY_ATTACHED if parsed.action == "attach" else RtvEvent.Kind.EWAY_VERIFIED
    event: RtvEvent = run.record(
        RtvEvent(
            movement=shipped.movement,
            kind=kind,
            shipment=shipped.shipment,
            sequence_no=rtv._next_sequence(shipped.movement),
            quantity=0,
            lines=[],
            details={"reference": parsed.reference, "note": parsed.note},
        )
    )
    if parsed.action == "attach":
        resolve_exceptions(
            run,
            kind=EWAY_EXCEPTION,
            subject_key=_subject(shipped.movement),
            reason_code="REFERENCE_ATTACHED",
            source_event_key=engine.event_key("rtv_eway_missing", shipped.shipment.pk),
        )
    run.audit_subject_key = _subject(shipped.movement)
    run.audit_site_id = shipped.site_id
    run.audit_after = {
        "movement_id": str(document_id),
        "shipment_id": str(shipment_id),
        "eway": parsed.action,
    }
    return event


# ---------------------------------------------------------------------------
# Closing a persistent acknowledgement shortfall (goods ticket 15H, GSA-R07)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureProposal:
    reason: str
    evidence_reference: str | None
    evidence_ids: tuple[uuid.UUID, ...]
    evidence_note: str | None
    reviewed_hash: str


def parse_closure(body: dict[str, Any]) -> ClosureProposal:
    rtv._closed(body, CLOSURE_FIELDS)
    reason = rtv._text(body.get("reason"), "reason", MAX_REASON)
    if not reason:
        raise _invalid("reason says why the difference is being closed as a shortfall.", "reason")
    reference = rtv._text(body.get("evidence_reference"), "evidence_reference", rtv.MAX_REFERENCE)
    note = rtv._text(body.get("evidence_note"), "evidence_note", rtv.MAX_NOTE)
    evidence = rtv._evidence_ids(body.get("evidence_ids"))
    if not (reference or note or evidence):
        # GSA-R07: a vendor letter, a photo or a note.
        raise _refuse(
            "Record the evidence: the vendor's letter (its reference), a photo, or a note.",
            "EVIDENCE_REQUIRED",
            field="evidence_reference",
        )
    reviewed = body.get("reviewed_hash")
    if not isinstance(reviewed, str) or len(reviewed) != 64:
        raise _invalid(
            "reviewed_hash is the shipment's state_hash as you read it.", "reviewed_hash"
        )
    return ClosureProposal(
        reason=reason,
        evidence_reference=reference,
        evidence_ids=evidence,
        evidence_note=note,
        reviewed_hash=reviewed,
    )


def _shortfall(account: rtv.ShipmentAccount) -> list[tuple[str, list[_Away]]]:
    """Per short line, exactly the pieces the vendor has not acknowledged, oldest first.

    What is still away is what left less what came back; the vendor holds as
    many of those as it acknowledged. Which pieces are the missing ones is not
    knowable from a count, so the closure takes them in the same FIFO order a
    return does - deterministically, so the frozen pieces and their recorded
    cost are exactly what the Owner approves.
    """
    out: list[tuple[str, list[_Away]]] = []
    for key in account.shipped:
        missing = account.unaccounted(key)
        if missing > 0:
            taken, _rest = _cut(_away(account, key), missing)
            out.append((key, taken))
    return out


def _closure_lines(shortfall: Sequence[tuple[str, list[_Away]]]) -> list[dict[str, Any]]:
    """The frozen pieces per line, each line valued at recorded layer cost like a GSA-R03
    adjustment - ``None`` (unknown, never zero) when any piece has no recorded origin:
    pre-PT custody keeps unknown value (GSA-R07)."""
    lines: list[dict[str, Any]] = []
    for key, pieces in shortfall:
        value = adjustments.layer_value([(ranges.length(p.interval), p.origin_id) for p in pieces])
        lines.append(
            {
                "line_key": key,
                "qty": sum(ranges.length(p.interval) for p in pieces),
                "value_paise": str(value) if value is not None else None,
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
        )
    return lines


def _lines_value(lines: Sequence[Mapping[str, Any]]) -> int | None:
    values = [line.get("value_paise") for line in lines]
    if any(value is None for value in values):
        return None
    return sum(int(str(value)) for value in values)


def _pending_closures(shipment_ids: Sequence[uuid.UUID]) -> dict[str, Any]:
    """The pending approval request for each shipment's prepared closure, if any."""
    from approvals.goods_models import ApprovalRequest

    return {
        request.subject_key: request
        for request in ApprovalRequest.objects.filter(
            subject_kind=CLOSURE_SUBJECT,
            subject_key__in=[str(pk) for pk in shipment_ids],
            state=ApprovalRequest.State.PENDING,
        )
    }


def propose_closure(
    run: CommandRun, document_id: uuid.UUID, shipment_id: uuid.UUID, parsed: ClosureProposal
) -> RtvEvent:
    """Prepare the closure of one shipment's acknowledgement shortfall for the Owner.

    Moves nothing and posts nothing. Freezes exactly the unacknowledged pieces
    and their value at recorded layer cost, and asks the Owner - under the RTV
    approval policy, never the preparer - to approve it. A closure prepared
    earlier and still waiting is superseded by this one.
    """
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A shortfall closure is prepared by a named person.")
    shipped = _lock_shipment(run, document_id, shipment_id, fence=False)
    account = shipped.account
    if parsed.reviewed_hash != account.state_hash():
        raise Refusal(
            "REVISION_SUPERSEDED",
            "Something was recorded against this shipment after you read it. Reload it.",
        )
    if account.accounted:
        raise rtv._state_conflict("Every piece of this shipment is already accounted for.")
    if account.acknowledged is None:
        raise rtv._state_conflict(
            "The vendor has not acknowledged this shipment yet, so nothing is known to be "
            "missing. Record what the vendor says it received first."
        )
    rtv._evidence_exist(run, parsed.evidence_ids)
    supersede_pending(run, CLOSURE_SUBJECT, str(shipment_id))
    lines = _closure_lines(_shortfall(account))
    quantity = sum(int(line["qty"]) for line in lines)
    value = _lines_value(lines)
    site_id = shipped.site_id
    policy = pin(
        run,
        action=CLOSURE_APPROVE_ACTION,
        purpose=CLOSURE_PURPOSE,
        site_id=site_id,
        brand_ids=[None],
        amounts=Amounts(quantity, value),
    )
    details: dict[str, Any] = {
        "reason": parsed.reason,
        "evidence_reference": parsed.evidence_reference,
        "evidence_ids": [str(e) for e in parsed.evidence_ids],
        "evidence_note": parsed.evidence_note,
        "reviewed_hash": parsed.reviewed_hash,
        "value_paise": str(value) if value is not None else None,
        "value_basis": VALUE_BASIS,
    }
    # What the Owner approves: this shipment as read, these exact pieces, this
    # reason and evidence. Any later change to the shipment supersedes it.
    reviewed = content_hash({"shipment": str(shipment_id), "lines": lines, **details})
    number = shipped.movement.document.official_number or ""
    request = create_request(
        run,
        subject_kind=CLOSURE_SUBJECT,
        subject_key=str(shipment_id),
        revision=len(account.proposals) + 1,
        reviewed_hash=reviewed,
        requested_action=CLOSURE_APPROVE_ACTION,
        site_id=site_id,
        title=(
            f"Close RTV {number} shipment {account.shipment.sequence_no}: {quantity} "
            "piece(s) the vendor did not acknowledge"
        ),
        policy=policy,
        new_subject=True,
    )
    event: RtvEvent = run.record(
        RtvEvent(
            movement=shipped.movement,
            kind=RtvEvent.Kind.SHORTFALL_PROPOSAL,
            shipment=account.shipment,
            sequence_no=rtv._next_sequence(shipped.movement),
            quantity=quantity,
            lines=lines,
            details={**details, "approval_request_id": str(request.pk)},
        )
    )
    record_event(
        run,
        document_id,
        "rtv_shortfall_proposed",
        version_id=shipped.version.pk,
        payload={"shipment_id": str(shipment_id), "quantity": quantity},
    )
    run.audit_subject_key = _subject(shipped.movement)
    run.audit_site_id = site_id
    run.audit_after = {
        "movement_id": str(document_id),
        "shipment_id": str(shipment_id),
        "rtv_event_id": str(event.pk),
        "approval_request_id": str(request.pk),
        "shortfall_qty": quantity,
    }
    return event


def decide_closure(run: CommandRun, context: DecisionContext) -> dict[str, Any]:
    """The Owner's decision on a prepared shortfall closure (E234 -> here).

    Approval recognises exactly the frozen pieces as a shortfall: P13 moves them
    from the ``returned`` boundary to ``consumed``, once; no value leg (it left
    stock at departure, at this same recorded cost) and no GL, vendor, payable,
    credit-note or tax entry. The difference's owned work closes and the RTV
    may close. A rejection keeps the difference open and the history.
    """
    request = context.request
    try:
        shipment_id = uuid.UUID(request.subject_key)
    except (TypeError, ValueError):
        raise Refusal("NOT_FOUND", "That shipment was not found.") from None
    known = (
        RtvEvent.objects.select_related("movement__document")
        .filter(tenant_id=request.tenant_id, pk=shipment_id, kind=RtvEvent.Kind.SHIPMENT)
        .first()
    )
    if known is None:
        raise Refusal("NOT_FOUND", "That shipment was not found.")
    document_id = known.movement.document_id
    site_id = known.movement.document.site_id
    context.access.require(CLOSURE_APPROVE_ACTION, site_id=site_id)
    shipped = _lock_shipment(run, document_id, shipment_id, fence=False)
    account = shipped.account
    proposal = next(
        (
            event
            for event in reversed(account.proposals)
            if event.details.get("approval_request_id") == str(request.pk)
        ),
        None,
    )
    if proposal is None:
        raise Refusal("STATE_CONFLICT", "This closure is no longer waiting for approval.")
    if str(context.checker_id) == str(proposal.actor_id):
        raise Refusal("SELF_APPROVAL", "Someone who prepared this closure cannot also approve it.")
    context.enforce_policy(run)
    if proposal.details.get("reviewed_hash") != account.state_hash() or account.accounted:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "Something was recorded against this shipment after the closure was prepared.",
        )
    if context.decision == "reject":
        record_event(
            run,
            document_id,
            "rtv_shortfall_rejected",
            version_id=shipped.version.pk,
            reason_code=(context.reason_code or "")[:60] or None,
            payload={"shipment_id": str(shipment_id), "proposal_id": str(proposal.pk)},
        )
        return {"state": "rejected", "shipment_id": str(shipment_id)}
    lines = [dict(line) for line in proposal.lines]
    for line in lines:
        # The frozen pieces must still be exactly what is missing on the line.
        if int(line["qty"]) != account.unaccounted(str(line["line_key"])):
            raise rtv._state_conflict(
                "What is missing on this shipment changed after the closure was prepared."
            )
    pieces = [
        _Away(
            lot_id=uuid.UUID(str(piece["lot_id"])),
            interval=(int(piece["lower"]), int(piece["upper"])),
            origin_id=piece.get("origin_id"),
            condition="",
            holds=(),
        )
        for line in lines
        for piece in line["portions"]
    ]
    engine.lock_lots(run, sorted({piece.lot_id for piece in pieces}, key=str))
    plan = engine.Plan(
        CLOSURE_POSTING, shipped.version.pk, engine.event_key(SHORTFALL_REASON, shipment_id)
    )
    for piece in pieces:
        _still_away(piece)
        engine.end_positions(
            run, plan, piece.lot_id, piece.interval, SHORTFALL_BOUNDARY, SHORTFALL_REASON
        )
    batch_id = engine.post(run, shipped.version, plan)
    event: RtvEvent = run.record(
        RtvEvent(
            movement=shipped.movement,
            kind=RtvEvent.Kind.SHORTFALL_CLOSURE,
            shipment=account.shipment,
            sequence_no=rtv._next_sequence(shipped.movement),
            quantity=proposal.quantity,
            lines=lines,
            details={
                **{key: proposal.details.get(key) for key in _CLOSURE_KEPT},
                "proposal_id": str(proposal.pk),
                "prepared_by": str(proposal.actor_id) if proposal.actor_id else None,
            },
            journal_batch_id=batch_id,
        )
    )
    after = _recount(shipped, event)
    _settle_shipment(run, shipped, after, event, "SHORTFALL_CLOSED")
    record_event(
        run,
        document_id,
        "rtv_shortfall_closed",
        version_id=shipped.version.pk,
        payload={"shipment_id": str(shipment_id), "quantity": event.quantity},
    )
    return {
        "movement_id": str(document_id),
        "shipment_id": str(shipment_id),
        "closed_qty": event.quantity,
        "rtv_state": shipped.movement.rtv_state,
    }


#: What a closure keeps of the proposal it applies.
_CLOSURE_KEPT = (
    "reason",
    "evidence_reference",
    "evidence_ids",
    "evidence_note",
    "reviewed_hash",
    "value_paise",
    "value_basis",
    "approval_request_id",
)


def register_approval_handlers() -> None:
    register_subject_handler(CLOSURE_SUBJECT, CLOSURE_APPROVE_ACTION, decide_closure)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def shipments(access: Any, movement: GoodsMovement) -> list[dict[str, Any]]:
    """Every shipment of an RTV: what it carried, how it is accounted for, what may follow."""
    from alerts.goods_models import GoodsException

    events = rtv._events(movement)
    accounts = rtv.shipment_accounts(events)
    if not accounts:
        return []
    names = rtv._names(e.actor_id for e in events)
    site_id = movement.document.site_id
    held_site = int(movement.document.held_site_id)
    can_execute = access.can(EXECUTE_ACTION, site_id=site_id)
    can_accept = access.can(ACCEPT_ACTION, site_id=site_id)
    can_verify = access.can(VERIFY_ACTION, site_id=site_id)
    can_prepare = access.can(PREPARE_CLOSURE_ACTION, site_id=site_id)
    can_close = access.can(CLOSURE_APPROVE_ACTION, site_id=site_id)
    # Whoever prepared a closure recorded its proposal, one of ``events``: named above.
    pending = _pending_closures([account.shipment.pk for account in accounts])
    show_value = (
        _value_visible(access, movement, site_id)
        if any(account.closures or account.proposals for account in accounts)
        else False
    )
    missing = set(
        GoodsException.objects.filter(
            kind=EWAY_EXCEPTION, subject_key=_subject(movement), state="open"
        ).values_list("source_event_key", flat=True)
    )
    out: list[dict[str, Any]] = []
    receiving = engine.system_location(held_site, "receiving")
    for account in accounts:
        shipment = account.shipment
        by_line = _waiting_by_line(account, held_site, receiving.pk)
        waiting = {
            key: ranges.total([p.interval for p in by_line.get(key, [])]) for key in account.shipped
        }
        eway = eway_state(account)
        eway["missing_exception_open"] = (
            engine.event_key("rtv_eway_missing", shipment.pk) in missing
        )
        actions: list[str] = []
        if not account.accounted and can_execute:
            actions += ["acknowledge", "return_to_source"]
        if any(waiting.values()) and can_accept:
            actions.append("putaway_returned")
        if can_execute:
            actions.append("eway_attach")
        if eway["reference"] is not None and not eway["verified"] and can_verify:
            actions.append("eway_verify")
        waiting_closure = pending.get(str(shipment.pk))
        if account.status == rtv.SHORT_ACKNOWLEDGED and can_prepare:
            actions.append("propose_closure")
        if (
            waiting_closure is not None
            and can_close
            and str(waiting_closure.maker_id) != str(access.human_id)
        ):
            actions.append("decide_closure")
        details = dict(shipment.details)
        out.append(
            {
                "id": str(shipment.pk),
                "sequence_no": shipment.sequence_no,
                "status": account.status,
                "shipped_at": shipment.event_at.isoformat(),
                "recorded_at": shipment.recorded_at.isoformat() if shipment.recorded_at else None,
                "recorded_by": _person(shipment.actor_id, names),
                "carrier": details.get("carrier"),
                "evidence_reference": details.get("evidence_reference"),
                "evidence_ids": list(details.get("evidence_ids") or []),
                "evidence_note": details.get("evidence_note"),
                "closed_damage_report_ids": list(details.get("closed_damage_report_ids") or []),
                "shipped_qty": account.shipped_qty,
                "acknowledged_qty": (
                    account.acknowledged_qty if account.acknowledged is not None else None
                ),
                "returned_qty": account.returned_qty,
                "closed_qty": account.closed_qty,
                "unaccounted_qty": account.unaccounted_qty,
                "awaiting_putaway_qty": sum(waiting.values()),
                "state_hash": account.state_hash(),
                "lines": [
                    {
                        "line_key": key,
                        "shipped_qty": qty,
                        "acknowledged_qty": (
                            account.acknowledged.get(key, 0)
                            if account.acknowledged is not None
                            else None
                        ),
                        "returned_qty": account.returned.get(key, 0),
                        "closed_qty": account.closed.get(key, 0),
                        "unaccounted_qty": account.unaccounted(key),
                        "awaiting_putaway_qty": waiting.get(key, 0),
                    }
                    for key, qty in account.shipped.items()
                ],
                "acknowledgements": [
                    {
                        "id": str(event.pk),
                        "acknowledged_at": event.event_at.isoformat(),
                        "recorded_at": event.recorded_at.isoformat() if event.recorded_at else None,
                        "recorded_by": _person(event.actor_id, names),
                        "quantity": event.quantity,
                        "shortfall_qty": int(event.details.get("shortfall_qty") or 0),
                        "recipient_reference": event.details.get("recipient_reference"),
                        "evidence_ids": list(event.details.get("evidence_ids") or []),
                        "evidence_note": event.details.get("evidence_note"),
                        "lines": [
                            {"line_key": line["line_key"], "qty": line["qty"]}
                            for line in event.lines
                        ],
                    }
                    for event in account.acknowledgements
                ],
                "returns": [
                    {
                        "id": str(event.pk),
                        "returned_at": event.event_at.isoformat(),
                        "recorded_at": event.recorded_at.isoformat() if event.recorded_at else None,
                        "recorded_by": _person(event.actor_id, names),
                        "quantity": event.quantity,
                        "reason": event.details.get("reason"),
                        "evidence_reference": event.details.get("evidence_reference"),
                        "evidence_ids": list(event.details.get("evidence_ids") or []),
                        "evidence_note": event.details.get("evidence_note"),
                        "damage_report_id": event.details.get("damage_report_id"),
                        "lines": [
                            {
                                "line_key": line["line_key"],
                                "qty": line["qty"],
                                "good": line["good"],
                                "damaged": line["damaged"],
                            }
                            for line in event.lines
                        ],
                    }
                    for event in account.returns
                ],
                "putaways": [
                    {
                        "id": str(event.pk),
                        "recorded_at": event.recorded_at.isoformat() if event.recorded_at else None,
                        "recorded_by": _person(event.actor_id, names),
                        "quantity": event.quantity,
                    }
                    for event in account.putaways
                ],
                "closures": [_closure_dto(event, names, show_value) for event in account.closures],
                "pending_closure": _pending_dto(account, waiting_closure, names, show_value),
                "eway": eway,
                "allowed_actions": actions,
            }
        )
    return out


def _value_visible(access: Any, movement: GoodsMovement, site_id: int | None) -> bool:
    """Money is shown only under the ``cost`` field grant over every line's brand."""
    from outbound.goods_writeoff import _brands

    head = DocumentHead.objects.select_related("live_version").get(document_id=movement.document_id)
    if head.live_version is None:
        return False
    skus = [str(b["sku_id"]) for b in rtv._official_lines(head.live_version) if b.get("sku_id")]
    brands: set[int | None] = set(_brands(skus).values()) or {None}
    return all(
        "cost" in access.field_grants(site_id=site_id, brand_id=brand, actions={VALUE_READ})
        for brand in brands
    )


def _evidence_dto(details: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "reason": details.get("reason"),
        "evidence_reference": details.get("evidence_reference"),
        "evidence_ids": list(details.get("evidence_ids") or []),
        "evidence_note": details.get("evidence_note"),
        "value_basis": details.get("value_basis"),
    }


def _money(details: Mapping[str, Any], show: bool) -> dict[str, Any]:
    """``value_paise`` only for a reader who may see money; null means unknown, never 0."""
    return {"value_paise": details.get("value_paise")} if show else {}


def _closure_dto(
    event: RtvEvent, names: Mapping[uuid.UUID, str], show_value: bool
) -> dict[str, Any]:
    prepared = event.details.get("prepared_by")
    return {
        "id": str(event.pk),
        "closed_at": event.recorded_at.isoformat() if event.recorded_at else None,
        "approved_by": _person(event.actor_id, names),
        "prepared_by": _person(uuid.UUID(str(prepared)), names) if prepared else None,
        "quantity": event.quantity,
        **_evidence_dto(event.details),
        **_money(event.details, show_value),
        "lines": [{"line_key": line["line_key"], "qty": line["qty"]} for line in event.lines],
    }


def _pending_dto(
    account: rtv.ShipmentAccount,
    request: Any,
    names: Mapping[uuid.UUID, str],
    show_value: bool,
) -> dict[str, Any] | None:
    """The prepared closure waiting for the Owner, with what the decision must quote."""
    if request is None:
        return None
    proposal = next(
        (
            event
            for event in reversed(account.proposals)
            if event.details.get("approval_request_id") == str(request.pk)
        ),
        None,
    )
    if proposal is None:  # pragma: no cover - a request is created with its proposal
        return None
    return {
        "approval_request_id": str(request.pk),
        "reviewed_hash": request.reviewed_hash,
        "revision": request.revision,
        "prepared_at": proposal.recorded_at.isoformat() if proposal.recorded_at else None,
        "prepared_by": _person(proposal.actor_id, names),
        "quantity": proposal.quantity,
        **_evidence_dto(proposal.details),
        **_money(proposal.details, show_value),
        "lines": [{"line_key": line["line_key"], "qty": line["qty"]} for line in proposal.lines],
    }


def _person(actor_id: uuid.UUID | None, names: Mapping[uuid.UUID, str]) -> dict[str, str] | None:
    if actor_id is None:
        return None
    return {"id": str(actor_id), "name": names.get(actor_id, "")}
