"""Goods-v1 write-off without physical disposal (goods ticket 15C; design §7 P20, E153).

Quarantine outcomes PRD §7 and goods PRD §14.10 GSA-R01 and GSA-R05. A write-off
records the authorised decision to recognise the loss of an *established
recorded value*. It is not a disposal and it moves nothing:

* **Owner only, a different person.** Whoever prepares it (the store person or
  the warehouse) never approves it; the approval policy for ``movement.approve``
  purpose ``writeoff`` names the Owner (GSA-R01), and nothing is recognised
  until that decision.
* **At recorded cost, never invented.** Each frozen portion is valued at its own
  origin's recorded unit cost - a receipt or opening PT line, or a found
  adjustment valued by such an origin. Pre-PT custody has no recorded cost and
  keeps unknown value; an operational ``value_damage`` memo is not recorded
  cost either. Both are refused, never written off at zero.
* **Company-owned stock only.** The SKU's brand must be the company's own (the
  brand master's ownership axis). Vendor-owned goods leave through an RTV.
* **Goods stay put.** Only recorded pieces standing in the site's quarantine
  under at least one hold are taken, never a reserved one. Approval posts P20 -
  value stock -> external at recorded cost, and a ``write_off`` hold over
  exactly those pieces - and no quantity leg: the pieces keep their site,
  location, condition, acceptance and every other hold. The write-off hold keeps
  them in quarantine and unavailable even if another hold is later lifted.
* **Once.** A written-off piece is never written off again (nor moved on a
  quarantine transfer or an RTV, which would carry its value again); the value
  journal is the permanent evidence (``engine.written_off``) and the database
  refuses a second recognition itself (stockledger migration 0019).

No GL, vendor, cash, payable or tax entry is written. Physical disposal is
goods tickets 15D/15E: a disposal names this record, takes only its pieces, ends
the write-off hold over what it removes and recognises no loss a second time
(``outbound.goods_disposal``); the read here links every such disposal.

Lock order: SITE (the site guard) -> DOCUMENT (the movement head) -> LOT -> SERIES.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
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
from core.operational import WRITE_OFF_POSTING, ValuePair
from core.refusals import Refusal, issue
from masters.goods_models import Location, SiteGuard
from outbound import goods_movements as movements
from outbound.goods_models import GoodsMovement
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import ActiveHold, ActiveReservation, Origin, Position

KIND = "writeoff"
#: Design §4.3's central document type for a write-off.
DOC_TYPE = "WOF"
POSTING = WRITE_OFF_POSTING
#: The hold kind a write-off places. No release lifts it (a release lifts only
#: its own ``movement_hold``); disposal (15D, ``goods_disposal``; 15E) is its exit.
HOLD_KIND = "write_off"
VALUE_BASIS = "recorded_layer_cost"
#: What the read says once the Owner approved it.
WRITTEN_OFF = "written_off"

DRAFT_ACTION = movements.DRAFT_ACTION
APPROVE_ACTION = movements.APPROVE_ACTION
APPROVAL_EXCEPTION = "approval_pending"
RESOLUTION_ACTIONS = movements.RESOLUTION_ACTIONS
#: The field grant that shows money, over the stock read (the stock-value rule).
VALUE_READ = "stock.view"

MAX_NOTE = 1000
MAX_EVIDENCE = 20

#: Line fields other movement kinds own. A write-off takes pieces where they stand.
NOT_FOR_WRITEOFF = (
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


# ---------------------------------------------------------------------------
# Input: MovementPayload with kind "writeoff"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Payload:
    site_id: int
    reason_code: str
    evidence_ids: tuple[uuid.UUID, ...]
    evidence_note: str
    lines: tuple[movements.Line, ...]

    def as_header(self) -> dict[str, Any]:
        return {
            "kind": KIND,
            "site_id": str(self.site_id),
            "reason_code": self.reason_code,
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
    """The closed MovementPayload for a write-off, refused before any scope lookup."""
    unknown = sorted(set(body) - movements.PAYLOAD_FIELDS)
    if unknown:
        raise _invalid(f"Unknown field(s): {', '.join(unknown)}.", unknown[0])
    if str(body.get("kind") or "") != KIND:
        raise _invalid("kind must be writeoff.", "kind")
    if body.get("count_id") is not None:
        raise _refuse(
            "A count_id belongs to a count's own delta, not to a write-off.", "COUNT_OWNED"
        )
    for field in ("source_document_id", *movements.RTV_HEADER_FIELDS):
        if body.get(field) not in (None, ""):
            raise _refuse(
                f"{field} is not part of a write-off: it names its pieces, reason and evidence.",
                "NOT_FOR_WRITEOFF",
                field=field,
            )
    reason = body.get("reason_code")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 60 or len(reason) > 60:
        raise _invalid("reason_code is 1 to 60 characters.", "reason_code")
    evidence, note = _evidence(body.get("evidence_ids"), body.get("evidence_note"))
    raw_lines = body.get("lines")
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= movements.MAX_LINES:
        raise _invalid(f"A write-off has 1 to {movements.MAX_LINES} lines.", "lines")
    lines = tuple(_line(item) for item in raw_lines)
    keys = [line.line_key for line in lines]
    if len(set(keys)) != len(keys):
        raise _invalid("Every line_key on a write-off is distinct.", "lines")
    return Payload(
        site_id=movements._int_id(body.get("site_id"), "site_id"),
        reason_code=reason,
        evidence_ids=evidence,
        evidence_note=note,
        lines=lines,
    )


def _evidence(raw: Any, note: Any) -> tuple[tuple[uuid.UUID, ...], str]:
    """An evidenced decision: a photo, a note or both. A reason code alone is not."""
    raw = raw or []
    if not isinstance(raw, list) or len(raw) > MAX_EVIDENCE:
        raise _invalid(f"evidence_ids is a list of at most {MAX_EVIDENCE} IDs.", "evidence_ids")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        raise _invalid(f"evidence_note is text of at most {MAX_NOTE} characters.", "evidence_note")
    text = (note or "").strip()
    evidence = tuple(movements._uuid(e, "evidence_ids") for e in raw)
    if not evidence and not text:
        raise _refuse(
            "A write-off needs evidence: attach a photo or write a note saying why the value "
            "is lost.",
            "EVIDENCE_REQUIRED",
            field="evidence_note",
        )
    return evidence, text


def _line(item: Any) -> movements.Line:
    if not isinstance(item, dict):
        raise _invalid("Every write-off line is an object.", "lines")
    for field in NOT_FOR_WRITEOFF:
        if item.get(field) not in (None, [], ""):
            raise _refuse(
                f"{field} is not part of a write-off: the pieces stay where they stand, under "
                "every hold they have, and no cost is supplied - it is their own recorded cost.",
                "NOT_FOR_WRITEOFF",
                field=field,
            )
    trimmed = {k: v for k, v in item.items() if k not in NOT_FOR_WRITEOFF}
    return movements._parse_line(trimmed, KIND)


# ---------------------------------------------------------------------------
# Source portions: quarantined, recorded at cost, company-owned, unreserved, once
# ---------------------------------------------------------------------------


def _quarantine_line(run: CommandRun, line: movements.Line, site_id: int) -> None:
    """A write-off takes quarantined goods from the site's own quarantine, nothing else."""
    location = Location.objects.filter(tenant_id=run.tenant_id, pk=line.source_location_id).first()
    if location is None:
        raise Refusal("NOT_FOUND", "That location was not found.")
    if location.site_id != site_id:
        raise _refuse(
            "That location is at another site. A write-off names goods at its own site.",
            "WRONG_SITE",
            line_key=str(line.line_key),
        )
    if not (location.system and location.kind == "quarantine"):
        raise _refuse(
            "Only goods held in the site's quarantine are written off. Goods still available "
            "are put on hold first; lost pieces are shrinkage, not a write-off.",
            "LOCATION_NOT_ELIGIBLE",
            line_key=str(line.line_key),
        )


def _recorded_cost(item: engine.Portion) -> bool:
    """Valued at an established recorded cost: a receipt or opening origin behind it."""
    return (
        item.address.sku_id is not None
        and (item.address.origin_id is not None or item.address.value_basis_origin_id is not None)
        and item.source_kind in (Origin.SourceKind.RECEIPT, Origin.SourceKind.OPENING)
    )


def _basis(address: engine.Address) -> str | None:
    found = address.origin_id or address.value_basis_origin_id
    return str(found) if found else None


def _vendor_owned(sku_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """SKUs whose brand is not the company's own (GSA-R05). Unknown is not "ours"."""
    from masters.goods_identity_models import ProductSku
    from masters.models import Brand

    wanted = {s for s in sku_ids if s is not None}
    owned = {
        pk
        for pk, ownership in ProductSku.objects.filter(pk__in=wanted).values_list(
            "pk", "style__brand__ownership"
        )
        if ownership == Brand.Ownership.OWNED
    }
    return wanted - owned


def _refuse_vendor_owned(line: movements.Line, sku_ids: Iterable[uuid.UUID | None]) -> None:
    if _vendor_owned([s for s in sku_ids if s is not None]):
        raise _refuse(
            "These goods are the brand's, not the company's. Only company-owned stock is "
            "written off; vendor-owned goods go back through a return to vendor.",
            "VENDOR_OWNED",
            line_key=str(line.line_key),
        )


def _free_slices(line: movements.Line, site_id: int) -> list[movements.Slice]:
    """The oldest eligible pieces at the line's quarantine, FIFO, and why the rest are not."""
    standing = movements._standing(line, site_id)
    _refuse_vendor_owned(line, [item.address.sku_id for item in standing])
    lot_ids = [p.lot_id for p in standing]
    holds, reservations = engine.active_encumbrances(lot_ids)
    gone = engine.written_off(lot_ids)
    free: list[movements.Slice] = []
    counts = {
        "UNVALUED_CUSTODY": 0,
        "MEMO_VALUE_ONLY": 0,
        "NOT_HELD": 0,
        "RESERVED": 0,
        "WRITTEN_OFF": 0,
    }
    for item in standing:
        size = ranges.length(item.interval)
        if not _recorded_cost(item):
            memo = item.source_kind == Origin.SourceKind.VALUE_DAMAGE
            counts["MEMO_VALUE_ONLY" if memo else "UNVALUED_CUSTODY"] += size
            continue
        held = ranges.intersect([item.interval], holds.get(item.lot_id, []))
        counts["NOT_HELD"] += size - ranges.total(held)
        lot_gone = gone.get(item.lot_id, [])
        counts["WRITTEN_OFF"] += ranges.total(ranges.intersect(held, lot_gone))
        live = ranges.subtract(held, lot_gone)
        lot_reserved = reservations.get(item.lot_id, [])
        counts["RESERVED"] += ranges.total(ranges.intersect(live, lot_reserved))
        pieces = ranges.subtract(live, lot_reserved)
        free.extend(movements.Slice(item.lot_id, piece, item.address) for piece in pieces)
    available = ranges.total([s.interval for s in free])
    if available < line.qty:
        raise Refusal(
            "MOVEMENT_INVALID",
            f"Only {available} of {line.qty} piece(s) there can be written off: a write-off "
            "takes company-owned pieces held in quarantine at their recorded cost, never a "
            "reserved piece, and never one whose loss was already recognised.",
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


def _still_eligible(line: movements.Line, slices: Sequence[movements.Slice]) -> None:
    """A frozen portion that left the pool since the draft is refused; nothing is written."""
    key = str(line.line_key)
    _refuse_vendor_owned(line, [piece.address.sku_id for piece in slices])
    lot_ids = [piece.lot_id for piece in slices]
    gone = engine.written_off(lot_ids)
    sources = _source_kinds(slices)
    for piece in slices:
        span = portion(*piece.interval)
        basis = _basis(piece.address)
        source = sources.get(basis) if basis is not None else None
        if source == Origin.SourceKind.VALUE_DAMAGE:
            raise _refuse(
                "Some of these pieces carry only a damage memo value, which is not a recorded "
                "cost. Their value stays unknown and is never written off.",
                "MEMO_VALUE_ONLY",
                line_key=key,
            )
        if piece.address.sku_id is None or source not in (
            Origin.SourceKind.RECEIPT,
            Origin.SourceKind.OPENING,
        ):
            raise _refuse(
                "Some of these pieces have no recorded cost. Their value stays unknown and is "
                "never written off at zero.",
                "UNVALUED_CUSTODY",
                line_key=key,
            )
        if ranges.intersect([piece.interval], gone.get(piece.lot_id, [])):
            raise _refuse(
                "The loss of some of these pieces was already recognised by another write-off. "
                "The same loss is never recognised twice.",
                "WRITTEN_OFF",
                line_key=key,
            )
        if ActiveReservation.objects.filter(lot_id=piece.lot_id, portion__overlap=span).exists():
            raise _refuse(
                "Another movement has reserved some of these pieces since the write-off was "
                "prepared. A reserved piece is never written off.",
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


def _source_kinds(slices: Sequence[movements.Slice]) -> dict[str, str]:
    """origin id -> its source kind, for the pieces' valuing bases."""
    wanted = {basis for piece in slices if (basis := _basis(piece.address))}
    return {
        str(pk): kind
        for pk, kind in Origin.objects.filter(pk__in=wanted).values_list("pk", "source_kind")
    }


def _line_body(line: movements.Line, slices: Sequence[movements.Slice]) -> dict[str, Any]:
    first = slices[0].address
    return {
        "line_key": str(line.line_key),
        "lot_id": str(line.lot_id) if line.lot_id else None,
        "qty": line.qty,
        "sku_id": str(first.sku_id) if first.sku_id else None,
        "origin_id": str(first.origin_id) if first.origin_id else None,
        "condition": first.condition,
        "source_location_id": str(line.source_location_id),
        # Nothing moves: the pieces stay where they stand.
        "destination_location_id": None,
        "hold_keys": [],
        # Each portion names the layer whose recorded cost is the loss, so the
        # reviewed document says exactly which value is recognised.
        "portions": [
            {
                "lot_id": str(piece.lot_id),
                "lower": piece.interval[0],
                "upper": piece.interval[1],
                "origin_id": _basis(piece.address),
            }
            for piece in slices
        ],
        "value_basis": VALUE_BASIS,
    }


Pinned = movements.Pinned


def _resolve(
    run: CommandRun, payload: Payload, pinned: Pinned | None = None
) -> list[tuple[uuid.UUID, dict[str, Any]]]:
    out: list[tuple[uuid.UUID, dict[str, Any]]] = []
    seen: dict[uuid.UUID, list[ranges.Interval]] = {}
    for line in payload.lines:
        _quarantine_line(run, line, payload.site_id)
        if pinned is not None:
            slices = movements.source_slices(line, payload.site_id, pinned)
        else:
            slices = _free_slices(line, payload.site_id)
        for piece in slices:
            taken = seen.setdefault(piece.lot_id, [])
            if ranges.intersect([piece.interval], taken):
                raise _refuse("Two lines of this write-off claim the same pieces.", "OVERLAP")
            taken.append(piece.interval)
        _still_eligible(line, slices)
        out.append((line.line_key, _line_body(line, slices)))
    return out


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
# E153 draft (carried by E151)
# ---------------------------------------------------------------------------


def create(run: CommandRun, payload: Payload) -> tuple[DocumentIdentity, str]:
    """Draft the write-off with its exact pieces frozen. Nothing is recognised yet."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A write-off is prepared by a named person.")
    movements.require_movement_site(run, payload.site_id)
    site = movements.site_of(payload.site_id)
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
    return payload, pinned


def _pinned_lots(pinned: Pinned) -> list[uuid.UUID]:
    return [lot_id for pieces in pinned.values() for lot_id, _l, _u in pieces]


def _costs(origin_ids: Iterable[str]) -> dict[str, int]:
    return {
        str(pk): int(cost)
        for pk, cost in Origin.objects.filter(pk__in=set(origin_ids)).values_list("pk", "unit_cost")
    }


def _pieces(bodies: Sequence[tuple[uuid.UUID, Mapping[str, Any]]]) -> list[tuple[int, str]]:
    return [
        (int(piece["upper"]) - int(piece["lower"]), str(piece["origin_id"]))
        for _key, body in bodies
        for piece in body["portions"]
    ]


def _amounts(payload: Payload, bodies: Sequence[tuple[uuid.UUID, Mapping[str, Any]]]) -> Amounts:
    """Quantity and the loss at recorded layer cost; a piece without one is refused earlier."""
    pieces = _pieces(bodies)
    costs = _costs(origin for _qty, origin in pieces)
    return Amounts(payload.quantity, sum(qty * costs[origin] for qty, origin in pieces))


# ---------------------------------------------------------------------------
# E155 submit (E200/E201)
# ---------------------------------------------------------------------------


def submit(
    run: CommandRun,
    movement: GoodsMovement,
    *,
    reviewed_hash: str,
    expected_revision: int | None,
) -> tuple[GoodsMovement, uuid.UUID]:
    """Send the write-off for the Owner's distinct approval (GSA-R01). No effects here."""
    document_id = movement.document_id
    site_id = movement.document.held_site_id
    movements.require_movement_site(run, site_id)
    head = lock_heads(run, [document_id])[document_id]
    if expected_revision is not None and expected_revision != head.revision:
        raise Refusal(
            "REVISION_SUPERSEDED", "This write-off changed after you loaded it. Reload it."
        )
    if reviewed_hash != movements.draft_hash(head):
        raise Refusal(
            "REVISION_SUPERSEDED", "What you reviewed is no longer this write-off's content."
        )
    if head.state != DocumentHead.State.DRAFT:
        raise _refuse("This write-off is not a draft.", "NOT_DRAFT")
    supersede_pending(run, "document", str(document_id), APPROVE_ACTION)
    payload, pinned = draft_payload(head)
    engine.lock_lots(run, _pinned_lots(pinned))
    bodies = _resolve(run, payload, pinned)
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
        title=f"Write off {payload.quantity} piece(s)",
        policy=policy,
        new_subject=True,
    )
    set_state(run, head, DocumentHead.State.SUBMITTED, event="submitted")
    open_exception(
        run,
        kind=APPROVAL_EXCEPTION,
        site_id=site_id,
        subject_key=f"movement:{document_id}",
        reason_code="WRITE_OFF_APPROVAL",
        source_event_key=engine.event_key("writeoff_approval", document_id, request.pk),
        allowed_resolution_actions=RESOLUTION_ACTIONS,
        note=f"approval_request:{request.pk}",
    )
    run.audit_subject_key = f"movement:{document_id}"
    run.audit_site_id = site_id
    run.audit_after = {"approval_request_id": str(request.pk), "state": "submitted"}
    return movement, request.pk


# ---------------------------------------------------------------------------
# The decision (E234 -> goods_movements.decide_release -> here): P20
# ---------------------------------------------------------------------------


def hold_key(version_id: uuid.UUID) -> uuid.UUID:
    """One write-off hold key per approved write-off, derived from its official version."""
    return uuid.uuid5(version_id, HOLD_KIND)


def decide(run: CommandRun, context: DecisionContext, movement: GoodsMovement) -> dict[str, Any]:
    request = context.request
    document_id = movement.document_id
    site_id = movement.document.held_site_id
    context.access.require(APPROVE_ACTION, site_id=site_id)
    movements.check_movement_site(SiteGuard.objects.filter(site_id=site_id).first())
    head = lock_heads(run, [document_id])[document_id]
    if str(context.checker_id) == str(head.document.maker_id):
        raise Refusal(
            "SELF_APPROVAL", "Someone who prepared this write-off cannot also approve it."
        )
    context.enforce_policy(run)
    if head.state != DocumentHead.State.SUBMITTED:
        raise Refusal("STATE_CONFLICT", "This write-off is no longer waiting for approval.")
    if request.reviewed_hash != movements.draft_hash(head):
        raise Refusal("REVISION_SUPERSEDED", "The write-off changed after it was submitted.")
    exception_key = engine.event_key("writeoff_approval", document_id, request.pk)
    if context.decision == "reject":
        set_state(
            run, head, DocumentHead.State.DRAFT, event="rejected", reason_code=context.reason_code
        )
        resolve_exceptions(
            run,
            kind=APPROVAL_EXCEPTION,
            subject_key=f"movement:{document_id}",
            reason_code="WRITE_OFF_REJECTED",
            source_event_key=exception_key,
        )
        return {"state": "rejected"}
    payload, pinned = draft_payload(head)
    engine.lock_lots(run, _pinned_lots(pinned))
    bodies = _resolve(run, payload, pinned)
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
    _post(run, version, site_id, bodies)
    resolve_exceptions(
        run,
        kind=APPROVAL_EXCEPTION,
        subject_key=f"movement:{document_id}",
        reason_code="WRITE_OFF_APPROVED",
        source_event_key=exception_key,
    )
    record_event(run, document_id, "written_off", version_id=version.pk)
    return {"movement_id": str(document_id), "number": number}


def _post(
    run: CommandRun,
    version: OfficialVersion,
    site_id: int,
    bodies: Sequence[tuple[uuid.UUID, Mapping[str, Any]]],
) -> None:
    """P20: the loss of exactly the frozen pieces, once, at their own recorded cost.

    Value stock -> external per portion, and the write-off hold over the same
    pieces. No quantity leg: nothing departs, nothing changes location,
    condition or acceptance, and every other hold stays in force.
    """
    plan = engine.Plan(POSTING, version.pk, engine.event_key(KIND, version.pk))
    costs = _costs(origin for _qty, origin in _pieces(bodies))
    key = hold_key(version.pk)
    for _key, body in bodies:
        for piece in body["portions"]:
            lot_id = uuid.UUID(str(piece["lot_id"]))
            interval = (int(piece["lower"]), int(piece["upper"]))
            engine.place_hold(
                run,
                plan,
                lot_id=lot_id,
                interval=interval,
                hold_key=key,
                kind=HOLD_KIND,
                site_id=site_id,
                source_version_id=version.pk,
            )
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


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def detail(access: Any, movement: GoodsMovement, head: DocumentHead) -> dict[str, Any]:
    """What was written off, on what basis, and that the goods are still there.

    ``value_paise`` (per line and in total) is shown only to a reader holding
    the ``cost`` field grant over the stock read at the site for the line's
    brand - the stock-value rule. Without it the key is absent, never zero.
    """
    version = head.live_version
    if version is not None:
        bodies = [dict(line.payload) for line in version.lines.order_by("line_no")]
    elif head.draft_revision is not None:
        bodies = [
            dict(state.payload)
            for state in revision_lines(head.document_id, head.draft_revision.revision)
        ]
    else:  # pragma: no cover - a movement always has a draft
        bodies = []
    site_id = movement.document.site_id
    brands = _brands(str(body.get("sku_id")) for body in bodies if body.get("sku_id"))
    costs = _costs(
        str(piece["origin_id"])
        for body in bodies
        for piece in body.get("portions") or []
        if piece.get("origin_id")
    )
    key = hold_key(version.pk) if version is not None else None
    lines: list[dict[str, Any]] = []
    shown = True
    total = 0
    pieces: list[tuple[uuid.UUID, ranges.Interval]] = []
    for body in bodies:
        frozen = [
            (uuid.UUID(str(p["lot_id"])), (int(p["lower"]), int(p["upper"])), p.get("origin_id"))
            for p in body.get("portions") or []
        ]
        pieces.extend((lot_id, interval) for lot_id, interval, _origin in frozen)
        row: dict[str, Any] = {"line_key": str(body["line_key"]), "qty": int(body["qty"])}
        visible = "cost" in access.field_grants(
            site_id=site_id, brand_id=brands.get(str(body.get("sku_id"))), actions={VALUE_READ}
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
    physical, quarantined = _standing(pieces)
    # Goods ticket 15D: the later disposal link, against these exact pieces.
    from outbound import goods_disposal

    out: dict[str, Any] = {
        "state": WRITTEN_OFF if version is not None else None,
        "value_basis": VALUE_BASIS,
        "qty": sum(line["qty"] for line in lines),
        "physical_qty": physical,
        "quarantine_qty": quarantined,
        "disposed_qty": goods_disposal.disposed_pieces(pieces) if pieces else 0,
        "still_held_qty": _held_by(key, pieces) if key is not None else 0,
        "hold_key": str(key) if key is not None else None,
        "disposals": goods_disposal.disposals_of(movement.document_id),
        "lines": lines,
    }
    if shown and lines:
        out["value_paise"] = str(total)
    return out


def _brands(sku_ids: Iterable[str]) -> dict[str, int]:
    from masters.goods_identity_models import ProductSku

    return {
        str(pk): brand
        for pk, brand in ProductSku.objects.filter(pk__in=set(sku_ids)).values_list(
            "pk", "style__brand_id"
        )
    }


def _standing(pieces: Sequence[tuple[uuid.UUID, ranges.Interval]]) -> tuple[int, int]:
    """How many of these pieces still stand physically at a site, and how many in quarantine."""
    wanted = _by_lot(pieces)
    physical = quarantined = 0
    for position in Position.objects.select_related("location").filter(
        lot_id__in=list(wanted), boundary="physical"
    ):
        part = ranges.total(ranges.intersect([bounds(position.portion)], wanted[position.lot_id]))
        physical += part
        if position.location is not None and position.location.kind == "quarantine":
            quarantined += part
    return physical, quarantined


def _held_by(key: uuid.UUID, pieces: Sequence[tuple[uuid.UUID, ranges.Interval]]) -> int:
    """How many of these pieces the write-off's own hold still covers."""
    wanted = _by_lot(pieces)
    held: dict[uuid.UUID, list[ranges.Interval]] = {}
    for hold in ActiveHold.objects.filter(lot_id__in=list(wanted), hold_key=key):
        held.setdefault(hold.lot_id, []).append(bounds(hold.portion))
    return sum(
        ranges.total(ranges.intersect(ranges.normalise(held.get(lot_id, [])), intervals))
        for lot_id, intervals in wanted.items()
    )


def _by_lot(
    pieces: Sequence[tuple[uuid.UUID, ranges.Interval]],
) -> dict[uuid.UUID, list[ranges.Interval]]:
    out: dict[uuid.UUID, list[ranges.Interval]] = {}
    for lot_id, interval in pieces:
        out.setdefault(lot_id, []).append(interval)
    return {lot_id: ranges.normalise(parts) for lot_id, parts in out.items()}
