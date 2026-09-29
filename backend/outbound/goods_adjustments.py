"""Goods-v1 evidenced adjustments and shrinkage (goods ticket 15A; design §7 P13/P14, E152).

Change PRD §14.10 GSA-R01 and GSA-R03: an adjustment needs a reason and evidence
(a photo or a note); a different person approves it (the Owner); it is valued at
the affected stock's own recorded layer cost; it creates no accounting, payable
or tax entry; receipt excess never uses an adjustment.

Three kinds ride the movement routes (E151/E155/E104/E105) - one document
family, one screen, one approval path:

* ``adjustment_down`` - fewer pieces than recorded, typically a count error;
* ``shrinkage`` - recorded pieces lost;
* ``adjustment_up`` - new-found pieces the books never had (``new_found`` only).

A removal (down or shrinkage) freezes exact source portions when it is drafted,
never takes a piece under a reservation or a hold, and on approval moves exactly
those portions out of stock once (P13): quantity to the ``consumed`` boundary,
value from stock to the external reduction boundary at each portion's own
recorded unit cost. Unvalued custody has no recorded layer to remove, so it is
refused towards its receipt correction route.

A found line opens a new adjustment custody lot on approval (P14). With valid
same-site origin evidence it is valued at that origin's recorded cost and stands
unaccepted in receiving; without it, it stays unvalued in quarantine. Either
way nothing is available until it is physically accepted, and an owned exception
says so. Existing custody - receipt excess, transfer excess - is never added or
valued here (GSA-R03; ticket 16A owns the transfer-excess exception).

Write-off (15C) and disposal (15D/15E) are not kinds here: nothing leaves by
being labelled written off.
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
from core.kernel_models import DocumentHead, DocumentIdentity, OfficialLine, OfficialVersion
from core.numbering import allocate
from core.operational import ValuePair
from core.refusals import Refusal, issue
from masters.goods_models import SiteGuard
from outbound import goods_movements as movements
from outbound.goods_models import GoodsMovement
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import (
    ActiveHold,
    ActiveReservation,
    CustodyLot,
    LiveCoverage,
    Origin,
    Position,
)

DOWN = "adjustment_down"
SHRINKAGE = "shrinkage"
UP = "adjustment_up"
KINDS: tuple[str, ...] = (DOWN, SHRINKAGE, UP)
REMOVING = frozenset({DOWN, SHRINKAGE})

#: Design §4.3's central document types: ADJ for adjustments, SHR for shrinkage.
DOC_TYPE: dict[str, str] = {DOWN: "ADJ", UP: "ADJ", SHRINKAGE: "SHR"}
POSTING: dict[str, str] = {DOWN: "P13", SHRINKAGE: "P13", UP: "P14"}
#: Where a removed portion goes and why (design §7.2 P13's documented boundary).
REMOVED_BOUNDARY = "consumed"
#: The count-evidence boundary a found lot comes from (P14 ``new_found``).
FOUND_BOUNDARY = "count_evidence"

APPROVE_ACTION = movements.APPROVE_ACTION
APPROVAL_EXCEPTION = "approval_pending"
#: Found goods are custody nobody can use until they are accepted (and valued).
CUSTODY_EXCEPTION = "acceptance_remaining"
FOUND_AWAITING_ACCEPTANCE = "FOUND_AWAITING_ACCEPTANCE"
FOUND_UNVALUED = "FOUND_UNVALUED"
RESOLUTION_ACTIONS = movements.RESOLUTION_ACTIONS
#: The acceptance sessions' resolution route: found goods are put away there.
ACCEPTANCE_ROUTE = "stockledger/acceptance-sessions"

MAX_NOTE = 1000
MAX_DESCRIPTION = 240
#: Documents a count-error adjustment may reference: the original receipt or its PT.
RECEIPT_DOC_KINDS = frozenset({"GRN", "CGRN", "RPT", "OPT"})
#: Transfer documents: their excess is valued only through ticket 16A's exception.
TRANSFER_DOC_KINDS = frozenset({"TRF", "TPT", "GAP"})
#: Origins that are cost evidence for found goods: an official receipt or opening line.
EVIDENCE_ORIGIN_KINDS = frozenset({Origin.SourceKind.RECEIPT, Origin.SourceKind.OPENING})

KIND_TITLE = {DOWN: "Adjust down", SHRINKAGE: "Shrinkage of", UP: "Found"}

FOUND_FIELDS = frozenset(
    {"line_key", "sku_id", "qty", "condition", "cost_evidence_origin_id", "description", "lot_id"}
)
#: Line fields a removal never takes: it removes the stock's own recorded cost from
#: the site, so it names no cost evidence, no destination and lifts no hold.
NOT_FOR_REMOVAL = ("cost_evidence_origin_id", "description", "destination_location_id", "hold_keys")


def _invalid(message: str, field: str) -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field)])


def _refuse(message: str, code: str, **extra: Any) -> Refusal:
    return Refusal("MOVEMENT_INVALID", message, status=422, issues=[issue(code, message, **extra)])


def _receipt_route(message: str, code: str, **extra: Any) -> Refusal:
    return Refusal(
        "SUPPLEMENT_ROUTE_REQUIRED", message, status=409, issues=[issue(code, message, **extra)]
    )


def _transfer_excess() -> Refusal:
    return _refuse(
        "Existing transfer excess is valued only through its own governed route "
        "(ticket 16A), never by recording it as found stock.",
        "TRANSFER_EXCESS_ROUTE",
    )


def _receipt_excess() -> Refusal:
    return _receipt_route(
        "Extra pieces against a receipt are receipt excess. Receipt excess never uses an "
        "adjustment; correct the receipt through its counter-GRN or excess route.",
        "RECEIPT_EXCESS",
    )


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FoundLine:
    line_key: uuid.UUID
    sku_id: uuid.UUID
    qty: int
    cost_evidence_origin_id: uuid.UUID | None
    description: str
    #: Only ever set to be refused: naming existing custody is the excess shortcut.
    lot_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Payload:
    kind: str
    site_id: int
    reason_code: str
    evidence_ids: tuple[uuid.UUID, ...]
    evidence_note: str
    source_document_id: uuid.UUID | None
    removals: tuple[movements.Line, ...]
    found: tuple[FoundLine, ...]

    def as_header(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "site_id": str(self.site_id),
            "reason_code": self.reason_code,
            "evidence_ids": [str(e) for e in self.evidence_ids],
            "evidence_note": self.evidence_note or None,
            "source_document_id": str(self.source_document_id) if self.source_document_id else None,
            "count_id": None,
        }

    def as_movement(self) -> movements.Payload:
        """The removal lines as a movement payload, for the shared portion selection."""
        return movements.Payload(
            kind=self.kind,
            site_id=self.site_id,
            reason_code=self.reason_code,
            evidence_ids=self.evidence_ids,
            source_document_id=self.source_document_id,
            lines=self.removals,
        )

    @property
    def quantity(self) -> int:
        return sum(line.qty for line in self.removals) + sum(line.qty for line in self.found)


def parse(body: dict[str, Any]) -> Payload:
    """MovementPayload for an adjustment kind, refused before any scope lookup."""
    unknown = sorted(set(body) - movements.PAYLOAD_FIELDS)
    if unknown:
        raise _invalid(f"Unknown field(s): {', '.join(unknown)}.", unknown[0])
    kind = str(body.get("kind") or "")
    if kind not in KINDS:
        raise _invalid("kind must be adjustment_down, shrinkage or adjustment_up.", "kind")
    if body.get("count_id") is not None:
        raise _refuse(
            "A count_id belongs to a count's own delta, not to this adjustment.", "COUNT_OWNED"
        )
    for field in movements.RTV_HEADER_FIELDS:
        if body.get(field) not in (None, ""):
            raise _refuse(
                f"{field} belongs to a return to vendor, not to an adjustment.",
                "NOT_FOR_ADJUSTMENT",
                field=field,
            )
    reason = body.get("reason_code")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 60 or len(reason) > 60:
        raise _invalid("reason_code is 1 to 60 characters.", "reason_code")
    evidence, note = _evidence(body.get("evidence_ids"), body.get("evidence_note"))
    raw_lines = body.get("lines")
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= movements.MAX_LINES:
        raise _invalid(f"An adjustment has 1 to {movements.MAX_LINES} lines.", "lines")
    removals: tuple[movements.Line, ...] = ()
    found: tuple[FoundLine, ...] = ()
    if kind in REMOVING:
        removals = tuple(_removal_line(item, kind) for item in raw_lines)
        keys = [line.line_key for line in removals]
    else:
        found = tuple(_found_line(item) for item in raw_lines)
        keys = [line.line_key for line in found]
    if len(set(keys)) != len(keys):
        raise _invalid("Every line_key on an adjustment is distinct.", "lines")
    return Payload(
        kind=kind,
        site_id=movements._int_id(body.get("site_id"), "site_id"),
        reason_code=reason,
        evidence_ids=evidence,
        evidence_note=note,
        source_document_id=movements._optional_uuid(
            body.get("source_document_id"), "source_document_id"
        ),
        removals=removals,
        found=found,
    )


def _evidence(raw: Any, note: Any) -> tuple[tuple[uuid.UUID, ...], str]:
    """GSA-R03: a reason and evidence - a photo or a note. A reason code alone is not."""
    raw = raw or []
    if not isinstance(raw, list) or len(raw) > 20:
        raise _invalid("evidence_ids is a list of at most 20 IDs.", "evidence_ids")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        raise _invalid(f"evidence_note is text of at most {MAX_NOTE} characters.", "evidence_note")
    text = (note or "").strip()
    evidence = tuple(movements._uuid(e, "evidence_ids") for e in raw)
    if not evidence and not text:
        raise _refuse(
            "An adjustment needs evidence: attach a photo or write a note saying what was found.",
            "EVIDENCE_REQUIRED",
            field="evidence_note",
        )
    return evidence, text


def _removal_line(item: Any, kind: str) -> movements.Line:
    if not isinstance(item, dict):
        raise _invalid("Every adjustment line is an object.", "lines")
    if item.get("transfer_exception_id") is not None:
        raise _transfer_excess()
    for field in NOT_FOR_REMOVAL:
        if item.get(field) not in (None, [], ""):
            raise _refuse(
                f"{field} is not part of a {kind}: the pieces leave the site at their own "
                "recorded cost, and a hold is never lifted by removing goods.",
                "NOT_FOR_REMOVAL",
                field=field,
            )
    trimmed = {k: v for k, v in item.items() if k not in (*NOT_FOR_REMOVAL,)}
    return movements._parse_line(trimmed, kind)


def _found_line(item: Any) -> FoundLine:
    if not isinstance(item, dict):
        raise _invalid("Every adjustment line is an object.", "lines")
    if item.get("transfer_exception_id") is not None:
        raise _transfer_excess()
    placed = [
        f
        for f in ("source_location_id", "destination_location_id", "origin_id", "hold_keys")
        if item.get(f) not in (None, [], "")
    ]
    if placed:
        raise _invalid(
            "A found line names no location, origin or hold: the server places new-found "
            "goods in receiving or quarantine.",
            placed[0],
        )
    unknown = sorted(
        set(item)
        - FOUND_FIELDS
        - {"source_location_id", "destination_location_id", "origin_id", "hold_keys"}
        - {"transfer_exception_id"}
    )
    if unknown:
        raise _invalid(f"Unknown line field(s): {', '.join(unknown)}.", unknown[0])
    qty = item.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= movements.MAX_QTY:
        raise _invalid(f"qty is 1 to {movements.MAX_QTY} pieces.", "qty")
    condition = item.get("condition")
    if condition not in (None, "good"):
        raise _refuse(
            "Found goods are recorded as good. Damage on them is reported through the "
            "damage lifecycle once they are recorded.",
            "CONDITION_NOT_GOOD",
            field="condition",
        )
    description = item.get("description") or ""
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
        raise _invalid(f"description is at most {MAX_DESCRIPTION} characters.", "description")
    if item.get("sku_id") in (None, ""):
        raise _invalid("A found line names the SKU that was found.", "sku_id")
    return FoundLine(
        line_key=movements._uuid(item.get("line_key"), "line_key"),
        sku_id=movements._uuid(item.get("sku_id"), "sku_id"),
        qty=qty,
        cost_evidence_origin_id=movements._optional_uuid(
            item.get("cost_evidence_origin_id"), "cost_evidence_origin_id"
        ),
        description=description,
        lot_id=movements._optional_uuid(item.get("lot_id"), "lot_id"),
    )


# ---------------------------------------------------------------------------
# Removal portions: exact, unencumbered, valued
# ---------------------------------------------------------------------------


def _free_slices(line: movements.Line, site_id: int) -> list[movements.Slice]:
    """The oldest standing pieces at the line's address that nobody holds or has reserved.

    A removal never takes a reserved piece - the reservation belongs to a transfer
    that will dispatch it - and never takes a held one: a hold is lifted only by
    its own release or disposition route. So a SKU line picks around them, FIFO,
    and says why when there are too few left.
    """
    standing = movements._standing(line, site_id)
    holds, reservations = engine.active_encumbrances([p.lot_id for p in standing])
    free: list[movements.Slice] = []
    reserved = held = 0
    for item in standing:
        lot_reserved = reservations.get(item.lot_id, [])
        lot_held = holds.get(item.lot_id, [])
        blocked_r = ranges.intersect([item.interval], lot_reserved)
        reserved += ranges.total(blocked_r)
        held += ranges.total(
            ranges.subtract(ranges.intersect([item.interval], lot_held), blocked_r)
        )
        for piece in ranges.subtract([item.interval], [*lot_held, *lot_reserved]):
            free.append(movements.Slice(item.lot_id, piece, item.address))
    available = ranges.total([s.interval for s in free])
    if available < line.qty:
        issues = [
            issue(
                "INSUFFICIENT_ELIGIBLE_STOCK",
                f"only {available} unencumbered piece(s) stand there",
                line_key=str(line.line_key),
                quantity=line.qty,
            )
        ]
        if reserved:
            issues.append(issue("RESERVED", "reserved for a transfer", quantity=reserved))
        if held:
            issues.append(issue("HOLD_ACTIVE", "under a hold", quantity=held))
        raise Refusal(
            "MOVEMENT_INVALID",
            f"Only {available} of {line.qty} piece(s) there can be adjusted: an adjustment "
            "never takes reserved or held stock.",
            status=422,
            issues=issues,
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


def _unencumbered(line: movements.Line, slices: Sequence[movements.Slice]) -> None:
    """A frozen portion a reservation or hold has reached since the draft is refused."""
    for piece in slices:
        span = portion(*piece.interval)
        if ActiveReservation.objects.filter(lot_id=piece.lot_id, portion__overlap=span).exists():
            raise _refuse(
                "A transfer has reserved some of these pieces since the adjustment was "
                "prepared. An adjustment never consumes a reservation.",
                "RESERVED",
                line_key=str(line.line_key),
            )
        hold = ActiveHold.objects.filter(lot_id=piece.lot_id, portion__overlap=span).first()
        if hold is not None:
            raise _refuse(
                f"A {hold.kind} hold covers some of these pieces. An adjustment never lifts "
                "or bypasses a hold.",
                "HOLD_ACTIVE",
                line_key=str(line.line_key),
            )


def _valued(line: movements.Line, slices: Sequence[movements.Slice]) -> None:
    """GSA-R03 values a removal at the stock's own recorded layer.

    Receipt custody no PT covers has no layer yet and is corrected through its
    receipt (counter-GRN or disposition), never here. New-found custody that
    nobody valued (P14 without evidence) has no receipt to go back to: it may
    be removed, and its value stays unknown - no value leg, never zero - as
    pre-PT custody's does elsewhere (GSA-R05, GSA-R07).
    """
    unvalued = [
        s for s in slices if s.address.origin_id is None and s.address.value_basis_origin_id is None
    ]
    if not unvalued:
        return
    found = set(
        CustodyLot.objects.filter(
            pk__in={s.lot_id for s in unvalued}, source_kind=CustodyLot.SourceKind.ADJUSTMENT
        ).values_list("pk", flat=True)
    )
    receipt = ranges.total([s.interval for s in unvalued if s.lot_id not in found])
    if receipt:
        raise _receipt_route(
            f"{receipt} of these pieces have no recorded value yet. Custody no PT covers is "
            "corrected through its receipt (counter-GRN or disposition), not an adjustment.",
            "UNVALUED_CUSTODY",
            line_key=str(line.line_key),
            quantity=receipt,
        )


def _removal_body(line: movements.Line, slices: Sequence[movements.Slice]) -> dict[str, Any]:
    first = slices[0].address

    def basis(piece: movements.Slice) -> str | None:
        found = piece.address.origin_id or piece.address.value_basis_origin_id
        return str(found) if found else None

    valued = any(basis(piece) for piece in slices)
    return {
        "line_key": str(line.line_key),
        "lot_id": str(line.lot_id) if line.lot_id else None,
        "qty": line.qty,
        "sku_id": str(first.sku_id) if first.sku_id else None,
        "origin_id": str(first.origin_id) if first.origin_id else None,
        "condition": first.condition,
        "source_location_id": str(line.source_location_id),
        "destination_location_id": None,
        "hold_keys": [],
        # Each portion names the layer whose recorded cost values it, so the
        # reviewed document says exactly what value leaves, not an average.
        # A portion of unvalued found custody names none: its value is unknown.
        "portions": [
            {
                "lot_id": str(piece.lot_id),
                "lower": piece.interval[0],
                "upper": piece.interval[1],
                "origin_id": basis(piece),
            }
            for piece in slices
        ],
        "value_basis": "recorded_layer_cost" if valued else "unvalued",
    }


Pinned = movements.Pinned


def _resolve_removals(
    payload: Payload, pinned: Pinned | None = None
) -> list[tuple[movements.Line, list[movements.Slice], dict[str, Any]]]:
    out: list[tuple[movements.Line, list[movements.Slice], dict[str, Any]]] = []
    seen: dict[uuid.UUID, list[ranges.Interval]] = {}
    for line in payload.removals:
        if pinned is not None:
            slices = movements.source_slices(line, payload.site_id, pinned)
        else:
            slices = _free_slices(line, payload.site_id)
        for piece in slices:
            taken = seen.setdefault(piece.lot_id, [])
            if ranges.intersect([piece.interval], taken):
                raise _refuse("Two lines of this adjustment claim the same pieces.", "OVERLAP")
            taken.append(piece.interval)
        _unencumbered(line, slices)
        _valued(line, slices)
        out.append((line, slices, _removal_body(line, slices)))
    return out


# ---------------------------------------------------------------------------
# Found lines: evidence, placement
# ---------------------------------------------------------------------------


def _origin_evidence(
    run: CommandRun, line: FoundLine, site_id: int
) -> tuple[Origin | None, dict[str, Any]]:
    """The contract's valid same-site origin evidence, or ``None`` (unvalued)."""
    if line.cost_evidence_origin_id is None:
        return None, {}
    origin = (
        Origin.objects.select_related("official_line__version")
        .filter(tenant_id=run.tenant_id, pk=line.cost_evidence_origin_id)
        .first()
    )

    def bad(message: str, code: str) -> Refusal:
        return Refusal(
            "COST_EVIDENCE_INVALID",
            message,
            status=422,
            issues=[issue(code, message, line_key=str(line.line_key))],
        )

    if origin is None:
        raise bad("That cost evidence was not found.", "ORIGIN_NOT_FOUND")
    if origin.site_id != site_id:
        raise bad("Cost evidence for found goods is an origin of this same site.", "WRONG_SITE")
    if origin.sku_id != line.sku_id:
        raise bad("The cost evidence is for a different SKU.", "WRONG_SKU")
    if origin.source_kind not in EVIDENCE_ORIGIN_KINDS or origin.official_line is None:
        raise bad(
            "Only an official receipt or opening line is cost evidence for found goods.",
            "NOT_OFFICIAL_ORIGIN",
        )
    head = DocumentHead.objects.filter(document_id=origin.official_line.version.document_id).first()
    if head is None or head.live_version_id != origin.official_line.version_id:
        raise bad(
            "That origin's PT version is no longer in force, so its cost is not evidence.",
            "ORIGIN_NOT_LIVE",
        )
    return origin, {
        "cost_evidence_origin_id": str(origin.pk),
        "value_basis": "origin_evidence",
        # The ticket MRP acceptance compares the found goods' tags against: the
        # recorded MRP of the same origin whose cost values them.
        "mrp_paise": str(int(origin.mrp)),
    }


def _found_bodies(run: CommandRun, payload: Payload) -> list[tuple[FoundLine, dict[str, Any]]]:
    from masters.goods_identity_models import ProductSku

    out: list[tuple[FoundLine, dict[str, Any]]] = []
    for line in payload.found:
        if line.lot_id is not None:
            lot = CustodyLot.objects.filter(tenant_id=run.tenant_id, pk=line.lot_id).first()
            if lot is not None and lot.source_kind == CustodyLot.SourceKind.TRANSFER_EXCESS:
                raise _transfer_excess()
            # Naming custody that already exists is not new-found stock: it is the
            # receipt-excess shortcut GSA-R03 closes.
            raise _receipt_excess()
        sku = ProductSku.objects.filter(
            tenant_id=run.tenant_id, pk=line.sku_id, retired_at__isnull=True
        ).first()
        if sku is None:
            raise Refusal("NOT_FOUND", "That SKU was not found.")
        origin, evidence = _origin_evidence(run, line, payload.site_id)
        location = engine.system_location(
            payload.site_id, "receiving" if origin is not None else "quarantine"
        )
        out.append(
            (
                line,
                {
                    "line_key": str(line.line_key),
                    "lot_id": None,
                    "qty": line.qty,
                    "sku_id": str(line.sku_id),
                    "origin_id": None,
                    "condition": "good",
                    "source_location_id": None,
                    "destination_location_id": str(location.pk),
                    "hold_keys": [],
                    "portions": [],
                    "description": line.description,
                    "cost_evidence_origin_id": None,
                    "value_basis": "unvalued",
                    "mrp_paise": None,
                    **evidence,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# The reference: the original GRN/PT of a count error found after use
# ---------------------------------------------------------------------------


Removals = Sequence[tuple[movements.Line, list[movements.Slice], dict[str, Any]]]


def _reference(run: CommandRun, payload: Payload, removals: Removals) -> None:
    """Check the route the adjustment is allowed to be, and its ``source_document_id``.

    Design §7.3 and goods PRD §5.5: while nothing downstream has used a PT's
    stock, a count error is corrected by reversing the PT and approving a
    counter-GRN, so an ``adjustment_down`` over such stock is refused towards
    that route - whether or not it names a reference. After use, the count error
    is an evidenced adjustment that references the original GRN or PT, and
    neither document is rewritten. Found goods referencing a receipt are receipt
    excess (GSA-R03). Shrinkage is a loss, not a count correction: it is not
    routed, and may name its source as a plain reference.
    """
    coverage = _coverage(removals)
    if payload.kind == DOWN:
        _downstream_route(payload, removals, coverage)
    if payload.source_document_id is None:
        return
    document = DocumentIdentity.objects.filter(
        tenant_id=run.tenant_id, pk=payload.source_document_id
    ).first()
    if document is None or document.site_id != payload.site_id:
        raise Refusal("NOT_FOUND", "That source document was not found at this site.")
    if payload.kind == UP:
        raise _found_reference(document)
    if document.kind not in RECEIPT_DOC_KINDS:
        raise _refuse(
            "An adjustment references the original GRN or PT of the stock it corrects.",
            "REFERENCE_NOT_SUPPORTED",
        )
    head = DocumentHead.objects.filter(document_id=document.pk).first()
    if head is None or head.live_version_id is None:
        raise _refuse("That document is not official.", "REFERENCE_NOT_OFFICIAL")
    _traced_to(document, removals, coverage)


@dataclass(frozen=True)
class _Coverage:
    """What the removed pieces came from, read once for the whole adjustment."""

    #: lot id -> the document whose official line opened it (its GRN), if any.
    lot_documents: Mapping[uuid.UUID, uuid.UUID | None]
    #: lot id -> [(covered interval, PT version id, PT document id)].
    covers: dict[uuid.UUID, list[tuple[ranges.Interval, uuid.UUID, uuid.UUID]]]

    def versions_over(self, piece: movements.Slice) -> set[uuid.UUID]:
        return {
            version
            for interval, version, _document in self.covers.get(piece.lot_id, [])
            if ranges.intersect([piece.interval], [interval])
        }

    def documents_over(self, piece: movements.Slice) -> set[uuid.UUID]:
        return {
            document
            for interval, _version, document in self.covers.get(piece.lot_id, [])
            if ranges.intersect([piece.interval], [interval])
        }


def _coverage(removals: Removals) -> _Coverage:
    lot_ids = {piece.lot_id for _line, slices, _body in removals for piece in slices}
    lot_documents = {
        pk: document
        for pk, document in CustodyLot.objects.filter(pk__in=lot_ids).values_list(
            "pk", "source_line__version__document_id"
        )
    }
    covers: dict[uuid.UUID, list[tuple[ranges.Interval, uuid.UUID, uuid.UUID]]] = {}
    for lot_id, stored, version_id, document_id in LiveCoverage.objects.filter(
        lot_id__in=lot_ids
    ).values_list(
        "lot_id", "portion", "cover_event__pt_version_id", "cover_event__pt_version__document_id"
    ):
        covers.setdefault(lot_id, []).append((bounds(stored), version_id, document_id))
    return _Coverage(lot_documents, covers)


def _downstream_route(payload: Payload, removals: Removals, coverage: _Coverage) -> None:
    """Before downstream use, PT reversal; after it, an adjustment naming its source."""
    from ptmapper.goods_pt_services import reversal_blockers

    covering = {
        version
        for _line, slices, _body in removals
        for piece in slices
        for version in coverage.versions_over(piece)
    }
    if not covering:
        # Found custody valued by origin evidence has no PT behind it: no receipt
        # route exists, so nothing is routed.
        return
    reversible = sorted(
        (version for version in covering if not reversal_blockers(version)), key=str
    )
    if reversible:
        raise _receipt_route(
            "Nothing downstream has used this PT's stock yet, so a count error is corrected "
            "by reversing the PT and approving a counter-GRN, not by an adjustment.",
            "NO_DOWNSTREAM_USE",
        )
    if payload.source_document_id is None:
        raise _refuse(
            "This stock has been used since its PT, so a count error is an adjustment that "
            "names the original GRN or PT it corrects.",
            "REFERENCE_REQUIRED",
            field="source_document_id",
        )


def _found_reference(document: DocumentIdentity) -> Refusal:
    """Found goods referencing a receipt are receipt excess; a transfer's, 16A's."""
    if document.kind in RECEIPT_DOC_KINDS:
        return _receipt_excess()
    if document.kind in TRANSFER_DOC_KINDS:
        return _transfer_excess()
    return _refuse("A found adjustment references no other document.", "REFERENCE_NOT_SUPPORTED")


def _traced_to(document: DocumentIdentity, removals: Removals, coverage: _Coverage) -> None:
    """Every removed piece came from ``document``: its GRN lot, or its PT's live coverage."""
    for line, slices, _body in removals:
        for piece in slices:
            from_grn = coverage.lot_documents.get(piece.lot_id) == document.pk
            if not (from_grn or document.pk in coverage.documents_over(piece)):
                raise _refuse(
                    "These pieces did not come from the referenced GRN or PT.",
                    "SOURCE_NOT_REFERENCED",
                    line_key=str(line.line_key),
                )


def _supported_reason(run: CommandRun, payload: Payload) -> None:
    """Design §5.8 "supported reasons": a configured ``reasons`` list binds the code.

    The list is the ``reasons`` configuration for ``movement.draft`` in force at
    the site for this kind (its purpose). Where one applies, only its unretired
    codes are accepted. Where none is configured the code is kept as typed, as
    every other goods movement keeps it today - requiring a list everywhere is
    a setup decision this ticket does not make.
    """
    from masters.goods_config import ConfigTarget, resolve

    try:
        version = resolve(
            run.tenant_id,
            "reasons",
            ConfigTarget.of(
                run.now, site_id=payload.site_id, brand_ids=[None], purpose=payload.kind
            ),
            match={"action": movements.DRAFT_ACTION},
            code="MOVEMENT_INVALID",
            path="reason_code",
        )
    except Refusal as refusal:
        if (refusal.issues or [{}])[0].get("code") == "CONFIG_MISSING":
            return
        raise
    codes = {
        str(row.get("code"))
        for row in (version.payload or {}).get("codes") or []
        if isinstance(row, dict) and not row.get("retired")
    }
    if payload.reason_code not in codes:
        raise _refuse(
            "That reason is not one of the reasons configured for this adjustment.",
            "REASON_NOT_SUPPORTED",
            field="reason_code",
        )


def _evidence_exists(run: CommandRun, payload: Payload) -> None:
    from files.goods_models import EvidenceObject

    wanted = set(payload.evidence_ids)
    found = set(
        EvidenceObject.objects.filter(tenant_id=run.tenant_id, pk__in=list(wanted)).values_list(
            "pk", flat=True
        )
    )
    missing = sorted(wanted - found, key=str)
    if missing:
        raise _refuse(
            "That evidence file was not found.", "EVIDENCE_NOT_FOUND", field="evidence_ids"
        )


# ---------------------------------------------------------------------------
# E152 draft (carried by E151)
# ---------------------------------------------------------------------------


def create(run: CommandRun, payload: Payload) -> tuple[DocumentIdentity, str]:
    """Draft the adjustment with its exact portions frozen. Nothing moves until approval."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "An adjustment is recorded by a named person.")
    movements.require_movement_site(run, payload.site_id)
    site = movements.site_of(payload.site_id)
    identity, head = new_document(
        run,
        kind=DOC_TYPE[payload.kind],
        purpose=payload.kind,
        entity_id=site.gstin.legal_entity_id,
        site_id=payload.site_id,
    )
    engine.lock_lots(run, movements._candidate_lots(payload.as_movement()))
    _evidence_exists(run, payload)
    _supported_reason(run, payload)
    lines = _lines(run, payload)
    append_revision(run, head, header=payload.as_header(), replace_lines=lines)
    movements._record_movement(run, identity, payload.kind, payload.source_document_id)
    run.audit_subject_key = f"movement:{identity.pk}"
    run.audit_site_id = payload.site_id
    run.audit_after = {"movement_id": str(identity.pk), "kind": payload.kind, "state": "draft"}
    return identity, "draft"


def _lines(
    run: CommandRun, payload: Payload, pinned: Pinned | None = None
) -> list[tuple[uuid.UUID, Mapping[str, Any]]]:
    if payload.kind in REMOVING:
        removals = _resolve_removals(payload, pinned)
        _reference(run, payload, removals)
        return [(line.line_key, body) for line, _slices, body in removals]
    _reference(run, payload, [])
    return [(line.line_key, body) for line, body in _found_bodies(run, payload)]


def draft_payload(head: DocumentHead) -> tuple[Payload, Pinned]:
    revision = head.draft_revision
    assert revision is not None
    header = dict(revision.payload)
    stored = [dict(state.payload) for state in revision_lines(head.document_id, revision.revision)]
    kind = str(header["kind"])
    if kind in REMOVING:
        lines = [
            {
                "line_key": row["line_key"],
                "lot_id": row.get("lot_id"),
                "qty": row["qty"],
                "sku_id": row.get("sku_id"),
                # The frozen portions pin the pieces exactly. The line's origin and
                # condition were read off its first portion only, and
                # ``goods_movements._standing`` applies them even when re-checking
                # pinned portions, so a line spanning two layers would read its
                # second layer as "moved". Replay without them.
                "origin_id": None,
                "condition": None,
                "source_location_id": row.get("source_location_id"),
            }
            for row in stored
        ]
    else:
        lines = [
            {
                "line_key": row["line_key"],
                "sku_id": row["sku_id"],
                "qty": row["qty"],
                "cost_evidence_origin_id": row.get("cost_evidence_origin_id"),
                "description": row.get("description") or "",
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
        if kind in REMOVING
    }
    return payload, pinned


def _pinned_lots(pinned: Pinned) -> list[uuid.UUID]:
    return [lot_id for pieces in pinned.values() for lot_id, _l, _u in pieces]


def layer_value(pieces: Iterable[tuple[int, str | None]]) -> int | None:
    """The value of ``(qty, origin_id)`` pieces at each one's own recorded layer cost (GSA-R03).

    ``None`` when any piece has no recorded origin - pre-PT or otherwise unvalued
    custody keeps unknown value, never zero. Shared with the RTV shortfall
    closure (goods ticket 15H, GSA-R07), which is valued like an adjustment.
    """
    pieces = list(pieces)
    if any(not origin_id for _qty, origin_id in pieces):
        return None
    costs = dict(
        Origin.objects.filter(pk__in={str(o) for _q, o in pieces}).values_list("pk", "unit_cost")
    )
    by_text = {str(pk): int(cost) for pk, cost in costs.items()}
    return sum(qty * by_text[str(o)] for qty, o in pieces)


def _amounts(payload: Payload, bodies: Sequence[tuple[uuid.UUID, Mapping[str, Any]]]) -> Amounts:
    """Quantity, and the value at recorded layer cost - ``None`` when any of it is unvalued."""
    if payload.kind in REMOVING:
        pieces = [
            (int(piece["upper"]) - int(piece["lower"]), piece.get("origin_id"))
            for _key, body in bodies
            for piece in body["portions"]
        ]
    else:
        pieces = [(int(body["qty"]), body.get("cost_evidence_origin_id")) for _key, body in bodies]
    return Amounts(payload.quantity, layer_value(pieces))


# ---------------------------------------------------------------------------
# E155 submit (E196/E197)
# ---------------------------------------------------------------------------


def submit(
    run: CommandRun,
    movement: GoodsMovement,
    *,
    reviewed_hash: str,
    expected_revision: int | None,
) -> tuple[GoodsMovement, uuid.UUID]:
    """Send the adjustment for a distinct Owner approval (GSA-R01). No effects here."""
    document_id = movement.document_id
    site_id = movement.document.held_site_id
    movements.require_movement_site(run, site_id)
    head = lock_heads(run, [document_id])[document_id]
    if expected_revision is not None and expected_revision != head.revision:
        raise Refusal(
            "REVISION_SUPERSEDED", "This adjustment changed after you loaded it. Reload it."
        )
    if reviewed_hash != movements.draft_hash(head):
        raise Refusal(
            "REVISION_SUPERSEDED", "What you reviewed is no longer this adjustment's content."
        )
    if head.state != DocumentHead.State.DRAFT:
        raise Refusal("MOVEMENT_INVALID", "This adjustment is not a draft.", status=422)
    supersede_pending(run, "document", str(document_id), APPROVE_ACTION)
    payload, pinned = draft_payload(head)
    engine.lock_lots(run, _pinned_lots(pinned))
    bodies = _lines(run, payload, pinned if payload.kind in REMOVING else None)
    amounts = _amounts(payload, bodies)
    policy = pin(
        run,
        action=APPROVE_ACTION,
        purpose=payload.kind,
        site_id=site_id,
        brand_ids=[None],
        amounts=amounts,
    )
    request = create_request(
        run,
        subject_kind="document",
        subject_key=str(document_id),
        revision=head.revision,
        reviewed_hash=movements.draft_hash(head),
        requested_action=APPROVE_ACTION,
        site_id=site_id,
        title=f"{KIND_TITLE[payload.kind]} {payload.quantity} piece(s)",
        policy=policy,
        new_subject=True,
    )
    set_state(run, head, DocumentHead.State.SUBMITTED, event="submitted")
    open_exception(
        run,
        kind=APPROVAL_EXCEPTION,
        site_id=site_id,
        subject_key=f"movement:{document_id}",
        reason_code="ADJUSTMENT_APPROVAL",
        source_event_key=engine.event_key("adjustment_approval", document_id, request.pk),
        allowed_resolution_actions=RESOLUTION_ACTIONS,
        note=f"approval_request:{request.pk}",
    )
    run.audit_subject_key = f"movement:{document_id}"
    run.audit_site_id = site_id
    run.audit_after = {"approval_request_id": str(request.pk), "state": "submitted"}
    return movement, request.pk


# ---------------------------------------------------------------------------
# The decision (E234 -> this handler, via goods_movements.decide_release)
# ---------------------------------------------------------------------------


def decide(run: CommandRun, context: DecisionContext, movement: GoodsMovement) -> dict[str, Any]:
    request = context.request
    document_id = movement.document_id
    site_id = movement.document.site_id
    context.access.require(APPROVE_ACTION, site_id=site_id)
    movements.check_movement_site(SiteGuard.objects.filter(site_id=site_id).first())
    head = lock_heads(run, [document_id])[document_id]
    if str(context.checker_id) == str(head.document.maker_id):
        raise Refusal(
            "SELF_APPROVAL", "Someone who prepared this adjustment cannot also approve it."
        )
    context.enforce_policy(run)
    if head.state != DocumentHead.State.SUBMITTED:
        raise Refusal("STATE_CONFLICT", "This adjustment is no longer waiting for approval.")
    if request.reviewed_hash != movements.draft_hash(head):
        raise Refusal("REVISION_SUPERSEDED", "The adjustment changed after it was submitted.")
    exception_key = engine.event_key("adjustment_approval", document_id, request.pk)
    if context.decision == "reject":
        set_state(
            run, head, DocumentHead.State.DRAFT, event="rejected", reason_code=context.reason_code
        )
        resolve_exceptions(
            run,
            kind=APPROVAL_EXCEPTION,
            subject_key=f"movement:{document_id}",
            reason_code="ADJUSTMENT_REJECTED",
            source_event_key=exception_key,
        )
        return {"state": "rejected"}
    payload, pinned = draft_payload(head)
    engine.lock_lots(run, _pinned_lots(pinned))
    bodies = _lines(run, payload, pinned if payload.kind in REMOVING else None)
    identity = head.document
    number = identity.official_number or allocate(run, identity.entity, DOC_TYPE[payload.kind])
    assert run.principal.human_id is not None
    version, official = officialise(
        run,
        head,
        approved_by_id=run.principal.human_id,
        canonical_header=payload.as_header(),
        lines=[(key, body) for key, body in bodies],
        authority=run.authority,
        number=number,
    )
    if payload.kind in REMOVING:
        _post_removal(run, payload, version, bodies)
    else:
        _post_found(run, payload, version, official)
    resolve_exceptions(
        run,
        kind=APPROVAL_EXCEPTION,
        subject_key=f"movement:{document_id}",
        reason_code="ADJUSTMENT_APPROVED",
        source_event_key=exception_key,
    )
    record_event(run, document_id, "adjusted", version_id=version.pk)
    return {"movement_id": str(document_id), "number": number}


def _post_removal(
    run: CommandRun,
    payload: Payload,
    version: OfficialVersion,
    bodies: Sequence[tuple[uuid.UUID, Mapping[str, Any]]],
) -> None:
    """P13: exactly the frozen portions leave stock once, at their own recorded cost.

    A portion of unvalued found custody leaves with no value leg: its value is
    unknown, never zero.
    """
    plan = engine.Plan(
        POSTING[payload.kind], version.pk, engine.event_key(payload.kind, version.pk)
    )
    origin_ids = {
        str(piece["origin_id"])
        for _key, body in bodies
        for piece in body["portions"]
        if piece.get("origin_id")
    }
    costs = {
        str(pk): int(cost)
        for pk, cost in Origin.objects.filter(pk__in=origin_ids).values_list("pk", "unit_cost")
    }
    lots: set[uuid.UUID] = set()
    for _key, body in bodies:
        for piece in body["portions"]:
            lot_id = uuid.UUID(str(piece["lot_id"]))
            lots.add(lot_id)
            interval = (int(piece["lower"]), int(piece["upper"]))
            engine.end_positions(run, plan, lot_id, interval, REMOVED_BOUNDARY, payload.kind)
            origin_id = piece.get("origin_id")
            if not origin_id:
                continue
            plan.value.append(
                ValuePair(
                    origin_id=uuid.UUID(str(origin_id)),
                    amount=ranges.length(interval) * costs[str(origin_id)],
                    source_bucket="stock",
                    destination_bucket="external",
                    source_site_id=payload.site_id,
                    destination_site_id=None,
                    lot_id=lot_id,
                    lower=interval[0],
                    upper=interval[1],
                )
            )
    engine.post(run, version, plan)
    _settle_unvalued_found(run, lots)


def _settle_unvalued_found(run: CommandRun, lot_ids: Iterable[uuid.UUID]) -> None:
    """Close a found adjustment's FOUND_UNVALUED work once none of its unvalued pieces remain."""
    documents = set(
        CustodyLot.objects.filter(
            pk__in=list(lot_ids), source_kind=CustodyLot.SourceKind.ADJUSTMENT
        ).values_list("adjustment_line__version__document_id", flat=True)
    )
    for document_id in sorted(documents, key=str):
        remaining = Position.objects.filter(
            lot__adjustment_line__version__document_id=document_id,
            boundary="physical",
            origin__isnull=True,
            value_basis_origin__isnull=True,
        ).exists()
        if not remaining:
            resolve_exceptions(
                run,
                kind=CUSTODY_EXCEPTION,
                subject_key=f"movement:{document_id}",
                reason_code="FOUND_REMOVED",
                source_event_key=engine.event_key("found_custody", document_id),
            )


def _post_found(
    run: CommandRun, payload: Payload, version: OfficialVersion, official: list[OfficialLine]
) -> None:
    """P14 ``new_found``: a new adjustment custody lot per line, held until accepted.

    Valued lines stand unaccepted in receiving; the acceptance sessions accept
    them against this document (P09) and close its ``acceptance_remaining`` work.
    Unvalued lines stay in quarantine (P14's "missing evidence" branch) with
    their own owned work, closed when they are removed by an adjustment.
    """
    plan = engine.Plan(POSTING[UP], version.pk, engine.event_key(UP, version.pk))
    wanted = {
        str(line.payload["cost_evidence_origin_id"])
        for line in official
        if line.payload.get("cost_evidence_origin_id")
    }
    origins = {str(o.pk): o for o in Origin.objects.filter(pk__in=wanted)}
    valued = unvalued = False
    for line in official:
        body = dict(line.payload)
        origin = origins.get(str(body.get("cost_evidence_origin_id") or ""))
        location = engine.system_location(
            payload.site_id, "receiving" if origin is not None else "quarantine"
        )
        qty = int(body["qty"])
        lot = engine.open_lot(
            run,
            plan,
            source_kind=CustodyLot.SourceKind.ADJUSTMENT,
            site_id=payload.site_id,
            qty=qty,
            identity={
                "sku_id": body["sku_id"],
                "description": body.get("description") or "",
                "condition": "good",
                "adjustment": "new_found",
            },
            source_time=run.now,
            address=engine.Address(
                boundary="physical",
                site_id=payload.site_id,
                location_id=location.pk,
                condition="good",
                sku_id=uuid.UUID(str(body["sku_id"])),
                value_basis_origin_id=origin.pk if origin is not None else None,
            ),
            adjustment_line_id=line.pk,
            from_boundary=FOUND_BOUNDARY,
        )
        if origin is None:
            unvalued = True
            continue
        valued = True
        plan.value.append(
            ValuePair(
                origin_id=origin.pk,
                amount=qty * int(origin.unit_cost),
                source_bucket="adjustment_evidence",
                destination_bucket="stock",
                source_site_id=None,
                destination_site_id=payload.site_id,
                lot_id=lot.pk,
                lower=0,
                upper=qty,
            )
        )
    engine.post(run, version, plan)
    if valued:
        # The acceptance sessions' own convention (subject, key and resolution
        # route), so scanning the goods away closes it exactly as for a PT.
        open_exception(
            run,
            kind=CUSTODY_EXCEPTION,
            site_id=payload.site_id,
            subject_key=f"document:{version.document_id}",
            reason_code=FOUND_AWAITING_ACCEPTANCE,
            source_event_key=version.pk,
            allowed_resolution_actions=[ACCEPTANCE_ROUTE],
            note=f"movement:{version.document_id}",
        )
    if unvalued:
        open_exception(
            run,
            kind=CUSTODY_EXCEPTION,
            site_id=payload.site_id,
            subject_key=f"movement:{version.document_id}",
            reason_code=FOUND_UNVALUED,
            source_event_key=engine.event_key("found_custody", version.document_id),
            allowed_resolution_actions=RESOLUTION_ACTIONS,
            note=f"movement:{version.document_id}",
        )


def found_lots(version: OfficialVersion | None) -> dict[str, str]:
    """line_key -> the custody lot a found line opened, for the detail read."""
    if version is None:
        return {}
    rows = CustodyLot.objects.filter(adjustment_line__version_id=version.pk).values_list(
        "adjustment_line__stable_line_key", "pk"
    )
    return {str(key): str(pk) for key, pk in rows}


# ---------------------------------------------------------------------------
# Reads: the reference a removal may name, the one it names, its owned work
# ---------------------------------------------------------------------------


def _document_row(identity: DocumentIdentity) -> dict[str, Any]:
    return {"id": str(identity.pk), "kind": identity.kind, "number": identity.official_number}


def references_for_origin(
    tenant_id: uuid.UUID, site_id: int, origin_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The original PT and GRN behind one stock layer at this site, for the reference picker.

    Only documents held at the site are offered: an adjustment references the
    receipt of the stock it corrects, and that stock is here.
    """
    from ptmapper.goods_models import GoodsPt

    origin = (
        Origin.objects.select_related("official_line__version__document")
        .filter(tenant_id=tenant_id, pk=origin_id, site_id=site_id)
        .first()
    )
    if origin is None or origin.official_line is None:
        return []
    pt = origin.official_line.version.document
    rows = [_document_row(pt)]
    source = GoodsPt.objects.select_related("grn__document").filter(document_id=pt.pk).first()
    if source is not None and source.grn is not None:
        rows.append(_document_row(source.grn.document))
    return rows


def named_reference(movement: GoodsMovement) -> dict[str, Any] | None:
    """The GRN or PT a removal names, by number, for the detail read."""
    if movement.source_document_id is None:
        return None
    identity = DocumentIdentity.objects.filter(pk=movement.source_document_id).first()
    return _document_row(identity) if identity is not None else None


def owned_work(document_id: uuid.UUID) -> list[dict[str, Any]]:
    """The exceptions this adjustment opened: its approval, and its found goods' work."""
    from alerts.goods_models import GoodsException

    rows = GoodsException.objects.filter(
        subject_key__in=[f"movement:{document_id}", f"document:{document_id}"]
    ).order_by("opened_at", "id")
    return [
        {
            "id": str(row.pk),
            "kind": row.kind,
            "subject_id": row.subject_key,
            "state": row.state,
            "reason_code": row.reason_code,
            "due_at": row.due_at.isoformat() if row.due_at else None,
            "allowed_resolution_actions": list(row.allowed_resolution_actions or []),
        }
        for row in rows
    ]
