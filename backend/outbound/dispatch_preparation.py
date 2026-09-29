"""Preparing one shipment by scanning it (goods ticket 13B; design E242-E244, GSA-T13).

The design's dispatch preparation was written for one exact, full-movement
dispatch. Ticket 13B adapts it to the selected shipment within the approved
movement (goods PRD §14.9.2): the source site scans what it is about to send,
and the dispatch that follows carries exactly that - the complete selected
shipment, which may be less than the whole movement.

Four facts hold the whole way through:

* **Preparing moves nothing.** No reservation is touched and no stock is
  posted. A preparation is evidence: who scanned which line, which tag, how
  many, from which origin if they said, and when.
* **It is durable and resumable.** One preparation is open per transfer at a
  time, bound to the exact approved PT version it was opened against. Anyone
  holding the dispatch grant at the source may pick it up on another device
  and carry on; every earlier scan keeps its own operator and time.
* **It is bounded.** A scan cannot take a line past what is still reserved on
  it, cannot name an origin the line does not have reserved, and cannot carry
  a tag that is not this line's. A scan key counts once.
* **It never goes stale silently.** Cancelling the balance, a change of the
  live approved version, or somebody starting the shipment again invalidates
  the open preparation - it is kept, with the reason, never deleted - and the
  dispatch refuses anything but the current preparation's current content.

Lock order, as in ``outbound.transfers``: SITE → DOCUMENT (transfer, its head,
then the preparation) → LOT.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import Q

from core.canonical import content_hash
from core.commands import CommandRun, LockRank
from core.kernel_models import OfficialVersion
from core.refusals import Refusal, issue
from outbound import transfers
from outbound.goods_models import (
    DispatchPreparation,
    DispatchScan,
    GoodsTransfer,
    TransferDispatch,
)

#: E242's body.
OPEN_FIELDS = frozenset({"replace"})
#: E244's body.
SCAN_FIELDS = frozenset({"observations"})
OBSERVATION_FIELDS = frozenset(
    {"scan_key", "line_key", "origin_id", "qty", "alias_value", "actual_at"}
)
MAX_OBSERVATIONS = 200

#: Why an open preparation stopped being usable. Kept on the row; never deleted.
INVALIDATED_REPLACED = "REPLACED"
INVALIDATED_BALANCE_CANCELLED = "BALANCE_CANCELLED"
INVALIDATED_VERSION_CHANGED = "APPROVAL_CHANGED"


def _plan_mismatch(message: str) -> Refusal:
    return Refusal("DISPATCH_PLAN_MISMATCH", message)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    scan_key: uuid.UUID
    line_key: uuid.UUID
    origin_id: uuid.UUID | None
    qty: int
    alias_value: str
    actual_at: datetime

    def fingerprint(self) -> str:
        """What a scan key binds. The business time is left out: a device that
        resends without one gets "now" again, and that is not a different scan."""
        return content_hash(
            {
                "line_key": str(self.line_key),
                "origin_id": str(self.origin_id) if self.origin_id else None,
                "qty": self.qty,
                "alias_value": self.alias_value,
            }
        )


def parse_open(body: dict[str, Any]) -> bool:
    replace = body.get("replace", False)
    if not isinstance(replace, bool):
        raise Refusal("INVALID_REQUEST", "replace is true or false.")
    return replace


def parse_scans(body: dict[str, Any], now: datetime) -> list[Observation]:
    raw = body.get("observations")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OBSERVATIONS:
        raise Refusal(
            "INVALID_REQUEST", f"observations is a list of 1 to {MAX_OBSERVATIONS} scans."
        )
    out: list[Observation] = []
    seen: set[uuid.UUID] = set()
    for index, item in enumerate(raw):
        at = f"observations[{index}]"
        if not isinstance(item, dict):
            raise Refusal("INVALID_REQUEST", "Every observation is an object.")
        unknown = sorted(set(item) - OBSERVATION_FIELDS)
        if unknown:
            raise Refusal("INVALID_REQUEST", f"Unknown field(s): {', '.join(unknown)}.")
        key = transfers._uuid(item.get("scan_key"), f"{at}.scan_key")
        if key in seen:
            raise Refusal("INVALID_REQUEST", "A scan_key appears twice in this request.")
        seen.add(key)
        alias = item.get("alias_value")
        if not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 128:
            raise Refusal("INVALID_REQUEST", "alias_value is the scanned tag, 1 to 128 characters.")
        out.append(
            Observation(
                scan_key=key,
                line_key=transfers._uuid(item.get("line_key"), f"{at}.line_key"),
                origin_id=transfers._optional_uuid(item.get("origin_id"), f"{at}.origin_id"),
                qty=transfers._qty(item.get("qty"), f"{at}.qty"),
                alias_value=alias.strip(),
                actual_at=transfers._moment(item.get("actual_at"), f"{at}.actual_at", now),
            )
        )
    return out


# ---------------------------------------------------------------------------
# What is still reserved, and what has been scanned against it
# ---------------------------------------------------------------------------


def _reserved_by_origin(
    outstanding: dict[uuid.UUID, transfers._Outstanding],
) -> dict[uuid.UUID, dict[uuid.UUID | None, int]]:
    """Per approved line, what is still reserved from each origin (held pieces included)."""
    out: dict[uuid.UUID, dict[uuid.UUID | None, int]] = {}
    for line_key, line in outstanding.items():
        shares: dict[uuid.UUID | None, int] = defaultdict(int)
        for piece in line.pieces:
            shares[piece.origin_id] += piece.interval[1] - piece.interval[0]
        out[line_key] = dict(shares)
    return out


def _scans(preparation: DispatchPreparation) -> list[DispatchScan]:
    return list(
        DispatchScan.objects.filter(preparation=preparation).order_by("recorded_at", "scan_key")
    )


def _scanned(
    scans: Iterable[DispatchScan],
) -> tuple[dict[uuid.UUID, int], dict[tuple[uuid.UUID, uuid.UUID], int]]:
    per_line: dict[uuid.UUID, int] = defaultdict(int)
    per_origin: dict[tuple[uuid.UUID, uuid.UUID], int] = defaultdict(int)
    for row in scans:
        per_line[row.line_key] += row.qty
        if row.origin_id is not None:
            per_origin[(row.line_key, row.origin_id)] += row.qty
    return dict(per_line), dict(per_origin)


def aliases_by_line(
    transfer: GoodsTransfer, version: OfficialVersion, now: datetime
) -> dict[uuid.UUID, list[str]]:
    """The tags a piece of each approved line may carry.

    A piece's tag is the one its receipt printed - the alias frozen on the
    official line its origin came from - or any alias of its SKU in force at
    the sending site now. Nothing else is this line's tag.

    A pre-PT line (goods ticket 13E) has no origin and may have no SKU: its
    pieces are identified from their GRN (overall PRD §15.2.1 rule 10), so its
    tags are the tag the count recorded, if any, its SKU's aliases if the count
    gave one, and the GRN's own number - the reference the held goods stand
    under, and the one thing every such piece is known by.
    """
    from core.kernel_models import OfficialLine
    from masters.goods_identity_models import SkuAlias
    from stockledger.goods_models import Origin

    lines = list(OfficialLine.objects.filter(version_id=version.pk).order_by("line_no"))
    origins: dict[str, set[str]] = defaultdict(set)
    wanted = {
        str(piece["origin_id"])
        for line in lines
        for piece in line.payload.get("portions") or []
        if piece.get("origin_id")
    }
    for origin_id, payload in Origin.objects.filter(pk__in=sorted(wanted)).values_list(
        "pk", "official_line__payload"
    ):
        alias = (payload or {}).get("alias_as_used")
        if alias:
            origins[str(origin_id)].add(str(alias))
    skus = {str(line.payload["sku_id"]) for line in lines if line.payload.get("sku_id")}
    by_sku: dict[str, set[str]] = defaultdict(set)
    for sku_id, value in (
        SkuAlias.objects.filter(
            sku_id__in=sorted(skus), governance_state="effective", effective_from__lte=now
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=now))
        .filter(Q(site__isnull=True) | Q(site_id=transfer.source_site_id))
        .values_list("sku_id", "value")
    ):
        by_sku[str(sku_id)].add(value)
    out: dict[uuid.UUID, list[str]] = {}
    for line in lines:
        values = set(by_sku.get(str(line.payload.get("sku_id")), set()))
        for piece in line.payload.get("portions") or []:
            values |= origins.get(str(piece.get("origin_id")), set())
        if line.payload.get("grn_id"):
            values |= {
                str(tag)
                for tag in (line.payload.get("raw_alias"), line.payload.get("grn_number"))
                if tag
            }
        out[line.stable_line_key] = sorted(values)
    return out


def content_of(preparation: DispatchPreparation, scans: Sequence[DispatchScan]) -> str:
    """The hash a dispatch must name: the preparation's binding and every scan in it."""
    return content_hash(
        {
            "preparation_id": str(preparation.pk),
            "transfer_id": str(preparation.transfer_id),
            "source_version_id": str(preparation.source_version_id),
            "state": preparation.state,
            "revision": preparation.revision,
            "scans": sorted(
                [
                    str(row.scan_key),
                    str(row.line_key),
                    str(row.origin_id) if row.origin_id else None,
                    row.qty,
                    row.alias_value,
                ]
                for row in scans
            ),
        }
    )


# ---------------------------------------------------------------------------
# E242: open or resume
# ---------------------------------------------------------------------------


def open_preparation(
    run: CommandRun, transfer_id: uuid.UUID, *, replace: bool
) -> tuple[DispatchPreparation, bool]:
    """Resume the transfer's open preparation, or open one for its live approved version.

    ``replace`` starts the shipment again: the open preparation is invalidated,
    kept with its scans, and a fresh one opened. That is the way out when a
    hold now covers a scanned piece, or the wrong pieces were scanned.
    """
    # Scanning happens at the source and moves nothing, so only the source's
    # goods fence applies: a count at the destination does not stop it.
    transfer, _head = transfers._locked(run, transfer_id, at="source")
    transfers.refuse_corrective_shipment(transfer)
    if transfer.state not in (GoodsTransfer.State.APPROVED, GoodsTransfer.State.DISPATCHING):
        raise transfers._state_conflict("Only an approved transfer can be prepared for dispatch.")
    version = transfers._pt_version(transfer)
    human_id = run.principal.human_id
    if human_id is None:
        raise Refusal("ACTION_DENIED", "A shipment is prepared by a named person.")
    existing = _lock_open(run, transfer)
    if existing is not None and existing.source_version_id != version.pk:
        _invalidate(run, existing, INVALIDATED_VERSION_CHANGED)
        existing = None
    if existing is not None and replace:
        _invalidate(run, existing, INVALIDATED_REPLACED)
        existing = None
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.source_site_id
    if existing is not None:
        run.audit_after = {"preparation_id": str(existing.pk), "resumed": True}
        return existing, False
    if not any(line.qty for line in transfers._outstanding(transfer, version).values()):
        raise transfers._state_conflict(
            "Nothing on this transfer is still reserved, so there is nothing left to send."
        )
    preparation = DispatchPreparation(
        tenant_id=run.tenant_id,
        transfer=transfer,
        source_version=version,
        opened_by_id=human_id,
        opened_at=run.now,
        last_activity_at=run.now,
    )
    preparation.content_hash = content_of(preparation, [])
    preparation.save()
    run.audit_after = {"preparation_id": str(preparation.pk), "source_version_id": str(version.pk)}
    return preparation, True


def _lock_open(run: CommandRun, transfer: GoodsTransfer) -> DispatchPreparation | None:
    rows: list[DispatchPreparation] = run.lock(
        LockRank.DOCUMENT,
        DispatchPreparation.objects.filter(transfer=transfer, state=DispatchPreparation.State.OPEN),
    )
    return rows[0] if rows else None


def _invalidate(run: CommandRun, preparation: DispatchPreparation, reason: str) -> None:
    preparation.state = DispatchPreparation.State.INVALIDATED
    preparation.invalidated_reason = reason
    preparation.closed_at = run.now
    preparation.revision += 1
    preparation.content_hash = content_of(preparation, _scans(preparation))
    preparation.save(
        update_fields=["state", "invalidated_reason", "closed_at", "revision", "content_hash"]
    )


def invalidate_open(run: CommandRun, transfer: GoodsTransfer, reason: str) -> None:
    """Invalidate the transfer's open preparation, if any, keeping it (called under its locks)."""
    existing = _lock_open(run, transfer)
    if existing is not None:
        _invalidate(run, existing, reason)


# ---------------------------------------------------------------------------
# E244: scan
# ---------------------------------------------------------------------------


def scan(
    run: CommandRun,
    preparation_id: uuid.UUID,
    observations: list[Observation],
    expected_revision: int | None,
) -> DispatchPreparation:
    """Acknowledge scans into the open preparation. No stock moves and nothing is reserved.

    Every observation is checked before any is kept, so a refused request keeps
    nothing. A scan key already acknowledged with the same content is counted
    once; with different content it is refused. A request made only of such
    replays needs no current revision - it changes nothing.
    """
    transfer_id = (
        DispatchPreparation.objects.filter(pk=preparation_id)
        .values_list("transfer_id", flat=True)
        .first()
    )
    if transfer_id is None:
        raise Refusal("NOT_FOUND", "That dispatch preparation was not found.")
    transfer, _head = transfers._locked(run, transfer_id, at="source")
    rows: list[DispatchPreparation] = run.lock(
        LockRank.DOCUMENT, DispatchPreparation.objects.filter(pk=preparation_id)
    )
    preparation = rows[0]
    earlier = {row.scan_key: row for row in _scans(preparation)}
    fresh: list[Observation] = []
    for item in observations:
        known = earlier.get(item.scan_key)
        if known is None:
            fresh.append(item)
        elif _fingerprint(known) != item.fingerprint():
            raise Refusal(
                "COMMAND_CONFLICT",
                "That scan_key was already used for a different scan in this preparation.",
                issues=[issue("SCAN_KEY_REUSED", "Use a new scan_key", field=str(item.scan_key))],
            )
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.source_site_id
    if not fresh:
        run.audit_after = {"preparation_id": str(preparation.pk), "posted": []}
        return preparation
    _require_live(preparation, transfer)
    if expected_revision != preparation.revision:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "Somebody scanned into this shipment after you loaded it. Reload and scan again.",
        )
    version = transfers._pt_version(transfer)
    reserved = _reserved_by_origin(transfers._outstanding(transfer, version))
    tags = aliases_by_line(transfer, version, run.now)
    per_line, per_origin = _scanned(earlier.values())
    for item in fresh:
        _check(item, reserved, tags, per_line, per_origin, transfer)
        per_line[item.line_key] = per_line.get(item.line_key, 0) + item.qty
        if item.origin_id is not None:
            key = (item.line_key, item.origin_id)
            per_origin[key] = per_origin.get(key, 0) + item.qty
    for item in fresh:
        run.record(
            DispatchScan(
                preparation=preparation,
                scan_key=item.scan_key,
                line_key=item.line_key,
                origin_id=item.origin_id,
                qty=item.qty,
                alias_value=item.alias_value,
            ),
            event_at=item.actual_at,
        )
    preparation.revision += 1
    preparation.last_activity_at = run.now
    preparation.content_hash = content_of(preparation, _scans(preparation))
    preparation.save(update_fields=["revision", "last_activity_at", "content_hash"])
    run.audit_after = {
        "preparation_id": str(preparation.pk),
        "posted": sorted(str(item.scan_key) for item in fresh),
        "revision": preparation.revision,
    }
    return preparation


def _fingerprint(row: DispatchScan) -> str:
    return Observation(
        scan_key=row.scan_key,
        line_key=row.line_key,
        origin_id=row.origin_id,
        qty=row.qty,
        alias_value=row.alias_value,
        actual_at=row.event_at,
    ).fingerprint()


def _require_live(preparation: DispatchPreparation, transfer: GoodsTransfer) -> None:
    if preparation.state != DispatchPreparation.State.OPEN:
        raise _plan_mismatch(
            "This preparation is no longer open - it was dispatched or invalidated. "
            "Open the shipment again to scan."
        )
    version = transfers._pt_version(transfer, required=False)
    if version is None or version.pk != preparation.source_version_id:
        raise _plan_mismatch(
            "This preparation was made against an approval that is no longer the live one. "
            "Open the shipment again."
        )


def _check(
    item: Observation,
    reserved: dict[uuid.UUID, dict[uuid.UUID | None, int]],
    tags: dict[uuid.UUID, list[str]],
    per_line: dict[uuid.UUID, int],
    per_origin: dict[tuple[uuid.UUID, uuid.UUID], int],
    transfer: GoodsTransfer,
) -> None:
    shares = reserved.get(item.line_key)
    if shares is None:
        raise Refusal("NOT_FOUND", "That line is not on this transfer.")
    if item.alias_value not in tags.get(item.line_key, []):
        raise Refusal(
            "TAG_MISMATCH",
            "That tag is not this line's. Scan a piece of the item on this line.",
            issues=[issue("TAG_MISMATCH", "Wrong tag for this line", line_key=item.line_key)],
        )
    if item.actual_at < transfer.document.created_at:
        raise Refusal(
            "EVENT_TIME_INVALID", "A scan cannot be earlier than the transfer.", status=422
        )
    line_total = sum(shares.values())
    if per_line.get(item.line_key, 0) + item.qty > line_total:
        raise _plan_mismatch(
            f"Only {line_total} piece(s) of that line are still reserved, and "
            f"{per_line.get(item.line_key, 0)} are already scanned. Sending more than was "
            "approved needs a fresh approval."
        )
    if item.origin_id is not None:
        room = shares.get(item.origin_id, 0)
        if room == 0:
            raise _plan_mismatch(
                "That origin is not reserved on this line. Scan a piece from an approved origin."
            )
        if per_origin.get((item.line_key, item.origin_id), 0) + item.qty > room:
            raise _plan_mismatch(
                f"Only {room} piece(s) of that origin are still reserved on this line."
            )


# ---------------------------------------------------------------------------
# E146: what the dispatch takes from the preparation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Selected:
    """The shipment the preparation acknowledged, per line: named origins, then the rest."""

    per_origin: dict[uuid.UUID, dict[uuid.UUID, int]]
    per_line: dict[uuid.UUID, int]


def consume(
    run: CommandRun,
    transfer: GoodsTransfer,
    version: OfficialVersion,
    *,
    preparation_id: uuid.UUID,
    revision: int,
    reviewed_hash: str,
    lines: Sequence[dict[str, Any]],
) -> tuple[DispatchPreparation, Selected]:
    """Recheck the preparation against the live approval and the submitted totals.

    Called by the dispatch under the transfer's locks. Anything that is not the
    current content of the current preparation for this live approval is
    refused; so is a total the scans do not support, either way.
    """
    rows: list[DispatchPreparation] = run.lock(
        LockRank.DOCUMENT,
        DispatchPreparation.objects.filter(pk=preparation_id, transfer=transfer),
    )
    if not rows:
        raise Refusal("NOT_FOUND", "That dispatch preparation was not found.")
    preparation = rows[0]
    _require_live(preparation, transfer)
    if preparation.source_version_id != version.pk:  # pragma: no cover - _require_live checks
        raise _plan_mismatch("This preparation is not for the live approval.")
    if preparation.revision != revision or preparation.content_hash != reviewed_hash:
        raise Refusal(
            "REVISION_SUPERSEDED",
            "The scanned shipment changed after you reviewed it. Reload it and dispatch again.",
        )
    scans = _scans(preparation)
    per_line, per_origin = _scanned(scans)
    if not per_line:
        raise _plan_mismatch("Nothing has been scanned for this shipment yet.")
    asked: dict[uuid.UUID, int] = {}
    for item in lines:
        if item["line_key"] in asked:
            raise Refusal("INVALID_REQUEST", "Every dispatch line names a different approved line.")
        asked[item["line_key"]] = item["qty"]
    if asked != per_line:
        raise _plan_mismatch(
            "The dispatch must carry exactly what was scanned for this shipment - "
            + ", ".join(f"{qty} of line {str(key)[:8]}" for key, qty in sorted(per_line.items()))
            + ". Scan the rest first, or dispatch what was scanned."
        )
    named: dict[uuid.UUID, dict[uuid.UUID, int]] = defaultdict(dict)
    for (line_key, origin_id), qty in per_origin.items():
        named[line_key][origin_id] = qty
    return preparation, Selected(per_origin=dict(named), per_line=per_line)


def mark_dispatched(
    run: CommandRun, preparation: DispatchPreparation, record: TransferDispatch
) -> None:
    preparation.state = DispatchPreparation.State.DISPATCHED
    preparation.dispatch = record
    preparation.closed_at = run.now
    preparation.revision += 1
    preparation.content_hash = content_of(preparation, _scans(preparation))
    preparation.save(update_fields=["state", "dispatch", "closed_at", "revision", "content_hash"])


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def preparation_of(tenant_id: uuid.UUID, preparation_id: uuid.UUID) -> DispatchPreparation:
    row = (
        DispatchPreparation.objects.select_related("transfer")
        .filter(tenant_id=tenant_id, pk=preparation_id)
        .first()
    )
    if row is None:
        raise Refusal("NOT_FOUND", "That dispatch preparation was not found.")
    return row


def preparation_dto(preparation: DispatchPreparation, now: datetime) -> dict[str, Any]:
    """E243: the acknowledged scans, the binding, and each line's reserved and scanned totals."""
    transfer = preparation.transfer
    scans = _scans(preparation)
    per_line, per_origin = _scanned(scans)
    version = OfficialVersion.objects.get(pk=preparation.source_version_id)
    outstanding = (
        transfers._outstanding(transfer, version)
        if preparation.state == DispatchPreparation.State.OPEN
        else {}
    )
    reserved = _reserved_by_origin(outstanding)
    tags = aliases_by_line(transfer, version, now)
    names = transfers.people_names(
        [preparation.opened_by_id, *[s.actor_id for s in scans if s.actor_id]]
    )
    from core.kernel_models import OfficialLine

    lines = []
    for line in OfficialLine.objects.filter(version_id=version.pk).order_by("line_no"):
        key = line.stable_line_key
        shares = reserved.get(key, {})
        origins = sorted(
            {origin for origin in shares if origin is not None}
            | {origin for (k, origin) in per_origin if k == key},
            key=str,
        )
        lines.append(
            {
                "line_key": str(key),
                "sku_id": str(line.payload["sku_id"]) if line.payload.get("sku_id") else None,
                "description": str(line.payload.get("description") or ""),
                "approved_qty": int(line.payload["qty"]),
                "reserved_qty": sum(shares.values()),
                "scanned_qty": per_line.get(key, 0),
                "alias_values": tags.get(key, []),
                "origins": [
                    {
                        "origin_id": str(origin),
                        "reserved_qty": shares.get(origin, 0),
                        "scanned_qty": per_origin.get((key, origin), 0),
                    }
                    for origin in origins
                ],
            }
        )
    return {
        "id": str(preparation.pk),
        "transfer_id": str(transfer.pk),
        "source_site_id": str(transfer.source_site_id),
        "source_version_id": str(preparation.source_version_id),
        "state": preparation.state,
        "revision": preparation.revision,
        "content_hash": preparation.content_hash,
        "opened_by": {
            "id": str(preparation.opened_by_id),
            "name": names.get(preparation.opened_by_id, ""),
        },
        "opened_at": preparation.opened_at.isoformat(),
        "last_activity_at": preparation.last_activity_at.isoformat(),
        "closed_at": preparation.closed_at.isoformat() if preparation.closed_at else None,
        "invalidated_reason": preparation.invalidated_reason,
        "dispatch_id": str(preparation.dispatch_id) if preparation.dispatch_id else None,
        "scanned_qty": sum(per_line.values()),
        "acknowledged_scan_keys": [str(row.scan_key) for row in scans],
        "lines": lines,
        "scans": [
            {
                "scan_key": str(row.scan_key),
                "line_key": str(row.line_key),
                "origin_id": str(row.origin_id) if row.origin_id else None,
                "qty": row.qty,
                "alias_value": row.alias_value,
                "actual_at": row.event_at.isoformat(),
                "recorded_at": row.recorded_at.isoformat(),
                "scanned_by": {
                    "id": str(row.actor_id) if row.actor_id else None,
                    "name": names.get(row.actor_id, "") if row.actor_id else "",
                },
            }
            for row in scans
        ],
    }


def open_summary(transfer: GoodsTransfer) -> dict[str, Any] | None:
    """The transfer's open preparation in a line, for the transfer record."""
    row = DispatchPreparation.objects.filter(
        transfer=transfer, state=DispatchPreparation.State.OPEN
    ).first()
    if row is None:
        return None
    scanned = sum(DispatchScan.objects.filter(preparation=row).values_list("qty", flat=True))
    return {
        "id": str(row.pk),
        "revision": row.revision,
        "scanned_qty": int(scanned),
        "opened_at": row.opened_at.isoformat(),
        "last_activity_at": row.last_activity_at.isoformat(),
    }
