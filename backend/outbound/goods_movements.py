"""Goods-v1 bin moves, holds, releases and mark-damaged (design §3.3, §7 P11/P12, E151/E155/E209).

A movement moves custody that already exists; it never creates acceptance.

* **P11 bin move** carries a portion's acceptance evidence with it to a permitted
  same-site location. Accepted goods stay accepted, unaccepted goods stay
  unaccepted, and a location at another site is a transfer, not a move.
* **P12 hold** is a safety action, so it takes effect at once under the actor's
  own authority even while a transfer reservation is live: the exact portions
  move to quarantine, the hold key activates and ATS drops in the same
  transaction. The reservation is left standing and becomes owned work for
  whoever holds it.
* **P12 release** is the opposite - it gives stock back - so it waits for a
  distinct governed ``movement.approve`` decision, and that decision re-validates
  everything the submission relied on. A portion that was never accepted here has
  no eligible location to be released into, and the release is refused. Holds
  this release does not name, and every reservation, stay exactly as they were.

Mark damaged (E209) is a hold that also records the condition. Quarantine is the
destination whatever the client asked for: damage is not a choice of where to put
the goods. Unlike every other movement it goes ahead during a stock count freeze
(goods PRD §14.10 GSA-R02).

Lock order: SITE (the site guard) → DOCUMENT (the movement head) → LOT → SERIES.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from alerts.goods_services import open_exception, resolve_exceptions
from approvals.goods_policy import Amounts, pin
from approvals.goods_services import (
    DecisionContext,
    create_request,
    register_subject_handler,
    supersede_pending,
)
from core.commands import CommandRun, LockRank
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
from core.refusals import Refusal, issue
from masters.goods_models import Location, SiteGuard
from outbound.goods_models import GoodsMovement
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import CONDITIONS, ActiveHold, ActiveReservation, Position

#: The movement kinds this module parses. Adjustments and shrinkage are parsed by
#: ``goods_adjustments`` (below); write-offs (15C), disposals (15D) and RTV (15B)
#: are their own modules, routed before this parser; any other kind would promise a posting
#: that does not exist yet, so it is refused rather than silently accepted.
ACTIVE_KINDS: tuple[str, ...] = ("bin_move", "hold", "release")
#: Goods ticket 15A's kinds. They share these routes, their document family and
#: the release's distinct-approval path, but their parsing, portions and postings
#: (P13/P14) are ``outbound.goods_adjustments``'s.
ADJUSTMENT_KINDS: tuple[str, ...] = ("adjustment_down", "shrinkage", "adjustment_up")
#: Goods ticket 15B: a return to vendor shares the draft, submit and approval
#: routes; its reservation, pickups and withdrawals are ``outbound.goods_rtv``'s.
RTV_KIND = "rtv"
#: The RTV's own document type; its official version keys its reservation.
RTV_DOC_TYPE = "RTV"
#: Goods ticket 15C: a write-off shares the draft, submit and approval routes;
#: its pool, its value-only posting (P20) and its read are ``outbound.goods_writeoff``'s.
WRITEOFF_KIND = "writeoff"
#: Goods ticket 15D: a disposal shares the same routes; its pool, its posting
#: (P21), its link to an earlier write-off and its read are ``outbound.goods_disposal``'s.
DISPOSAL_KIND = "disposal"
#: Header fields only an RTV carries.
RTV_HEADER_FIELDS = ("vendor_id", "agreement_reference")
#: The approval action a release waits for, and the two-person floor it sits under.
APPROVE_ACTION = "movement.approve"
DRAFT_ACTION = "movement.draft"

#: One numbering series per movement kind (design §4.3's document types).
DOC_TYPE: dict[str, str] = {"bin_move": "MOV", "hold": "HLD", "release": "REL"}
#: The posting each kind makes (design §7 P11/P12).
POSTING: dict[str, str] = {"bin_move": "P11", "hold": "P12", "release": "P12"}

#: Locations a person may move goods into. The six protected system locations
#: (receiving, quarantine, excess hold, RTV hold, transit out, custody) are
#: reached only by the command that owns them - acceptance puts away, a hold
#: quarantines, a dispatch goes to transit - so a generic move can neither
#: smuggle goods into one nor take them out of one by hand.
STORAGE_KINDS = frozenset({"floor", "backstore", "zone", "rack", "bin", "fixture"})
#: Locations whose whole purpose is that what stands there is held. A bin move
#: out of one would undo the location half of P12 under the actor's own
#: authority - the release is the command for that (change PRD §6.3 invariant 5).
HOLD_LOCATION_KINDS = frozenset({"quarantine", "excess_hold", "rtv_hold"})
#: The one protected system location a bin move may take goods *out* of. Receiving
#: holds no encumbrance of its own, and this ticket's own criterion needs
#: unaccepted goods moved out of it into a storage location. Every other system
#: kind - quarantine, excess hold, RTV hold, transit out, custody - is left to
#: the command that owns it, so the claim above holds for sources as well as
#: destinations. `transit_out` and `custody` are unreachable today (nothing
#: writes a physical position at either; tickets 13b and 16 own those paths), so
#: the fence is preventive rather than a fix for a live route.
MOVABLE_SYSTEM_KIND = "receiving"

#: ``HoldEvent.kind`` for an ordinary hold, and for damage. Damage shares
#: acceptance's own kind, so the two routes to "this piece is damaged" read as
#: one thing on the stock screen rather than two competing holds.
HOLD_KIND = "movement_hold"
DAMAGE_KIND = "damage"
#: The one hold kind a release here may lift: the operational hold this module's
#: own hold command places over good goods.
#:
#: A damage hold is *not* liftable here. Overall PRD §15.2.1 rule 3 says damaged,
#: wrong and unidentified quantities "remain in quarantine ... removable only
#: through their approved return, disposal, identity-resolution,
#: excess-acceptance or value-damage route", and change PRD J5 says damaged value
#: uses only ``value_damage``. A movement release is none of those routes, so
#: lifting a damage key here would put damaged goods back in an ordinary bin
#: having taken no disposition at all. Those routes are tickets 15a/15b.
#:
#: A receipt's own excess or damage hold (``inbound.goods_services``) and an RTV
#: hold are likewise each lifted by the command that placed them.
RELEASABLE_KIND = HOLD_KIND

#: Exceptions this ticket registers with the shared centre (ticket 08).
HOLD_EXCEPTION = "stock_hold_active"
RELEASE_APPROVAL_EXCEPTION = "approval_pending"
#: A live reservation over goods a hold has just taken out of play.
RESERVATION_BLOCKED_EXCEPTION = "transfer_discrepancy"
#: The screen that resolves both: the movements screen runs the release command.
RESOLUTION_ACTIONS = ["outbound/movements"]

MAX_LINES = 500
MAX_QTY = 999_999

PAYLOAD_FIELDS = frozenset(
    {
        "kind",
        "site_id",
        "source_document_id",
        "reason_code",
        "evidence_ids",
        # Goods ticket 15A: an adjustment's evidence may be a note (GSA-R03).
        "evidence_note",
        # Goods ticket 15B: an RTV names its vendor and the agreement's reference (GSA-R04).
        "vendor_id",
        "agreement_reference",
        "count_id",
        "lines",
    }
)
LINE_FIELDS = frozenset(
    {
        "line_key",
        "sku_id",
        "lot_id",
        "qty",
        "origin_id",
        "source_location_id",
        "destination_location_id",
        "hold_keys",
        "cost_evidence_origin_id",
        "transfer_exception_id",
        "condition",
        # Goods ticket 15A: what a new-found adjustment line says was found.
        "description",
    }
)
#: Line fields that belong to the adjustment and transfer-excess routes. They are
#: part of ``MovementPayload``'s closed schema, so they are accepted by the schema
#: and refused with a reason rather than reported as unknown fields.
NOT_FOR_THESE_KINDS = ("cost_evidence_origin_id", "transfer_exception_id", "description")

CONDITION_VALUES = frozenset(value for value, _label in CONDITIONS)


def _invalid(message: str, field: str) -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field)])


def _refuse(message: str, issues: list[dict[str, Any]] | None = None) -> Refusal:
    return Refusal("MOVEMENT_INVALID", message, status=422, issues=issues or [])


# ---------------------------------------------------------------------------
# Input: MovementPayload (design §5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    line_key: uuid.UUID
    qty: int
    #: Where the goods are standing now. Without it a line names a quantity, not a
    #: portion, and the official record could not say where the pieces came from.
    source_location_id: uuid.UUID
    #: Exactly this lot, or - when the caller works from a stock row, which is an
    #: aggregate and carries no lot - ``None`` and the SKU below, letting the
    #: server take the eligible portions in FIFO order (design §7.3).
    lot_id: uuid.UUID | None
    sku_id: uuid.UUID | None
    origin_id: uuid.UUID | None
    destination_location_id: uuid.UUID | None
    hold_keys: tuple[uuid.UUID, ...]
    condition: str | None


@dataclass(frozen=True)
class Payload:
    kind: str
    site_id: int
    reason_code: str
    evidence_ids: tuple[uuid.UUID, ...]
    source_document_id: uuid.UUID | None
    lines: tuple[Line, ...]

    def as_header(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "site_id": str(self.site_id),
            "reason_code": self.reason_code,
            "evidence_ids": [str(e) for e in self.evidence_ids],
            "source_document_id": str(self.source_document_id) if self.source_document_id else None,
            "count_id": None,
        }


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise _invalid(f"{field} must be an ID.", field) from None


def _optional_uuid(value: Any, field: str) -> uuid.UUID | None:
    return None if value in (None, "") else _uuid(value, field)


def _int_id(value: Any, field: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise _invalid(f"{field} must be an ID.", field) from None
    if parsed < 1:
        raise _invalid(f"{field} must be an ID.", field)
    return parsed


def parse(body: dict[str, Any], *, kind: str | None = None) -> Payload:
    """The closed ``MovementPayload`` schema, refused before any scope lookup."""
    unknown = sorted(set(body) - PAYLOAD_FIELDS)
    if unknown:
        raise _invalid(f"Unknown field(s): {', '.join(unknown)}.", unknown[0])
    given = str(body.get("kind") or "")
    if kind is not None and given not in ("", kind):
        raise _invalid(f"This route records a {kind} movement.", "kind")
    given = given or (kind or "")
    if given not in ACTIVE_KINDS:
        if given in set(GoodsMovement.Kind.values):
            raise _refuse(f"{given} movements are not available in this stage yet.")
        raise _invalid("kind must be bin_move, hold or release.", "kind")
    _refuse_foreign_header(body)
    reason = body.get("reason_code")
    if not isinstance(reason, str) or not 1 <= len(reason) <= 60:
        raise _invalid("reason_code is 1 to 60 characters.", "reason_code")
    raw_evidence = body.get("evidence_ids") or []
    if not isinstance(raw_evidence, list) or len(raw_evidence) > 20:
        raise _invalid("evidence_ids is a list of at most 20 IDs.", "evidence_ids")
    raw_lines = body.get("lines")
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= MAX_LINES:
        raise _invalid(f"A movement has 1 to {MAX_LINES} lines.", "lines")
    lines = tuple(_parse_line(item, given) for item in raw_lines)
    keys = [line.line_key for line in lines]
    if len(set(keys)) != len(keys):
        raise _invalid("Every line_key on a movement is distinct.", "lines")
    return Payload(
        kind=given,
        site_id=_int_id(body.get("site_id"), "site_id"),
        reason_code=reason,
        evidence_ids=tuple(_uuid(e, "evidence_ids") for e in raw_evidence),
        source_document_id=_optional_uuid(body.get("source_document_id"), "source_document_id"),
        lines=lines,
    )


def _refuse_foreign_header(body: dict[str, Any]) -> None:
    """Header fields a count delta or an adjustment owns, never these kinds."""
    if body.get("count_id") is not None:
        raise _refuse("A count_id belongs to a count's own delta, not to this movement.")
    if body.get("evidence_note") not in (None, ""):
        raise _refuse("An evidence note belongs to an adjustment, not to this movement.")
    for field in RTV_HEADER_FIELDS:
        if body.get(field) not in (None, ""):
            raise _refuse(f"{field} belongs to a return to vendor, not to this movement.")


def _parse_line(item: Any, kind: str) -> Line:
    if not isinstance(item, dict):
        raise _invalid("Every movement line is an object.", "lines")
    unknown = sorted(set(item) - LINE_FIELDS)
    if unknown:
        raise _invalid(f"Unknown line field(s): {', '.join(unknown)}.", unknown[0])
    for field in NOT_FOR_THESE_KINDS:
        if item.get(field) is not None:
            raise _refuse(f"{field} belongs to an adjustment, not to a {kind}.")
    qty = item.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
        raise _invalid(f"qty is 1 to {MAX_QTY} pieces.", "qty")
    condition = item.get("condition")
    if condition is not None and condition not in CONDITION_VALUES:
        raise _invalid("condition is not a supported value.", "condition")
    raw_keys = item.get("hold_keys") or []
    if not isinstance(raw_keys, list) or len(raw_keys) > 20:
        raise _invalid("hold_keys is a list of at most 20 IDs.", "hold_keys")
    lot_id = _optional_uuid(item.get("lot_id"), "lot_id")
    sku_id = _optional_uuid(item.get("sku_id"), "sku_id")
    if lot_id is None and sku_id is None:
        raise _invalid("A movement line names either its lot or its SKU.", "lot_id")
    return Line(
        line_key=_uuid(item.get("line_key"), "line_key"),
        lot_id=lot_id,
        qty=qty,
        source_location_id=_uuid(item.get("source_location_id"), "source_location_id"),
        sku_id=sku_id,
        origin_id=_optional_uuid(item.get("origin_id"), "origin_id"),
        destination_location_id=_optional_uuid(
            item.get("destination_location_id"), "destination_location_id"
        ),
        hold_keys=tuple(_uuid(k, "hold_keys") for k in raw_keys),
        condition=condition,
    )


# ---------------------------------------------------------------------------
# Site, locations and source portions
# ---------------------------------------------------------------------------


def require_movement_site(
    run: CommandRun, site_id: int, *, during_count: bool = False
) -> SiteGuard:
    """Lock the site guard and check the goods fence, capability and count freeze.

    Opening setup cannot authorise a routine movement (E151 step 8): a site still
    being set up has ``opening_setup_ready`` and not ``goods_ready``, and only the
    opening routes accept that.

    ``during_count=True`` is for damage reporting alone. A count freeze stops
    sales and stock movements for the counted scope, but it does not stop damage
    reporting: damage found during a count still goes to quarantine at once
    (goods PRD §14.10 GSA-R02). Everything else about the site is still checked.
    """
    guards = run.lock(LockRank.SITE, SiteGuard.objects.filter(site_id=site_id))
    return check_movement_site(guards[0] if guards else None, during_count=during_count)


def check_movement_site(guard: SiteGuard | None, *, during_count: bool = False) -> SiteGuard:
    """The same three checks without taking the lock.

    An approval decision has already passed rank SITE by the time its handler
    runs (``approvals.goods_services.decide`` locks the subject's guard first),
    so it re-reads the guard rather than claiming a rank the fixed lock order has
    closed behind it.
    """
    if guard is None or guard.stock_contract != SiteGuard.StockContract.GOODS_V1:
        raise Refusal("CONTRACT_DISABLED", "This site does not move goods through goods-v1.")
    if not guard.goods_ready or guard.lifecycle in ("planned", "closing"):
        raise Refusal("SITE_NOT_READY", "This site is not approved to move goods.")
    if guard.freeze_id and not during_count:
        raise Refusal("UNDER_COUNT", "A stock count has frozen this site.")
    return guard


def storage_location(tenant_id: uuid.UUID, location_id: uuid.UUID, site_id: int) -> Location:
    """A permitted same-site destination, or the refusal that says why it is not one."""
    location = Location.objects.filter(tenant_id=tenant_id, pk=location_id).first()
    if location is None:
        raise Refusal("NOT_FOUND", "That location was not found.")
    if location.site_id != site_id:
        raise _refuse(
            "That location is at another site. Moving goods between sites is a transfer.",
            [
                issue(
                    "WRONG_SITE",
                    "the destination is at another site",
                    field="destination_location_id",
                )
            ],
        )
    if location.retired_at is not None:
        raise _refuse("That location has been retired.")
    if location.system or location.kind not in STORAGE_KINDS:
        raise _refuse(
            "Goods can only be moved into an ordinary storage location, "
            "never into a protected system location."
        )
    return location


def quarantine_of(site_id: int) -> Any:
    """Where a hold puts goods. Quarantine, whatever the client asked for."""
    return engine.system_location(site_id, "quarantine")


@dataclass(frozen=True)
class Slice:
    lot_id: uuid.UUID
    interval: ranges.Interval
    address: engine.Address


#: Exact portions a draft already froze, as ``(lot id, lower, upper)`` per line key.
Pinned = dict[uuid.UUID, list[tuple[uuid.UUID, int, int]]]


def _standing(line: Line, site_id: int, *, narrow: bool = True) -> list[engine.Portion]:
    """Every physical portion at the line's own address, oldest source first.

    Ordering is design §7.3's FIFO key - source time, stable origin lineage, lot,
    range lower bound - so a line that names a SKU rather than a lot still takes
    the oldest pieces, and takes the same ones on a replay.

    ``narrow=False`` drops the line's own condition/origin/SKU filters. A frozen
    line records those three from its *first* portion, so re-applying them to a
    line whose portions differ in any of them would filter out the very pieces it
    pinned - and a draft has no edit route, so that refusal would be permanent.
    """
    if line.lot_id is not None:
        candidates = [
            item
            for item in engine.portions_from(Position.objects.filter(lot_id=line.lot_id))
            if item.address.boundary == "physical"
        ]
    elif line.sku_id is not None:
        candidates = engine.physical_portions(site_id, sku_ids=[line.sku_id])
    else:  # pragma: no cover - `parse` already refused a line naming neither
        candidates = []
    held = [
        item
        for item in candidates
        if item.address.site_id == site_id
        and item.address.location_id == line.source_location_id
        and (line.sku_id is None or item.address.sku_id == line.sku_id)
        and (line.condition is None or item.address.condition == line.condition)
        and (
            line.origin_id is None
            or line.origin_id in (item.address.origin_id, item.address.value_basis_origin_id)
        )
    ]
    return sorted(
        held,
        key=lambda item: (item.source_time, item.lineage_key, str(item.lot_id), item.interval[0]),
    )


def source_slices(line: Line, site_id: int, pinned: Pinned | None = None) -> list[Slice]:
    """The exact portions this line covers, never more than are standing there.

    A draft that already froze its portions (a release waiting for approval) is
    re-checked against those exact pieces rather than re-selected, so approving it
    moves what the approver reviewed and nothing else.
    """
    frozen_first = (pinned or {}).get(line.line_key) is not None
    standing = _standing(line, site_id, narrow=not frozen_first)
    by_lot: dict[uuid.UUID, list[engine.Portion]] = {}
    for item in standing:
        by_lot.setdefault(item.lot_id, []).append(item)
    frozen = (pinned or {}).get(line.line_key)
    if frozen is not None:
        out: list[Slice] = []
        for lot_id, lower, upper in frozen:
            covering = next(
                (
                    item
                    for item in by_lot.get(lot_id, [])
                    if ranges.contains([item.interval], [(lower, upper)])
                ),
                None,
            )
            if covering is None:
                raise _refuse(
                    "These goods have moved since this movement was prepared. "
                    "Reload it and prepare it again.",
                    [
                        issue(
                            "MOVED",
                            "a frozen portion is no longer there",
                            line_key=str(line.line_key),
                        )
                    ],
                )
            out.append(Slice(lot_id, (lower, upper), covering.address))
        return out
    available = ranges.total([item.interval for item in standing])
    if available < line.qty:
        raise _refuse(
            f"Only {available} of {line.qty} piece(s) are where this line says they are.",
            [
                issue(
                    "INSUFFICIENT_ELIGIBLE_STOCK",
                    "there are fewer pieces at that location than the line asks for",
                    line_key=str(line.line_key),
                    quantity=line.qty,
                )
            ],
        )
    out = []
    remaining = line.qty
    for item in standing:
        if remaining <= 0:
            break
        size = min(remaining, ranges.length(item.interval))
        lower = item.interval[0]
        out.append(Slice(item.lot_id, (lower, lower + size), item.address))
        remaining -= size
    return out


def _movable(run: CommandRun, line: Line, slices: Sequence[Slice]) -> None:
    """Held goods do not move by a bin move, and do not leave a hold location by one.

    A move carries encumbrances with the portion, so a hold would still be active
    afterwards - but the goods would have left quarantine under the actor's own
    authority, which is exactly what the release's distinct approval exists to
    decide (change PRD §6.3 invariant 5, design §7 P12).
    """
    for piece in slices:
        held = ActiveHold.objects.filter(
            lot_id=piece.lot_id, portion__overlap=portion(*piece.interval)
        ).first()
        if held is not None:
            raise _refuse(
                "Those goods are on hold. Release the hold first; a move cannot lift one.",
                [issue("HOLD_ACTIVE", held.kind, line_key=str(line.line_key))],
            )
    source = Location.objects.filter(tenant_id=run.tenant_id, pk=line.source_location_id).first()
    if source is None or not source.system or source.kind == MOVABLE_SYSTEM_KIND:
        return
    if source.kind in HOLD_LOCATION_KINDS:
        raise _refuse(
            "Goods leave a hold location through their own release or disposal, "
            "never through a bin move.",
            [issue("LOCATION_NOT_ELIGIBLE", source.kind, line_key=str(line.line_key))],
        )
    raise _refuse(
        f"That is the site's {source.kind} location. Goods leave it through the command "
        "that put them there, never through a bin move.",
        [issue("LOCATION_NOT_ELIGIBLE", source.kind, line_key=str(line.line_key))],
    )


def _release_target(line: Line, slices: Sequence[Slice]) -> None:
    """A release cannot skip acceptance (change PRD §5.6, design §7 P12)."""
    if line.destination_location_id is None:
        raise _refuse("A release names the eligible location the goods go back into.")
    unaccepted = ranges.total([s.interval for s in slices if s.address.accepted_event_id is None])
    if unaccepted:
        raise _refuse(
            f"{unaccepted} piece(s) were never accepted here, so there is no eligible "
            "location to release them into. Accept them first.",
            [
                issue(
                    "NOT_ACCEPTED",
                    "a release cannot create acceptance",
                    line_key=str(line.line_key),
                    quantity=unaccepted,
                )
            ],
        )


def _named_holds(line: Line, slices: Sequence[Slice]) -> None:
    """Every named key is an operational hold, active over the whole released range.

    A release gives good goods back, so it lifts only the operational hold this
    module places. Damage has its own routes and keeps its goods in quarantine
    until one of them runs (overall PRD §15.2.1 rules 3 and 4, change PRD J5).
    """
    if not line.hold_keys:
        raise _refuse("A release names the hold key(s) it lifts.")
    wanted: dict[uuid.UUID, list[ranges.Interval]] = {}
    for piece in slices:
        wanted.setdefault(piece.lot_id, []).append(piece.interval)
    for key in line.hold_keys:
        if any(hold.kind == DAMAGE_KIND for hold in ActiveHold.objects.filter(hold_key=key)):
            raise _refuse(
                "These goods are held as damaged. Damaged goods stay in quarantine "
                "until they are returned, disposed of, or their damage is valued; "
                "a release cannot put them back into stock.",
                [issue("HOLD_NOT_OURS", DAMAGE_KIND, line_key=str(line.line_key))],
            )
        foreign = sorted(
            {
                hold.kind
                for hold in ActiveHold.objects.filter(hold_key=key)
                if hold.kind != RELEASABLE_KIND
            }
        )
        if foreign:
            raise _refuse(
                f"That is a {foreign[0]} hold. It is lifted by the command that placed it, "
                "not by a movement release.",
                [issue("HOLD_NOT_OURS", foreign[0], line_key=str(line.line_key))],
            )
        for lot_id, intervals in wanted.items():
            active = ranges.normalise(
                [
                    bounds(hold.portion)
                    for hold in ActiveHold.objects.filter(lot_id=lot_id, hold_key=key)
                ]
            )
            if not ranges.contains(active, ranges.normalise(intervals)):
                raise _refuse(
                    "That hold is not active over the whole quantity being released.",
                    [issue("HOLD_NOT_ACTIVE", str(key), line_key=str(line.line_key))],
                )
    # A named key is checked above. Any *unnamed* hold still covering the same
    # portion refuses the release outright, whoever placed it: moving the goods
    # into an ordinary location while a hold stands is exactly bypassing that
    # hold through another document type (change PRD §6.3 invariant 5, overall
    # PRD AC-09 "generic move/adjustment cannot bypass hold"). Two holds need two
    # releases, and the goods do not move until the last one is lifted.
    for piece in slices:
        outside = (
            ActiveHold.objects.filter(
                lot_id=piece.lot_id, portion__overlap=portion(*piece.interval)
            )
            .exclude(hold_key__in=list(line.hold_keys))
            .first()
        )
        if outside is not None:
            raise _refuse(
                f"A {outside.kind} hold still covers these goods. Every hold over them "
                "has to be lifted before they can be released.",
                [issue("HOLD_ACTIVE", outside.kind, line_key=str(line.line_key))],
            )


# ---------------------------------------------------------------------------
# Creating the document
# ---------------------------------------------------------------------------


Resolved = list[tuple["Line", list[Slice], dict[str, Any]]]


def _line_payload(
    line: Line,
    slices: Sequence[Slice],
    destination_id: uuid.UUID,
    *,
    damaged: bool,
    hold_keys: Sequence[uuid.UUID] | None = None,
) -> dict[str, Any]:
    """What the official line freezes: exactly which portions moved, and between where.

    ``portions`` names its own lot per piece, so a line that asked for a SKU is
    afterwards as exact as one that named a lot: the record says which pieces of
    which lot moved, and a later read of it never re-selects anything.
    """
    first = slices[0].address
    return {
        "line_key": str(line.line_key),
        "lot_id": str(line.lot_id) if line.lot_id else None,
        "qty": line.qty,
        "sku_id": str(first.sku_id) if first.sku_id else None,
        "origin_id": str(first.origin_id) if first.origin_id else None,
        "condition": "damaged" if damaged else first.condition,
        "source_location_id": str(line.source_location_id),
        "destination_location_id": str(destination_id),
        "hold_keys": [str(k) for k in (line.hold_keys if hold_keys is None else hold_keys)],
        "portions": [
            {"lot_id": str(piece.lot_id), "lower": piece.interval[0], "upper": piece.interval[1]}
            for piece in slices
        ],
    }


def _hold_key(run: CommandRun, line: Line, *, damaged: bool) -> list[uuid.UUID]:
    """The one hold key this line activates.

    One key per line, derived from the command identity, so the official line can
    *name* the hold it placed: that name is what a later release quotes back, and
    what the owned exception is keyed on. A replay of the same command derives the
    same key and changes nothing.

    A hold is always placed, even over pieces an earlier hold already covers.
    Holds are per-key ranges, so two people holding the same goods for different
    reasons each need their own release - returning "nothing to do" here would
    let the first release free goods the second person separately held.
    """
    kind = DAMAGE_KIND if damaged else HOLD_KIND
    return [uuid.uuid5(run.key_id, f"{kind}:{line.line_key}")]


def _resolve(
    run: CommandRun, payload: Payload, *, damaged: bool = False, pinned: Pinned | None = None
) -> Resolved:
    """Every line's exact source portions and destination, validated for its kind."""
    out: Resolved = []
    seen: dict[uuid.UUID, list[ranges.Interval]] = {}
    for line in payload.lines:
        slices = source_slices(line, payload.site_id, pinned)
        for piece in slices:
            taken = seen.setdefault(piece.lot_id, [])
            if ranges.intersect([piece.interval], taken):
                raise _refuse("Two lines of this movement claim the same pieces.")
            taken.append(piece.interval)
        if payload.kind == "bin_move":
            if line.hold_keys:
                raise _refuse("A bin move does not lift a hold; use a release.")
            if line.destination_location_id is None:
                raise _refuse("A bin move names the location the goods go to.")
            _movable(run, line, slices)
            destination_id = storage_location(
                run.tenant_id, line.destination_location_id, payload.site_id
            ).pk
        elif payload.kind == "hold":
            quarantine = quarantine_of(payload.site_id)
            if (
                line.destination_location_id is not None
                and line.destination_location_id != quarantine.pk
            ):
                # The destination of a hold is not the client's to choose (E209 step 12).
                raise _refuse("Goods put on hold go to quarantine, not to a chosen location.")
            destination_id = quarantine.pk
            placed = _hold_key(run, line, damaged=damaged)
            out.append(
                (
                    line,
                    slices,
                    _line_payload(line, slices, destination_id, damaged=damaged, hold_keys=placed),
                )
            )
            continue
        else:
            _release_target(line, slices)
            _named_holds(line, slices)
            assert line.destination_location_id is not None
            destination_id = storage_location(
                run.tenant_id, line.destination_location_id, payload.site_id
            ).pk
        out.append((line, slices, _line_payload(line, slices, destination_id, damaged=damaged)))
    return out


def create(run: CommandRun, payload: Payload) -> tuple[DocumentIdentity, str]:
    """E151: draft the movement, and post it now when the actor's own authority carries it.

    Bin moves and holds officialise inside this command (design E151 step 14).
    A release is drafted only; it goes on to E155 and a distinct approval.
    """
    identity, head = _open_document(run, payload, purpose=payload.kind)
    resolved = _resolve(run, payload)
    append_revision(
        run,
        head,
        header=payload.as_header(),
        replace_lines=[(line.line_key, body) for line, _slices, body in resolved],
    )
    _record_movement(run, identity, payload.kind, payload.source_document_id)
    run.audit_subject_key = f"movement:{identity.pk}"
    run.audit_site_id = payload.site_id
    if payload.kind == "release":
        run.audit_after = {"movement_id": str(identity.pk), "kind": payload.kind, "state": "draft"}
        return identity, "draft"
    version = _officialise(run, head, payload, resolved)
    _apply(run, payload, resolved, version)
    run.audit_after = {
        "movement_id": str(identity.pk),
        "kind": payload.kind,
        "number": identity.official_number,
    }
    return identity, "official"


def mark_damaged(run: CommandRun, payload: Payload) -> DocumentIdentity:
    """E209: a hold that also records the condition. Quarantine is not negotiable.

    The same command opens the report a second person has to decide (OPS-05,
    ticket 12A). It is one transaction on purpose: the goods are quarantined and
    the review is open together, so there is never a moment where damage was
    reported and nobody owns deciding it.

    A count freeze does not stop it (GSA-R02): damage found while the site is
    being counted is still quarantined at once. Reserved pieces are not refused
    either - the reservation stands and its owner gets owned work (E209 step 13).
    """
    from outbound import damage_review

    identity, head = _open_document(run, payload, purpose="mark_damaged", during_count=True)
    resolved = _resolve(run, payload, damaged=True)
    append_revision(
        run,
        head,
        header=payload.as_header(),
        replace_lines=[(line.line_key, body) for line, _slices, body in resolved],
    )
    _record_movement(run, identity, GoodsMovement.Kind.HOLD, payload.source_document_id)
    version = _officialise(run, head, payload, resolved)
    _apply(run, payload, resolved, version, damaged=True)
    report = damage_review.open_for_movement(
        run,
        document=identity,
        site_id=payload.site_id,
        reason_code=payload.reason_code,
        evidence_ids=payload.evidence_ids,
        resolved=resolved,
    )
    run.audit_subject_key = f"movement:{identity.pk}"
    run.audit_site_id = payload.site_id
    run.audit_after = {
        "movement_id": str(identity.pk),
        "kind": "mark_damaged",
        "damage_report_id": str(report.pk),
    }
    return identity


def _open_document(
    run: CommandRun, payload: Payload, *, purpose: str, during_count: bool = False
) -> tuple[DocumentIdentity, DocumentHead]:
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A movement is recorded by a named person.")
    require_movement_site(run, payload.site_id, during_count=during_count)
    site = site_of(payload.site_id)
    identity, head = new_document(
        run,
        kind=DOC_TYPE[payload.kind],
        purpose=purpose,
        entity_id=site.gstin.legal_entity_id,
        site_id=payload.site_id,
    )
    engine.lock_lots(run, _candidate_lots(payload))
    return identity, head


def _candidate_lots(payload: Payload) -> list[uuid.UUID]:
    """Every lot a line could touch, so the LOT rank covers the whole selection.

    A line that names its lot contributes that one. A line that names a SKU is
    resolved after the lock, so every lot of that SKU standing at its source
    location is locked first; the selection then re-reads them under the lock.
    """
    wanted: set[uuid.UUID] = set()
    for line in payload.lines:
        if line.lot_id is not None:
            wanted.add(line.lot_id)
            continue
        wanted.update(
            Position.objects.filter(
                site_id=payload.site_id,
                boundary="physical",
                location_id=line.source_location_id,
                sku_id=line.sku_id,
            ).values_list("lot_id", flat=True)
        )
    return sorted(wanted, key=str)


def _record_movement(
    run: CommandRun, identity: DocumentIdentity, kind: str, source_document_id: uuid.UUID | None
) -> GoodsMovement:
    movement = GoodsMovement.objects.create(
        tenant_id=run.tenant_id,
        document_id=identity.pk,
        kind=kind,
        source_document_id=source_document_id,
    )
    movement.document = identity
    return movement


def site_of(site_id: int) -> Any:
    from masters.models import Store

    site = Store.objects.select_related("gstin").filter(pk=site_id).first()
    if site is None:
        raise Refusal("NOT_FOUND", "That site was not found.")
    return site


def _officialise(
    run: CommandRun, head: DocumentHead, payload: Payload, resolved: Resolved
) -> OfficialVersion:
    identity = head.document
    number = identity.official_number or allocate(run, identity.entity, DOC_TYPE[payload.kind])
    assert run.principal.human_id is not None
    version, _lines = officialise(
        run,
        head,
        approved_by_id=run.principal.human_id,
        canonical_header=payload.as_header(),
        lines=[(line.line_key, body) for line, _slices, body in resolved],
        authority=run.authority,
        number=number,
    )
    return version


# ---------------------------------------------------------------------------
# The postings: P11 and P12
# ---------------------------------------------------------------------------


def _apply(
    run: CommandRun,
    payload: Payload,
    resolved: Resolved,
    version: OfficialVersion,
    *,
    damaged: bool = False,
) -> None:
    plan = engine.Plan(
        POSTING[payload.kind], version.pk, engine.event_key(payload.kind, version.pk)
    )
    blocked: set[uuid.UUID] = set()
    released_over: set[uuid.UUID] = set()
    placed: list[uuid.UUID] = []
    for line, slices, body in resolved:
        destination_id = uuid.UUID(str(body["destination_location_id"]))
        keys = [uuid.UUID(str(k)) for k in body["hold_keys"]]
        if payload.kind == "hold":
            placed.extend(keys)
        for piece in slices:
            if payload.kind == "hold":
                blocked |= _reserved_versions(piece)
            _move(run, plan, piece, destination_id, damaged=damaged)
            if payload.kind == "hold":
                for key in keys:
                    _place(run, plan, payload, piece, key, damaged=damaged)
            elif payload.kind == "release":
                released_over |= _reserved_versions(piece)
                for key in line.hold_keys:
                    engine.release_hold(
                        run,
                        plan,
                        lot_id=piece.lot_id,
                        interval=piece.interval,
                        hold_key=key,
                        site_id=payload.site_id,
                        source_version_id=version.pk,
                    )
    engine.post(run, version, plan)
    _register_exceptions(
        run,
        payload,
        version,
        blocked=blocked,
        placed=placed,
        released_over=released_over,
        damaged=damaged,
    )


def _move(
    run: CommandRun,
    plan: engine.Plan,
    piece: Slice,
    destination_id: uuid.UUID,
    *,
    damaged: bool,
) -> None:
    """Change only the address. Acceptance travels with the goods; it is never created here."""

    def change(old: engine.Address) -> engine.Address:
        if damaged:
            return replace(old, location_id=destination_id, condition="damaged")
        return replace(old, location_id=destination_id)

    engine.change_address(run, plan, piece.lot_id, piece.interval, change)


def _place(
    run: CommandRun,
    plan: engine.Plan,
    payload: Payload,
    piece: Slice,
    key: uuid.UUID,
    *,
    damaged: bool,
) -> None:
    """Activate the line's hold key over the part of this portion it does not hold yet."""
    kind = DAMAGE_KIND if damaged else HOLD_KIND
    active = [
        bounds(hold.portion)
        for hold in ActiveHold.objects.filter(lot_id=piece.lot_id, hold_key=key)
    ]
    for interval in ranges.subtract([piece.interval], active):
        engine.place_hold(
            run,
            plan,
            lot_id=piece.lot_id,
            interval=interval,
            hold_key=key,
            kind=kind,
            site_id=payload.site_id,
            source_version_id=None,
        )


def _reserved_versions(piece: Slice) -> set[uuid.UUID]:
    """Transfer versions whose live reservation overlaps the portion being held.

    A hold never waits for them (design E209 step 13) - it takes effect at once
    and their owners are told, because a reservation nobody can dispatch is work,
    not a reason to leave damaged goods sellable.
    """
    return reserved_versions_over([(piece.lot_id, piece.interval)])


def reserved_versions_over(pieces: Iterable[tuple[uuid.UUID, ranges.Interval]]) -> set[uuid.UUID]:
    """Transfer versions whose live reservation overlaps any of these exact portions.

    A return to vendor's reservation (goods ticket 15B) is left out: a hold does
    not stop the vendor collecting held goods - returning them is their exit
    route - so there is no "reserved stock is held" work for anybody to do.
    """
    out: set[uuid.UUID] = set()
    for lot_id, interval in pieces:
        out |= {
            reservation.transfer_version_id
            for reservation in ActiveReservation.objects.filter(
                lot_id=lot_id, portion__overlap=portion(*interval)
            ).exclude(transfer_version__document__kind=RTV_DOC_TYPE)
        }
    return out


def settle_reservation_blocks(run: CommandRun, transfer_version_ids: Iterable[uuid.UUID]) -> None:
    """Close the "reserved stock is held" work once no hold stands over the reservation.

    A hold over reserved goods opens that work (design E209 step 13); it is
    finished when either side is gone - the hold lifted (a release, a rejected
    damage report) or the reservation under it cancelled. While any active hold
    still overlaps any live reservation of the transfer version, it stays open.
    Confirming damage lifts nothing, so it never closes it.
    """
    for version_id in sorted(set(transfer_version_ids), key=str):
        if any(
            ActiveHold.objects.filter(
                lot_id=reservation.lot_id, portion__overlap=reservation.portion
            ).exists()
            for reservation in ActiveReservation.objects.filter(transfer_version_id=version_id)
        ):
            continue
        resolve_exceptions(
            run,
            kind=RESERVATION_BLOCKED_EXCEPTION,
            subject_key=f"transfer_version:{version_id}",
            reason_code="RESERVED_STOCK_FREE",
        )


def _register_exceptions(
    run: CommandRun,
    payload: Payload,
    version: OfficialVersion,
    *,
    blocked: set[uuid.UUID],
    placed: Sequence[uuid.UUID],
    released_over: set[uuid.UUID] | None = None,
    damaged: bool = False,
) -> None:
    """Hold and release exceptions, opened and resolved through ticket 08's centre."""
    document_id = version.document_id
    # A damage hold is not lifted from the movements screen - a release refuses
    # it - so offering that route would send its owner to a command that says no.
    # The work is real and stays owned; the button arrives with tickets 15a/15b.
    resolution = [] if damaged else RESOLUTION_ACTIONS
    for key in placed:
        open_exception(
            run,
            kind=HOLD_EXCEPTION,
            site_id=payload.site_id,
            subject_key=f"hold:{key}",
            reason_code=payload.reason_code[:60],
            source_event_key=engine.event_key("hold", document_id, key),
            allowed_resolution_actions=resolution,
            note=f"movement:{document_id}",
        )
    for transfer_version_id in sorted(blocked, key=str):
        open_exception(
            run,
            kind=RESERVATION_BLOCKED_EXCEPTION,
            site_id=payload.site_id,
            subject_key=f"transfer_version:{transfer_version_id}",
            reason_code="RESERVED_STOCK_HELD",
            source_event_key=engine.event_key("hold_blocks", document_id, transfer_version_id),
            note=f"movement:{document_id}",
        )
    if payload.kind == "release":
        settle_reservation_blocks(run, released_over or set())
        for line in payload.lines:
            for key in line.hold_keys:
                # A release may lift part of a hold. The pieces still standing
                # under the key are still work, so the exception closes only
                # once nothing is held under it any more.
                if ActiveHold.objects.filter(hold_key=key).exists():
                    continue
                resolve_exceptions(
                    run, kind=HOLD_EXCEPTION, subject_key=f"hold:{key}", reason_code="RELEASED"
                )


# ---------------------------------------------------------------------------
# E155: submit a release for its distinct approval
# ---------------------------------------------------------------------------


def movement_of(document_id: uuid.UUID) -> GoodsMovement:
    movement = (
        GoodsMovement.objects.select_related("document").filter(document_id=document_id).first()
    )
    if movement is None:
        raise Refusal("NOT_FOUND", "That movement was not found.")
    return movement


def draft_hash(head: DocumentHead) -> str:
    revision = head.draft_revision
    return revision.content_hash if revision is not None else ""


def reviewable_hash(head: DocumentHead) -> str:
    """What a client reviews and quotes back: the live version once official, else the draft."""
    if head.live_version is not None:
        return str(head.live_version.content_hash)
    return draft_hash(head)


def submit(
    run: CommandRun, document_id: uuid.UUID, *, reviewed_hash: str, expected_revision: int | None
) -> tuple[GoodsMovement, uuid.UUID]:
    """E155: create the distinct C-INV/C-OWN approval a release needs.

    There is no tolerance-based automatic approval here, and a submission that
    would bypass acceptance is refused before an approver ever sees it.
    """
    movement = movement_of(document_id)
    if movement.kind in ADJUSTMENT_KINDS:
        from outbound import goods_adjustments

        return goods_adjustments.submit(
            run, movement, reviewed_hash=reviewed_hash, expected_revision=expected_revision
        )
    if movement.kind == RTV_KIND:
        from outbound import goods_rtv

        return goods_rtv.submit(
            run, movement, reviewed_hash=reviewed_hash, expected_revision=expected_revision
        )
    if movement.kind == WRITEOFF_KIND:
        from outbound import goods_writeoff

        return goods_writeoff.submit(
            run, movement, reviewed_hash=reviewed_hash, expected_revision=expected_revision
        )
    if movement.kind == DISPOSAL_KIND:
        from outbound import goods_disposal

        return goods_disposal.submit(
            run, movement, reviewed_hash=reviewed_hash, expected_revision=expected_revision
        )
    site_id = movement.document.held_site_id
    require_movement_site(run, site_id)
    head = lock_heads(run, [document_id])[document_id]
    if expected_revision is not None and expected_revision != head.revision:
        raise Refusal(
            "REVISION_SUPERSEDED", "This movement changed after you loaded it. Reload it."
        )
    if reviewed_hash != draft_hash(head):
        raise Refusal(
            "REVISION_SUPERSEDED", "What you reviewed is no longer this movement's content."
        )
    if movement.kind != GoodsMovement.Kind.RELEASE:
        raise _refuse(
            "Only a release or an adjustment waits for approval; the others are already official."
        )
    if head.state != DocumentHead.State.DRAFT:
        raise _refuse("This movement is not a draft.")
    # Rank DOCUMENT closes once the lots are locked, so any earlier request for
    # this movement is superseded here rather than inside `create_request`.
    supersede_pending(run, "document", str(document_id), APPROVE_ACTION)
    payload, pinned = _draft_payload(head)
    engine.lock_lots(run, _pinned_lots(pinned))
    _resolve(run, payload, pinned=pinned)
    quantity = sum(line.qty for line in payload.lines)
    policy = pin(
        run,
        action=APPROVE_ACTION,
        purpose=payload.kind,
        site_id=site_id,
        brand_ids=[None],
        amounts=Amounts(quantity, None),
    )
    reviewed = draft_hash(head)
    request = create_request(
        run,
        subject_kind="document",
        subject_key=str(document_id),
        revision=head.revision,
        reviewed_hash=reviewed,
        requested_action=APPROVE_ACTION,
        site_id=site_id,
        title=f"Release {quantity} piece(s)",
        policy=policy,
        new_subject=True,
    )
    set_state(run, head, DocumentHead.State.SUBMITTED, event="submitted")
    open_exception(
        run,
        kind=RELEASE_APPROVAL_EXCEPTION,
        site_id=site_id,
        subject_key=f"movement:{document_id}",
        reason_code="RELEASE_APPROVAL",
        source_event_key=engine.event_key("release_approval", document_id),
        allowed_resolution_actions=RESOLUTION_ACTIONS,
        note=f"approval_request:{request.pk}",
    )
    run.audit_subject_key = f"movement:{document_id}"
    run.audit_site_id = site_id
    run.audit_after = {"approval_request_id": str(request.pk), "state": "submitted"}
    return movement, request.pk


def _draft_payload(head: DocumentHead) -> tuple[Payload, Pinned]:
    """The typed payload of the locked draft, and the exact portions it froze."""
    revision = head.draft_revision
    assert revision is not None
    header = dict(revision.payload)
    lines = [dict(state.payload) for state in revision_lines(head.document_id, revision.revision)]
    payload = parse({**header, "lines": [_replay_line(line) for line in lines]})
    pinned: Pinned = {
        uuid.UUID(str(line["line_key"])): [
            (uuid.UUID(str(piece["lot_id"])), int(piece["lower"]), int(piece["upper"]))
            for piece in line.get("portions") or []
        ]
        for line in lines
    }
    return payload, pinned


def _pinned_lots(pinned: Pinned) -> list[uuid.UUID]:
    return [lot_id for pieces in pinned.values() for lot_id, _lower, _upper in pieces]


def _replay_line(stored: dict[str, Any]) -> dict[str, Any]:
    return {
        "line_key": stored["line_key"],
        "lot_id": stored.get("lot_id"),
        "qty": stored["qty"],
        "sku_id": stored.get("sku_id"),
        "origin_id": stored.get("origin_id"),
        "condition": stored.get("condition"),
        "source_location_id": stored.get("source_location_id"),
        "destination_location_id": stored.get("destination_location_id"),
        "hold_keys": list(stored.get("hold_keys") or []),
    }


# ---------------------------------------------------------------------------
# The release decision (E234 → this handler)
# ---------------------------------------------------------------------------


def _refuse_self_decision(context: DecisionContext, head: DocumentHead) -> None:
    """E234 step 10: the checker neither drafted this release nor sent it for approval.

    ``decide`` already compares the checker with whoever pressed send. The person
    who drafted the release is a maker too, or A drafts, B submits and A approves.
    """
    if str(context.checker_id) == str(head.document.maker_id):
        raise Refusal("SELF_APPROVAL", "Someone who drafted this release cannot also decide it.")


def decide_release(run: CommandRun, context: DecisionContext) -> dict[str, Any] | None:
    request = context.request
    document_id = _subject_uuid(request.subject_key)
    movement = movement_of(document_id)
    if movement.kind in ADJUSTMENT_KINDS:
        from outbound import goods_adjustments

        return goods_adjustments.decide(run, context, movement)
    if movement.kind == RTV_KIND:
        from outbound import goods_rtv

        return goods_rtv.decide(run, context, movement)
    if movement.kind == WRITEOFF_KIND:
        from outbound import goods_writeoff

        return goods_writeoff.decide(run, context, movement)
    if movement.kind == DISPOSAL_KIND:
        from outbound import goods_disposal

        return goods_disposal.decide(run, context, movement)
    site_id = movement.document.site_id
    context.access.require(APPROVE_ACTION, site_id=site_id)
    check_movement_site(SiteGuard.objects.filter(site_id=site_id).first())
    head = lock_heads(run, [document_id])[document_id]
    _refuse_self_decision(context, head)
    context.enforce_policy(run)
    if head.state != DocumentHead.State.SUBMITTED:
        raise Refusal("STATE_CONFLICT", "This release is no longer waiting for approval.")
    if request.reviewed_hash != draft_hash(head):
        raise Refusal("REVISION_SUPERSEDED", "The release changed after it was submitted.")
    if context.decision == "reject":
        set_state(
            run,
            head,
            DocumentHead.State.DRAFT,
            event="rejected",
            reason_code=context.reason_code,
        )
        resolve_exceptions(
            run,
            kind=RELEASE_APPROVAL_EXCEPTION,
            subject_key=f"movement:{document_id}",
            reason_code="RELEASE_REJECTED",
            source_event_key=engine.event_key("release_approval", document_id),
        )
        return {"state": "rejected"}
    payload, pinned = _draft_payload(head)
    engine.lock_lots(run, _pinned_lots(pinned))
    resolved = _resolve(run, payload, pinned=pinned)
    version = _officialise(run, head, payload, resolved)
    _apply(run, payload, resolved, version)
    resolve_exceptions(
        run,
        kind=RELEASE_APPROVAL_EXCEPTION,
        subject_key=f"movement:{document_id}",
        reason_code="RELEASE_APPROVED",
        source_event_key=engine.event_key("release_approval", document_id),
    )
    record_event(run, document_id, "released", version_id=version.pk)
    movement.document.refresh_from_db()
    return {"movement_id": str(document_id), "number": movement.document.official_number}


def _subject_uuid(subject_key: str) -> uuid.UUID:
    try:
        return uuid.UUID(subject_key)
    except (AttributeError, TypeError, ValueError):
        raise Refusal("NOT_FOUND", "That movement was not found.") from None


def register_approval_handlers() -> None:
    register_subject_handler("document", APPROVE_ACTION, decide_release)


# ---------------------------------------------------------------------------
# Reads (E104/E105)
# ---------------------------------------------------------------------------


def visible_movements(tenant_id: uuid.UUID, site_ids: Iterable[int] | None) -> Any:
    queryset = GoodsMovement.objects.select_related("document").filter(tenant_id=tenant_id)
    if site_ids is not None:
        queryset = queryset.filter(document__site_id__in=sorted(site_ids))
    return queryset.order_by("-document__created_at", "-document_id")


def summary(movement: GoodsMovement, head: DocumentHead | None) -> dict[str, Any]:
    document = movement.document
    return {
        "id": str(document.pk),
        "record_contract": "goods-v1",
        "kind": document.kind,
        "purpose": document.purpose,
        "number": document.official_number,
        "state": head.state if head is not None else DocumentHead.State.DRAFT,
        "site_id": str(document.site_id),
        "brand_id": None,
        "created_at": document.created_at.isoformat(),
        "updated_at": (head.updated_at if head is not None else document.created_at).isoformat(),
        "owner_role": None,
        "due_at": None,
        # Goods ticket 15B: where a return to vendor stands; null for every other kind.
        "rtv_state": movement.rtv_state,
    }


def detail(head: DocumentHead, *, offset: int, limit: int) -> dict[str, Any]:
    """``MovementDetailDTO``: the header without lines, its paged lines, and its authority.

    ``authority`` is this ticket's one addition to the DTO. The movements screen
    has to show "source portions, reason, actor and approval" side by side, and
    nothing else in ``ResourceDTO`` names *who* recorded a movement or who
    approved it - the ids live on the document identity and the official version.
    """
    header: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    if head.live_version is not None:
        header = dict(head.live_version.canonical_payload)
        rows = [dict(line.payload) for line in head.live_version.lines.order_by("line_no")]
    elif head.draft_revision is not None:
        header = dict(head.draft_revision.payload)
        rows = [
            dict(state.payload)
            for state in revision_lines(head.document_id, head.draft_revision.revision)
        ]
    return {
        "header": header,
        "lines": {
            "items": rows[offset : offset + limit],
            "next_cursor": None if offset + limit >= len(rows) else str(offset + limit),
            "total": len(rows),
        },
        "authority": _authority(head),
    }


def _authority(head: DocumentHead) -> dict[str, Any]:
    """Who recorded this movement, and - for a release - who separately approved it."""
    from accounts.goods_models import HumanIdentity
    from approvals.goods_models import ApprovalRequest

    version = head.live_version
    wanted = {head.document.maker_id}
    if version is not None:
        wanted.add(version.approved_by_id)
    names = dict(
        HumanIdentity.objects.filter(pk__in=[w for w in wanted if w]).values_list(
            "pk", "display_name"
        )
    )
    request = (
        ApprovalRequest.objects.filter(
            subject_kind="document",
            subject_key=str(head.document_id),
            requested_action=APPROVE_ACTION,
        )
        .order_by("-created_at")
        .first()
    )
    needs_approval = head.document.purpose in (
        "release",
        RTV_KIND,
        WRITEOFF_KIND,
        DISPOSAL_KIND,
        *ADJUSTMENT_KINDS,
    )
    approver = version.approved_by_id if version is not None else None
    return {
        # A bin move and a hold carry the actor's own authority (design E151 step
        # 14); only a release has a second person, so the two are never conflated.
        "kind": "approval" if needs_approval else "actor",
        "actor": {
            "id": str(head.document.maker_id),
            "name": names.get(head.document.maker_id, ""),
        },
        "approved_by": (
            {"id": str(approver), "name": names.get(approver, "")}
            if needs_approval and approver is not None
            else None
        ),
        "approved_at": (
            request.decided_at.isoformat()
            if request is not None and request.decided_at is not None
            else None
        ),
        "approval_request_id": str(request.pk) if request is not None else None,
        "approval_state": request.state if request is not None else None,
    }
