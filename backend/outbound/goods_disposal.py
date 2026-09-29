"""Goods-v1 disposal of recorded stock (goods ticket 15D; design §7 P21, §7.3).

Quarantine outcomes PRD §7 and goods PRD §14.9.2, §14.10 GSA-R01 and GSA-R05. A
disposal records the *actual* physical removal of goods from company custody -
their destruction, or their handover for scrap/recycling - as one numbered
record: site, the exact pieces and their source references, the actual quantity,
reason, method, the actual event time and its recording time, the actor, and
the evidence of destruction or handover.

* **Recorded, then approved by the Owner.** Whoever records it (the store person
  or the warehouse, at their own site) never approves it; the approval policy for
  ``movement.approve`` purpose ``disposal`` names the Owner (GSA-R01). Nothing
  leaves the books until that decision, and a rejection returns the draft.
* **Quarantined, recorded, company-owned stock.** Only recorded pieces standing in
  the site's own quarantine under at least one hold are taken, valued at an
  established recorded cost (a receipt or opening PT origin, or a found
  adjustment valued by one). Pre-PT custody keeps unknown value and is ticket
  15E's; an operational ``value_damage`` memo is not recorded cost. Vendor-owned
  goods leave through an RTV (GSA-R05). Nothing is disposed of merely because it
  is old, damaged or not returnable: only an explicit record does it.
* **Never a reserved piece.** A piece still committed to a pending RTV is refused
  (``RTV_RESERVED``) until the RTV's balance is explicitly withdrawn, which keeps
  the RTV's history; any other reservation is refused as ``RESERVED``.
* **Only what actually went, once.** Approval posts P21: exactly the frozen pieces
  go to the ``disposed`` boundary and every hold over exactly those pieces ends
  (overall PRD §15.2.1 rule 3: disposal is their exit route). Every other piece,
  and every hold on it, stays - a partial disposal leaves the balance physically
  in quarantine.
* **The loss is recognised once.** A disposal that names an approved write-off
  (``source_document_id``) takes only that write-off's pieces and posts no value
  leg: their loss was recognised by the write-off (``written_off``). A disposal
  naming none takes only pieces no write-off has touched and recognises their
  loss now, at each portion's recorded layer cost (``recorded_layer_cost``). The
  ledger refuses a second recognition itself (stockledger migration 0020).
* **Scrap proceeds are a separate fact.** What a scrap dealer paid is recorded on
  the disposal and never netted against the loss or posted anywhere.

No GL, vendor, cash, payable or tax entry is written. Donation and sale as damaged
merchandise are not offered. An approved disposal is never edited or deleted.

Lock order: SITE (the site guard) -> DOCUMENT (the movement head, then pending
damage reports) -> LOT -> SERIES.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from django.utils.dateparse import parse_datetime

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
from core.operational import DISPOSAL_POSTING, ValuePair
from core.refusals import Refusal, issue
from masters.goods_models import Location, SiteGuard
from outbound import damage_review
from outbound import goods_movements as movements
from outbound import goods_writeoff as writeoff
from outbound.goods_models import DamageReport, GoodsMovement
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import ActiveHold, ActiveReservation, Origin, Position

KIND = movements.DISPOSAL_KIND
#: Design §4.3's central document type for a disposal (aligned by ticket 15D).
DOC_TYPE = "DSP"
POSTING = DISPOSAL_POSTING
BOUNDARY = "disposed"
#: The header object only a disposal carries.
HEADER_FIELD = "disposal"

DESTRUCTION = "destruction"
SCRAP_HANDOVER = "scrap_handover"
#: Quarantine outcomes PRD §7.2: the initial methods. Donation and sale as damaged
#: merchandise need a later decision and are refused, never mapped onto these.
METHODS = (DESTRUCTION, SCRAP_HANDOVER)

#: A disposal naming no write-off recognises the loss now, at recorded layer cost.
BASIS_RECORDED = "recorded_layer_cost"
#: A disposal after a write-off: the loss was recognised by that write-off.
BASIS_WRITTEN_OFF = "written_off"
#: What the read says once the Owner approved it.
DISPOSED = "disposed"

APPROVE_ACTION = movements.APPROVE_ACTION
APPROVAL_EXCEPTION = "approval_pending"
RESOLUTION_ACTIONS = movements.RESOLUTION_ACTIONS
#: The field grant that shows money, over the stock read (the stock-value rule).
VALUE_READ = writeoff.VALUE_READ

MAX_NOTE = 1000
MAX_EVIDENCE = 20
MAX_REFERENCE = 100
MAX_RECIPIENT = 120
#: Paise; a guard against a mistyped amount, not a business limit.
MAX_PROCEEDS = 10**13

FACT_FIELDS = frozenset(
    {"method", "disposed_at", "handed_over_to", "scrap_proceeds_paise", "evidence_reference"}
)
#: Line fields other movement kinds own. A disposal takes pieces where they stand.
NOT_FOR_DISPOSAL = writeoff.NOT_FOR_WRITEOFF


def _invalid(message: str, field: str) -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field)])


def _refuse(message: str, code: str, **extra: Any) -> Refusal:
    return Refusal("MOVEMENT_INVALID", message, status=422, issues=[issue(code, message, **extra)])


# ---------------------------------------------------------------------------
# Input: MovementPayload with kind "disposal" and its ``disposal`` facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """What actually happened to the goods, as the recorder states it."""

    method: str
    disposed_at: datetime
    handed_over_to: str | None
    scrap_proceeds_paise: int | None
    evidence_reference: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "disposed_at": self.disposed_at.isoformat(),
            "handed_over_to": self.handed_over_to,
            "scrap_proceeds_paise": (
                str(self.scrap_proceeds_paise) if self.scrap_proceeds_paise is not None else None
            ),
            "evidence_reference": self.evidence_reference,
        }


@dataclass(frozen=True)
class Payload:
    site_id: int
    reason_code: str
    evidence_ids: tuple[uuid.UUID, ...]
    evidence_note: str
    #: The approved write-off whose pieces this disposal removes, if any.
    write_off_id: uuid.UUID | None
    facts: Facts
    lines: tuple[movements.Line, ...]

    def as_header(self) -> dict[str, Any]:
        return {
            "kind": KIND,
            "site_id": str(self.site_id),
            "reason_code": self.reason_code,
            "evidence_ids": [str(e) for e in self.evidence_ids],
            "evidence_note": self.evidence_note or None,
            "source_document_id": str(self.write_off_id) if self.write_off_id else None,
            "count_id": None,
            HEADER_FIELD: self.facts.as_json(),
        }

    def as_movement(self) -> movements.Payload:
        return movements.Payload(
            kind=KIND,
            site_id=self.site_id,
            reason_code=self.reason_code,
            evidence_ids=self.evidence_ids,
            source_document_id=self.write_off_id,
            lines=self.lines,
        )

    @property
    def quantity(self) -> int:
        return sum(line.qty for line in self.lines)


def parse(body: dict[str, Any]) -> Payload:
    """The closed MovementPayload for a disposal, refused before any scope lookup."""
    unknown = sorted(set(body) - movements.PAYLOAD_FIELDS - {HEADER_FIELD})
    if unknown:
        raise _invalid(f"Unknown field(s): {', '.join(unknown)}.", unknown[0])
    if str(body.get("kind") or "") != KIND:
        raise _invalid("kind must be disposal.", "kind")
    if body.get("count_id") is not None:
        raise _refuse(
            "A count_id belongs to a count's own delta, not to a disposal.", "COUNT_OWNED"
        )
    for field in movements.RTV_HEADER_FIELDS:
        if body.get(field) not in (None, ""):
            raise _refuse(
                f"{field} belongs to a return to vendor, not to a disposal.",
                "NOT_FOR_DISPOSAL",
                field=field,
            )
    reason = body.get("reason_code")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 60 or len(reason) > 60:
        raise _invalid("reason_code is 1 to 60 characters.", "reason_code")
    facts = _facts(body.get(HEADER_FIELD))
    evidence, note = _evidence(body.get("evidence_ids"), body.get("evidence_note"), facts)
    raw_lines = body.get("lines")
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= movements.MAX_LINES:
        raise _invalid(f"A disposal has 1 to {movements.MAX_LINES} lines.", "lines")
    lines = tuple(_line(item) for item in raw_lines)
    keys = [line.line_key for line in lines]
    if len(set(keys)) != len(keys):
        raise _invalid("Every line_key on a disposal is distinct.", "lines")
    return Payload(
        site_id=movements._int_id(body.get("site_id"), "site_id"),
        reason_code=reason,
        evidence_ids=evidence,
        evidence_note=note,
        write_off_id=movements._optional_uuid(body.get("source_document_id"), "source_document_id"),
        facts=facts,
        lines=lines,
    )


def _text(raw: Any, field: str, limit: int) -> str | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, str) or len(raw) > limit or not raw.strip():
        raise _invalid(f"{field} is text of 1 to {limit} characters.", field)
    return raw.strip()


def _facts(raw: Any) -> Facts:
    """Method, actual time, recipient and proceeds: the closed ``disposal`` object."""
    if not isinstance(raw, dict):
        raise _invalid(
            "disposal is an object saying how and when the goods were actually disposed of.",
            HEADER_FIELD,
        )
    unknown = sorted(set(raw) - FACT_FIELDS)
    if unknown:
        raise _invalid(f"Unknown disposal field(s): {', '.join(unknown)}.", unknown[0])
    method = raw.get("method")
    if not isinstance(method, str) or not method:
        raise _invalid("disposal.method is destruction or scrap_handover.", "method")
    if method not in METHODS:
        raise _refuse(
            "Goods are disposed of by destruction or by handover for scrap/recycling. Donation "
            "and sale as damaged merchandise are not offered.",
            "METHOD_NOT_SUPPORTED",
            field="method",
        )
    when = raw.get("disposed_at")
    parsed = parse_datetime(when) if isinstance(when, str) else None
    if parsed is None or parsed.tzinfo is None:
        raise _invalid(
            "disposal.disposed_at is when the goods were actually destroyed or handed over, "
            "as a date-time with its time zone.",
            "disposed_at",
        )
    recipient = _text(raw.get("handed_over_to"), "handed_over_to", MAX_RECIPIENT)
    proceeds = _proceeds(raw.get("scrap_proceeds_paise"))
    if method == SCRAP_HANDOVER and recipient is None:
        raise _refuse(
            "A scrap/recycling handover names who took the goods.",
            "RECIPIENT_REQUIRED",
            field="handed_over_to",
        )
    if method == DESTRUCTION and (recipient is not None or proceeds is not None):
        raise _refuse(
            "Destroyed goods are handed to nobody and fetch nothing: a recipient and scrap "
            "proceeds belong to a scrap/recycling handover.",
            "METHOD_FIELD_MISMATCH",
            field="handed_over_to" if recipient is not None else "scrap_proceeds_paise",
        )
    reference = _text(raw.get("evidence_reference"), "evidence_reference", MAX_REFERENCE)
    return Facts(
        method=method,
        disposed_at=parsed,
        handed_over_to=recipient,
        scrap_proceeds_paise=proceeds,
        evidence_reference=reference,
    )


def _proceeds(raw: Any) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.isdigit() or int(raw) > MAX_PROCEEDS:
        raise _invalid(
            "scrap_proceeds_paise is a whole number of paise, written as a string.",
            "scrap_proceeds_paise",
        )
    return int(raw)


def _evidence(raw: Any, note: Any, facts: Facts) -> tuple[tuple[uuid.UUID, ...], str]:
    """Evidence of the destruction or handover: a photo, a note or a reference."""
    raw = raw or []
    if not isinstance(raw, list) or len(raw) > MAX_EVIDENCE:
        raise _invalid(f"evidence_ids is a list of at most {MAX_EVIDENCE} IDs.", "evidence_ids")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        raise _invalid(f"evidence_note is text of at most {MAX_NOTE} characters.", "evidence_note")
    text = (note or "").strip()
    evidence = tuple(movements._uuid(e, "evidence_ids") for e in raw)
    if not evidence and not text and facts.evidence_reference is None:
        raise _refuse(
            "A disposal needs evidence of the destruction or handover: attach a photo, write a "
            "note or give the handover slip's reference.",
            "EVIDENCE_REQUIRED",
            field="evidence_note",
        )
    return evidence, text


def _line(item: Any) -> movements.Line:
    if not isinstance(item, dict):
        raise _invalid("Every disposal line is an object.", "lines")
    for field in NOT_FOR_DISPOSAL:
        if item.get(field) not in (None, [], ""):
            raise _refuse(
                f"{field} is not part of a disposal: the pieces leave from where they stand, "
                "and no cost is supplied - it is their own recorded cost.",
                "NOT_FOR_DISPOSAL",
                field=field,
            )
    trimmed = {k: v for k, v in item.items() if k not in NOT_FOR_DISPOSAL}
    return movements._parse_line(trimmed, KIND)


# ---------------------------------------------------------------------------
# The write-off a disposal follows, if it names one
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    """An approved write-off at the disposal's site, and exactly the pieces it wrote off."""

    document_id: uuid.UUID
    number: str | None
    pieces: dict[uuid.UUID, list[ranges.Interval]]


def _link(run: CommandRun, payload: Payload) -> Link | None:
    if payload.write_off_id is None:
        return None
    document = DocumentIdentity.objects.filter(
        tenant_id=run.tenant_id, pk=payload.write_off_id
    ).first()
    if document is None or document.site_id != payload.site_id:
        raise Refusal("NOT_FOUND", "That write-off was not found at this site.")
    movement = GoodsMovement.objects.filter(document_id=document.pk).first()
    if movement is None or movement.kind != GoodsMovement.Kind.WRITEOFF:
        raise _refuse(
            "A disposal names only the write-off whose goods it removes, or nothing.",
            "SOURCE_NOT_WRITE_OFF",
            field="source_document_id",
        )
    head = DocumentHead.objects.select_related("live_version").get(document_id=document.pk)
    if head.live_version is None:
        raise _refuse(
            "That write-off is not approved yet: nothing was written off to follow.",
            "WRITE_OFF_NOT_APPROVED",
            field="source_document_id",
        )
    return Link(document.pk, document.official_number, _frozen_pieces(head.live_version))


def _frozen_pieces(version: OfficialVersion) -> dict[uuid.UUID, list[ranges.Interval]]:
    out: dict[uuid.UUID, list[ranges.Interval]] = {}
    for line in version.lines.all():
        for piece in dict(line.payload).get("portions") or []:
            out.setdefault(uuid.UUID(str(piece["lot_id"])), []).append(
                (int(piece["lower"]), int(piece["upper"]))
            )
    return {lot_id: ranges.normalise(parts) for lot_id, parts in out.items()}


# ---------------------------------------------------------------------------
# Source portions: quarantined, recorded at cost, company-owned, unreserved
# ---------------------------------------------------------------------------


def _quarantine_line(run: CommandRun, line: movements.Line, site_id: int) -> None:
    """A disposal takes quarantined goods from the site's own quarantine, nothing else."""
    location = Location.objects.filter(tenant_id=run.tenant_id, pk=line.source_location_id).first()
    if location is None:
        raise Refusal("NOT_FOUND", "That location was not found.")
    if location.site_id != site_id:
        raise _refuse(
            "That location is at another site. A disposal names goods at its own site.",
            "WRONG_SITE",
            line_key=str(line.line_key),
        )
    if not (location.system and location.kind == "quarantine"):
        raise _refuse(
            "Only goods held in the site's quarantine are disposed of. Available goods are put "
            "on hold first.",
            "LOCATION_NOT_ELIGIBLE",
            line_key=str(line.line_key),
        )


def _refuse_vendor_owned(line: movements.Line, sku_ids: Iterable[uuid.UUID | None]) -> None:
    if writeoff._vendor_owned([s for s in sku_ids if s is not None]):
        raise _refuse(
            "These goods are the brand's, not the company's. Only company-owned stock is "
            "disposed of here; vendor-owned goods go back through a return to vendor.",
            "VENDOR_OWNED",
            line_key=str(line.line_key),
        )


@dataclass(frozen=True)
class _Reserved:
    interval: ranges.Interval
    rtv: bool
    number: str | None


def _reservations(lot_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[_Reserved]]:
    """Live reservations per lot, and whether each is a pending RTV's (and its number)."""
    out: dict[uuid.UUID, list[_Reserved]] = {}
    rows = ActiveReservation.objects.filter(lot_id__in=list(lot_ids)).values_list(
        "lot_id",
        "portion",
        "transfer_version__document__goods_movement__kind",
        "transfer_version__document__official_number",
    )
    for lot_id, stored, kind, number in rows:
        out.setdefault(lot_id, []).append(
            _Reserved(bounds(stored), kind == GoodsMovement.Kind.RTV, number)
        )
    return out


def _free_slices(line: movements.Line, site_id: int, link: Link | None) -> list[movements.Slice]:
    """The oldest eligible pieces at the line's quarantine, FIFO, and why the rest are not."""
    standing = movements._standing(line, site_id)
    _refuse_vendor_owned(line, [item.address.sku_id for item in standing])
    lot_ids = [p.lot_id for p in standing]
    holds, _reserved = engine.active_encumbrances(lot_ids)
    reserved = _reservations(lot_ids)
    gone = engine.written_off(lot_ids)
    free: list[movements.Slice] = []
    counts = dict.fromkeys(
        (
            "UNVALUED_CUSTODY",
            "MEMO_VALUE_ONLY",
            "NOT_HELD",
            "RTV_RESERVED",
            "RESERVED",
            "WRITTEN_OFF",
            "NOT_IN_WRITE_OFF",
        ),
        0,
    )
    for item in standing:
        size = ranges.length(item.interval)
        if not writeoff._recorded_cost(item):
            memo = item.source_kind == Origin.SourceKind.VALUE_DAMAGE
            counts["MEMO_VALUE_ONLY" if memo else "UNVALUED_CUSTODY"] += size
            continue
        held = ranges.intersect([item.interval], holds.get(item.lot_id, []))
        counts["NOT_HELD"] += size - ranges.total(held)
        if link is not None:
            live = ranges.intersect(held, link.pieces.get(item.lot_id, []))
            counts["NOT_IN_WRITE_OFF"] += ranges.total(held) - ranges.total(live)
        else:
            lot_gone = gone.get(item.lot_id, [])
            counts["WRITTEN_OFF"] += ranges.total(ranges.intersect(held, lot_gone))
            live = ranges.subtract(held, lot_gone)
        for claim in reserved.get(item.lot_id, []):
            taken = ranges.total(ranges.intersect(live, [claim.interval]))
            counts["RTV_RESERVED" if claim.rtv else "RESERVED"] += taken
        pieces = ranges.subtract(live, [claim.interval for claim in reserved.get(item.lot_id, [])])
        free.extend(movements.Slice(item.lot_id, piece, item.address) for piece in pieces)
    available = ranges.total([s.interval for s in free])
    if available < line.qty:
        raise Refusal(
            "MOVEMENT_INVALID",
            f"Only {available} of {line.qty} piece(s) there can be disposed of: a disposal takes "
            "company-owned pieces held in quarantine at their recorded cost and never a reserved "
            "one - withdraw pieces from a pending RTV first. Written-off pieces are disposed of "
            "by naming their write-off, and only its pieces are then taken.",
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


def _still_eligible(
    line: movements.Line, slices: Sequence[movements.Slice], link: Link | None
) -> None:
    """A frozen portion that left the pool since the draft is refused; nothing is written."""
    key = str(line.line_key)
    _refuse_vendor_owned(line, [piece.address.sku_id for piece in slices])
    lot_ids = [piece.lot_id for piece in slices]
    gone = engine.written_off(lot_ids)
    reserved = _reservations(lot_ids)
    sources = writeoff._source_kinds(slices)
    for piece in slices:
        span = portion(*piece.interval)
        basis = writeoff._basis(piece.address)
        source = sources.get(basis) if basis is not None else None
        if source == Origin.SourceKind.VALUE_DAMAGE:
            raise _refuse(
                "Some of these pieces carry only a damage memo value, which is not a recorded "
                "cost. Goods of unknown value are disposed of through their receipt (15E).",
                "MEMO_VALUE_ONLY",
                line_key=key,
            )
        if piece.address.sku_id is None or source not in (
            Origin.SourceKind.RECEIPT,
            Origin.SourceKind.OPENING,
        ):
            raise _refuse(
                "Some of these pieces have no recorded cost. Their value stays unknown; they are "
                "not disposed of here.",
                "UNVALUED_CUSTODY",
                line_key=key,
            )
        lot_gone = gone.get(piece.lot_id, [])
        if link is None and ranges.intersect([piece.interval], lot_gone):
            raise _refuse(
                "Some of these pieces were written off since the disposal was recorded. Name "
                "their write-off, so that their loss is not recognised a second time.",
                "WRITTEN_OFF",
                line_key=key,
            )
        if link is not None and not ranges.contains(
            link.pieces.get(piece.lot_id, []), [piece.interval]
        ):
            raise _refuse(
                "Some of these pieces are not the named write-off's pieces.",
                "NOT_IN_WRITE_OFF",
                line_key=key,
            )
        for claim in reserved.get(piece.lot_id, []):
            if not ranges.intersect([piece.interval], [claim.interval]):
                continue
            if claim.rtv:
                raise _refuse(
                    f"Some of these pieces are committed to pending RTV {claim.number or ''}. "
                    "Withdraw them from that RTV first - its history is kept - and then dispose "
                    "of them.",
                    "RTV_RESERVED",
                    line_key=key,
                )
            raise _refuse(
                "Another movement has reserved some of these pieces. A reserved piece is never "
                "disposed of.",
                "RESERVED",
                line_key=key,
            )
        holds = [
            bounds(h.portion)
            for h in ActiveHold.objects.filter(lot_id=piece.lot_id, portion__overlap=span)
        ]
        if not ranges.contains(ranges.normalise(holds), [piece.interval]):
            raise _refuse(
                "Some of these pieces are no longer held in quarantine.",
                "NOT_HELD",
                line_key=key,
            )


def _line_body(
    line: movements.Line, slices: Sequence[movements.Slice], link: Link | None
) -> dict[str, Any]:
    first = slices[0].address
    basis = BASIS_WRITTEN_OFF if link is not None else BASIS_RECORDED
    return {
        "line_key": str(line.line_key),
        "lot_id": str(line.lot_id) if line.lot_id else None,
        "qty": line.qty,
        "sku_id": str(first.sku_id) if first.sku_id else None,
        "origin_id": str(first.origin_id) if first.origin_id else None,
        "condition": first.condition,
        "source_location_id": str(line.source_location_id),
        # The pieces leave for the disposed boundary, not another location.
        "destination_location_id": None,
        "hold_keys": [],
        # Each portion names the layer it came from: the source reference, and -
        # when no write-off came first - the recorded cost the loss is valued at.
        "portions": [
            {
                "lot_id": str(piece.lot_id),
                "lower": piece.interval[0],
                "upper": piece.interval[1],
                "origin_id": writeoff._basis(piece.address),
            }
            for piece in slices
        ],
        "value_basis": basis,
    }


Pinned = movements.Pinned


def _resolve(
    run: CommandRun, payload: Payload, link: Link | None, pinned: Pinned | None = None
) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    out: list[tuple[uuid.UUID, dict[str, Any]]] = []
    seen: dict[uuid.UUID, list[ranges.Interval]] = {}
    for line in payload.lines:
        _quarantine_line(run, line, payload.site_id)
        if pinned is not None:
            slices = movements.source_slices(line, payload.site_id, pinned)
        else:
            slices = _free_slices(line, payload.site_id, link)
        for piece in slices:
            taken = seen.setdefault(piece.lot_id, [])
            if ranges.intersect([piece.interval], taken):
                raise _refuse("Two lines of this disposal claim the same pieces.", "OVERLAP")
            taken.append(piece.interval)
        _still_eligible(line, slices, link)
        out.append((line.line_key, _line_body(line, slices, link)))
    return out


# ---------------------------------------------------------------------------
# Draft (carried by E151)
# ---------------------------------------------------------------------------


def create(run: CommandRun, payload: Payload) -> tuple[DocumentIdentity, str]:
    """Record the disposal with its exact pieces frozen. Nothing leaves the books yet."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A disposal is recorded by a named person.")
    movements.require_movement_site(run, payload.site_id)
    if payload.facts.disposed_at > run.now:
        raise _refuse(
            "A disposal records what actually happened: its time cannot be in the future.",
            "EVENT_TIME_INVALID",
            field="disposed_at",
        )
    site = movements.site_of(payload.site_id)
    link = _link(run, payload)
    identity, head = new_document(
        run,
        kind=DOC_TYPE,
        purpose=KIND,
        entity_id=site.gstin.legal_entity_id,
        site_id=payload.site_id,
    )
    engine.lock_lots(run, movements._candidate_lots(payload.as_movement()))
    writeoff._evidence_exist(run, payload.evidence_ids)
    lines = _resolve(run, payload, link)
    append_revision(run, head, header=payload.as_header(), replace_lines=list(lines))
    movements._record_movement(run, identity, KIND, payload.write_off_id)
    run.audit_subject_key = f"movement:{identity.pk}"
    run.audit_site_id = payload.site_id
    run.audit_after = {"movement_id": str(identity.pk), "kind": KIND, "state": "draft"}
    return identity, "draft"


def draft_payload(head: DocumentHead) -> tuple[Payload, Pinned]:
    """The typed payload of the locked draft and its frozen portions."""
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
            # The frozen portions pin the pieces exactly (see goods_writeoff).
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
    return payload, pinned


def _draft_lots(head: DocumentHead) -> list[uuid.UUID]:
    """Every lot the head's current draft froze a portion of."""
    if head.draft_revision is None:
        return []
    return [
        uuid.UUID(str(piece["lot_id"]))
        for state in revision_lines(head.document_id, head.draft_revision.revision)
        for piece in dict(state.payload).get("portions") or []
    ]


def _pinned_lots(pinned: Pinned) -> list[uuid.UUID]:
    return [lot_id for pieces in pinned.values() for lot_id, _l, _u in pieces]


def _recognised(bodies: Sequence[tuple[uuid.UUID, Mapping[str, Any]]]) -> list[tuple[int, str]]:
    """(pieces, origin) of every portion whose loss this disposal recognises itself."""
    return [
        (int(piece["upper"]) - int(piece["lower"]), str(piece["origin_id"]))
        for _key, body in bodies
        if body.get("value_basis") == BASIS_RECORDED
        for piece in body["portions"]
    ]


def _amounts(payload: Payload, bodies: Sequence[tuple[uuid.UUID, Mapping[str, Any]]]) -> Amounts:
    """Quantity, and the loss this disposal recognises: none after a write-off."""
    pieces = _recognised(bodies)
    costs = writeoff._costs(origin for _qty, origin in pieces)
    return Amounts(payload.quantity, sum(qty * costs[origin] for qty, origin in pieces))


# ---------------------------------------------------------------------------
# Submit (E155)
# ---------------------------------------------------------------------------


def submit(
    run: CommandRun,
    movement: GoodsMovement,
    *,
    reviewed_hash: str,
    expected_revision: int | None,
) -> tuple[GoodsMovement, uuid.UUID]:
    """Send the disposal for the Owner's distinct approval (GSA-R01). No effects here."""
    document_id = movement.document_id
    site_id = movement.document.held_site_id
    movements.require_movement_site(run, site_id)
    head = lock_heads(run, [document_id])[document_id]
    if expected_revision is not None and expected_revision != head.revision:
        raise Refusal(
            "REVISION_SUPERSEDED", "This disposal changed after you loaded it. Reload it."
        )
    if reviewed_hash != movements.draft_hash(head):
        raise Refusal(
            "REVISION_SUPERSEDED", "What you reviewed is no longer this disposal's content."
        )
    if head.state != DocumentHead.State.DRAFT:
        raise _refuse("This disposal is not a draft.", "NOT_DRAFT")
    supersede_pending(run, "document", str(document_id), APPROVE_ACTION)
    payload, pinned = draft_payload(head)
    link = _link(run, payload)
    engine.lock_lots(run, _pinned_lots(pinned))
    bodies = _resolve(run, payload, link, pinned)
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
        title=f"Dispose of {payload.quantity} piece(s)",
        policy=policy,
        new_subject=True,
    )
    set_state(run, head, DocumentHead.State.SUBMITTED, event="submitted")
    open_exception(
        run,
        kind=APPROVAL_EXCEPTION,
        site_id=site_id,
        subject_key=f"movement:{document_id}",
        reason_code="DISPOSAL_APPROVAL",
        source_event_key=engine.event_key("disposal_approval", document_id, request.pk),
        allowed_resolution_actions=RESOLUTION_ACTIONS,
        note=f"approval_request:{request.pk}",
    )
    run.audit_subject_key = f"movement:{document_id}"
    run.audit_site_id = site_id
    run.audit_after = {"approval_request_id": str(request.pk), "state": "submitted"}
    return movement, request.pk


# ---------------------------------------------------------------------------
# The decision (E234 -> goods_movements.decide_release -> here): P21
# ---------------------------------------------------------------------------


def decide(run: CommandRun, context: DecisionContext, movement: GoodsMovement) -> dict[str, Any]:
    request = context.request
    document_id = movement.document_id
    site_id = movement.document.held_site_id
    context.access.require(APPROVE_ACTION, site_id=site_id)
    movements.check_movement_site(SiteGuard.objects.filter(site_id=site_id).first())
    head = lock_heads(run, [document_id])[document_id]
    if str(context.checker_id) == str(head.document.maker_id):
        raise Refusal("SELF_APPROVAL", "Someone who recorded this disposal cannot also approve it.")
    # DOCUMENT rank, before the policy check claims a later rank: approving may
    # close a pending damage report over these lots, so it waits behind a review
    # decision already holding that report.
    reports = (
        damage_review.lock_pending_over(run, _draft_lots(head))
        if context.decision != "reject"
        else []
    )
    context.enforce_policy(run)
    if head.state != DocumentHead.State.SUBMITTED:
        raise Refusal("STATE_CONFLICT", "This disposal is no longer waiting for approval.")
    if request.reviewed_hash != movements.draft_hash(head):
        raise Refusal("REVISION_SUPERSEDED", "The disposal changed after it was submitted.")
    exception_key = engine.event_key("disposal_approval", document_id, request.pk)
    if context.decision == "reject":
        set_state(
            run, head, DocumentHead.State.DRAFT, event="rejected", reason_code=context.reason_code
        )
        resolve_exceptions(
            run,
            kind=APPROVAL_EXCEPTION,
            subject_key=f"movement:{document_id}",
            reason_code="DISPOSAL_REJECTED",
            source_event_key=exception_key,
        )
        return {"state": "rejected"}
    payload, pinned = draft_payload(head)
    link = _link(run, payload)
    engine.lock_lots(run, _pinned_lots(pinned))
    bodies = _resolve(run, payload, link, pinned)
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
    ended = _post(run, version, site_id, bodies)
    resolve_exceptions(
        run,
        kind=APPROVAL_EXCEPTION,
        subject_key=f"movement:{document_id}",
        reason_code="DISPOSAL_APPROVED",
        source_event_key=exception_key,
    )
    record_event(run, document_id, "disposed", version_id=version.pk)
    _close_disposed_reports(run, document_id, version, number, reports, bodies)
    _close_hold_work(run, ended)
    return {"movement_id": str(document_id), "number": number}


def _post(
    run: CommandRun,
    version: OfficialVersion,
    site_id: int,
    bodies: Sequence[tuple[uuid.UUID, Mapping[str, Any]]],
) -> set[uuid.UUID]:
    """P21: exactly the frozen pieces leave custody, once; their loss is recognised once.

    Per portion: every hold over exactly those pieces ends (the disposal is their
    exit route), the pieces go to the ``disposed`` boundary, and - only where no
    write-off recognised the loss already - value stock -> external at the
    portion's own recorded unit cost. Returns the hold keys it ended.
    """
    plan = engine.Plan(POSTING, version.pk, engine.event_key(KIND, version.pk))
    costs = writeoff._costs(origin for _qty, origin in _recognised(bodies))
    ended: set[uuid.UUID] = set()
    for _key, body in bodies:
        recognise = body.get("value_basis") == BASIS_RECORDED
        for piece in body["portions"]:
            lot_id = uuid.UUID(str(piece["lot_id"]))
            interval = (int(piece["lower"]), int(piece["upper"]))
            ended |= _end_holds(run, plan, lot_id, interval, site_id, version.pk)
            engine.end_positions(run, plan, lot_id, interval, BOUNDARY, KIND)
            if not recognise:
                continue
            origin_id = str(piece["origin_id"])
            plan.value.append(
                ValuePair(
                    origin_id=uuid.UUID(origin_id),
                    amount=ranges.length(interval) * costs[origin_id],
                    source_bucket="stock",
                    destination_bucket="external",
                    source_site_id=site_id,
                    destination_site_id=None,
                    lot_id=lot_id,
                    lower=interval[0],
                    upper=interval[1],
                )
            )
    engine.post(run, version, plan)
    return ended


def _end_holds(
    run: CommandRun,
    plan: engine.Plan,
    lot_id: uuid.UUID,
    interval: ranges.Interval,
    site_id: int,
    version_id: uuid.UUID,
) -> set[uuid.UUID]:
    """End every hold over exactly this piece, and no more of any hold than that."""
    ended: set[uuid.UUID] = set()
    for hold in list(ActiveHold.objects.filter(lot_id=lot_id, portion__overlap=portion(*interval))):
        for sub in ranges.intersect([bounds(hold.portion)], [interval]):
            engine.release_hold(
                run,
                plan,
                lot_id=lot_id,
                interval=sub,
                hold_key=hold.hold_key,
                site_id=site_id,
                source_version_id=version_id,
            )
        ended.add(hold.hold_key)
    return ended


def _close_disposed_reports(
    run: CommandRun,
    document_id: uuid.UUID,
    version: OfficialVersion,
    number: str,
    reports: Sequence[DamageReport],
    bodies: Sequence[tuple[uuid.UUID, Mapping[str, Any]]],
) -> None:
    """Close every pending damage report whose last piece this disposal removed.

    The same rule as an RTV pickup (Anand's 15B decision 3): a report over
    disposed and remaining pieces stays pending while any of them is still here,
    and closes with the disposal that takes the last. Nobody reviewed the damage,
    so no reviewer is written.
    """
    taken: dict[uuid.UUID, list[ranges.Interval]] = {}
    for _key, body in bodies:
        for piece in body["portions"]:
            taken.setdefault(uuid.UUID(str(piece["lot_id"])), []).append(
                (int(piece["lower"]), int(piece["upper"]))
            )
    for report in reports:
        covered = damage_review.report_pieces(report)
        if not any(
            ranges.intersect(spans, taken.get(lot_id, [])) for lot_id, spans in covered.items()
        ):
            continue
        if not damage_review.all_at_boundary(report, BOUNDARY):
            continue
        damage_review.close_departed(run, report, note=f"Disposed of on {number}")
        record_event(
            run,
            document_id,
            "damage_report_closed",
            version_id=version.pk,
            reason_code="DISPOSED",
            payload={"details": [], "damage_report_id": str(report.pk)},
        )


def _close_hold_work(run: CommandRun, keys: Iterable[uuid.UUID]) -> None:
    """A hold's owned work ends once nothing stands under its key any more."""
    for key in sorted(set(keys), key=str):
        if ActiveHold.objects.filter(hold_key=key).exists():
            continue
        resolve_exceptions(
            run,
            kind=movements.HOLD_EXCEPTION,
            subject_key=f"hold:{key}",
            reason_code="DISPOSED",
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def detail(access: Any, movement: GoodsMovement, head: DocumentHead) -> dict[str, Any]:
    """What was disposed of, how the loss stands, and the write-off it follows.

    ``value_paise`` is the loss *this* disposal recognised, per line and in
    total, shown only to a reader holding the ``cost`` field grant over the stock
    read at the site for the line's brand - the stock-value rule - and absent
    after a write-off, whose own read shows the loss it recognised. Scrap
    proceeds are a separate fact, never netted against the loss.
    """
    version = head.live_version
    if version is not None:
        header = dict(version.canonical_payload)
        bodies = [dict(line.payload) for line in version.lines.order_by("line_no")]
    elif head.draft_revision is not None:
        header = dict(head.draft_revision.payload)
        bodies = [
            dict(state.payload)
            for state in revision_lines(head.document_id, head.draft_revision.revision)
        ]
    else:  # pragma: no cover - a movement always has a draft
        header, bodies = {}, []
    facts = dict(header.get(HEADER_FIELD) or {})
    site_id = movement.document.site_id
    brands = writeoff._brands(str(body.get("sku_id")) for body in bodies if body.get("sku_id"))
    costs = writeoff._costs(
        str(piece["origin_id"])
        for body in bodies
        for piece in body.get("portions") or []
        if piece.get("origin_id")
    )
    lines: list[dict[str, Any]] = []
    pieces: list[tuple[uuid.UUID, ranges.Interval]] = []
    shown = True
    total = 0
    recognised_qty = 0
    for body in bodies:
        frozen = [
            (uuid.UUID(str(p["lot_id"])), (int(p["lower"]), int(p["upper"])), p.get("origin_id"))
            for p in body.get("portions") or []
        ]
        pieces.extend((lot_id, interval) for lot_id, interval, _origin in frozen)
        basis = body.get("value_basis") or BASIS_RECORDED
        row: dict[str, Any] = {
            "line_key": str(body["line_key"]),
            "qty": int(body["qty"]),
            "value_basis": basis,
        }
        if basis == BASIS_RECORDED:
            recognised_qty += row["qty"]
            visible = "cost" in access.field_grants(
                site_id=site_id,
                brand_id=brands.get(str(body.get("sku_id"))),
                actions={VALUE_READ},
            )
            if visible:
                value = sum(
                    ranges.length(interval) * costs.get(str(origin), 0)
                    for _lot, interval, origin in frozen
                )
                row["value_paise"] = str(value)
                total += value
            shown = shown and visible
        lines.append(row)
    qty = sum(line["qty"] for line in lines)
    out: dict[str, Any] = {
        "state": DISPOSED if version is not None else None,
        "method": facts.get("method"),
        "disposed_at": facts.get("disposed_at"),
        # When it was recorded, beside when it actually happened.
        "recorded_at": movement.document.created_at.isoformat(),
        "qty": qty,
        "disposed_qty": disposed_pieces(pieces) if version is not None else 0,
        "value_basis": BASIS_WRITTEN_OFF if movement.source_document_id else BASIS_RECORDED,
        "recognised_here_qty": recognised_qty,
        "written_off_earlier_qty": qty - recognised_qty,
        "write_off": _write_off_ref(movement),
        "scrap_proceeds_paise": facts.get("scrap_proceeds_paise"),
        "lines": lines,
    }
    if shown and recognised_qty:
        out["value_paise"] = str(total)
    return out


def _write_off_ref(movement: GoodsMovement) -> dict[str, Any] | None:
    if movement.source_document_id is None:
        return None
    number = (
        DocumentIdentity.objects.filter(pk=movement.source_document_id)
        .values_list("official_number", flat=True)
        .first()
    )
    return {"id": str(movement.source_document_id), "number": number}


def disposed_pieces(pieces: Sequence[tuple[uuid.UUID, ranges.Interval]]) -> int:
    """How many of these pieces stand at the disposed boundary."""
    wanted = writeoff._by_lot(pieces)
    return sum(
        ranges.total(ranges.intersect([bounds(position.portion)], wanted[position.lot_id]))
        for position in Position.objects.filter(lot_id__in=list(wanted), boundary=BOUNDARY)
    )


def disposals_of(write_off_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every disposal naming this write-off, with its state, number and quantity."""
    found = list(
        GoodsMovement.objects.select_related("document")
        .filter(source_document_id=write_off_id, kind=GoodsMovement.Kind.DISPOSAL)
        .order_by("document__created_at")
    )
    heads = {
        head.document_id: head
        for head in DocumentHead.objects.select_related("live_version", "draft_revision").filter(
            document_id__in=[movement.document_id for movement in found]
        )
    }
    rows: list[dict[str, Any]] = []
    for movement in found:
        head = heads[movement.document_id]
        if head.live_version is not None:
            bodies = [dict(line.payload) for line in head.live_version.lines.all()]
        elif head.draft_revision is not None:
            bodies = [
                dict(state.payload)
                for state in revision_lines(head.document_id, head.draft_revision.revision)
            ]
        else:  # pragma: no cover - a movement always has a draft
            bodies = []
        rows.append(
            {
                "id": str(movement.document_id),
                "number": movement.document.official_number,
                "state": head.state,
                "qty": sum(int(body["qty"]) for body in bodies),
            }
        )
    return rows
