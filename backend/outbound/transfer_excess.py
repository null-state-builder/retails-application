"""Excess and wrong goods at a transfer's destination, and corrective transfers (goods ticket 16).

Transfers PRD §5, goods PRD §5.7 and §14.9.2, design §7.2 P10/P17 and §7.3,
GSA-R01. What the destination count finds that nobody sent is recorded as it
was found, and resolved only through a named route:

* **An observation, not stock.** Goods in the box nobody sent - and goods that
  came *in place of* expected ones (``wrong``) - are one excess observation
  each: a ``ScanObservation`` bound to the shipment whose count recorded it,
  carrying the identity evidence the counter had, and an unvalued
  ``transfer_excess`` custody lot in the destination's excess hold, under its
  own hold (P10). Nothing is copied from the source: no identity, cost or stock
  layer, and the approved movement and its reservation never grow.
* **Wrong goods are two decisions.** The expected pieces did not come, so they
  stay in transit, short, and are resolved as a shortage (``transfer_shortages``);
  what came instead is an excess observation. The two carry one ``pairing_key``
  and each has its own numbered GAP decision. An official line is never given
  another item's barcode.
* **Owned until resolved.** Every observation opens a ``transfer_discrepancy``
  (``TRANSFER_EXCESS`` or ``WRONG_GOODS``) at the destination with its follow-up
  date. It closes only when a corrective transfer has matched the whole
  observation - never by a generic close, never by an adjustment (GSA-R03, 15A
  refuses it) and never through a supplier receipt route (05D).
* **The corrective transfer.** The source, holding evidence that the goods are
  its own, proposes one: a transfer of that item from the source's *eligible*
  stock to the destination, linked to the original movement, frozen at once and
  sent for approval. A source that cannot show eligible stock is refused
  (``EXCESS_EVIDENCE_MISSING``) and the excess stays unvalued and visible; its
  valuation route is ticket 16A's. The proposal claims an exact interval of the
  observation, so two corrective transfers can never claim the same pieces.
* **Distinct approval.** The Owner approves the corrective transfer through the
  ordinary transfer approval - never its proposer (GSA-R01) - which reserves the
  source pieces and numbers the excess-observed GAP decision.
* **One confirmation, one match.** The corrective transfer never ships: its
  goods are already at the destination. The source confirms it once, with its
  evidence, and that one command records the dispatch/arrival pair (P17): the
  reserved source pieces, their origins kept, leave the source and arrive at the
  destination's receiving, unaccepted; the claimed observation interval leaves
  custody for the ``matched_observation`` boundary with a ``CustodyMatch`` per
  piece. The destination's physical quantity does not rise a second time, no
  destination GRN or purchase value is made, and acceptance puts the pieces away
  through the ordinary acceptance of the corrective transfer's shipment.

Lock order, as everywhere: SITE → DOCUMENT (transfer, GAP decision) → LOT → SERIES.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from alerts.goods_services import open_exception, resolve_exceptions
from core.commands import CommandRun, LockRank
from core.goods_documents import (
    append_revision,
    lock_heads,
    new_document,
    officialise,
    record_event,
    set_state,
)
from core.goods_fields import bounds, portion
from core.kernel_models import DocumentHead, DocumentIdentity
from core.numbering import allocate
from core.refusals import Refusal, issue
from inbound.goods_models import ScanObservation
from outbound import transfers
from outbound.goods_models import GapResolution, GoodsTransfer, TransferDispatch, TransferEvent
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import CustodyMatch

#: The source drafts the corrective transfer, with the grant that drafts any transfer.
PROPOSE_ACTION = transfers.ALLOCATE_ACTION
#: The source records the corrective confirmation, as it records any departure.
CONFIRM_ACTION = transfers.MOVE_ACTION

EXCESS_HOLD = "transfer_excess"
EXCESS_LOCATION = "excess_hold"
#: Owned work over one observation (``transfer_excess:{lot}``).
EXCESS_EXCEPTION = transfers.SHORTAGE_EXCEPTION
EXCESS_REASON = "TRANSFER_EXCESS"
WRONG_REASON = "WRONG_GOODS"
MATCHED_REASON = "CORRECTIVE_MATCHED"
#: Design §7.2: the corrective confirmation event pair.
MATCH_POSTING = "P17"
MATCHED_BOUNDARY = "matched_observation"
GAP_DOC_TYPE = "GAP"
RESOLUTION = "corrective_transfer"

MAX_ENTRIES = 50
ENTRY_FIELDS = frozenset({"description", "qty", "sku_id", "alias_value", "in_place_of_line_key"})
PROPOSE_FIELDS = frozenset({"sku_id", "qty", "source_evidence_reference", "reason", "origin_ids"})
PROPOSE_REQUIRED = ("sku_id", "qty", "source_evidence_reference", "reason")
CONFIRM_FIELDS = frozenset({"source_evidence_reference", "confirmed_at", "note"})
CONFIRM_REQUIRED = ("source_evidence_reference",)


def subject_of(lot_id: Any) -> str:
    return f"transfer_excess:{lot_id}"


# ---------------------------------------------------------------------------
# The count: what came that nobody sent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    description: str
    qty: int
    sku_id: uuid.UUID | None
    alias_value: str | None
    #: Set on wrong goods: the shipped line whose expected pieces this came in place of.
    in_place_of: uuid.UUID | None


def parse_entries(raw: Any) -> list[Entry]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ENTRIES:
        raise transfers._invalid(f"excess is a list of at most {MAX_ENTRIES} entries.", "excess")
    out: list[Entry] = []
    for item in raw:
        if not isinstance(item, dict):
            raise transfers._invalid("Every excess entry is an object.", "excess")
        transfers._closed(item, ENTRY_FIELDS)
        description = transfers._text(item.get("description"), "description", limit=240)
        assert description is not None
        out.append(
            Entry(
                description=description,
                qty=transfers._qty(item.get("qty"), "qty"),
                sku_id=transfers._optional_uuid(item.get("sku_id"), "sku_id"),
                alias_value=transfers._text(
                    item.get("alias_value"), "alias_value", limit=128, required=False
                ),
                in_place_of=transfers._optional_uuid(
                    item.get("in_place_of_line_key"), "in_place_of_line_key"
                ),
            )
        )
    return out


def check_entries(
    run: CommandRun,
    record: TransferDispatch,
    rows: Sequence[dict[str, Any]],
    entries: Sequence[Entry],
) -> None:
    """Every wrong piece names what came instead, and nothing else is invented.

    A line's ``wrong`` says how many expected pieces did not come because other
    goods came in their place; the excess entries that name that line say what
    those goods were. The two must agree, so a wrong piece is never recorded
    without its identity evidence, and a wrong-goods observation never exists
    without the expected pieces it stands against.
    """
    from masters.goods_identity_models import ProductSku

    shipped = {uuid.UUID(str(line["line_key"])) for line in record.lines}
    named: dict[uuid.UUID, int] = {}
    for entry in entries:
        if entry.in_place_of is not None:
            if entry.in_place_of not in shipped:
                raise Refusal("NOT_FOUND", "That line is not on this shipment.")
            named[entry.in_place_of] = named.get(entry.in_place_of, 0) + entry.qty
        if (
            entry.sku_id is not None
            and not ProductSku.objects.filter(tenant_id=run.tenant_id, pk=entry.sku_id).exists()
        ):
            raise Refusal("NOT_FOUND", "That item was not found.")
    for row in rows:
        wrong = int(row["wrong"])
        said = named.get(row["line_key"], 0)
        if wrong != said:
            message = (
                f"A line counts {wrong} wrong piece(s) and the excess names {said} that came "
                "in their place. Say what came instead of every wrong piece, and only those."
            )
            raise Refusal(
                "INVALID_REQUEST",
                message,
                issues=[issue("WRONG_GOODS_UNNAMED", message, field="excess")],
            )


def record(
    run: CommandRun,
    plan: engine.Plan,
    transfer: GoodsTransfer,
    record: TransferDispatch,
    entries: Sequence[Entry],
    counted_at: datetime,
) -> list[dict[str, Any]]:
    """Each observation, with its evidence, as its own unvalued held custody lot (P10)."""
    if not entries:
        return []
    site_id = transfer.destination_site_id
    hold = engine.system_location(site_id, EXCESS_LOCATION)
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        condition = "wrong" if entry.in_place_of is not None else "good"
        pairing = uuid.uuid5(record.pk, f"wrong:{index}") if entry.in_place_of else None
        observation = run.record(
            ScanObservation(
                transfer_dispatch_id=record.pk,
                site_id=site_id,
                scan_key=uuid.uuid5(record.pk, f"excess:{index}"),
                sku_id=entry.sku_id,
                description=entry.description,
                alias_value=entry.alias_value,
                condition=condition,
                qty=entry.qty,
                location_id=hold.pk,
            ),
            event_at=counted_at,
        )
        lot = engine.open_lot(
            run,
            plan,
            source_kind="transfer_excess",
            site_id=site_id,
            qty=entry.qty,
            identity={
                "description": entry.description,
                "sku_id": str(entry.sku_id) if entry.sku_id else None,
                "alias_value": entry.alias_value,
                "transfer_id": str(transfer.pk),
                "dispatch_id": str(record.pk),
                "in_place_of_line_key": str(entry.in_place_of) if entry.in_place_of else None,
            },
            source_time=counted_at,
            address=engine.Address(
                boundary="physical",
                site_id=site_id,
                location_id=hold.pk,
                condition=condition,
                sku_id=entry.sku_id,
            ),
            source_observation_id=observation.pk,
            from_boundary="transfer_excess_observation",
        )
        key = uuid.uuid5(record.pk, f"excess:{index}")
        engine.place_hold(
            run,
            plan,
            lot_id=lot.pk,
            interval=(0, entry.qty),
            hold_key=key,
            kind=EXCESS_HOLD,
            site_id=site_id,
            source_version_id=None,
        )
        out.append(
            {
                "lot_id": str(lot.pk),
                "observation_id": str(observation.pk),
                "qty": entry.qty,
                "description": entry.description,
                "sku_id": str(entry.sku_id) if entry.sku_id else None,
                "alias_value": entry.alias_value,
                "in_place_of_line_key": str(entry.in_place_of) if entry.in_place_of else None,
                "pairing_key": str(pairing) if pairing else None,
                "hold_key": str(key),
                "condition": condition,
            }
        )
    return out


def open_work(
    run: CommandRun,
    transfer: GoodsTransfer,
    record: TransferDispatch,
    excess: Sequence[dict[str, Any]],
) -> None:
    """Each observation is owned work at the destination until its route resolves it."""
    for entry in excess:
        open_exception(
            run,
            kind=EXCESS_EXCEPTION,
            site_id=transfer.destination_site_id,
            subject_key=subject_of(entry["lot_id"]),
            reason_code=WRONG_REASON if entry.get("in_place_of_line_key") else EXCESS_REASON,
            source_event_key=engine.event_key("transfer_excess", entry["lot_id"]),
            allowed_resolution_actions=transfers.RESOLUTION_ACTIONS,
            note=f"transfer_dispatch:{record.pk}",
        )


# ---------------------------------------------------------------------------
# Reading an observation
# ---------------------------------------------------------------------------


def entries_of(record: TransferDispatch) -> list[dict[str, Any]]:
    return list((record.count or {}).get("excess") or [])


def find(transfer: GoodsTransfer, lot_id: uuid.UUID) -> tuple[TransferDispatch, dict[str, Any]]:
    """The shipment whose count recorded observation ``lot_id``, and its entry."""
    for record in TransferDispatch.objects.filter(transfer=transfer).order_by("sequence_no"):
        for entry in entries_of(record):
            if entry.get("lot_id") == str(lot_id):
                return record, entry
    raise Refusal("NOT_FOUND", "That excess was not found on this transfer.")


def _held(lot_id: uuid.UUID, site_id: int) -> list[ranges.Interval]:
    """What of the observation still stands in the destination's excess hold."""
    hold = engine.system_location(site_id, EXCESS_LOCATION)
    return ranges.normalise(
        [
            bounds(p.portion)
            for p in engine.positions_of(lot_id)
            if p.boundary == "physical" and p.site_id == site_id and p.location_id == hold.pk
        ]
    )


def _portions(gap: GapResolution) -> list[tuple[uuid.UUID, ranges.Interval]]:
    return [
        (transfers._lot(piece), transfers._interval(piece))
        for line in gap.payload.get("lines", [])
        for piece in line["portions"]
    ]


OPEN_STATES = (GapResolution.State.PENDING, GapResolution.State.APPROVED)


def _claimed(lot_id: uuid.UUID) -> list[ranges.Interval]:
    """Intervals of the observation an open or matched corrective decision names."""
    out: list[ranges.Interval] = []
    for gap in GapResolution.objects.filter(
        kind=GapResolution.Kind.EXCESS, state__in=OPEN_STATES, payload__lot_id=str(lot_id)
    ):
        out.extend(interval for lot, interval in _portions(gap) if lot == lot_id)
    return ranges.normalise(out)


def matched_qty(lot_id: uuid.UUID) -> int:
    return sum(
        ranges.length(bounds(row.observed_portion))
        for row in CustodyMatch.objects.filter(observed_lot_id=lot_id)
    )


def unclaimed(lot_id: uuid.UUID, site_id: int) -> list[ranges.Interval]:
    """What of the observation is still held and named by no corrective decision."""
    return ranges.subtract(_held(lot_id, site_id), _claimed(lot_id))


# ---------------------------------------------------------------------------
# Proposing a corrective transfer (E232)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    sku_id: uuid.UUID
    qty: int
    source_evidence_reference: str
    reason: str
    origin_ids: tuple[uuid.UUID, ...]


def parse_proposal(body: dict[str, Any]) -> Proposal:
    transfers._closed(body, PROPOSE_FIELDS)
    raw_origins = body.get("origin_ids") or []
    if not isinstance(raw_origins, list) or len(raw_origins) > 50:
        raise transfers._invalid("origin_ids is a list of at most 50 origins.", "origin_ids")
    evidence = transfers._text(
        body.get("source_evidence_reference"), "source_evidence_reference", limit=100
    )
    reason = transfers._text(body.get("reason"), "reason", limit=500)
    assert evidence is not None and reason is not None
    return Proposal(
        sku_id=transfers._uuid(body.get("sku_id"), "sku_id"),
        qty=transfers._qty(body.get("qty"), "qty"),
        source_evidence_reference=evidence,
        reason=reason,
        origin_ids=tuple(transfers._uuid(o, "origin_ids") for o in raw_origins),
    )


def _evidence_missing(message: str) -> Refusal:
    return Refusal("EXCESS_EVIDENCE_MISSING", message, status=409)


def propose(
    run: CommandRun, transfer_id: uuid.UUID, lot_id: uuid.UUID, proposal: Proposal
) -> GapResolution:
    """Draft, freeze and send for approval a corrective transfer for exact observed pieces.

    The source says the goods are its own item, with its evidence; the source
    must hold that many eligible pieces of it, which are frozen now exactly as
    any transfer's are. The claimed interval of the observation is named by this
    decision alone from here on. Nothing moves and nothing is reserved.
    """
    original, _head = transfers._locked(run, transfer_id)
    if original.corrective_for_id is not None:
        raise Refusal("NOT_FOUND", "That excess was not found on this transfer.")
    record, entry = find(original, lot_id)
    observed_sku = entry.get("sku_id")
    if observed_sku and observed_sku != str(proposal.sku_id):
        raise _evidence_missing(
            "The destination recorded these goods as a different item. A corrective "
            "transfer carries only the item that was observed."
        )
    source = original.source_site_id
    line = transfers.DraftLine(
        line_key=uuid.uuid4(),
        sku_id=proposal.sku_id,
        qty=proposal.qty,
        origin_ids=proposal.origin_ids,
        note=None,
    )
    engine.lock_lots(run, sorted({lot_id, *transfers._candidate_lots(source, [line])}, key=str))
    free = unclaimed(lot_id, original.destination_site_id)
    still = ranges.total(free)
    if proposal.qty > still:
        message = (
            f"Only {still} piece(s) of this excess are still held and not already named "
            f"by a corrective transfer, and this names {proposal.qty}."
        )
        raise Refusal(
            "GAP_ALREADY_RESOLVED",
            message,
            status=409,
            issues=[issue("GAP_ALREADY_RESOLVED", message, quantity=still)],
        )
    try:
        engine.select_fifo(
            source, proposal.sku_id, proposal.qty, origin_ids=line.origin_ids or None
        )
    except Refusal as refused:
        if refused.code != "INSUFFICIENT_ELIGIBLE_STOCK":
            raise
        raise _evidence_missing(
            "The source cannot show that many eligible pieces of this item, so no corrective "
            "transfer can carry them. The excess stays held, unvalued and visible."
        ) from refused
    corrective, corrective_head = transfers.create_locked(
        run,
        transfers.DraftPayload(
            source_site_id=source,
            destination_site_id=original.destination_site_id,
            request_id=None,
            note=f"Corrective for {original.document.official_number or original.pk}"[:500],
            lines=(line,),
        ),
        corrective_for=original,
    )
    transfers.submit_locked(run, corrective, corrective_head, [line])
    claimed = _first(free, proposal.qty)
    check = (
        TransferEvent.objects.filter(
            transfer=original, kind=TransferEvent.Kind.CHECK, details__dispatch_id=str(record.pk)
        )
        .order_by("recorded_at")
        .values_list("pk", flat=True)
        .first()
    )
    identity, head = new_document(
        run,
        kind=GAP_DOC_TYPE,
        purpose=DocumentIdentity.Purpose.GAP_RESOLUTION,
        entity_id=original.document.entity_id,
        site_id=original.destination_site_id,
    )
    pairing = entry.get("pairing_key")
    header = {
        "kind": GapResolution.Kind.EXCESS.value,
        "resolution": RESOLUTION,
        "transfer_id": str(original.pk),
        "dispatch_id": str(record.pk),
        "check_event_id": str(check) if check else None,
        "lot_id": str(lot_id),
        "observation_id": entry.get("observation_id"),
        "observed_description": entry.get("description"),
        "pairing_key": pairing,
        "sku_id": str(proposal.sku_id),
        "corrective_transfer_id": str(corrective.pk),
        "source_evidence_reference": proposal.source_evidence_reference,
        "reason": proposal.reason,
    }
    line_key = uuid.uuid4()
    body: dict[str, Any] = {
        "line_key": str(line_key),
        "sku_id": str(proposal.sku_id),
        "qty": proposal.qty,
        "portions": [
            {"lot_id": str(lot_id), "lower": lower, "upper": upper} for lower, upper in claimed
        ],
    }
    append_revision(run, head, header=header, replace_lines=[(line_key, body)])
    set_state(run, head, DocumentHead.State.SUBMITTED, event="submitted")
    assert run.principal.human_id is not None
    gap = GapResolution.objects.create(
        tenant_id=run.tenant_id,
        document=identity,
        transfer=original,
        kind=GapResolution.Kind.EXCESS,
        pairing_key=uuid.UUID(pairing) if pairing else None,
        payload={**header, "lines": [body]},
        corrective_transfer=corrective,
        dispatch=record,
        state=GapResolution.State.PENDING,
        quantity=proposal.qty,
        proposed_by_id=run.principal.human_id,
        proposed_at=run.now,
    )
    transfers._log(
        run,
        original,
        TransferEvent.Kind.CORRECTIVE_PROPOSED,
        site_id=original.source_site_id,
        details={
            "dispatch_id": str(record.pk),
            "gap_id": str(gap.pk),
            "lot_id": str(lot_id),
            "quantity": proposal.qty,
            "corrective_transfer_id": str(corrective.pk),
            "source_evidence_reference": proposal.source_evidence_reference,
            "reason": proposal.reason,
        },
    )
    run.audit_subject_key = f"transfer:{original.pk}"
    run.audit_site_id = original.source_site_id
    run.audit_after = {
        "gap_id": str(gap.pk),
        "corrective_transfer_id": str(corrective.pk),
        "quantity": proposal.qty,
    }
    return gap


def _first(free: Sequence[ranges.Interval], qty: int) -> list[ranges.Interval]:
    """The lowest ``qty`` pieces of ``free``, oldest first."""
    out: list[ranges.Interval] = []
    left = qty
    for lower, upper in free:
        if left <= 0:
            break
        size = min(left, upper - lower)
        out.append((lower, lower + size))
        left -= size
    return out


# ---------------------------------------------------------------------------
# The decision a corrective transfer serves: approved, withdrawn
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    gap: GapResolution
    head: DocumentHead
    lot_id: uuid.UUID
    portions: list[tuple[uuid.UUID, ranges.Interval]]


def lock_decision(run: CommandRun, corrective: GoodsTransfer) -> Decision:
    """The excess-observed decision this corrective transfer serves, locked (DOCUMENT)."""
    rows: list[GapResolution] = run.lock(
        LockRank.DOCUMENT,
        GapResolution.objects.filter(
            tenant_id=run.tenant_id,
            corrective_transfer=corrective,
            kind=GapResolution.Kind.EXCESS,
            state__in=OPEN_STATES,
        ),
    )
    if not rows:
        raise transfers._state_conflict(
            "This corrective transfer no longer serves an open excess decision."
        )
    gap = rows[0]
    head = lock_heads(run, [gap.document_id])[gap.document_id]
    return Decision(
        gap=gap, head=head, lot_id=uuid.UUID(gap.payload["lot_id"]), portions=_portions(gap)
    )


def recheck_claim(decision: Decision) -> None:
    """Every claimed piece still stands held at the destination, unmatched."""
    site_id = decision.gap.transfer.destination_site_id
    held = _held(decision.lot_id, site_id)
    for _lot, interval in decision.portions:
        if not ranges.contains(held, [interval]):
            raise transfers._state_conflict(
                "Part of the excess this corrective transfer would match is no longer "
                "held at the destination."
            )


def approve_decision(
    run: CommandRun, corrective: GoodsTransfer, decision: Decision, reason: str | None
) -> None:
    """The Owner's approval of the corrective transfer numbers its GAP decision (GSA-R01)."""
    gap = decision.gap
    if gap.state != GapResolution.State.PENDING:
        raise transfers._state_conflict(f"This excess decision was already {gap.state}.")
    assert run.principal.human_id is not None
    lines = list(gap.payload.get("lines", []))
    header = {key: value for key, value in gap.payload.items() if key != "lines"}
    officialise(
        run,
        decision.head,
        approved_by_id=run.principal.human_id,
        canonical_header=header,
        lines=[(uuid.UUID(str(line["line_key"])), line) for line in lines],
        authority=run.authority,
        number=allocate(run, corrective.document.entity, GAP_DOC_TYPE),
    )
    gap.state = GapResolution.State.APPROVED
    gap.decided_by_id = run.principal.human_id
    gap.decided_at = run.now
    gap.decision_reason = reason or "Approved with its corrective transfer."
    gap.save(update_fields=["state", "decided_by", "decided_at", "decision_reason"])


def withdraw_decision(
    run: CommandRun, corrective: GoodsTransfer, decision: Decision, reason: str
) -> None:
    """Cancelling the corrective transfer's balance frees the observation it claimed."""
    gap = decision.gap
    gap.state = GapResolution.State.WITHDRAWN
    gap.save(update_fields=["state"])
    set_state(
        run, decision.head, DocumentHead.State.REVERSED, event="withdrawn", reason_code=reason[:60]
    )
    original = gap.transfer
    transfers._log(
        run,
        original,
        TransferEvent.Kind.CORRECTIVE_WITHDRAWN,
        site_id=original.source_site_id,
        details={
            "gap_id": str(gap.pk),
            "lot_id": str(decision.lot_id),
            "quantity": gap.quantity,
            "corrective_transfer_id": str(corrective.pk),
            "reason": reason,
        },
    )


# ---------------------------------------------------------------------------
# The corrective confirmation (E233): P17, once
# ---------------------------------------------------------------------------


def parse_confirmation(body: dict[str, Any], now: datetime) -> dict[str, Any]:
    transfers._closed(body, CONFIRM_FIELDS)
    evidence = transfers._text(
        body.get("source_evidence_reference"), "source_evidence_reference", limit=100
    )
    return {
        "source_evidence_reference": evidence,
        "confirmed_at": transfers._moment(body.get("confirmed_at"), "confirmed_at", now),
        "note": transfers._text(body.get("note"), "note", limit=500, required=False),
    }


def _match_invalid(message: str) -> Refusal:
    return Refusal("CUSTODY_MATCH_INVALID", message, status=409)


def _confirmable(
    run: CommandRun, transfer_id: uuid.UUID
) -> tuple[GoodsTransfer, Decision, Any, dict[uuid.UUID, Any]]:
    """Lock and check everything a confirmation relies on, before anything is written."""
    corrective, _head = transfers._locked(run, transfer_id)
    if corrective.corrective_for_id is None:
        raise transfers._refuse("Only a corrective transfer is confirmed against observed excess.")
    if corrective.state != GoodsTransfer.State.APPROVED:
        raise transfers._state_conflict(
            "Only an approved corrective transfer that has not been confirmed yet can be confirmed."
        )
    decision = lock_decision(run, corrective)
    gap = decision.gap
    if gap.state != GapResolution.State.APPROVED:
        raise transfers._state_conflict(
            "This corrective transfer's excess decision is not approved."
        )
    version = transfers._pt_version(corrective)
    outstanding = transfers._outstanding(corrective, version)
    engine.lock_lots(
        run,
        sorted(
            {piece.lot_id for line in outstanding.values() for piece in line.pieces}
            | {decision.lot_id},
            key=str,
        ),
    )
    free = transfers._unheld(outstanding)
    if any(line.held_qty for line in free.values()):
        raise Refusal(
            "RESERVATION_BLOCKED",
            "A reserved piece of this corrective transfer is on hold at the source, so it "
            "cannot be confirmed. Wait for the hold to be resolved, or cancel the balance.",
        )
    total = sum(line.qty for line in free.values())
    if total != gap.quantity:
        raise _match_invalid(
            f"This corrective transfer holds {total} reserved piece(s) and its excess "
            f"decision names {gap.quantity}; they must be the same pieces, exactly."
        )
    held = _held(decision.lot_id, corrective.destination_site_id)
    for lot_id, interval in decision.portions:
        already = CustodyMatch.objects.filter(
            observed_lot_id=lot_id, observed_portion__overlap=portion(*interval)
        ).exists()
        if already or not ranges.contains(held, [interval]):
            raise _match_invalid(
                "Part of the excess this corrective transfer names is no longer held at the "
                "destination, or has already been matched. It cannot be matched again."
            )
    return corrective, decision, version, free


def confirm(run: CommandRun, transfer_id: uuid.UUID, parsed: dict[str, Any]) -> TransferDispatch:
    """Record the corrective transfer's dispatch/arrival pair and match the observation, once.

    Both legs are recorded in one command because nothing travels: the goods
    came on the original shipment and have stood in the destination's excess
    hold since its count. The reserved source pieces leave the source for the
    destination's receiving (unaccepted, origins kept, no new value), and the
    claimed observation interval leaves custody for the ``matched_observation``
    boundary, so the destination's physical total is unchanged.
    """
    corrective, decision, version, free = _confirmable(run, transfer_id)
    gap = decision.gap
    total = gap.quantity
    destination = corrective.destination_site_id
    _record, entry = find(gap.transfer, decision.lot_id)
    confirmed_at: datetime = parsed["confirmed_at"]
    assert run.principal.human_id is not None
    lines: list[dict[str, Any]] = [
        {
            "line_key": str(line_key),
            "sku_id": line.sku_id,
            "qty": line.qty,
            "portions": [piece.as_payload() for piece in line.pieces],
        }
        for line_key, line in free.items()
        if line.pieces
    ]
    summary = {
        "counted_at": confirmed_at.isoformat(),
        "note": parsed["note"],
        "lines": [
            {
                "line_key": line["line_key"],
                "dispatched": line["qty"],
                "good": line["qty"],
                "damaged": 0,
                "wrong": 0,
                "unidentified": 0,
                "short": 0,
            }
            for line in lines
        ],
        "good_total": total,
        "held_total": 0,
        "short_total": 0,
        "wrong_total": 0,
        "excess": [],
        "dispatch_sequence_no": 1,
        "corrective": {
            "gap_id": str(gap.pk),
            "lot_id": str(decision.lot_id),
            "observation_id": entry.get("observation_id"),
            "source_evidence_reference": parsed["source_evidence_reference"],
        },
    }
    shipment = TransferDispatch.objects.create(
        tenant_id=run.tenant_id,
        transfer=corrective,
        sequence_no=1,
        dispatched_at=confirmed_at,
        recorded_by_id=run.principal.human_id,
        transport={"corrective_confirmation": True},
        lines=lines,
        state=TransferDispatch.State.COUNTED,
        arrived_at=confirmed_at,
        arrival_recorded_by_id=run.principal.human_id,
        counted_at=confirmed_at,
        counted_by_id=run.principal.human_id,
        count=summary,
    )
    plan = engine.Plan(
        MATCH_POSTING, version.pk, engine.event_key("corrective_confirmation", corrective.pk)
    )
    receiving = engine.system_location(destination, "receiving")
    arrived: list[tuple[uuid.UUID, ranges.Interval]] = []
    for line in free.values():
        for piece in line.pieces:
            engine.end_reservation(
                run,
                plan,
                lot_id=piece.lot_id,
                interval=piece.interval,
                transfer_version_id=version.pk,
                effect="consume",
                site_id=corrective.source_site_id,
            )
            # The dispatch leg (P08's shape), then the arrival leg (P09's check leg).
            engine.change_address(
                run,
                plan,
                piece.lot_id,
                piece.interval,
                lambda old, tid=corrective.pk: replace(
                    old,
                    boundary="transit",
                    site_id=None,
                    location_id=None,
                    transfer_id=tid,
                    accepted_event_id=None,
                ),
            )
            engine.change_address(
                run,
                plan,
                piece.lot_id,
                piece.interval,
                lambda old, loc=receiving.pk: replace(
                    old,
                    boundary="physical",
                    site_id=destination,
                    location_id=loc,
                    transfer_id=None,
                    condition="good",
                    accepted_event_id=None,
                ),
            )
            arrived.append((piece.lot_id, piece.interval))
    hold_key = uuid.UUID(str(entry["hold_key"]))
    for lot_id, interval in decision.portions:
        engine.release_hold(
            run,
            plan,
            lot_id=lot_id,
            interval=interval,
            hold_key=hold_key,
            site_id=destination,
            source_version_id=version.pk,
        )
        engine.change_address(
            run,
            plan,
            lot_id,
            interval,
            lambda old: replace(
                old,
                boundary=MATCHED_BOUNDARY,
                site_id=None,
                location_id=None,
                transfer_id=None,
                accepted_event_id=None,
                reason="corrective_match",
            ),
        )
    matched_before = matched_qty(decision.lot_id)
    for observed, source in _pairs(decision.portions, arrived):
        run.record(
            CustodyMatch(
                observed_lot_id=observed[0],
                observed_portion=portion(*observed[1]),
                source_lot_id=source[0],
                source_portion=portion(*source[1]),
                corrective_transfer_id=corrective.pk,
            ),
            event_at=confirmed_at,
        )
    engine.post(run, version, plan)
    corrective.state = GoodsTransfer.State.DISPATCHING
    corrective.dispatched_at = confirmed_at
    corrective.save(update_fields=["state", "dispatched_at"])
    common = {
        "dispatch_id": str(shipment.pk),
        "sequence_no": 1,
        "quantity": total,
        "corrective": True,
        "gap_id": str(gap.pk),
        "lot_id": str(decision.lot_id),
        "source_evidence_reference": parsed["source_evidence_reference"],
    }
    transfers._log(
        run,
        corrective,
        TransferEvent.Kind.DISPATCH,
        site_id=corrective.source_site_id,
        details=common,
        actual_at=confirmed_at,
    )
    transfers._log(
        run,
        corrective,
        TransferEvent.Kind.ARRIVAL,
        site_id=destination,
        details=common,
        actual_at=confirmed_at,
    )
    original = gap.transfer
    transfers._log(
        run,
        original,
        TransferEvent.Kind.CORRECTIVE_MATCHED,
        site_id=destination,
        details={
            "gap_id": str(gap.pk),
            "gap_number": gap.document.official_number,
            "lot_id": str(decision.lot_id),
            "quantity": total,
            "corrective_transfer_id": str(corrective.pk),
            "corrective_number": corrective.document.official_number,
        },
        actual_at=confirmed_at,
    )
    record_event(
        run,
        gap.document_id,
        "excess_matched",
        payload={
            "corrective_transfer_id": str(corrective.pk),
            "dispatch_id": str(shipment.pk),
            "quantity": total,
        },
    )
    if matched_before + total >= int(entry["qty"]):
        resolve_exceptions(
            run,
            kind=EXCESS_EXCEPTION,
            subject_key=subject_of(decision.lot_id),
            reason_code=MATCHED_REASON,
            source_event_key=engine.event_key("transfer_excess", decision.lot_id),
        )
    run.audit_subject_key = f"transfer:{corrective.pk}"
    run.audit_site_id = corrective.source_site_id
    run.audit_after = {"dispatch_id": str(shipment.pk), "matched": total, "gap_id": str(gap.pk)}
    return shipment


def _pairs(
    observed: Sequence[tuple[uuid.UUID, ranges.Interval]],
    arrived: Sequence[tuple[uuid.UUID, ranges.Interval]],
) -> list[tuple[tuple[uuid.UUID, ranges.Interval], tuple[uuid.UUID, ranges.Interval]]]:
    """Walk both sides in order, cutting them into equal-length matched pairs."""
    left: list[tuple[uuid.UUID, ranges.Interval]] = list(observed)
    right: list[tuple[uuid.UUID, ranges.Interval]] = list(arrived)
    out: list[tuple[tuple[uuid.UUID, ranges.Interval], tuple[uuid.UUID, ranges.Interval]]] = []
    while left and right:
        o_lot, (o_low, o_up) = left[0]
        s_lot, (s_low, s_up) = right[0]
        size = min(o_up - o_low, s_up - s_low)
        out.append(((o_lot, (o_low, o_low + size)), (s_lot, (s_low, s_low + size))))
        if o_low + size == o_up:
            left.pop(0)
        else:
            left[0] = (o_lot, (o_low + size, o_up))
        if s_low + size == s_up:
            right.pop(0)
        else:
            right[0] = (s_lot, (s_low + size, s_up))
    return out


# ---------------------------------------------------------------------------
# Reads and actions
# ---------------------------------------------------------------------------


def _person(pk: uuid.UUID | None, names: dict[uuid.UUID, str]) -> dict[str, str] | None:
    return {"id": str(pk), "name": names.get(pk, "")} if pk else None


def decision_dto(gap: GapResolution, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    payload = gap.payload
    corrective = gap.corrective_transfer
    return {
        "id": str(gap.pk),
        "number": gap.document.official_number,
        "state": gap.state,
        "quantity": gap.quantity,
        "lot_id": payload.get("lot_id"),
        "pairing_key": str(gap.pairing_key) if gap.pairing_key else None,
        "sku_id": payload.get("sku_id"),
        "source_evidence_reference": payload.get("source_evidence_reference"),
        "reason": payload.get("reason"),
        "matched": bool(
            corrective is not None
            and CustodyMatch.objects.filter(corrective_transfer_id=corrective.pk).exists()
        ),
        "corrective_transfer": (
            {
                "id": str(corrective.pk),
                "number": corrective.document.official_number,
                "state": corrective.state,
            }
            if corrective is not None
            else None
        ),
        "proposed_by": _person(gap.proposed_by_id, names),
        "proposed_at": gap.proposed_at.isoformat() if gap.proposed_at else None,
        "decided_by": _person(gap.decided_by_id, names),
        "decided_at": gap.decided_at.isoformat() if gap.decided_at else None,
    }


def _decisions(record: TransferDispatch) -> list[GapResolution]:
    return list(
        GapResolution.objects.select_related(
            "document", "corrective_transfer", "corrective_transfer__document"
        )
        .filter(dispatch=record, kind=GapResolution.Kind.EXCESS)
        .order_by("proposed_at", "id")
    )


def shipment_excess(
    record: TransferDispatch, transfer: GoodsTransfer, names: dict[uuid.UUID, str]
) -> list[dict[str, Any]]:
    """Every excess observation this shipment's count recorded, and where it has got to."""
    entries = entries_of(record)
    if not entries:
        return []
    decisions = _decisions(record)
    out: list[dict[str, Any]] = []
    for entry in entries:
        lot_id = uuid.UUID(str(entry["lot_id"]))
        mine = [gap for gap in decisions if gap.payload.get("lot_id") == str(lot_id)]
        matched = matched_qty(lot_id)
        claimed = sum(
            gap.quantity
            for gap in mine
            if gap.state in OPEN_STATES
            and not CustodyMatch.objects.filter(
                corrective_transfer_id=gap.corrective_transfer_id
            ).exists()
        )
        qty = int(entry["qty"])
        out.append(
            {
                "lot_id": str(lot_id),
                "observation_id": entry.get("observation_id"),
                "description": entry.get("description"),
                "sku_id": entry.get("sku_id"),
                "alias_value": entry.get("alias_value"),
                "qty": qty,
                "in_place_of_line_key": entry.get("in_place_of_line_key"),
                "pairing_key": entry.get("pairing_key"),
                "matched_qty": matched,
                "claimed_qty": claimed,
                "unresolved_qty": qty - matched,
                "unclaimed_qty": ranges.total(unclaimed(lot_id, transfer.destination_site_id)),
                "decisions": [decision_dto(gap, names) for gap in mine],
            }
        )
    return out


def excess_qty(records: Sequence[TransferDispatch]) -> int:
    return sum(int(entry.get("qty") or 0) for record in records for entry in entries_of(record))


def people_of(transfer: GoodsTransfer) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for proposer, decider in GapResolution.objects.filter(corrective_transfer=transfer).values_list(
        "proposed_by_id", "decided_by_id"
    ):
        ids.extend(i for i in (proposer, decider) if i)
    return ids


def corrective_dto(transfer: GoodsTransfer, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    """What a transfer says about corrections: what it corrects, and what corrects it."""
    original = transfer.corrective_for
    decision = (
        GapResolution.objects.select_related(
            "document", "corrective_transfer", "corrective_transfer__document"
        )
        .filter(corrective_transfer=transfer, kind=GapResolution.Kind.EXCESS)
        .order_by("-proposed_at")
        .first()
    )
    return {
        "corrective_for": (
            {"id": str(original.pk), "number": original.document.official_number}
            if original is not None
            else None
        ),
        "corrective_decision": decision_dto(decision, names) if decision is not None else None,
    }


def excess_actions(
    access: Any, transfer: GoodsTransfer, records: Sequence[TransferDispatch]
) -> list[str]:
    """Proposing a correction at the source; confirming an approved one there."""
    out: list[str] = []
    source = transfer.source_site_id
    if transfer.corrective_for_id is not None:
        if transfer.state == GoodsTransfer.State.APPROVED and access.can(
            CONFIRM_ACTION, site_id=source
        ):
            out.append("confirm_corrective")
        return out
    if transfer.state == GoodsTransfer.State.CANCELLED or not access.can(
        PROPOSE_ACTION, site_id=source
    ):
        return out
    for record in records:
        for entry in entries_of(record):
            if unclaimed(uuid.UUID(str(entry["lot_id"])), transfer.destination_site_id):
                out.append("propose_corrective")
                return out
    return out
