"""A shipment on the road: its arrival and its e-way evidence (goods ticket 13B).

Two facts about one dispatch, each kept apart from the stock it carries:

* **Arrival (design E147).** The destination records that the shipment is
  physically there. That is all it records. The pieces stay in transit - not
  destination stock, not available, not accepted - until the destination
  counts the whole shipment, which is goods ticket 14's receiving step.
* **E-way evidence (design E150; transfers PRD §8; overall PRD R-FIN-018/019).**
  Whether a reference went with the shipment is fixed when it leaves. A
  shipment with none is recorded all the same and opens an owned
  ``eway_missing`` exception at the sending site. A reference attached later is
  its own event and resolves that exception; verifying a reference is a
  different, later event by a different authority. None of the three erases
  another, and nothing physical - arrival, count, acceptance, completion -
  resolves the exception. Nothing here says a movement was lawful.

Who may do what (design E150, read through the operations PRD §3.2): the
sending site's dispatcher (``transfer.move`` there) attaches; the inventory
controller's authority verifies, which in this increment the Owner carries -
the transfer approver's grant, ``pt.approve.transfer`` at the sending site.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from alerts.goods_services import open_exception, resolve_exceptions
from core.commands import CommandRun, LockRank
from core.refusals import Refusal
from outbound import transfers
from outbound.goods_models import GoodsTransfer, TransferDispatch, TransferEvent
from stockledger import goods_engine as engine

ARRIVAL_FIELDS = frozenset({"arrived_at", "packages_received", "note"})
EWAY_FIELDS = frozenset({"action", "reference", "note"})
EWAY_ACTIONS = ("attach", "verify")

#: Missing e-way evidence is owned work at the sending site (the shared
#: centre's ``eway_missing`` default: the dispatching site, one working day,
#: closed by a reference being attached).
EWAY_EXCEPTION = "eway_missing"
EWAY_REASON = "EWAY_NOT_PRESENT"
#: Verifying needs the inventory controller's authority (design E150), which the
#: Owner carries in this increment (operations PRD §3.2) - the approver's grant.
VERIFY_ACTION = transfers.APPROVE_ACTION
ATTACH_ACTION = transfers.MOVE_ACTION


def _subject(record: TransferDispatch) -> str:
    return f"transfer_dispatch:{record.pk}"


def open_missing_eway(run: CommandRun, transfer: GoodsTransfer, record: TransferDispatch) -> None:
    """The shipment left with no e-way reference: owned work, not a blocker."""
    open_exception(
        run,
        kind=EWAY_EXCEPTION,
        site_id=transfer.source_site_id,
        subject_key=_subject(record),
        reason_code=EWAY_REASON,
        source_event_key=engine.event_key("transfer_eway_missing", record.pk),
        allowed_resolution_actions=transfers.RESOLUTION_ACTIONS,
        note=f"transfer:{transfer.pk}",
    )


# ---------------------------------------------------------------------------
# Arrival (E147)
# ---------------------------------------------------------------------------


def parse_arrival(body: dict[str, Any], now: datetime) -> dict[str, Any]:
    transfers._closed(body, ARRIVAL_FIELDS)
    packages = body.get("packages_received")
    if packages is not None and (
        isinstance(packages, bool) or not isinstance(packages, int) or not 0 <= packages <= 9999
    ):
        raise Refusal("INVALID_REQUEST", "packages_received is a whole number from 0 to 9999.")
    return {
        "arrived_at": transfers._moment(body.get("arrived_at"), "arrived_at", now),
        "packages_received": packages,
        "note": transfers._text(body.get("note"), "note", limit=500, required=False),
    }


def record_arrival(
    run: CommandRun, transfer_id: uuid.UUID, dispatch_id: uuid.UUID, parsed: dict[str, Any]
) -> TransferDispatch:
    """The shipment is physically at the destination. Nothing becomes available.

    Only the destination's goods fence applies: it is the site the step
    happens at, and a count at the source does not stop goods arriving.
    """
    transfer, _head = transfers._locked(run, transfer_id, at="destination")
    record = transfers._locked_dispatch(run, transfer, dispatch_id)
    if record.state != TransferDispatch.State.IN_TRANSIT:
        raise transfers._state_conflict(
            "This shipment is no longer on the road - it was counted or came back."
        )
    if record.arrived_at is not None:
        raise transfers._state_conflict("This shipment's arrival is already recorded.")
    at: datetime = parsed["arrived_at"]
    if at < record.dispatched_at:
        raise Refusal("EVENT_TIME_INVALID", "A shipment cannot arrive before it left.", status=422)
    record.arrived_at = at
    record.arrival_recorded_by_id = run.principal.human_id
    record.save(update_fields=["arrived_at", "arrival_recorded_by"])
    transfers._log(
        run,
        transfer,
        TransferEvent.Kind.ARRIVAL,
        site_id=transfer.destination_site_id,
        details={
            "dispatch_id": str(record.pk),
            "sequence_no": record.sequence_no,
            "packages_received": parsed["packages_received"],
            "note": parsed["note"],
        },
        actual_at=at,
    )
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.destination_site_id
    run.audit_after = {"dispatch_id": str(record.pk), "arrived_at": at.isoformat()}
    return record


# ---------------------------------------------------------------------------
# E-way evidence (E150)
# ---------------------------------------------------------------------------


def parse_eway(body: dict[str, Any]) -> dict[str, Any]:
    transfers._closed(body, EWAY_FIELDS)
    action = body.get("action")
    if action not in EWAY_ACTIONS:
        raise Refusal("INVALID_REQUEST", "action is attach or verify.")
    reference = transfers._text(
        body.get("reference"), "reference", limit=transfers.EWAY_REFERENCE_LIMIT
    )
    return {
        "action": action,
        "reference": reference,
        "note": transfers._text(body.get("note"), "note", limit=500, required=False),
    }


def _locked_for_evidence(
    run: CommandRun, transfer_id: uuid.UUID, dispatch_id: uuid.UUID
) -> tuple[GoodsTransfer, TransferDispatch]:
    """The transfer and the shipment, locked. No site fence: this is paperwork.

    Recording documentary evidence moves no stock, so neither a count freeze
    nor a site's state stops it (transfers PRD §8: documentation and physical
    recording are separate).
    """
    rows: list[GoodsTransfer] = run.lock(
        LockRank.DOCUMENT, GoodsTransfer.objects.filter(pk=transfer_id)
    )
    if not rows:
        raise Refusal("NOT_FOUND", "That transfer was not found.")
    return rows[0], transfers._locked_dispatch(run, rows[0], dispatch_id)


def record_eway(
    run: CommandRun, transfer_id: uuid.UUID, dispatch_id: uuid.UUID, parsed: dict[str, Any]
) -> TransferDispatch:
    transfer, record = _locked_for_evidence(run, transfer_id, dispatch_id)
    if transfer.corrective_for_id is not None:
        # Goods ticket 16: a corrective confirmation moves nothing on the road;
        # the goods travelled on the original shipment, whose e-way evidence
        # is that shipment's own.
        raise transfers._refuse(
            "A corrective transfer's confirmation is not a movement on the road, so it "
            "has no e-way evidence. Record it on the original shipment."
        )
    state = eway_state(record, _eway_events(record))
    reference: str = parsed["reference"]
    if parsed["action"] == "attach":
        transfers._log(
            run,
            transfer,
            TransferEvent.Kind.EWAY_ADDED,
            site_id=transfer.source_site_id,
            details={
                "dispatch_id": str(record.pk),
                "reference": reference,
                "note": parsed["note"],
                "at_dispatch": record.eway_at_dispatch,
            },
        )
        resolve_exceptions(
            run, kind=EWAY_EXCEPTION, subject_key=_subject(record), reason_code="REFERENCE_ATTACHED"
        )
    else:
        if state["reference"] is None:
            raise Refusal(
                "EWAY_VERIFY_DENIED",
                "There is no e-way reference on this shipment to verify. Attach one first.",
                status=403,
            )
        if reference != state["reference"]:
            raise Refusal(
                "EWAY_VERIFY_DENIED",
                "That is not the e-way reference on file for this shipment.",
                status=403,
            )
        if state["verified"]:
            raise transfers._state_conflict("This e-way reference is already verified.")
        transfers._log(
            run,
            transfer,
            TransferEvent.Kind.EWAY_VERIFIED,
            site_id=transfer.source_site_id,
            details={"dispatch_id": str(record.pk), "reference": reference, "note": parsed["note"]},
        )
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.source_site_id
    run.audit_after = {"dispatch_id": str(record.pk), "eway": parsed["action"]}
    return record


def _eway_events(record: TransferDispatch) -> list[TransferEvent]:
    return list(
        TransferEvent.objects.filter(
            transfer_id=record.transfer_id,
            kind__in=(TransferEvent.Kind.EWAY_ADDED, TransferEvent.Kind.EWAY_VERIFIED),
            details__dispatch_id=str(record.pk),
        ).order_by("recorded_at", "id")
    )


def eway_state(record: TransferDispatch, events: list[TransferEvent]) -> dict[str, Any]:
    """The shipment's e-way evidence as three separate facts, never merged.

    ``at_dispatch`` is what left with the goods and never changes. ``reference``
    is the reference on file now - the latest attached, else the one that left
    with them. ``verified`` is true only when that same reference was verified.
    """
    reference: str | None = None
    if record.eway_at_dispatch == "present":
        reference = str((record.transport or {}).get(transfers.EWAY_REFERENCE) or "").strip()
        reference = reference or None
    attached_at = attached_by = None
    verified_at = verified_by = None
    verified_reference: str | None = None
    for event in events:
        details = event.details or {}
        if event.kind == TransferEvent.Kind.EWAY_ADDED:
            reference = str(details.get("reference"))
            attached_at, attached_by = event.recorded_at, event.actor_id
            verified_reference = None
            verified_at = verified_by = None
        elif event.kind == TransferEvent.Kind.EWAY_VERIFIED:
            verified_reference = str(details.get("reference"))
            verified_at, verified_by = event.recorded_at, event.actor_id
    verified = reference is not None and verified_reference == reference
    return {
        "at_dispatch": record.eway_at_dispatch,
        "reference": reference,
        "attached_at": attached_at.isoformat() if attached_at else None,
        "attached_by": str(attached_by) if attached_by else None,
        "verified": verified,
        "verified_at": verified_at.isoformat() if verified and verified_at else None,
        "verified_by": str(verified_by) if verified and verified_by else None,
    }


def eway_dto(record: TransferDispatch) -> dict[str, Any]:
    from alerts.goods_models import GoodsException

    state = eway_state(record, _eway_events(record))
    state["missing_exception_open"] = GoodsException.objects.filter(
        kind=EWAY_EXCEPTION, subject_key=_subject(record), state="open"
    ).exists()
    return state
