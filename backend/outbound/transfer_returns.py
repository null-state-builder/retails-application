"""A failed delivery, recorded back at the source as it actually came back (goods ticket 13C).

Transfers PRD §6 and goods PRD §14.9.2: if dispatched goods come back, record
the actual receipt at the source, linked to the original movement and
shipment, without deleting the departure or claiming the destination received
anything. Four facts hold the whole way through:

* **Only what is physically back is accounted for.** A return receipt names,
  per line, how many pieces came back good and how many damaged. Whatever it
  does not name stays in transit under the transfer - still the shipment's,
  still unresolved - and the shipment is ``partly_returned`` until later
  receipts bring the rest. An absent balance is never closed by a return: it
  is owned work (``transfer_discrepancy``, ``RETURN_INCOMPLETE``) at the
  source, resolved only when the last piece is recorded back.
* **Returned goods are received like any goods.** Good pieces come ashore into
  the source's receiving location, unaccepted and not sellable or sendable,
  until somebody at the source accepts and puts them away. Damaged pieces go
  to the source's quarantine under a damage hold and open the same pending
  report damage anywhere else opens. A piece that travelled in any condition
  other than good keeps it and goes to quarantine: nothing is upgraded by
  coming back, and no hold is lifted by it.
* **Nothing is rewritten.** The dispatch, its departure, its e-way facts and
  its origins stay exactly as they were; no destination count is written and
  none may follow, because a delivery that failed was not also received. Each
  piece keeps its origin and its value: the return is a transit-to-site move
  at unchanged value (the check leg of P09, at the source).
* **A receipt counts once.** ``receipt_key`` binds the whole receipt: the same
  key with the same content has one effect however often it is sent; with
  different content it is refused. A receipt for more than is still in
  transit, for a line the shipment did not carry, or at any site other than
  the source is refused and writes nothing.

Transport-provider rerouting is out of scope. Lock order, as in
``outbound.transfers``: SITE → DOCUMENT (transfer, head, dispatch) → LOT.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from alerts.goods_services import open_exception, resolve_exceptions
from core.canonical import content_hash
from core.commands import CommandRun
from core.refusals import Refusal, issue
from outbound import transfers
from outbound.goods_models import GoodsTransfer, TransferDispatch, TransferEvent, TransferReturn
from stockledger import goods_engine as engine
from stockledger import ranges

RETURN_FIELDS = frozenset(
    {"receipt_key", "site_id", "reason", "returned_at", "evidence_reference", "note", "lines"}
)
RETURN_REQUIRED = ("receipt_key", "site_id", "reason", "lines")
RETURN_LINE_FIELDS = frozenset({"line_key", "good", "damaged"})
#: What a returning piece can be found as. The pieces are the shipment's own,
#: identified by the shipment, so "wrong" and "unidentified" are not answers
#: here; goods that are not the shipment's are not a return of it.
RETURN_CONDITIONS = ("good", "damaged")

ACCEPT_RETURNED_FIELDS = transfers.ACCEPT_FIELDS

#: The missing rest of a partly returned shipment is owned work at the source.
INCOMPLETE_REASON = "RETURN_INCOMPLETE"
RETURN_DAMAGE_REASON = "TRANSFER_RETURN_DAMAGE"


@dataclass(frozen=True)
class ReturnLine:
    line_key: uuid.UUID
    good: int
    damaged: int

    @property
    def qty(self) -> int:
        return self.good + self.damaged


@dataclass(frozen=True)
class ReturnReceipt:
    receipt_key: uuid.UUID
    site_id: int
    reason: str
    returned_at: datetime
    evidence_reference: str | None
    note: str | None
    lines: tuple[ReturnLine, ...]

    @property
    def quantity(self) -> int:
        return sum(line.qty for line in self.lines)

    def fingerprint(self) -> str:
        """What a receipt key binds. The business time is left out, as for a
        scan: a device that resends without one gets "now" again, and that is
        not a different receipt."""
        return content_hash(
            {
                "site_id": self.site_id,
                "reason": self.reason,
                "evidence_reference": self.evidence_reference,
                "note": self.note,
                "lines": sorted(
                    [str(line.line_key), line.good, line.damaged] for line in self.lines
                ),
            }
        )


def _count(value: Any, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= transfers.MAX_QTY:
        raise transfers._invalid(f"{field} is a whole number from 0 to {transfers.MAX_QTY}.", field)
    return int(value)


def parse_return(body: dict[str, Any], now: datetime) -> ReturnReceipt:
    transfers._closed(body, RETURN_FIELDS)
    raw = body.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= transfers.MAX_LINES:
        raise transfers._invalid(f"A return receipt has 1 to {transfers.MAX_LINES} lines.", "lines")
    lines: list[ReturnLine] = []
    seen: set[uuid.UUID] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise transfers._invalid("Every return line is an object.", "lines")
        transfers._closed(item, RETURN_LINE_FIELDS)
        key = transfers._uuid(item.get("line_key"), "line_key")
        if key in seen:
            raise transfers._invalid("Every return line names a different shipped line.", "lines")
        seen.add(key)
        lines.append(
            ReturnLine(
                line_key=key,
                good=_count(item.get("good"), "good"),
                damaged=_count(item.get("damaged"), "damaged"),
            )
        )
    if sum(line.qty for line in lines) == 0:
        raise transfers._invalid(
            "A return receipt records at least one piece that is physically back.", "lines"
        )
    reason = transfers._text(body.get("reason"), "reason", limit=500)
    assert reason is not None
    return ReturnReceipt(
        receipt_key=transfers._uuid(body.get("receipt_key"), "receipt_key"),
        site_id=transfers._site_id(body.get("site_id"), "site_id"),
        reason=reason,
        returned_at=transfers._moment(body.get("returned_at"), "returned_at", now),
        evidence_reference=transfers._text(
            body.get("evidence_reference"), "evidence_reference", limit=100, required=False
        ),
        note=transfers._text(body.get("note"), "note", limit=500, required=False),
        lines=tuple(lines),
    )


def _subject(record: TransferDispatch) -> str:
    return f"transfer_dispatch:{record.pk}"


def _incomplete_key(record: TransferDispatch) -> uuid.UUID:
    return engine.event_key("transfer_return_incomplete", record.pk)


# ---------------------------------------------------------------------------
# The return receipt
# ---------------------------------------------------------------------------


def return_to_source(
    run: CommandRun, transfer_id: uuid.UUID, dispatch_id: uuid.UUID, receipt: ReturnReceipt
) -> TransferDispatch:
    """Record what of one shipment is physically back at the source, and nothing more.

    Only the source's goods fence applies: the goods come ashore there, and the
    destination - which never received them - is not touched.
    """
    transfer, _head = transfers._locked(run, transfer_id, at="source")
    record = transfers._locked_dispatch(run, transfer, dispatch_id)
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.source_site_id
    known = TransferReturn.objects.filter(dispatch=record, receipt_key=receipt.receipt_key).first()
    if known is not None:
        if known.content_hash != receipt.fingerprint():
            raise Refusal(
                "COMMAND_CONFLICT",
                "That receipt_key was already used for a different return of this shipment.",
                issues=[
                    issue(
                        "RECEIPT_KEY_REUSED",
                        "Use a new receipt_key",
                        field=str(receipt.receipt_key),
                    )
                ],
            )
        # The same receipt again: it already had its one effect.
        run.audit_after = {"dispatch_id": str(record.pk), "return_id": str(known.pk)}
        return record
    _refuse_wrong_site(transfer, receipt)
    if record.state in (TransferDispatch.State.COUNTED, TransferDispatch.State.ACCEPTED):
        raise transfers._state_conflict(
            "The destination has already counted this shipment. Goods that arrived "
            "cannot also be recorded as never delivered."
        )
    if record.state == TransferDispatch.State.RETURNED_TO_SOURCE:
        raise transfers._state_conflict(
            "Every piece of this shipment is already recorded back at the source."
        )
    if receipt.returned_at < record.dispatched_at:
        raise Refusal("EVENT_TIME_INVALID", "Goods cannot come back before they left.", status=422)
    moves = _plan(record, receipt)
    engine.lock_lots(run, sorted({piece.lot_id for piece, _c, _k in moves}, key=str))
    version = transfers._pt_version(transfer)
    source = transfer.source_site_id
    receiving = engine.system_location(source, "receiving")
    quarantine = engine.system_location(source, "quarantine")
    plan = engine.Plan(
        transfers.CHECK_POSTING,
        version.pk,
        engine.event_key("transfer_return", record.pk, receipt.receipt_key),
    )
    damaged: list[dict[str, Any]] = []
    # A held-custody transfer's goods (goods tickets 13D, 13E) left held and
    # come back held: all of them to the source's quarantine, never to its
    # receiving.
    quarantined = transfers.held_custody(transfer)
    for piece, condition, line_key in moves:
        engine.change_address(
            run,
            plan,
            piece.lot_id,
            piece.interval,
            lambda old, cond=condition: replace(
                old,
                boundary="physical",
                site_id=source,
                # Good only if it left good and came back good; anything else is
                # quarantined and keeps the worse of the two conditions.
                location_id=(
                    receiving.pk
                    if cond == "good" and old.condition == "good" and not quarantined
                    else quarantine.pk
                ),
                transfer_id=None,
                condition=old.condition if old.condition != "good" else cond,
                # Back at the source but not accepted there: acceptance belongs
                # to whoever physically puts the goods away, and nobody has yet.
                accepted_event_id=None,
            ),
        )
        if condition != "damaged":
            continue
        key = uuid.uuid5(record.pk, f"return-damaged:{piece.lot_id}:{piece.interval[0]}")
        engine.place_hold(
            run,
            plan,
            lot_id=piece.lot_id,
            interval=piece.interval,
            hold_key=key,
            kind=transfers.DAMAGE_HOLD,
            site_id=source,
            source_version_id=version.pk,
        )
        damaged.append(
            _damage_line(piece, line_key, quarantine.pk if quarantined else receiving.pk, key)
        )
    engine.post(run, version, plan)
    row: TransferReturn = run.record(
        TransferReturn(
            dispatch=record,
            receipt_key=receipt.receipt_key,
            site_id=source,
            reason=receipt.reason,
            evidence_reference=receipt.evidence_reference,
            note=receipt.note,
            lines=[
                {"line_key": str(line.line_key), "good": line.good, "damaged": line.damaged}
                for line in receipt.lines
            ],
            quantity=receipt.quantity,
            content_hash=receipt.fingerprint(),
        ),
        event_at=receipt.returned_at,
    )
    record.returned_qty = int(record.returned_qty) + receipt.quantity
    record.return_reason = record.return_reason or receipt.reason
    still = transfers.shipped_qty(record) - record.returned_qty
    if still == 0:
        record.state = TransferDispatch.State.RETURNED_TO_SOURCE
        record.returned_at = receipt.returned_at
    else:
        record.state = TransferDispatch.State.PARTLY_RETURNED
    record.save(update_fields=["state", "returned_qty", "return_reason", "returned_at"])
    _track_the_rest(run, transfer, record, still)
    if damaged:
        from outbound.damage_review import open_for_dispatch

        open_for_dispatch(
            run,
            dispatch=record,
            site_id=source,
            reason_code=RETURN_DAMAGE_REASON,
            lines=damaged,
        )
    transfers._log(
        run,
        transfer,
        TransferEvent.Kind.RETURNED,
        site_id=source,
        details={
            "dispatch_id": str(record.pk),
            "sequence_no": record.sequence_no,
            "return_id": str(row.pk),
            "receipt_key": str(receipt.receipt_key),
            "quantity": receipt.quantity,
            "good": sum(line.good for line in receipt.lines),
            "damaged": sum(line.damaged for line in receipt.lines),
            "still_in_transit": still,
            "reason": receipt.reason,
            "evidence_reference": receipt.evidence_reference,
        },
        actual_at=receipt.returned_at,
    )
    transfers._close_if_settled(run, transfer, record)
    run.audit_after = {
        "dispatch_id": str(record.pk),
        "return_id": str(row.pk),
        "state": record.state,
        "returned_qty": receipt.quantity,
        "still_in_transit": still,
    }
    return record


def _refuse_wrong_site(transfer: GoodsTransfer, receipt: ReturnReceipt) -> None:
    if receipt.site_id == transfer.source_site_id:
        return
    message = (
        "A failed delivery is recorded back at the site it left from. Goods taken "
        "anywhere else are not a return to source, and rerouting is not recorded here."
    )
    raise Refusal(
        "TRANSFER_INVALID",
        message,
        status=422,
        issues=[issue("RETURN_NOT_AT_SOURCE", message, field="site_id")],
    )


def _plan(
    record: TransferDispatch, receipt: ReturnReceipt
) -> list[tuple[transfers.FrozenPortion, str, uuid.UUID]]:
    """Cut each named line's still-in-transit pieces into what came back, by condition."""
    by_key = {uuid.UUID(str(line["line_key"])): line for line in record.lines}
    moves: list[tuple[transfers.FrozenPortion, str, uuid.UUID]] = []
    for item in receipt.lines:
        line = by_key.get(item.line_key)
        if line is None:
            raise Refusal("NOT_FOUND", "That line is not on this shipment.")
        pool = transfers._transit_pieces(record, line)
        still = sum(ranges.length(piece.interval) for piece in pool)
        if item.qty > still:
            message = (
                f"Only {still} piece(s) of that line are still unaccounted for on this "
                f"shipment, and the receipt says {item.qty} came back. A return records "
                "only what is physically back."
            )
            raise Refusal(
                "TRANSFER_INVALID",
                message,
                status=422,
                issues=[
                    issue(
                        "RETURN_EXCEEDS_TRANSIT",
                        message,
                        line_key=item.line_key,
                        quantity=still,
                    )
                ],
            )
        for condition in RETURN_CONDITIONS:
            for piece in transfers._take(pool, getattr(item, condition)):
                moves.append((piece, condition, item.line_key))
    return moves


def _damage_line(
    piece: transfers.FrozenPortion, line_key: uuid.UUID, receiving: uuid.UUID, key: uuid.UUID
) -> Any:
    return {
        "line_key": str(line_key),
        "lot_id": str(piece.lot_id),
        "sku_id": None,
        "origin_id": str(piece.origin_id) if piece.origin_id else None,
        "qty": ranges.length(piece.interval),
        # Where a rejected report puts the pieces back: the source's receiving,
        # waiting to be accepted - what they would have been had nobody called
        # them damaged.
        "source_location_id": str(receiving),
        "hold_keys": [str(key)],
        "portions": [
            {
                "lot_id": str(piece.lot_id),
                "lower": piece.interval[0],
                "upper": piece.interval[1],
                "condition": "good",
            }
        ],
    }


def _track_the_rest(
    run: CommandRun, transfer: GoodsTransfer, record: TransferDispatch, still: int
) -> None:
    """What has not come back is owned, unresolved work at the source - until it does."""
    if still > 0:
        open_exception(
            run,
            kind=transfers.SHORTAGE_EXCEPTION,
            site_id=transfer.source_site_id,
            subject_key=_subject(record),
            reason_code=INCOMPLETE_REASON,
            source_event_key=_incomplete_key(record),
            allowed_resolution_actions=transfers.RESOLUTION_ACTIONS,
            note=f"transfer:{transfer.pk}",
        )
        return
    resolve_exceptions(
        run,
        kind=transfers.SHORTAGE_EXCEPTION,
        subject_key=_subject(record),
        reason_code="RETURNED_TO_SOURCE",
        source_event_key=_incomplete_key(record),
    )


# ---------------------------------------------------------------------------
# Accepting returned goods back at the source
# ---------------------------------------------------------------------------


def accept_returned(
    run: CommandRun, transfer_id: uuid.UUID, dispatch_id: uuid.UUID, lines: list[dict[str, Any]]
) -> TransferDispatch:
    """Put returned good pieces away at the source, where they become sendable and sellable again.

    The same acceptance evidence a destination writes, written by the source
    for its own receiving. The shipment's return state is untouched: this is
    the source receiving its goods, not a change in what came back.
    """
    transfer, _head = transfers._locked(run, transfer_id, at="source")
    record = transfers._locked_dispatch(run, transfer, dispatch_id)
    if not record.returned_qty:
        raise transfers._state_conflict("Nothing from this shipment has come back yet.")
    site_id = transfer.source_site_id
    accepted, remaining = transfers.put_away(
        run, transfer, record, lines, site_id=site_id, event_name="transfer_return_accept"
    )
    transfers._log(
        run,
        transfer,
        TransferEvent.Kind.ACCEPT,
        site_id=site_id,
        details={
            "dispatch_id": str(record.pk),
            "returned_goods": True,
            "quantity": accepted,
            "still_to_accept": remaining,
        },
    )
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = site_id
    run.audit_after = {"dispatch_id": str(record.pk), "accepted_qty": accepted}
    return record


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def return_actions(
    access: Any, transfer: GoodsTransfer, records: list[TransferDispatch]
) -> list[str]:
    """Recording a return, and putting returned goods away, both at the source.

    A return is offered for as long as any shipment is still unaccounted for
    and uncounted; putting away, while returned good pieces wait in the source's
    receiving.
    """
    out: list[str] = []
    source = transfer.source_site_id
    if any(
        r.state in (TransferDispatch.State.IN_TRANSIT, TransferDispatch.State.PARTLY_RETURNED)
        for r in records
    ) and access.can(transfers.MOVE_ACTION, site_id=source):
        out.append("return_to_source")
    if access.can(transfers.ACCEPT_ACTION, site_id=source) and any(
        waiting_at_source(record, transfer) for record in records if record.returned_qty
    ):
        out.append("accept_returned")
    return out


def waiting_at_source(record: TransferDispatch, transfer: GoodsTransfer) -> int:
    """Returned good pieces of this shipment standing unaccepted in the source's receiving."""
    site_id = transfer.source_site_id
    return transfers._unaccepted_good(record, site_id, engine.system_location(site_id, "receiving"))


def returns_of(
    record: TransferDispatch, transfer: GoodsTransfer, names: dict[uuid.UUID, str]
) -> dict[str, Any]:
    """The shipment's return receipts, what each line got back, and what waits to be put away."""
    if not record.returned_qty:
        return {"receipts": [], "by_line": {}, "waiting": {}}
    rows = list(TransferReturn.objects.filter(dispatch=record).order_by("recorded_at", "id"))
    by_line: dict[str, int] = {}
    for row in rows:
        for line in row.lines:
            key = str(line["line_key"])
            by_line[key] = by_line.get(key, 0) + int(line["good"]) + int(line["damaged"])
    site_id = transfer.source_site_id
    receiving = engine.system_location(site_id, "receiving")
    waiting = {
        str(line["line_key"]): sum(
            ranges.length(piece.interval)
            for piece in transfers._arrived_good(record, line, site_id, receiving)
        )
        for line in record.lines
    }
    return {
        "receipts": [
            {
                "id": str(row.pk),
                "receipt_key": str(row.receipt_key),
                "site_id": str(row.site_id),
                "returned_at": row.event_at.isoformat(),
                "recorded_at": row.recorded_at.isoformat(),
                "recorded_by": {
                    "id": str(row.actor_id),
                    "name": names.get(row.actor_id, "") if row.actor_id else "",
                },
                "reason": row.reason,
                "evidence_reference": row.evidence_reference,
                "note": row.note,
                "quantity": row.quantity,
                "lines": row.lines,
            }
            for row in rows
        ],
        "by_line": by_line,
        "waiting": waiting,
    }
