"""Internal transfers between a store and a warehouse, end to end (OPS-06).

The shape the store-and-warehouse operations PRD §7 selects out of the
transfers PRD, and nothing wider:

    request or draft → approval and reservation → dispatch → destination count
    → acceptance

Read it as five separate facts, because that is what makes the record truthful:

* **A request is not a movement.** The receiving site asks; the sending site
  drafts. Nothing is reserved by asking, and a request that is never drafted
  simply stays open.
* **Approval reserves, it does not move.** A distinct second person approves
  the transfer PT, and that approval reserves the exact source portions the
  draft froze. Reserved pieces are neither sellable nor available to another
  transfer, and they stay reserved with no expiry until they are dispatched or
  the balance is explicitly cancelled.
* **A dispatch is one shipment, not the movement.** One approved transfer may
  take several. Each consumes only the reservation that actually left, moves
  exactly those portions into transit under this transfer, and keeps its own
  departure evidence. A smaller dispatch leaves the rest reserved.
* **A count accounts for a whole shipment; it does not assert that everything
  arrived.** Good, damaged, wrong and unidentified pieces are recorded as they
  were found. Damage opens a report for a second person exactly as reporting
  damage on the stock screen does. Missing pieces are an explicit shortage that
  stays in transit - absent goods never enter quarantine and are never quietly
  written off. There is no staged partial receipt inside one shipment.
* **Acceptance is a separate act from receipt.** Counting puts the good pieces
  in the destination's receiving location, held and unaccepted. Only acceptance
  puts them away, and only then are they sellable - at the destination, on the
  destination's own acceptance evidence, never on the source's.

A failed delivery comes back through ``outbound.transfer_returns`` (goods
ticket 13C): the source records what actually came back, the dispatch is kept
with its departure intact, anything still missing stays in transit, and no
destination receipt is ever written.

A quarantine transfer (goods ticket 13D, overall PRD §15.2.1 rule 10) is the
same lifecycle over a different, closed source pool: recorded pieces held in
the source's quarantine, which travel with every hold and condition, arrive
into the destination's quarantine and are never made available. ``custody``
on the transfer says which pool it takes from; see ``_freeze`` and ``count``.

A pre-PT custody transfer (goods ticket 13E, the same rule 10) runs the same
lifecycle over a third closed pool: damaged goods a GRN counted and no PT
covers yet, identified from their GRN (``outbound.pre_pt_custody``). They move
exactly as quarantined goods do, but no PT is ever made for them: the approved
movement is the transfer's own document, officialised without a transfer PT,
and every line says its value is unknown.

Excess and wrong goods found by the count, and the corrective transfers that
resolve them, live in ``outbound.transfer_excess`` (goods ticket 16): a
corrective transfer is drafted, approved and reserved here like any other, but
never shipped - it is confirmed once against the goods already observed.

Out of scope here, and refused plainly where a caller could try: RTV and
staged partial receipt within one dispatch.

Lock order, as everywhere: SITE (both site guards) → DOCUMENT (the transfer,
its PT head, the dispatch) → LOT → SERIES.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from alerts.goods_services import open_exception, resolve_exceptions
from core.commands import CommandRun, LockRank
from core.goods_documents import (
    append_revision,
    lock_heads,
    new_document,
    officialise,
    set_state,
)
from core.goods_fields import bounds, portion
from core.kernel_models import DocumentHead, DocumentIdentity, OfficialVersion
from core.numbering import allocate
from core.refusals import Refusal
from masters.goods_models import Location, SiteGuard
from outbound.goods_models import GoodsTransfer, TransferDispatch, TransferEvent, TransferRequest
from outbound.goods_movements import settle_reservation_blocks

if TYPE_CHECKING:
    from outbound.dispatch_preparation import Selected
from ptmapper.goods_models import GoodsPt
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import (
    AcceptanceEvent,
    AcceptanceSession,
    ActiveHold,
    ActiveReservation,
    Position,
)

# ---------------------------------------------------------------------------
# What the routes are allowed to ask for
# ---------------------------------------------------------------------------

#: Draft or request a transfer at a site (``accounts.actions``).
ALLOCATE_ACTION = "transfer.allocate"
#: Approve the transfer PT. One of ``DISTINCT_IDENTITY_ACTIONS``: the approver
#: is never the person who drafted or submitted it.
APPROVE_ACTION = "pt.approve.transfer"
#: Dispatch at the source, count at the destination, send a failed delivery back.
MOVE_ACTION = "transfer.move"
#: Put the good arrived pieces away at the destination.
ACCEPT_ACTION = "stock.accept"
#: Who may read a transfer: anybody who could draft, approve, move or accept one,
#: plus an ordinary stock reader at one of its two sites.
READ_ACTIONS = (ALLOCATE_ACTION, APPROVE_ACTION, MOVE_ACTION, ACCEPT_ACTION, "stock.view")

#: The transfer's own document, and the transfer PT that carries its plan. Only
#: the PT is numbered: the PT *is* the approved movement (transfers PRD §2).
TRANSFER_DOC_KIND = "TRF"
PT_DOC_TYPE = "TPT"

#: Design §7: approval reserves (P07), dispatch consumes into transit (P08),
#: the destination check brings transit ashore (P09), and damage, wrong and
#: excess goods at arrival are held (P10).
RESERVE_POSTING = "P07"
DISPATCH_POSTING = "P08"
CHECK_POSTING = "P09"
ARRIVAL_HOLD_POSTING = "P10"

#: Hold kinds an arrival places. Damage shares the one kind every damage route
#: uses, so the stock screen reads one thing rather than three.
DAMAGE_HOLD = "damage"
HOLD_KIND = {
    "damaged": DAMAGE_HOLD,
    "unidentified": "transfer_unidentified",
}

#: The conditions a whole-shipment count may report, beside what is simply missing.
COUNT_CONDITIONS = ("good", "damaged", "wrong", "unidentified")
#: Of those, the ones that say an expected piece physically arrived. ``wrong``
#: says it did not: something else came in its place, so the expected piece
#: stays short in transit and the other goods are an excess observation of
#: their own (goods ticket 16; goods PRD §5.7).
ARRIVED_CONDITIONS = ("good", "damaged", "unidentified")
#: Conditions whose arrived pieces go to the destination's quarantine and stay held.
ARRIVAL_HELD_CONDITIONS = ("damaged", "unidentified")

#: A shortage is owned work, not a silent adjustment (transfers PRD §5).
SHORTAGE_EXCEPTION = "transfer_discrepancy"
RESOLUTION_ACTIONS = ["outbound/transfers"]
#: A transfer waiting for its approval is owned work in the shared centre, at
#: the sending site, until a different authorised person approves it (goods
#: ticket 13A). It is the same kind every other waiting approval uses.
APPROVAL_EXCEPTION = "approval_pending"
APPROVAL_REASON = "TRANSFER_APPROVAL"

MAX_LINES = 200
MAX_QTY = 999_999


def _invalid(message: str, field: str | None = None) -> Refusal:
    from core.refusals import issue

    return Refusal(
        "INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field)] if field else []
    )


def _refuse(message: str) -> Refusal:
    return Refusal("TRANSFER_INVALID", message, status=422)


def _state_conflict(message: str) -> Refusal:
    return Refusal("STATE_CONFLICT", message)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise _invalid(f"{field} must be an ID.", field) from None


def _optional_uuid(value: Any, field: str) -> uuid.UUID | None:
    return None if value in (None, "") else _uuid(value, field)


def _site_id(value: Any, field: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        raise _invalid(f"{field} must be an ID.", field) from None
    if parsed < 1:
        raise _invalid(f"{field} must be an ID.", field)
    return parsed


def _qty(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_QTY:
        raise _invalid(f"{field} is {minimum} to {MAX_QTY} pieces.", field)
    return int(value)


def _text(value: Any, field: str, *, limit: int, required: bool = True) -> str | None:
    if value in (None, ""):
        if required:
            raise _invalid(f"{field} is required.", field)
        return None
    text = str(value).strip()
    if not 1 <= len(text) <= limit:
        raise _invalid(f"{field} is 1 to {limit} characters.", field)
    return text


def _closed(body: dict[str, Any], fields: frozenset[str]) -> None:
    unknown = sorted(set(body) - fields)
    if unknown:
        raise _invalid(f"Unknown field(s): {', '.join(unknown)}.", unknown[0])


@dataclass(frozen=True)
class DraftLine:
    line_key: uuid.UUID
    #: ``None`` only on a pre-PT line (goods ticket 13E), which names its GRN
    #: line instead: the count may never have given the goods a SKU.
    sku_id: uuid.UUID | None
    qty: int
    #: Origins the drafter chose by hand. Empty means "the oldest eligible ones",
    #: which is the FIFO rule the engine already applies everywhere else.
    origin_ids: tuple[uuid.UUID, ...]
    note: str | None
    #: A pre-PT line's source: the GRN (its document id) and the GRN line.
    grn_id: uuid.UUID | None = None
    grn_line_key: uuid.UUID | None = None

    def as_payload(self) -> dict[str, Any]:
        if self.grn_id is not None:
            return {
                "line_key": str(self.line_key),
                "grn_id": str(self.grn_id),
                "grn_line_key": str(self.grn_line_key),
                "qty": self.qty,
                "note": self.note,
            }
        return {
            "line_key": str(self.line_key),
            "sku_id": str(self.sku_id),
            "qty": self.qty,
            "origin_ids": [str(o) for o in self.origin_ids],
            "note": self.note,
        }


DRAFT_FIELDS = frozenset(
    {"source_site_id", "destination_site_id", "request_id", "note", "lines", "custody"}
)
#: The source pools a draft may name (``GoodsTransfer.Custody``).
ORDINARY = GoodsTransfer.Custody.ORDINARY
QUARANTINE = GoodsTransfer.Custody.QUARANTINE
PRE_PT = GoodsTransfer.Custody.PRE_PT
#: The controlled custody pools (overall PRD §15.2.1 rule 10): held goods that
#: travel held, arrive into quarantine and are never made available.
HELD_CUSTODY = frozenset({QUARANTINE, PRE_PT})


def held_custody(transfer: GoodsTransfer) -> bool:
    """Does this movement carry held goods that stay held (goods tickets 13D, 13E)?"""
    return transfer.custody in HELD_CUSTODY


DRAFT_LINE_FIELDS = frozenset({"line_key", "sku_id", "qty", "origin_ids", "note"})
REQUEST_FIELDS = frozenset({"source_site_id", "destination_site_id", "note", "lines"})
REQUEST_LINE_FIELDS = frozenset({"line_key", "sku_id", "qty", "note"})


@dataclass(frozen=True)
class DraftPayload:
    source_site_id: int
    destination_site_id: int
    request_id: uuid.UUID | None
    note: str | None
    lines: tuple[DraftLine, ...]
    custody: str = ORDINARY

    def as_header(self) -> dict[str, Any]:
        header: dict[str, Any] = {
            "source_site_id": str(self.source_site_id),
            "destination_site_id": str(self.destination_site_id),
            "request_id": str(self.request_id) if self.request_id else None,
            "note": self.note,
        }
        # Only a held-custody draft says so: an ordinary draft's header is
        # exactly what it was before goods ticket 13D.
        if self.custody in HELD_CUSTODY:
            header["custody"] = str(self.custody)
        return header


def parse_draft(body: dict[str, Any]) -> DraftPayload:
    _closed(body, DRAFT_FIELDS)
    custody = body.get("custody") or ORDINARY
    if custody not in GoodsTransfer.Custody.values:
        raise _invalid(f"custody is one of {', '.join(GoodsTransfer.Custody.values)}.", "custody")
    raw = body.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LINES:
        raise _invalid(f"A transfer has 1 to {MAX_LINES} lines.", "lines")
    parse = _parse_pre_pt_line if custody == PRE_PT else _parse_draft_line
    lines = tuple(parse(item) for item in raw)
    keys = [line.line_key for line in lines]
    if len(set(keys)) != len(keys):
        raise _invalid("Every line_key on a transfer is distinct.", "lines")
    payload = DraftPayload(
        source_site_id=_site_id(body.get("source_site_id"), "source_site_id"),
        destination_site_id=_site_id(body.get("destination_site_id"), "destination_site_id"),
        request_id=_optional_uuid(body.get("request_id"), "request_id"),
        note=_text(body.get("note"), "note", limit=500, required=False),
        lines=lines,
        custody=str(custody),
    )
    if payload.custody in HELD_CUSTODY and payload.request_id is not None:
        raise _refuse(
            "A request asks for stock to use. Held goods are sent on a custody transfer "
            "of their own, never as the answer to a request."
        )
    if payload.source_site_id == payload.destination_site_id:
        raise _refuse("A transfer moves goods between two different sites.")
    return payload


def _parse_draft_line(item: Any) -> DraftLine:
    if not isinstance(item, dict):
        raise _invalid("Every transfer line is an object.", "lines")
    _closed(item, DRAFT_LINE_FIELDS)
    raw_origins = item.get("origin_ids") or []
    if not isinstance(raw_origins, list) or len(raw_origins) > 50:
        raise _invalid("origin_ids is a list of at most 50 IDs.", "origin_ids")
    return DraftLine(
        line_key=_uuid(item.get("line_key"), "line_key"),
        sku_id=_uuid(item.get("sku_id"), "sku_id"),
        qty=_qty(item.get("qty"), "qty"),
        origin_ids=tuple(_uuid(o, "origin_ids") for o in raw_origins),
        note=_text(item.get("note"), "note", limit=240, required=False),
    )


def _parse_pre_pt_line(item: Any) -> DraftLine:
    """A pre-PT line (goods ticket 13E): a GRN line and how many of its damaged pieces."""
    from outbound.pre_pt_custody import LINE_FIELDS

    if not isinstance(item, dict):
        raise _invalid("Every transfer line is an object.", "lines")
    _closed(item, LINE_FIELDS)
    return DraftLine(
        line_key=_uuid(item.get("line_key"), "line_key"),
        sku_id=None,
        qty=_qty(item.get("qty"), "qty"),
        origin_ids=(),
        note=_text(item.get("note"), "note", limit=240, required=False),
        grn_id=_uuid(item.get("grn_id"), "grn_id"),
        grn_line_key=_uuid(item.get("grn_line_key"), "grn_line_key"),
    )


def parse_request(body: dict[str, Any]) -> dict[str, Any]:
    _closed(body, REQUEST_FIELDS)
    raw = body.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LINES:
        raise _invalid(f"A request has 1 to {MAX_LINES} lines.", "lines")
    lines = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("Every request line is an object.", "lines")
        _closed(item, REQUEST_LINE_FIELDS)
        lines.append(
            {
                "line_key": str(_uuid(item.get("line_key"), "line_key")),
                "sku_id": str(_uuid(item.get("sku_id"), "sku_id")),
                "qty": _qty(item.get("qty"), "qty"),
                "note": _text(item.get("note"), "note", limit=240, required=False),
            }
        )
    keys = [line["line_key"] for line in lines]
    if len(set(keys)) != len(keys):
        raise _invalid("Every line_key on a request is distinct.", "lines")
    source = _site_id(body.get("source_site_id"), "source_site_id")
    destination = _site_id(body.get("destination_site_id"), "destination_site_id")
    if source == destination:
        raise _refuse("A request asks another site for stock, not this one.")
    return {
        "source_site_id": source,
        "destination_site_id": destination,
        "note": _text(body.get("note"), "note", limit=500, required=False),
        "lines": lines,
    }


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


def lock_sites(
    run: CommandRun, site_ids: Sequence[int], *, fenced: Sequence[int] | None = None
) -> dict[int, SiteGuard]:
    """Lock both site guards at rank SITE and check the goods fence of ``fenced``.

    Both in one query, in primary-key order, so two transfers running in
    opposite directions between the same pair cannot deadlock on each other.
    ``fenced`` defaults to every site locked; a step that happens at one site
    only (scanning at the source, arrival at the destination) checks that one.
    """
    guards = {
        guard.site_id: guard
        for guard in run.lock(
            LockRank.SITE, SiteGuard.objects.filter(site_id__in=sorted(set(site_ids)))
        )
    }
    for site_id in site_ids if fenced is None else fenced:
        _check_site(guards.get(site_id))
    return guards


#: Lifecycle stages at which a site moves no goods, even when approved to.
NOT_MOVING = ("planned", "closing")


def _check_site(guard: SiteGuard | None) -> SiteGuard:
    if guard is None or guard.stock_contract != SiteGuard.StockContract.GOODS_V1:
        raise Refusal("CONTRACT_DISABLED", "This site does not move goods through goods-v1.")
    if not guard.goods_ready or guard.lifecycle in NOT_MOVING:
        raise Refusal("SITE_NOT_READY", "This site is not approved to move goods.")
    if guard.freeze_id:
        raise Refusal("UNDER_COUNT", "A stock count has frozen this site.")
    return guard


def site_of(site_id: int) -> Any:
    from masters.models import Store

    site = Store.objects.select_related("gstin").filter(pk=site_id).first()
    if site is None:
        raise Refusal("NOT_FOUND", "That site was not found.")
    return site


def site_ref(site: Any) -> dict[str, str]:
    """A site as another site's paperwork names it: id, code and name, no more."""
    return {"id": str(site.pk), "code": site.code, "name": site.name}


def destinations(tenant_id: uuid.UUID, source_site_id: int) -> list[dict[str, str]]:
    """Every site a transfer from ``source_site_id`` may be drafted to (OPS-11).

    Every other site of the same legal entity that moves goods through goods-v1
    and is approved to (Anand, 25 September 2026): the warehouse and the other
    stores alike. A site of another company is a sale (``_require_same_entity``)
    and a site that cannot move goods is refused by ``lock_sites``, so neither
    is offered. A count freeze is not filtered out: it passes, and the draft
    says so in its own words rather than the site silently vanishing.

    Name, code and kind only. Being offered a site to send to is not reading it.
    """
    from masters.models import Store

    source = site_of(source_site_id)
    moving = (
        SiteGuard.objects.filter(
            tenant_id=tenant_id,
            stock_contract=SiteGuard.StockContract.GOODS_V1,
            goods_ready=True,
        )
        .exclude(lifecycle__in=NOT_MOVING)
        .values("site_id")
    )
    sites = (
        Store.objects.filter(gstin__legal_entity_id=source.gstin.legal_entity_id, pk__in=moving)
        .exclude(pk=source.pk)
        .order_by("name", "pk")
    )
    return [
        {"id": str(site.pk), "code": site.code, "name": site.name, "kind": site.store_type}
        for site in sites
    ]


def _require_same_entity(source: Any, destination: Any) -> None:
    """One legal entity on both ends. Anything else is a sale, not a transfer."""
    if source.gstin.legal_entity_id != destination.gstin.legal_entity_id:
        raise Refusal(
            "TRANSFER_POLICY_BLOCKED",
            "These two sites belong to different legal entities. A movement between them "
            "is a commercial transaction, not an internal transfer.",
        )


def quarantine_stock(site_id: int) -> list[dict[str, Any]]:
    """What a quarantine transfer from ``site_id`` could take, per SKU, origin and condition.

    Exactly the pool ``engine.select_quarantined`` freezes from (goods ticket
    13D), grouped for a person choosing what to send: recorded pieces held in
    the site's quarantine and reserved to nobody, each group with the hold kinds
    standing over it. Separate origins stay separate rows. No value.
    """
    portions = engine.quarantined_portions(site_id)
    holds: dict[uuid.UUID, list[tuple[ranges.Interval, str]]] = {}
    for hold in ActiveHold.objects.filter(lot_id__in={p.lot_id for p in portions}):
        holds.setdefault(hold.lot_id, []).append((bounds(hold.portion), hold.kind))
    rows: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    for item in portions:
        address = item.address
        origin = address.origin_id or address.value_basis_origin_id
        key = (str(address.sku_id), str(origin) if origin else None, address.condition)
        row = rows.setdefault(
            key,
            {
                "sku_id": key[0],
                "description": item.description,
                "origin_id": key[1],
                "condition": address.condition,
                "qty": 0,
                "hold_kinds": set(),
            },
        )
        row["qty"] += ranges.length(item.interval)
        row["hold_kinds"].update(
            kind
            for interval, kind in holds.get(item.lot_id, [])
            if ranges.intersect([interval], [item.interval])
        )
    return [
        {**row, "hold_kinds": sorted(row["hold_kinds"])}
        for _key, row in sorted(rows.items(), key=lambda kv: (kv[1]["description"], str(kv[0])))
    ]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def create_request(run: CommandRun, body: dict[str, Any]) -> TransferRequest:
    """The receiving site asks the sending site for stock. Nothing moves or reserves."""
    parsed = parse_request(body)
    assert run.principal.human_id is not None
    lock_sites(run, [parsed["source_site_id"], parsed["destination_site_id"]])
    _require_same_entity(site_of(parsed["source_site_id"]), site_of(parsed["destination_site_id"]))
    record = TransferRequest.objects.create(
        tenant_id=run.tenant_id,
        source_site_id=parsed["source_site_id"],
        destination_site_id=parsed["destination_site_id"],
        requested_by_id=run.principal.human_id,
        requested_at=run.now,
        lines=parsed["lines"],
        note=parsed["note"],
    )
    run.audit_subject_key = f"transfer_request:{record.pk}"
    run.audit_site_id = parsed["destination_site_id"]
    run.audit_after = {"transfer_request_id": str(record.pk), "state": record.state}
    return record


def visible_requests(
    tenant_id: uuid.UUID, site_ids: frozenset[int] | None, *, site_id: int | None = None
) -> list[TransferRequest]:
    from django.db.models import Q

    queryset = TransferRequest.objects.filter(tenant_id=tenant_id)
    if site_ids is not None:
        queryset = queryset.filter(
            Q(source_site_id__in=sorted(site_ids)) | Q(destination_site_id__in=sorted(site_ids))
        )
    if site_id is not None:
        queryset = queryset.filter(Q(source_site_id=site_id) | Q(destination_site_id=site_id))
    return list(queryset.order_by("-requested_at", "-id"))


def request_dto(record: TransferRequest, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    return {
        "id": str(record.pk),
        "record_contract": "goods-v1",
        "source_site_id": str(record.source_site_id),
        "destination_site_id": str(record.destination_site_id),
        "requested_by": {
            "id": str(record.requested_by_id),
            "name": names.get(record.requested_by_id, ""),
        },
        "requested_at": record.requested_at.isoformat(),
        "state": record.state,
        "note": record.note,
        "lines": record.lines,
        "transfer_id": str(record.transfer_id) if record.transfer_id else None,
    }


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def create(run: CommandRun, payload: DraftPayload) -> GoodsTransfer:
    """A draft movement: what somebody proposes to send, and from where.

    Nothing is reserved and nothing moves. The draft names SKUs and quantities;
    which exact pieces will go is frozen at submission, when the transfer PT is
    built, and only approval turns that into a reservation.
    """
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A transfer is drafted by a named person.")
    lock_sites(run, [payload.source_site_id, payload.destination_site_id])
    return create_locked(run, payload)[0]


def create_locked(
    run: CommandRun, payload: DraftPayload, *, corrective_for: GoodsTransfer | None = None
) -> tuple[GoodsTransfer, DocumentHead]:
    """``create`` once both site guards are already held (goods ticket 16).

    A corrective transfer is drafted inside the command that proposes it, which
    has locked both sites and the original movement first; taking the site
    guards again here would ask for a rank the lock order has already closed.
    Answers the new transfer and its document head, whose draft revision this
    command has recorded but not yet written.
    """
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A transfer is drafted by a named person.")
    source = site_of(payload.source_site_id)
    destination = site_of(payload.destination_site_id)
    _require_same_entity(source, destination)
    grn_brands: set[int] = set()
    if payload.custody == PRE_PT:
        from outbound.pre_pt_custody import check_draft

        grn_brands = check_draft(source, destination, payload.lines)
    _require_active_sbus(run, payload, grn_brands)
    request = _request_for(run, payload)
    identity, head = new_document(
        run,
        kind=TRANSFER_DOC_KIND,
        purpose=DocumentIdentity.Purpose.TRANSFER,
        entity_id=source.gstin.legal_entity_id,
        site_id=payload.source_site_id,
    )
    append_revision(
        run,
        head,
        header=payload.as_header(),
        replace_lines=[(line.line_key, line.as_payload()) for line in payload.lines],
    )
    transfer = GoodsTransfer.objects.create(
        tenant_id=run.tenant_id,
        document=identity,
        source_site_id=payload.source_site_id,
        destination_site_id=payload.destination_site_id,
        state=GoodsTransfer.State.DRAFT,
        custody=payload.custody,
        corrective_for=corrective_for,
    )
    if request is not None:
        request.transfer = transfer
        request.state = TransferRequest.State.DRAFTED
        request.save(update_fields=["transfer", "state"])
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = payload.source_site_id
    run.audit_after = {"transfer_id": str(transfer.pk), "state": transfer.state}
    return transfer, head


def _require_active_sbus(
    run: CommandRun, payload: DraftPayload, grn_brands: Iterable[int] = ()
) -> None:
    """No new transfer for a brand whose business unit is retired at either end.

    Implementation decision GSA-T02 (18 September 2026): every line's brand is
    checked at both the source and the destination site, and one retired unit
    refuses the whole draft (``SBU_RETIRED``) before anything is written. The
    brand is the SKU's own: at drafting no pieces have been chosen yet. A
    pre-PT line (goods ticket 13E) has no SKU to ask, so its brand is its GRN's.
    """
    from masters.goods_identity_models import ProductSku
    from masters.goods_sbu import require_active_sbu

    brands = {
        brand
        for brand in ProductSku.objects.filter(
            pk__in={line.sku_id for line in payload.lines if line.sku_id is not None}
        ).values_list("style__brand_id", flat=True)
        if brand is not None
    } | set(grn_brands)
    for site_id in (payload.source_site_id, payload.destination_site_id):
        for brand_id in sorted(brands):
            require_active_sbu(site_id, brand_id, run.now)


def _request_for(run: CommandRun, payload: DraftPayload) -> TransferRequest | None:
    if payload.request_id is None:
        return None
    rows: list[TransferRequest] = run.lock(
        LockRank.DOCUMENT, TransferRequest.objects.filter(pk=payload.request_id)
    )
    if not rows:
        raise Refusal("NOT_FOUND", "That request was not found.")
    request = rows[0]
    if request.state != TransferRequest.State.OPEN:
        raise _state_conflict("That request has already been answered.")
    if (
        request.source_site_id != payload.source_site_id
        or request.destination_site_id != payload.destination_site_id
    ):
        raise _refuse("This draft does not send from and to the sites the request asked about.")
    return request


# ---------------------------------------------------------------------------
# Submission: the transfer PT, built from the draft and the eligible origins
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenPortion:
    lot_id: uuid.UUID
    interval: ranges.Interval
    origin_id: uuid.UUID | None
    location_id: uuid.UUID | None
    description: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "lot_id": str(self.lot_id),
            "lower": self.interval[0],
            "upper": self.interval[1],
            "origin_id": str(self.origin_id) if self.origin_id else None,
            "source_location_id": str(self.location_id) if self.location_id else None,
        }


def submit(run: CommandRun, transfer_id: uuid.UUID) -> tuple[GoodsTransfer, GoodsPt | None]:
    """Freeze the exact source pieces and build the transfer PT that carries them.

    The draft said "six of this SKU". This says *which* six - lot, range, origin
    and the location each piece is standing at - so the approver reviews real
    pieces and the approval can reserve them exactly. Separate origins stay
    separate rows: a transfer creates no new purchase value and never merges two
    costs into one (transfers PRD §4).

    A pre-PT custody transfer (goods ticket 13E) freezes its pieces the same
    way, from its own pool, but onto the transfer's own document: no transfer
    PT is made for goods no PT has registered (overall PRD §15.2.1 rule 10).
    """
    transfer, head = _locked(run, transfer_id)
    return submit_locked(run, transfer, head)


def submit_locked(
    run: CommandRun,
    transfer: GoodsTransfer,
    head: DocumentHead,
    drafted: Sequence[DraftLine] | None = None,
) -> tuple[GoodsTransfer, GoodsPt | None]:
    """``submit`` once the sites and the transfer are already held (goods ticket 16).

    ``drafted`` is the draft's lines when this same command wrote them (a
    corrective transfer is drafted and submitted at once): its draft revision
    is not written until the command ends, so it cannot be read back yet.
    """
    if transfer.state != GoodsTransfer.State.DRAFT:
        raise _state_conflict("Only a draft transfer can be sent for approval.")
    lines = list(drafted) if drafted is not None else _draft_lines(head)
    goods_pt: GoodsPt | None = None
    if transfer.custody == PRE_PT:
        plan_id = _submit_pre_pt(run, transfer, head, lines)
        details: dict[str, Any] = {
            "document_id": str(plan_id),
            "lines": len(lines),
            "custody": str(PRE_PT),
        }
        note = f"transfer:{transfer.pk}"
    else:
        engine.lock_lots(run, _candidate_lots(transfer.source_site_id, lines))
        frozen = [
            (line, _freeze(transfer.source_site_id, line, transfer.custody)) for line in lines
        ]
        pt_identity, pt_head = new_document(
            run,
            kind=PT_DOC_TYPE,
            purpose=DocumentIdentity.Purpose.TRANSFER,
            entity_id=transfer.document.entity_id,
            site_id=transfer.source_site_id,
        )
        header = _pt_header(transfer)
        append_revision(
            run,
            pt_head,
            header=header,
            replace_lines=[(line.line_key, _pt_line(line, pieces)) for line, pieces in frozen],
        )
        assert run.principal.human_id is not None
        goods_pt = GoodsPt.objects.create(
            tenant_id=run.tenant_id,
            document=pt_identity,
            transfer=transfer,
            preparer_id=run.principal.human_id,
        )
        set_state(run, pt_head, DocumentHead.State.SUBMITTED, event="submitted")
        plan_id = pt_identity.pk
        details = {"pt_id": str(pt_identity.pk), "lines": len(frozen)}
        note = f"transfer_pt:{pt_identity.pk}"
    transfer.state = GoodsTransfer.State.SUBMITTED
    transfer.save(update_fields=["state"])
    open_exception(
        run,
        kind=APPROVAL_EXCEPTION,
        site_id=transfer.source_site_id,
        subject_key=f"transfer:{transfer.pk}",
        reason_code=APPROVAL_REASON,
        source_event_key=engine.event_key("transfer_approval", plan_id),
        allowed_resolution_actions=RESOLUTION_ACTIONS,
        note=note,
    )
    _log(
        run,
        transfer,
        TransferEvent.Kind.SUBMITTED,
        site_id=transfer.source_site_id,
        details=details,
    )
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.source_site_id
    run.audit_after = {"transfer_id": str(transfer.pk), "state": transfer.state}
    return transfer, goods_pt


def _submit_pre_pt(
    run: CommandRun, transfer: GoodsTransfer, head: DocumentHead, lines: Sequence[DraftLine]
) -> uuid.UUID:
    """Freeze a pre-PT transfer's pieces onto its own document (goods ticket 13E).

    Each line keeps what its GRN says about the goods and what is known of
    them - identity only if the count gave one, the condition counted, the
    remark, the damage reports and evidence - with its value ``unknown``.
    Separate lots stay separate portions, exactly as origins do elsewhere.
    Answers the document the approval will officialise: the transfer's own.
    """
    from outbound import pre_pt_custody

    # The approver must be told apart from whoever sent it (GSA-R01), and the
    # submitter is read back from this event's actor: a named person only.
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A transfer is sent for approval by a named person.")
    engine.lock_lots(
        run,
        sorted({p.lot_id for p in pre_pt_custody.pool(transfer.source_site_id)}, key=str),
    )
    frozen: list[tuple[uuid.UUID, Mapping[str, Any]]] = []
    for line in lines:
        pieces, facts = pre_pt_custody.freeze(transfer.source_site_id, line)
        frozen.append(
            (
                line.line_key,
                {
                    "line_key": str(line.line_key),
                    **facts,
                    "qty": line.qty,
                    "note": line.note,
                    "portions": [
                        FrozenPortion(
                            lot_id=piece.lot_id,
                            interval=piece.interval,
                            origin_id=None,
                            location_id=piece.location_id,
                            description=piece.description,
                        ).as_payload()
                        for piece in pieces
                    ],
                },
            )
        )
    draft = head.draft_revision
    header = {
        **(dict(draft.payload) if draft is not None else {}),
        **_pt_header(transfer),
        # The approved movement is this document, and it is not a PT.
        "purpose": "custody_transfer",
    }
    append_revision(run, head, header=header, replace_lines=frozen)
    set_state(run, head, DocumentHead.State.SUBMITTED, event="submitted")
    return transfer.document_id


def _pt_header(transfer: GoodsTransfer) -> dict[str, Any]:
    header = {
        "transfer_id": str(transfer.pk),
        "source_site_id": str(transfer.source_site_id),
        "destination_site_id": str(transfer.destination_site_id),
        "purpose": "transfer",
    }
    # The approved document of a held-custody transfer says so on its own
    # face, so it cannot be read as an ordinary movement of stock.
    if transfer.custody in HELD_CUSTODY:
        header["custody"] = str(transfer.custody)
    return header


def _pt_line(line: DraftLine, pieces: Sequence[FrozenPortion]) -> dict[str, Any]:
    return {
        "line_key": str(line.line_key),
        "sku_id": str(line.sku_id),
        "qty": line.qty,
        "description": pieces[0].description if pieces else "",
        "note": line.note,
        "portions": [piece.as_payload() for piece in pieces],
    }


def _draft_lines(head: DocumentHead) -> list[DraftLine]:
    from core.goods_documents import revision_lines

    if head.draft_revision is None:
        raise _state_conflict("This transfer has no draft to send.")
    states = revision_lines(head.document_id, head.draft_revision.revision)
    out: list[DraftLine] = []
    for state in states:
        body = state.payload
        grn_id = body.get("grn_id")
        out.append(
            DraftLine(
                line_key=uuid.UUID(str(body["line_key"])),
                sku_id=uuid.UUID(str(body["sku_id"])) if body.get("sku_id") else None,
                qty=int(body["qty"]),
                origin_ids=tuple(uuid.UUID(str(o)) for o in body.get("origin_ids") or []),
                note=body.get("note"),
                grn_id=uuid.UUID(str(grn_id)) if grn_id else None,
                grn_line_key=(
                    uuid.UUID(str(body["grn_line_key"])) if body.get("grn_line_key") else None
                ),
            )
        )
    if not out:
        raise _state_conflict("This transfer has no lines to send.")
    return out


def _candidate_lots(site_id: int, lines: Sequence[DraftLine]) -> list[uuid.UUID]:
    wanted: set[uuid.UUID] = set()
    for line in lines:
        wanted.update(
            Position.objects.filter(
                site_id=site_id, boundary="physical", sku_id=line.sku_id
            ).values_list("lot_id", flat=True)
        )
    return sorted(wanted, key=str)


def _freeze(site_id: int, line: DraftLine, custody: str = ORDINARY) -> list[FrozenPortion]:
    """The oldest eligible pieces of this SKU at this site, or the chosen origins.

    For an ordinary transfer ``engine.select_fifo`` is the same selection every
    other allocation uses, so "eligible" means exactly what it means everywhere
    else: accepted, valued, good, in a storage location, under no hold and
    reserved to nobody. A quarantined or held piece is never offered to it.

    A quarantine transfer (goods ticket 13D, overall PRD §15.2.1 rule 10) takes
    from the other pool and only from it: recorded pieces standing in the
    source's quarantine under a hold (``engine.select_quarantined``). Each pool
    is closed to the other, so neither kind of transfer can carry the other's
    goods - an ordinary transfer never moves held stock, and a quarantine
    transfer never moves sellable stock past the ordinary controls.
    """
    assert line.sku_id is not None  # a pre-PT line is frozen by ``_submit_pre_pt``
    select = engine.select_quarantined if custody == QUARANTINE else engine.select_fifo
    chosen = select(site_id, line.sku_id, line.qty, origin_ids=line.origin_ids or None)
    return [
        FrozenPortion(
            lot_id=item.lot_id,
            interval=item.interval,
            origin_id=item.address.origin_id or item.address.value_basis_origin_id,
            location_id=item.address.location_id,
            description=item.description,
        )
        for item in chosen
    ]


# ---------------------------------------------------------------------------
# Approval: P07, the reservation
# ---------------------------------------------------------------------------


def approve(run: CommandRun, transfer_id: uuid.UUID, *, reason: str | None) -> GoodsTransfer:
    """A distinct second person officialises the transfer PT, which reserves the pieces.

    The reservation is the whole point of approving: from here the exact
    portions are unavailable to a sale or to another transfer, and they stay
    that way with no expiry until they are dispatched or explicitly cancelled
    (transfers PRD §3).

    A pre-PT custody transfer (goods ticket 13E) officialises the transfer's own
    document instead - there is no transfer PT to approve - and takes no
    number from the transfer PT series: nothing about it is a PT.
    """
    transfer, head = _locked(run, transfer_id)
    if transfer.state != GoodsTransfer.State.SUBMITTED:
        raise _state_conflict("Only a submitted transfer is waiting for approval.")
    pre_pt = transfer.custody == PRE_PT
    goods_pt: GoodsPt | None = None
    if pre_pt:
        pt_head = head
        _refuse_self_approval(run, transfer, _submitter(transfer))
    else:
        goods_pt = _pt_of(transfer)
        pt_head = lock_heads(run, [goods_pt.document_id])[goods_pt.document_id]
        _refuse_self_approval(run, transfer, goods_pt.preparer_id)
    # Goods ticket 16: a corrective transfer's approval is also the Owner's
    # decision on the excess-observed GAP it serves; that decision is locked
    # here, at DOCUMENT rank, before any lot.
    correction = None
    if transfer.corrective_for_id is not None:
        from outbound import transfer_excess

        correction = transfer_excess.lock_decision(run, transfer)
    lines: list[tuple[uuid.UUID, Mapping[str, Any]]] = _official_lines_from_draft(pt_head)
    engine.lock_lots(
        run,
        sorted(
            {_lot(p) for _key, body in lines for p in body["portions"]}
            | ({correction.lot_id} if correction is not None else set()),
            key=str,
        ),
    )
    if correction is not None:
        transfer_excess.recheck_claim(correction)
    if transfer.custody in HELD_CUSTODY:
        # Pre-PT pieces are quarantined under their damage hold exactly as
        # recorded ones are; the same "still where it was reviewed" rule holds.
        _recheck_still_quarantined(transfer.source_site_id, lines)
    else:
        _recheck_still_eligible(transfer.source_site_id, lines)
    # Stock a counter took offline is not free to promise elsewhere (PRD §10.2).
    # Checked here, where the reservation happens, rather than at draft or submit:
    # a drafted transfer moves nothing and a store may perfectly well plan one
    # while its counter is out, but approving it is the moment the pieces stop
    # being sellable - which is the moment they would be taken from under a till
    # that may already have sold them.
    #
    # Imported here rather than at the top: `sell.services` reaches into this
    # module for the transfer lifecycle, and a top-level import in both
    # directions is a cycle at start-up.
    from sell.services.till_authority import refuse_if_allocated

    # Held goods were never on a counter's shelf, so a till's allocation
    # cannot be taken from under it by reserving them.
    if transfer.custody not in HELD_CUSTODY:
        refuse_if_allocated(transfer.source_site_id)
    assert run.principal.human_id is not None
    version, _official = officialise(
        run,
        pt_head,
        approved_by_id=run.principal.human_id,
        canonical_header=(
            dict(pt_head.draft_revision.payload)
            if pre_pt and pt_head.draft_revision is not None
            else _pt_header(transfer)
        ),
        lines=lines,
        authority=run.authority,
        number=None if pre_pt else allocate(run, transfer.document.entity, PT_DOC_TYPE),
    )
    plan = engine.Plan(
        RESERVE_POSTING, version.pk, engine.event_key("transfer_approve", version.pk)
    )
    for _key, body in lines:
        for piece in body["portions"]:
            engine.reserve(
                run,
                plan,
                lot_id=_lot(piece),
                interval=_interval(piece),
                transfer_version_id=version.pk,
                site_id=transfer.source_site_id,
            )
    engine.post(run, version, plan)
    transfer.state = GoodsTransfer.State.APPROVED
    transfer.save(update_fields=["state"])
    if correction is not None:
        transfer_excess.approve_decision(run, transfer, correction, reason)
    resolve_exceptions(
        run,
        kind=APPROVAL_EXCEPTION,
        subject_key=f"transfer:{transfer.pk}",
        reason_code="TRANSFER_APPROVED",
    )
    _log(
        run,
        transfer,
        TransferEvent.Kind.APPROVED,
        site_id=transfer.source_site_id,
        details=(
            {
                "document_id": str(transfer.document_id),
                "version_id": str(version.pk),
                "custody": str(PRE_PT),
                "reason": reason,
            }
            if goods_pt is None
            else {
                "pt_id": str(goods_pt.document_id),
                "pt_number": transfer.document.official_number,
                "pt_version_id": str(version.pk),
                "reason": reason,
            }
        ),
    )
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.source_site_id
    run.audit_after = {"transfer_id": str(transfer.pk), "state": transfer.state}
    return transfer


def _submitter(transfer: GoodsTransfer) -> uuid.UUID | None:
    """Who sent a transfer for approval: the actor of its latest ``submitted`` event."""
    return (
        TransferEvent.objects.filter(transfer=transfer, kind=TransferEvent.Kind.SUBMITTED)
        .order_by("-recorded_at", "-id")
        .values_list("actor_id", flat=True)
        .first()
    )


def _refuse_self_approval(
    run: CommandRun, transfer: GoodsTransfer, preparer_id: uuid.UUID | None
) -> None:
    """PRD §13: a transfer's drafter and approver are different humans."""
    if run.principal.human_id is None:
        raise Refusal("ACTION_DENIED", "A transfer is approved by a named person.")
    me = str(run.principal.human_id)
    if me in (str(transfer.document.maker_id), str(preparer_id)):
        raise Refusal(
            "SELF_APPROVAL",
            "Whoever drafted or sent this transfer cannot also approve it. "
            "A different authorised person approves it.",
        )


def _official_lines_from_draft(
    head: DocumentHead,
) -> list[tuple[uuid.UUID, Mapping[str, Any]]]:
    from core.goods_documents import revision_lines

    if head.draft_revision is None:
        raise _state_conflict("This transfer PT has no content to approve.")
    return [
        (state.line_key, dict(state.payload))
        for state in revision_lines(head.document_id, head.draft_revision.revision)
    ]


def _recheck_still_eligible(
    site_id: int, lines: Sequence[tuple[uuid.UUID, Mapping[str, Any]]]
) -> None:
    """The pieces the draft froze must still be exactly where and what it said.

    Between submission and approval somebody may have sold, moved, held or
    reserved them. An approval that quietly re-picked different pieces would
    approve something nobody reviewed, so this refuses instead.
    """
    for _key, body in lines:
        for piece in body["portions"]:
            lot_id, interval = _lot(piece), _interval(piece)
            standing = [
                p
                for p in engine.positions_of(lot_id, interval)
                if p.boundary == "physical" and p.site_id == site_id
            ]
            covered = ranges.intersect([bounds(p.portion) for p in standing], [interval])
            if ranges.total(covered) != ranges.length(interval):
                raise Refusal(
                    "INSUFFICIENT_ELIGIBLE_STOCK",
                    "Some of the pieces this transfer names are no longer at the source. "
                    "Reload the transfer and draft it again.",
                )
            if ActiveHold.objects.filter(lot_id=lot_id, portion__overlap=portion(*interval)):
                raise Refusal(
                    "INSUFFICIENT_ELIGIBLE_STOCK",
                    "Some of the pieces this transfer names are now held and cannot be reserved.",
                )


def _recheck_still_quarantined(
    site_id: int, lines: Sequence[tuple[uuid.UUID, Mapping[str, Any]]]
) -> None:
    """A quarantine transfer's frozen pieces must still be quarantined at the source.

    The same "approve only what was reviewed" rule as ``_recheck_still_eligible``,
    for the other pool: every frozen piece still stands physically in the
    source's quarantine and a hold still covers it. A piece whose hold was lifted
    meanwhile (a rejected damage report, say) is no longer quarantined and
    cannot be moved as if it were; it goes on an ordinary transfer instead.
    """
    quarantine = engine.system_location(site_id, "quarantine")
    for _key, body in lines:
        for piece in body["portions"]:
            lot_id, interval = _lot(piece), _interval(piece)
            if ranges.total(_quarantined_part(lot_id, interval, site_id, quarantine.pk)) != (
                ranges.length(interval)
            ):
                raise Refusal(
                    "INSUFFICIENT_ELIGIBLE_STOCK",
                    "Some of the pieces this quarantine transfer names are no longer held in "
                    "the source's quarantine. Reload the transfer and draft it again.",
                )


def _quarantined_part(
    lot_id: uuid.UUID, interval: ranges.Interval, site_id: int, quarantine_id: uuid.UUID
) -> list[ranges.Interval]:
    """The part of ``interval`` standing in the site's quarantine under at least one hold.

    A written-off part (goods ticket 15C) is left out: its value already left
    stock, so it is not moved as if it still carried it (``engine.written_off``).
    """
    standing = [
        bounds(p.portion)
        for p in engine.positions_of(lot_id, interval)
        if p.boundary == "physical" and p.site_id == site_id and p.location_id == quarantine_id
    ]
    held = [
        bounds(hold.portion)
        for hold in ActiveHold.objects.filter(lot_id=lot_id, portion__overlap=portion(*interval))
    ]
    part = ranges.intersect(ranges.intersect(standing, [interval]), held)
    return ranges.subtract(part, engine.written_off([lot_id]).get(lot_id, []))


# ---------------------------------------------------------------------------
# Cancelling the outstanding balance
# ---------------------------------------------------------------------------


def cancel_outstanding(run: CommandRun, transfer_id: uuid.UUID, *, reason: str) -> GoodsTransfer:
    """Release only what has not been dispatched, and keep who, when, how many and why.

    An open dispatch is not closed by this (transfers PRD §7): the pieces that
    already left still have to be accounted for, and cancelling the balance says
    nothing about them.
    """
    transfer, _head = _locked(run, transfer_id)
    if transfer.state not in (GoodsTransfer.State.APPROVED, GoodsTransfer.State.DISPATCHING):
        raise _state_conflict("There is no approved balance on this transfer to cancel.")
    version = _pt_version(transfer)
    # A shipment being scanned was being scanned against the balance this
    # releases, so it can no longer be dispatched. It is kept, with the reason.
    from outbound import dispatch_preparation as preparing

    preparing.invalidate_open(run, transfer, preparing.INVALIDATED_BALANCE_CANCELLED)
    # Goods ticket 16: cancelling a corrective transfer before its confirmation
    # withdraws the excess-observed decision it served, so the observation is
    # free for another route. Locked at DOCUMENT rank, before any lot.
    correction = None
    if transfer.corrective_for_id is not None:
        from outbound import transfer_excess

        correction = transfer_excess.lock_decision(run, transfer)
    engine.lock_lots(
        run,
        sorted(
            ActiveReservation.objects.filter(transfer_version_id=version.pk).values_list(
                "lot_id", flat=True
            ),
            key=str,
        ),
    )
    plan = engine.Plan(
        RESERVE_POSTING, version.pk, engine.event_key("transfer_cancel", version.pk, run.key_id)
    )
    released = engine.end_reservations(
        run,
        plan,
        transfer_version_id=version.pk,
        effect="release",
        site_id=transfer.source_site_id,
    )
    if not released:
        raise _state_conflict("Nothing on this transfer is still reserved.")
    engine.post(run, version, plan)
    if correction is not None:
        transfer_excess.withdraw_decision(run, transfer, correction, reason)
    # A hold over the cancelled pieces (damage reported on them, say) stays in
    # force; only the owner's "reserved stock is held" work is finished, because
    # there is no reservation left for the hold to block.
    settle_reservation_blocks(run, [version.pk])
    quantity = sum(ranges.length(interval) for _lot_id, interval in released)
    _log(
        run,
        transfer,
        TransferEvent.Kind.CANCELLED,
        site_id=transfer.source_site_id,
        details={"quantity": quantity, "reason": reason, "scope": "outstanding"},
    )
    _settle(run, transfer)
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.source_site_id
    run.audit_after = {
        "transfer_id": str(transfer.pk),
        "state": transfer.state,
        "released_qty": quantity,
    }
    return transfer


# ---------------------------------------------------------------------------
# Dispatch: P08, reservation consumed into transit
# ---------------------------------------------------------------------------

DISPATCH_FIELDS = frozenset(
    {
        "dispatched_at",
        "transport",
        "lines",
        "dispatch_session_id",
        "dispatch_session_revision",
        "dispatch_session_hash",
    }
)
DISPATCH_REQUIRED = (
    "lines",
    "dispatch_session_id",
    "dispatch_session_revision",
    "dispatch_session_hash",
)
DISPATCH_LINE_FIELDS = frozenset({"line_key", "qty"})
#: The e-way reference a shipment may leave with (design TransportDetails).
EWAY_REFERENCE = "eway_reference"
EWAY_REFERENCE_LIMIT = 100


def parse_dispatch(body: dict[str, Any], now: datetime) -> dict[str, Any]:
    _closed(body, DISPATCH_FIELDS)
    raw = body.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LINES:
        raise _invalid(f"A dispatch has 1 to {MAX_LINES} lines.", "lines")
    lines = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("Every dispatch line is an object.", "lines")
        _closed(item, DISPATCH_LINE_FIELDS)
        lines.append(
            {
                "line_key": _uuid(item.get("line_key"), "line_key"),
                "qty": _qty(item.get("qty"), "qty"),
            }
        )
    transport = body.get("transport") or {}
    if not isinstance(transport, dict) or len(transport) > 20:
        raise _invalid("transport is an object of at most 20 entries.", "transport")
    reference = transport.get(EWAY_REFERENCE)
    if reference is not None and (
        not isinstance(reference, str) or len(reference.strip()) > EWAY_REFERENCE_LIMIT
    ):
        raise _invalid(
            f"transport.{EWAY_REFERENCE} is text of at most {EWAY_REFERENCE_LIMIT} characters.",
            f"transport.{EWAY_REFERENCE}",
        )
    revision = body.get("dispatch_session_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise _invalid(
            "dispatch_session_revision is the preparation revision you reviewed.",
            "dispatch_session_revision",
        )
    reviewed = body.get("dispatch_session_hash")
    if not isinstance(reviewed, str) or len(reviewed) != 64:
        raise _invalid(
            "dispatch_session_hash is the preparation content hash you reviewed.",
            "dispatch_session_hash",
        )
    return {
        "dispatched_at": _moment(body.get("dispatched_at"), "dispatched_at", now),
        "transport": transport,
        "eway_reference": (reference or "").strip() or None,
        "lines": lines,
        "session_id": _uuid(body.get("dispatch_session_id"), "dispatch_session_id"),
        "session_revision": revision,
        "session_hash": reviewed,
    }


def _moment(value: Any, field: str, now: datetime) -> datetime:
    """A business time that is not in the future. Recording time is the kernel's own."""
    if value in (None, ""):
        return now
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(str(value))
    if parsed is None or parsed.tzinfo is None:
        raise _invalid(f"{field} must be a date and time with a time zone.", field)
    if parsed > now:
        raise Refusal("EVENT_TIME_INVALID", f"{field} cannot be later than now.", status=422)
    return parsed


def dispatch(run: CommandRun, transfer_id: uuid.UUID, parsed: dict[str, Any]) -> TransferDispatch:
    """One shipment leaves: exactly what its preparation scanned, and nothing else.

    The live approval, the preparation's revision and content, the submitted
    totals, the holds and the reservations are all rechecked here, under the
    transfer's locks, in the same transaction that consumes the reservation
    (design E146, adapted by goods ticket 13B to the selected shipment). A
    shipment that leaves with no e-way reference is recorded all the same, and
    opens an owned exception at the sending site (transfers PRD §8).
    """
    from outbound import dispatch_preparation as preparing

    transfer, _head = _locked(run, transfer_id)
    refuse_corrective_shipment(transfer)
    if transfer.state not in (GoodsTransfer.State.APPROVED, GoodsTransfer.State.DISPATCHING):
        raise _state_conflict("Only an approved transfer can be dispatched.")
    version = _pt_version(transfer)
    preparation, selected = preparing.consume(
        run,
        transfer,
        version,
        preparation_id=parsed["session_id"],
        revision=parsed["session_revision"],
        reviewed_hash=parsed["session_hash"],
        lines=parsed["lines"],
    )
    outstanding = _outstanding(transfer, version)
    # Every reserved lot is locked before anything is chosen, so a hold placed
    # over a reserved piece cannot slip in between reading it free and moving it.
    engine.lock_lots(
        run,
        sorted({piece.lot_id for line in outstanding.values() for piece in line.pieces}, key=str),
    )
    quarantined = held_custody(transfer)
    chosen = _choose_for_dispatch(
        selected,
        _still_quarantined(outstanding, transfer.source_site_id)
        if quarantined
        else _unheld(outstanding),
        quarantined=quarantined,
    )
    assert run.principal.human_id is not None
    sequence = TransferDispatch.objects.filter(transfer=transfer).count() + 1
    lines: list[dict[str, Any]] = [
        {
            "line_key": str(line_key),
            "sku_id": outstanding[line_key].sku_id,
            "qty": sum(ranges.length(piece.interval) for piece in pieces),
            "portions": [piece.as_payload() for piece in pieces],
        }
        for line_key, pieces in chosen.items()
    ]
    eway = "present" if parsed["eway_reference"] else "not_present"
    record = TransferDispatch.objects.create(
        tenant_id=run.tenant_id,
        transfer=transfer,
        sequence_no=sequence,
        dispatched_at=parsed["dispatched_at"],
        recorded_by_id=run.principal.human_id,
        transport=parsed["transport"],
        lines=lines,
        eway_at_dispatch=eway,
    )
    plan = engine.Plan(
        DISPATCH_POSTING, version.pk, engine.event_key("transfer_dispatch", record.pk)
    )
    for pieces in chosen.values():
        for piece in pieces:
            engine.end_reservation(
                run,
                plan,
                lot_id=piece.lot_id,
                interval=piece.interval,
                transfer_version_id=version.pk,
                effect="consume",
                site_id=transfer.source_site_id,
            )
            engine.change_address(
                run,
                plan,
                piece.lot_id,
                piece.interval,
                lambda old, tid=transfer.pk: replace(
                    old,
                    boundary="transit",
                    site_id=None,
                    location_id=None,
                    transfer_id=tid,
                    # Acceptance does not travel: a piece is sellable only where
                    # it was accepted, and it has not been accepted anywhere yet
                    # at the destination (overall PRD R-INV-002).
                    accepted_event_id=None,
                ),
            )
    engine.post(run, version, plan)
    preparing.mark_dispatched(run, preparation, record)
    transfer.state = GoodsTransfer.State.DISPATCHING
    transfer.dispatched_at = transfer.dispatched_at or parsed["dispatched_at"]
    transfer.save(update_fields=["state", "dispatched_at"])
    _log(
        run,
        transfer,
        TransferEvent.Kind.DISPATCH,
        site_id=transfer.source_site_id,
        details={
            "dispatch_id": str(record.pk),
            "sequence_no": sequence,
            "quantity": sum(int(line["qty"]) for line in lines),
            "transport": parsed["transport"],
            "preparation_id": str(preparation.pk),
            "eway_at_dispatch": eway,
        },
        actual_at=parsed["dispatched_at"],
    )
    if eway == "not_present":
        from outbound.transfer_shipments import open_missing_eway

        open_missing_eway(run, transfer, record)
    # Store operations ticket 36: the delivery challan or tax invoice the goods
    # leave with, where the sending site's switch is on. A refusal here undoes
    # the whole dispatch - the goods never leave without their document.
    from outbound.transfer_documents import issue_for_dispatch

    document = issue_for_dispatch(run, transfer, record)
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = transfer.source_site_id
    run.audit_after = {"dispatch_id": str(record.pk), "sequence_no": sequence}
    if document is not None:
        run.audit_after["document"] = {"kind": document.kind, "number": document.number}
    return record


def refuse_corrective_shipment(transfer: GoodsTransfer) -> None:
    """A corrective transfer never ships (goods ticket 16).

    Its goods are already standing at the destination as observed excess. It is
    completed by one corrective confirmation that records its dispatch and
    arrival pair and matches that observation - a shipment prepared, scanned
    and counted as well would put the same pieces at the destination twice.
    """
    if transfer.corrective_for_id is not None:
        raise _refuse(
            "A corrective transfer is not shipped. Its goods are already at the "
            "destination as the excess it corrects: record its corrective "
            "confirmation instead."
        )


@dataclass(frozen=True)
class _Outstanding:
    #: ``None`` on a pre-PT line whose count gave the goods no SKU (goods ticket 13E).
    sku_id: str | None
    description: str
    pieces: tuple[FrozenPortion, ...]
    #: Reserved pieces of this line a hold stands over (``_unheld``); they stay
    #: reserved but cannot be dispatched.
    held_qty: int = 0

    @property
    def qty(self) -> int:
        return sum(ranges.length(piece.interval) for piece in self.pieces)


def _outstanding(
    transfer: GoodsTransfer, version: OfficialVersion
) -> dict[uuid.UUID, _Outstanding]:
    """What is still reserved, per approved line - the only thing a dispatch may take."""
    from core.kernel_models import OfficialLine

    reserved: dict[uuid.UUID, list[ranges.Interval]] = {}
    for row in ActiveReservation.objects.filter(transfer_version_id=version.pk):
        reserved.setdefault(row.lot_id, []).append(bounds(row.portion))
    out: dict[uuid.UUID, _Outstanding] = {}
    for line in OfficialLine.objects.filter(version_id=version.pk).order_by("line_no"):
        body = dict(line.payload)
        pieces: list[FrozenPortion] = []
        for piece in body["portions"]:
            lot_id, interval = _lot(piece), _interval(piece)
            for still in ranges.intersect(reserved.get(lot_id, []), [interval]):
                pieces.append(
                    FrozenPortion(
                        lot_id=lot_id,
                        interval=still,
                        origin_id=_optional_uuid(piece.get("origin_id"), "origin_id"),
                        location_id=_optional_uuid(
                            piece.get("source_location_id"), "source_location_id"
                        ),
                        description=str(body.get("description") or ""),
                    )
                )
        out[line.stable_line_key] = _Outstanding(
            sku_id=str(body["sku_id"]) if body.get("sku_id") else None,
            description=str(body.get("description") or ""),
            pieces=tuple(sorted(pieces, key=lambda p: (str(p.lot_id), p.interval[0]))),
        )
    return out


def _unheld(outstanding: dict[uuid.UUID, _Outstanding]) -> dict[uuid.UUID, _Outstanding]:
    """The reserved pieces that may actually leave: those no hold stands over.

    Damage reported on a reserved piece quarantines it at once and leaves the
    reservation in place (design §3.2 and E209 step 13; the transfers PRD §3 keeps
    a reservation until it is dispatched or explicitly cancelled). That overlap
    blocks dispatching the held piece until it is resolved - by a rejected report,
    or by cancelling the outstanding balance. Everything else still dispatches.
    """
    out: dict[uuid.UUID, _Outstanding] = {}
    for line_key, line in outstanding.items():
        free: list[FrozenPortion] = []
        for piece in line.pieces:
            held = [
                bounds(hold.portion)
                for hold in ActiveHold.objects.filter(
                    lot_id=piece.lot_id, portion__overlap=portion(*piece.interval)
                )
            ]
            free.extend(
                replace(piece, interval=interval)
                for interval in ranges.subtract([piece.interval], held)
            )
        out[line_key] = replace(line, pieces=tuple(free), held_qty=line.qty - _qty_of(free))
    return out


def _still_quarantined(
    outstanding: dict[uuid.UUID, _Outstanding], site_id: int
) -> dict[uuid.UUID, _Outstanding]:
    """A quarantine transfer's reserved pieces that may leave: those still quarantined.

    The mirror of ``_unheld`` (goods ticket 13D). A quarantine transfer carries
    held goods by design, so a hold never blocks it; what it may never carry is
    a piece that is no longer held in the source's quarantine - one whose hold
    was lifted since approval. That piece stays reserved but cannot leave as
    quarantined goods, and it cannot leave as ordinary goods on this movement
    either: it waits for the balance to be cancelled.
    """
    quarantine = engine.system_location(site_id, "quarantine")
    out: dict[uuid.UUID, _Outstanding] = {}
    for line_key, line in outstanding.items():
        free: list[FrozenPortion] = []
        for piece in line.pieces:
            free.extend(
                replace(piece, interval=interval)
                for interval in _quarantined_part(
                    piece.lot_id, piece.interval, site_id, quarantine.pk
                )
            )
        out[line_key] = replace(line, pieces=tuple(free), held_qty=line.qty - _qty_of(free))
    return out


def _qty_of(pieces: Sequence[FrozenPortion]) -> int:
    return sum(ranges.length(piece.interval) for piece in pieces)


def _choose_for_dispatch(
    selected: Selected,
    outstanding: dict[uuid.UUID, _Outstanding],
    *,
    quarantined: bool = False,
) -> dict[uuid.UUID, list[FrozenPortion]]:
    """The exact reserved pieces the scanned shipment takes.

    Scans that named an origin take that origin's pieces first; the rest of the
    line takes the oldest reserved pieces left. Held pieces never leave
    (E146 step 15): if the unheld pieces cannot make up the scanned shipment,
    the dispatch is refused and nothing moves.
    """
    chosen: dict[uuid.UUID, list[FrozenPortion]] = {}
    for line_key, qty in selected.per_line.items():
        available = outstanding.get(line_key)
        if available is None:
            raise Refusal("NOT_FOUND", "That line is not on this transfer.")
        if qty > available.qty + available.held_qty:
            raise Refusal(
                "DISPATCH_PLAN_MISMATCH",
                f"Only {available.qty + available.held_qty} piece(s) of that line are still "
                "reserved. Dispatching more than was approved needs a fresh approval.",
            )
        pool = list(available.pieces)
        taken: list[FrozenPortion] = []
        for origin_id, wanted in sorted(
            selected.per_origin.get(line_key, {}).items(), key=lambda kv: str(kv[0])
        ):
            from_origin = [piece for piece in pool if piece.origin_id == origin_id]
            got = _take_upto(from_origin, wanted)
            if _qty_of(got) < wanted:
                raise _blocked(available, qty, quarantined=quarantined)
            taken.extend(got)
            pool = _without(pool, got)
        rest = qty - _qty_of(taken)
        got = _take_upto(pool, rest)
        if _qty_of(got) < rest:
            raise _blocked(available, qty, quarantined=quarantined)
        taken.extend(got)
        chosen[line_key] = taken
    if not chosen:
        raise _invalid("A dispatch carries at least one line.", "lines")
    return chosen


def _take_upto(pool: Sequence[FrozenPortion], qty: int) -> list[FrozenPortion]:
    """Up to ``qty`` pieces from the front of ``pool``, oldest first; ``pool`` is left alone."""
    taken: list[FrozenPortion] = []
    left = qty
    for piece in pool:
        if left <= 0:
            break
        size = min(left, ranges.length(piece.interval))
        lower = piece.interval[0]
        taken.append(replace(piece, interval=(lower, lower + size)))
        left -= size
    return taken


def _blocked(available: _Outstanding, qty: int, *, quarantined: bool = False) -> Refusal:
    if quarantined:
        # Goods ticket 13D: only goods still held in quarantine leave on this movement.
        return Refusal(
            "RESERVATION_BLOCKED",
            f"{available.held_qty} of the reserved piece(s) on that line are no longer held in "
            f"the source's quarantine, so the {qty} scanned cannot all go on a quarantine "
            "transfer. Start the shipment again with what is still quarantined, or cancel the "
            "outstanding balance.",
        )
    # E146 step 15: reserved portions must still be free of safety holds.
    return Refusal(
        "RESERVATION_BLOCKED",
        f"{available.held_qty} of the reserved piece(s) on that line are on hold - reported "
        f"damaged or otherwise held - and cannot leave, so the {qty} scanned cannot all go. "
        "Start the shipment again with what can leave, cancel the outstanding balance, or "
        "wait for the hold to be resolved.",
    )


def _without(pool: list[FrozenPortion], gone: list[FrozenPortion]) -> list[FrozenPortion]:
    out: list[FrozenPortion] = []
    for piece in pool:
        left = [piece.interval]
        for taken in gone:
            if taken.lot_id == piece.lot_id:
                left = ranges.subtract(left, [taken.interval])
        out.extend(replace(piece, interval=interval) for interval in left)
    return out


# ---------------------------------------------------------------------------
# The destination count: P09 ashore, P10 held
# ---------------------------------------------------------------------------

COUNT_FIELDS = frozenset({"counted_at", "lines", "excess", "note"})
COUNT_LINE_FIELDS = frozenset({"line_key", *COUNT_CONDITIONS})


def parse_count(body: dict[str, Any], now: datetime) -> dict[str, Any]:
    _closed(body, COUNT_FIELDS)
    raw = body.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LINES:
        raise _invalid(f"A count has 1 to {MAX_LINES} lines.", "lines")
    lines = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("Every count line is an object.", "lines")
        _closed(item, COUNT_LINE_FIELDS)
        counts = {name: _qty(item.get(name, 0), name, minimum=0) for name in COUNT_CONDITIONS}
        lines.append({"line_key": _uuid(item.get("line_key"), "line_key"), **counts})
    from outbound.transfer_excess import parse_entries

    excess = parse_entries(body.get("excess"))
    return {
        "counted_at": _moment(body.get("counted_at"), "counted_at", now),
        "lines": lines,
        "excess": excess,
        "note": _text(body.get("note"), "note", limit=500, required=False),
    }


def count(
    run: CommandRun, transfer_id: uuid.UUID, dispatch_id: uuid.UUID, parsed: dict[str, Any]
) -> TransferDispatch:
    """Account for one whole shipment at the destination, exactly as it was found.

    Good pieces come ashore into the destination's receiving location - arrived,
    not accepted, not sellable. Damaged, wrong and unidentified pieces go to the
    destination's quarantine and stay held; damage also opens a report for a
    second person, the same report reporting damage anywhere else opens. Missing
    pieces stay in transit as an explicit, owned shortage: a count never turns
    absent goods into quarantined ones and never short-closes a shipment.

    A quarantine transfer's shipment (goods ticket 13D) comes ashore into the
    destination's quarantine, all of it. ``good`` then means "arrived as it
    left": the piece keeps the condition it travelled in and every hold it
    left under, and nothing is waiting to be put away, so the shipment is
    accounted for at once. ``damaged``, ``wrong`` and ``unidentified`` are new
    observations at arrival and add their own hold, exactly as on an ordinary
    shipment. Nothing is released, accepted or made available by arriving.
    """
    transfer, _head = _locked(run, transfer_id)
    record = _locked_dispatch(run, transfer, dispatch_id)
    quarantined = held_custody(transfer)
    if record.state != TransferDispatch.State.IN_TRANSIT:
        raise _state_conflict(
            "This shipment has already been counted. One dispatch is received once, "
            "as a whole shipment."
        )
    from outbound import transfer_excess

    plan_lines = _plan_count(record, parsed["lines"])
    transfer_excess.check_entries(
        run, record, [row for row, _moves in plan_lines], parsed["excess"]
    )
    engine.lock_lots(
        run, sorted({piece.lot_id for _row, moves in plan_lines for piece, _c in moves}, key=str)
    )
    version = _pt_version(transfer)
    destination = transfer.destination_site_id
    receiving = engine.system_location(destination, "receiving")
    quarantine = engine.system_location(destination, "quarantine")
    plan = engine.Plan(CHECK_POSTING, version.pk, engine.event_key("transfer_count", record.pk))
    damaged: list[dict[str, Any]] = []
    for _row, moves in plan_lines:
        for piece, condition in moves:
            location = receiving if condition == "good" and not quarantined else quarantine
            engine.change_address(
                run,
                plan,
                piece.lot_id,
                piece.interval,
                lambda old, site=destination, loc=location.pk, cond=condition: replace(
                    old,
                    boundary="physical",
                    site_id=site,
                    location_id=loc,
                    transfer_id=None,
                    # Arrived as it left keeps the condition it left in; a new
                    # observation at arrival is never an upgrade.
                    condition=old.condition if quarantined and cond == "good" else cond,
                    accepted_event_id=None,
                ),
            )
            if condition == "good":
                continue
            key = uuid.uuid5(record.pk, f"{condition}:{piece.lot_id}:{piece.interval[0]}")
            engine.place_hold(
                run,
                plan,
                lot_id=piece.lot_id,
                interval=piece.interval,
                hold_key=key,
                kind=HOLD_KIND[condition],
                site_id=destination,
                source_version_id=version.pk,
            )
            if condition == "damaged":
                damaged.append(
                    {
                        "line_key": str(_row["line_key"]),
                        "lot_id": str(piece.lot_id),
                        "sku_id": None,
                        "origin_id": str(piece.origin_id) if piece.origin_id else None,
                        "qty": ranges.length(piece.interval),
                        # Where a rejected report puts the pieces back: an
                        # ordinary arrival waiting to be accepted, which is what
                        # they would have been had nobody called them damaged -
                        # or, for a quarantine transfer, the quarantine they
                        # arrived into under their own holds (goods ticket 13D).
                        "source_location_id": str(quarantine.pk if quarantined else receiving.pk),
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
                )
    excess = transfer_excess.record(
        run, plan, transfer, record, parsed["excess"], parsed["counted_at"]
    )
    engine.post(run, version, plan)
    summary = _count_summary(record, plan_lines, parsed, excess, quarantined=quarantined)
    record.state = TransferDispatch.State.COUNTED
    record.arrived_at = record.arrived_at or parsed["counted_at"]
    record.counted_at = parsed["counted_at"]
    record.counted_by_id = run.principal.human_id
    record.count = summary
    fields = ["state", "arrived_at", "counted_at", "counted_by", "count"]
    if summary["good_total"] == 0:
        # Nothing arrived good, so there is nothing to put away: the shipment is
        # as accepted as it will ever be, and only its held pieces and any
        # shortage are left - each as its own owned work (goods ticket 14).
        record.state = TransferDispatch.State.ACCEPTED
        record.accepted_at = run.now
        fields.append("accepted_at")
    record.save(update_fields=fields)
    if damaged:
        _open_damage_report(run, record, destination, damaged, parsed["note"])
        _open_damage_work(run, record, destination, damaged)
    _open_shortage(run, transfer, record, summary)
    if quarantined:
        _hand_over_hold_work(run, transfer, record, plan_lines)
    _log(
        run,
        transfer,
        TransferEvent.Kind.CHECK,
        site_id=destination,
        details={"dispatch_id": str(record.pk), **summary},
        actual_at=parsed["counted_at"],
    )
    transfer_excess.open_work(run, transfer, record, excess)
    _close_if_settled(run, transfer, record)
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = destination
    run.audit_after = {"dispatch_id": str(record.pk), "state": record.state, **summary}
    return record


def _plan_count(
    record: TransferDispatch, asked: Sequence[dict[str, Any]]
) -> list[tuple[dict[str, Any], list[tuple[FrozenPortion, str]]]]:
    """Cut each line's transit pieces into what was found, in condition order.

    The shipment's own frozen lines say what left; the count says what arrived.
    Anything above what left is not this shipment's - it is excess, and excess is
    recorded separately with no source quantity invented for it.

    ``wrong`` pieces are expected pieces that did not come: something else came
    in their place (goods ticket 16; goods PRD §5.7, design §7.3). They are cut
    from nothing - the expected pieces stay in transit, short, and what came
    instead is recorded as an excess observation of its own. An official line is
    never given another item's identity.
    """
    by_key = {uuid.UUID(str(line["line_key"])): line for line in record.lines}
    seen: set[uuid.UUID] = set()
    out: list[tuple[dict[str, Any], list[tuple[FrozenPortion, str]]]] = []
    for item in asked:
        key = item["line_key"]
        if key in seen:
            raise _invalid("Every count line names a different dispatched line.", "lines")
        seen.add(key)
        line = by_key.get(key)
        if line is None:
            raise Refusal("NOT_FOUND", "That line is not on this shipment.")
        dispatched = int(line["qty"])
        found = sum(int(item[name]) for name in COUNT_CONDITIONS)
        if found > dispatched:
            raise _refuse(
                f"This shipment carried {dispatched} piece(s) of that line and the count "
                f"says {found}. Goods that were not dispatched are recorded as excess, "
                "never as more of what was sent."
            )
        pool = _transit_pieces(record, line)
        moves: list[tuple[FrozenPortion, str]] = []
        for condition in ARRIVED_CONDITIONS:
            for piece in _take(pool, int(item[condition])):
                moves.append((piece, condition))
        out.append(
            (
                {
                    "line_key": key,
                    "dispatched": dispatched,
                    **{name: int(item[name]) for name in COUNT_CONDITIONS},
                },
                moves,
            )
        )
    missing = sorted(set(by_key) - seen)
    if missing:
        raise _refuse(
            "A shipment is counted whole. Every line it carried has to be answered, "
            "even if the answer is that none of it arrived."
        )
    return out


def _transit_pieces(record: TransferDispatch, line: dict[str, Any]) -> list[FrozenPortion]:
    """This line's pieces that are genuinely still in transit under this transfer."""
    pool: list[FrozenPortion] = []
    for piece in line["portions"]:
        lot_id, interval = _lot(piece), _interval(piece)
        standing = [
            bounds(p.portion)
            for p in engine.positions_of(lot_id, interval)
            if p.boundary == "transit" and p.transfer_id == record.transfer_id
        ]
        for still in ranges.intersect(standing, [interval]):
            pool.append(
                FrozenPortion(
                    lot_id=lot_id,
                    interval=still,
                    origin_id=_optional_uuid(piece.get("origin_id"), "origin_id"),
                    location_id=_optional_uuid(
                        piece.get("source_location_id"), "source_location_id"
                    ),
                    description="",
                )
            )
    return sorted(pool, key=lambda p: (str(p.lot_id), p.interval[0]))


def _take(pool: list[FrozenPortion], qty: int) -> list[FrozenPortion]:
    taken: list[FrozenPortion] = []
    left = qty
    while left > 0:
        if not pool:
            raise _refuse(
                "The count adds up to more pieces than are still in transit for this shipment."
            )
        piece = pool[0]
        size = ranges.length(piece.interval)
        if size <= left:
            taken.append(pool.pop(0))
            left -= size
            continue
        lower = piece.interval[0]
        taken.append(replace(piece, interval=(lower, lower + left)))
        pool[0] = replace(piece, interval=(lower + left, piece.interval[1]))
        left = 0
    return taken


def _count_summary(
    record: TransferDispatch,
    plan_lines: Sequence[tuple[dict[str, Any], list[tuple[FrozenPortion, str]]]],
    parsed: dict[str, Any],
    excess: list[dict[str, Any]],
    *,
    quarantined: bool = False,
) -> dict[str, Any]:
    lines = []
    for row, _moves in plan_lines:
        # Wrong pieces are not among what arrived: they are short-expected
        # (goods ticket 16), and what came instead is in ``excess``.
        found = sum(int(row[name]) for name in ARRIVED_CONDITIONS)
        lines.append(
            {
                "line_key": str(row["line_key"]),
                "dispatched": row["dispatched"],
                **{name: int(row[name]) for name in COUNT_CONDITIONS},
                "short": row["dispatched"] - found,
            }
        )
    return {
        "counted_at": parsed["counted_at"].isoformat(),
        "note": parsed["note"],
        "lines": lines,
        # A quarantine transfer's pieces all arrive held, "arrived as sent"
        # included: none is good goods waiting to be put away (goods ticket 13D).
        "good_total": 0 if quarantined else sum(int(line["good"]) for line in lines),
        "held_total": sum(
            int(line[name])
            for line in lines
            for name in (ARRIVED_CONDITIONS if quarantined else ARRIVAL_HELD_CONDITIONS)
        ),
        "short_total": sum(int(line["short"]) for line in lines),
        "wrong_total": sum(int(line["wrong"]) for line in lines),
        "excess": excess,
        "dispatch_sequence_no": record.sequence_no,
    }


def _open_damage_report(
    run: CommandRun,
    record: TransferDispatch,
    site_id: int,
    lines: list[dict[str, Any]],
    note: str | None,
) -> None:
    """Damage at arrival opens the same pending report damage anywhere else opens."""
    from outbound.damage_review import open_for_dispatch

    open_for_dispatch(
        run,
        dispatch=record,
        site_id=site_id,
        reason_code=(note or "TRANSFER_ARRIVAL_DAMAGE")[:60],
        lines=lines,
    )


def _open_damage_work(
    run: CommandRun, record: TransferDispatch, site_id: int, lines: list[dict[str, Any]]
) -> None:
    """Arrival damage is owned work with its follow-up due date (goods ticket 14).

    The same ``stock_hold_active`` work damage reported on the stock screen
    opens, one per hold, with no resolution action offered: confirmed damage may
    stay in quarantine indefinitely, so the due date is a follow-up, never a
    pickup or disposal deadline (goods PRD §14.9.2). Rejecting the report closes
    it (``damage_review._close_hold_exceptions``).
    """
    from outbound.goods_movements import HOLD_EXCEPTION

    for line in lines:
        for key in line["hold_keys"]:
            open_exception(
                run,
                kind=HOLD_EXCEPTION,
                site_id=site_id,
                subject_key=f"hold:{key}",
                reason_code="TRANSFER_ARRIVAL_DAMAGE",
                source_event_key=engine.event_key("transfer_arrival_hold", record.pk, key),
                allowed_resolution_actions=[],
                note=f"transfer_dispatch:{record.pk}",
            )


def _hand_over_hold_work(
    run: CommandRun,
    transfer: GoodsTransfer,
    record: TransferDispatch,
    plan_lines: Sequence[tuple[dict[str, Any], list[tuple[FrozenPortion, str]]]],
) -> None:
    """A quarantine shipment's owned hold work moves to the site now holding the goods.

    Goods ticket 13D. The holds themselves travel with the pieces untouched;
    what must follow them is the owned work each hold has in the shared centre
    (``hold:{key}``), so that nobody at the source is left owning goods it no
    longer has and the destination owns what now stands in its quarantine. The
    destination's work opens once per hold and movement. The source's closes
    (``MOVED_BY_TRANSFER``) only when nothing under that hold still stands at
    the source - part of a hold may have stayed behind, and that part is still
    the source's work. Nothing about the hold, its condition or its damage
    report is decided by the move.
    """
    from alerts.goods_models import GoodsException

    arrived = [(piece.lot_id, piece.interval) for _row, moves in plan_lines for piece, _c in moves]
    keys: set[uuid.UUID] = set()
    for lot_id, interval in arrived:
        keys.update(
            ActiveHold.objects.filter(
                lot_id=lot_id, portion__overlap=portion(*interval)
            ).values_list("hold_key", flat=True)
        )
    source, destination = transfer.source_site_id, transfer.destination_site_id
    for key in sorted(keys, key=str):
        subject = f"hold:{key}"
        at_source = list(
            GoodsException.objects.filter(
                tenant_id=run.tenant_id, subject_key=subject, site_id=source, state="open"
            )
        )
        for work in at_source:
            open_exception(
                run,
                kind=work.kind,
                site_id=destination,
                subject_key=subject,
                reason_code="QUARANTINE_TRANSFER_ARRIVAL",
                source_event_key=engine.event_key("quarantine_transfer_hold", transfer.pk, key),
                allowed_resolution_actions=list(work.allowed_resolution_actions),
                note=f"transfer_dispatch:{record.pk}",
            )
            if _still_held_at(key, source):
                continue
            resolve_exceptions(
                run,
                kind=work.kind,
                subject_key=subject,
                reason_code="MOVED_BY_TRANSFER",
                source_event_key=work.source_event_key,
            )


def _still_held_at(hold_key: uuid.UUID, site_id: int) -> bool:
    """Does any piece under this hold still stand physically at ``site_id``?"""
    for hold in ActiveHold.objects.filter(hold_key=hold_key):
        interval = bounds(hold.portion)
        if any(
            p.boundary == "physical" and p.site_id == site_id
            for p in engine.positions_of(hold.lot_id, interval)
        ):
            return True
    return False


def _open_shortage(
    run: CommandRun, transfer: GoodsTransfer, record: TransferDispatch, summary: dict[str, Any]
) -> None:
    """A shortage is owned work with a name on it, not a number in a payload."""
    if summary["short_total"] <= 0:
        return
    open_exception(
        run,
        kind=SHORTAGE_EXCEPTION,
        site_id=transfer.destination_site_id,
        subject_key=f"transfer_dispatch:{record.pk}",
        reason_code="TRANSIT_SHORTAGE",
        source_event_key=engine.event_key("transfer_shortage", record.pk),
        allowed_resolution_actions=RESOLUTION_ACTIONS,
        note=f"transfer:{transfer.pk}",
    )


# ---------------------------------------------------------------------------
# Acceptance at the destination: P09, the putaway
# ---------------------------------------------------------------------------

ACCEPT_FIELDS = frozenset({"lines"})
ACCEPT_LINE_FIELDS = frozenset({"line_key", "qty", "destination_location_id"})


def parse_accept(body: dict[str, Any]) -> list[dict[str, Any]]:
    _closed(body, ACCEPT_FIELDS)
    raw = body.get("lines")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LINES:
        raise _invalid(f"An acceptance has 1 to {MAX_LINES} lines.", "lines")
    lines = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("Every acceptance line is an object.", "lines")
        _closed(item, ACCEPT_LINE_FIELDS)
        lines.append(
            {
                "line_key": _uuid(item.get("line_key"), "line_key"),
                "qty": _qty(item.get("qty"), "qty"),
                "destination_location_id": _uuid(
                    item.get("destination_location_id"), "destination_location_id"
                ),
            }
        )
    return lines


def accept(
    run: CommandRun, transfer_id: uuid.UUID, dispatch_id: uuid.UUID, lines: list[dict[str, Any]]
) -> TransferDispatch:
    """Put the good arrived pieces away, and only then are they sellable here.

    The origin, cost and identity each piece arrived with are untouched: a
    transfer creates no new purchase value. What acceptance adds is the
    destination's own evidence that somebody physically put these pieces in this
    location - which is the thing overall PRD R-INV-002 requires before anything
    can be sold.
    """
    transfer, _head = _locked(run, transfer_id)
    record = _locked_dispatch(run, transfer, dispatch_id)
    if record.state != TransferDispatch.State.COUNTED:
        raise _state_conflict("Only a counted shipment has goods to accept.")
    site_id = transfer.destination_site_id
    accepted, remaining = put_away(
        run, transfer, record, lines, site_id=site_id, event_name="transfer_accept"
    )
    if remaining == 0:
        record.state = TransferDispatch.State.ACCEPTED
        record.accepted_at = run.now
        record.save(update_fields=["state", "accepted_at"])
    _log(
        run,
        transfer,
        TransferEvent.Kind.ACCEPT,
        site_id=site_id,
        details={
            "dispatch_id": str(record.pk),
            "quantity": accepted,
            "still_to_accept": remaining,
        },
    )
    _close_if_settled(run, transfer, record)
    run.audit_subject_key = f"transfer:{transfer.pk}"
    run.audit_site_id = site_id
    run.audit_after = {
        "dispatch_id": str(record.pk),
        "state": record.state,
        "accepted_qty": accepted,
    }
    return record


def put_away(
    run: CommandRun,
    transfer: GoodsTransfer,
    record: TransferDispatch,
    lines: list[dict[str, Any]],
    *,
    site_id: int,
    event_name: str,
) -> tuple[int, int]:
    """Accept good, unaccepted pieces of one shipment standing in ``site_id``'s receiving.

    The destination after a count, or the source after a failed delivery came
    back (goods ticket 13C): either way the pieces are put in an ordinary
    storage location with that site's own acceptance evidence, and only then
    may they be sold or sent again. Returns what was accepted and what of the
    shipment still waits in that site's receiving.
    """
    receiving = engine.system_location(site_id, "receiving")
    plan_rows = _plan_accept(record, lines, site_id, receiving)
    engine.lock_lots(run, sorted({p.lot_id for _row, pieces in plan_rows for p in pieces}, key=str))
    version = _pt_version(transfer)
    session = _acceptance_session(run, site_id, version)
    plan = engine.Plan(
        CHECK_POSTING, version.pk, engine.event_key(event_name, record.pk, run.key_id)
    )
    accepted = 0
    for row, pieces in plan_rows:
        for piece in pieces:
            event = _acceptance_event(run, session, site_id, piece, row["destination_location_id"])
            engine.change_address(
                run,
                plan,
                piece.lot_id,
                piece.interval,
                lambda old, loc=row["destination_location_id"], ev=event.pk: replace(
                    old, location_id=loc, accepted_event_id=ev
                ),
            )
            accepted += ranges.length(piece.interval)
    engine.post(run, version, plan)
    return accepted, _unaccepted_good(record, site_id, receiving)


def _plan_accept(
    record: TransferDispatch,
    asked: Sequence[dict[str, Any]],
    site_id: int,
    receiving: Any,
) -> list[tuple[dict[str, Any], list[FrozenPortion]]]:
    out: list[tuple[dict[str, Any], list[FrozenPortion]]] = []
    by_key = {uuid.UUID(str(line["line_key"])): line for line in record.lines}
    seen: set[uuid.UUID] = set()
    for item in asked:
        key = item["line_key"]
        if key in seen:
            raise _invalid("Every acceptance line names a different dispatched line.", "lines")
        seen.add(key)
        line = by_key.get(key)
        if line is None:
            raise Refusal("NOT_FOUND", "That line is not on this shipment.")
        destination = _putaway_location(site_id, item["destination_location_id"])
        pool = _arrived_good(record, line, site_id, receiving)
        available = sum(ranges.length(piece.interval) for piece in pool)
        if item["qty"] > available:
            raise _refuse(
                f"Only {available} good piece(s) of that line are waiting to be put away."
            )
        out.append(({"destination_location_id": destination.pk}, _take(pool, item["qty"])))
    return out


def _putaway_location(site_id: int, location_id: uuid.UUID) -> Location:
    location = Location.objects.filter(pk=location_id).first()
    if location is None:
        raise Refusal("NOT_FOUND", "That location was not found.")
    if location.site_id != site_id:
        raise _refuse("Goods are put away at the site that received them.")
    if location.retired_at is not None:
        raise _refuse("That location has been retired.")
    if location.system or location.kind not in engine.TRANSFERABLE_KINDS:
        raise _refuse(
            "Goods can only be put away in an ordinary storage location, never in a "
            "protected system location."
        )
    return location


def _arrived_good(
    record: TransferDispatch, line: dict[str, Any], site_id: int, receiving: Any
) -> list[FrozenPortion]:
    """This line's pieces standing good and unaccepted in the destination's receiving."""
    pool: list[FrozenPortion] = []
    for piece in line["portions"]:
        lot_id, interval = _lot(piece), _interval(piece)
        for position in engine.positions_of(lot_id, interval):
            if (
                position.boundary != "physical"
                or position.site_id != site_id
                or position.location_id != receiving.pk
                or position.condition != "good"
                or position.accepted_event_id is not None
            ):
                continue
            for still in ranges.intersect([bounds(position.portion)], [interval]):
                pool.append(
                    FrozenPortion(
                        lot_id=lot_id,
                        interval=still,
                        origin_id=_optional_uuid(piece.get("origin_id"), "origin_id"),
                        location_id=receiving.pk,
                        description="",
                    )
                )
    return sorted(pool, key=lambda p: (str(p.lot_id), p.interval[0]))


def _unaccepted_good(record: TransferDispatch, site_id: int, receiving: Any) -> int:
    return sum(
        ranges.length(piece.interval)
        for line in record.lines
        for piece in _arrived_good(record, line, site_id, receiving)
    )


def _acceptance_session(
    run: CommandRun, site_id: int, version: OfficialVersion
) -> AcceptanceSession:
    """One open acceptance session per transfer PT version at the destination.

    The same record a receipt's acceptance uses, so "who accepted these pieces,
    where and when" is one kind of evidence in the system rather than two. It is
    not the receipt acceptance *flow*: a transfer PT is not a receipt or opening
    PT, so it never appears in the pending-acceptance queue those screens read.
    """
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
    piece: FrozenPortion,
    location_id: uuid.UUID,
) -> AcceptanceEvent:
    event: AcceptanceEvent = run.record(
        AcceptanceEvent(
            session_id=session.pk,
            site_id=site_id,
            scan_key=uuid.uuid5(session.pk, f"{piece.lot_id}:{piece.interval[0]}"),
            official_line=None,
            lot_id=piece.lot_id,
            portion=portion(*piece.interval),
            destination_location_id=location_id,
            observed_alias="transfer arrival",
            observed_ticket_mrp=None,
            label_evidence_ref=None,
            tag_verdict="matched",
            outcome=AcceptanceEvent.Outcome.ACCEPTED_GOOD,
        )
    )
    return event


# ---------------------------------------------------------------------------
# Closing the movement
# ---------------------------------------------------------------------------


def damage_report_rejected(run: CommandRun, report: Any, record: TransferDispatch) -> None:
    """A damage report from this shipment's count was rejected (goods ticket 12B).

    The rejection lifted only the damage: the pieces are back in the
    destination's receiving location, good, arrived and not accepted - exactly
    what they would have been had nobody called them damaged. Acceptance is
    still the only way from there to the shelf, so a shipment whose other
    pieces were already put away is open for acceptance again. The transfer's
    own state is left as it is: its shipment was received and accounted for
    (transfers PRD §7); what is left is putting the pieces away. The transfer's
    history records the correction beside the count it corrects.

    A report from a failed delivery's return (goods ticket 13C) was raised at
    the source, and the rejection puts the pieces in the source's receiving
    instead. They wait there to be accepted back like any returned good piece;
    the shipment's own state - what came back and what is still missing - is
    untouched by it.
    """
    transfer = GoodsTransfer.objects.get(pk=record.transfer_id)
    site_id = int(report.site_id)
    receiving = engine.system_location(site_id, "receiving")
    waiting = _unaccepted_good(record, site_id, receiving)
    reopened = (
        site_id == transfer.destination_site_id
        and record.state == TransferDispatch.State.ACCEPTED
        and waiting > 0
    )
    if reopened:
        record.state = TransferDispatch.State.COUNTED
        record.save(update_fields=["state"])
    _log(
        run,
        transfer,
        TransferEvent.Kind.DAMAGE_REJECTED,
        site_id=site_id,
        details={
            "dispatch_id": str(record.pk),
            "damage_report_id": str(report.pk),
            "quantity": int(report.quantity),
            "release_movement_id": str(report.release_movement_id),
            "waiting_to_be_accepted": waiting,
            "reopened_for_acceptance": reopened,
        },
    )


def _close_if_settled(run: CommandRun, transfer: GoodsTransfer, record: TransferDispatch) -> None:
    record.refresh_from_db()
    _settle(run, transfer)


def _settle(run: CommandRun, transfer: GoodsTransfer) -> None:
    """The movement closes only when every piece is accounted for and none is reserved.

    "Accounted for" is deliberately strict (transfers PRD §7): a shipment whose
    shortage no approved correction has resolved is not accounted for (goods
    ticket 14), and neither is one whose
    good pieces are still waiting to be put away. Cancelling the undispatched
    balance closes nothing that is still open.

    One case reaches the other way (goods ticket 12B): a damage report from the
    arrival count rejected *after* the transfer completed puts pieces back in
    receiving to be accepted. The transfer stays completed - its shipments were
    received and accounted for when it closed, and reopening a completed
    movement is Anand's to rule on - while that one dispatch waits, as
    ``counted``, for the pieces to be put away.
    """
    from outbound.transfer_shortages import unresolved_qty

    if transfer.state in (GoodsTransfer.State.COMPLETED, GoodsTransfer.State.CANCELLED):
        return
    version = _pt_version(transfer, required=False)
    if version is None:
        return
    if ActiveReservation.objects.filter(transfer_version_id=version.pk).exists():
        return
    dispatches = list(TransferDispatch.objects.filter(transfer=transfer))
    if not dispatches:
        # Approved, then the whole balance cancelled before anything left.
        transfer.state = GoodsTransfer.State.CANCELLED
        transfer.save(update_fields=["state"])
        return
    for record in dispatches:
        if record.state == TransferDispatch.State.RETURNED_TO_SOURCE:
            continue
        if record.state != TransferDispatch.State.ACCEPTED:
            return
        # A shortage counts as accounted for only once an approved correction
        # has resolved it (transfers PRD §5, goods ticket 14).
        if unresolved_qty(record) > 0:
            return
    transfer.state = GoodsTransfer.State.COMPLETED
    transfer.arrived_at = transfer.arrived_at or run.now
    transfer.save(update_fields=["state", "arrived_at"])
    _log(
        run,
        transfer,
        TransferEvent.Kind.COMPLETED,
        site_id=transfer.destination_site_id,
        details={"dispatches": len(dispatches)},
    )
    resolve_exceptions(
        run, kind=SHORTAGE_EXCEPTION, subject_key=f"transfer:{transfer.pk}", reason_code="SETTLED"
    )


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _lot(piece: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(str(piece["lot_id"]))


def _interval(piece: dict[str, Any]) -> ranges.Interval:
    return (int(piece["lower"]), int(piece["upper"]))


def transfer_of(tenant_id: uuid.UUID, transfer_id: uuid.UUID) -> GoodsTransfer:
    transfer = (
        GoodsTransfer.objects.select_related("document")
        .filter(tenant_id=tenant_id, pk=transfer_id)
        .first()
    )
    if transfer is None:
        raise Refusal("NOT_FOUND", "That transfer was not found.")
    return transfer


def _locked(
    run: CommandRun, transfer_id: uuid.UUID, *, at: str | None = None
) -> tuple[GoodsTransfer, DocumentHead]:
    """The transfer, its head, and both site guards - locked in the fixed order.

    Which two sites to guard is read without a lock first, because SITE ranks
    above DOCUMENT and asking afterwards would be asking for a rank the order
    has already closed. The transfer row is then re-read under its own lock, so
    nothing is decided from the unlocked read but the pair of site ids, which a
    transfer never changes. ``at`` names the one site whose goods fence (count
    freeze, readiness) a step that happens only there must pass; by default
    both must, as for a dispatch or a count, which change stock at both ends.
    """
    known = GoodsTransfer.objects.filter(pk=transfer_id).first()
    if known is None:
        raise Refusal("NOT_FOUND", "That transfer was not found.")
    fenced = {
        None: None,
        "source": [known.source_site_id],
        "destination": [known.destination_site_id],
    }[at]
    lock_sites(run, [known.source_site_id, known.destination_site_id], fenced=fenced)
    rows: list[GoodsTransfer] = run.lock(
        LockRank.DOCUMENT, GoodsTransfer.objects.filter(pk=transfer_id)
    )
    if not rows:
        raise Refusal("NOT_FOUND", "That transfer was not found.")
    transfer = rows[0]
    if transfer.state == GoodsTransfer.State.CANCELLED:
        raise _state_conflict("This transfer was cancelled.")
    head = lock_heads(run, [transfer.document_id])[transfer.document_id]
    transfer.document = head.document
    return transfer, head


def _locked_dispatch(
    run: CommandRun, transfer: GoodsTransfer, dispatch_id: uuid.UUID
) -> TransferDispatch:
    rows: list[TransferDispatch] = run.lock(
        LockRank.DOCUMENT, TransferDispatch.objects.filter(pk=dispatch_id, transfer=transfer)
    )
    if not rows:
        raise Refusal("NOT_FOUND", "That shipment was not found.")
    return rows[0]


def _pt_of(transfer: GoodsTransfer) -> GoodsPt:
    goods_pt = GoodsPt.objects.select_related("document").filter(transfer=transfer).first()
    if goods_pt is None:
        raise _state_conflict("This transfer has not been sent for approval yet.")
    return goods_pt


def _pt_version(transfer: GoodsTransfer, *, required: bool = True) -> Any:
    """The movement's live approved version: its transfer PT's, or for a pre-PT
    custody transfer (goods ticket 13E) its own document's."""
    head = _plan_head(transfer)
    version = head.live_version if head is not None else None
    if version is None and required:
        raise _state_conflict("This transfer has not been approved yet.")
    return version


def _plan_head(transfer: GoodsTransfer) -> DocumentHead | None:
    """The head of the document that carries the movement's plan.

    The transfer PT, once one exists; for a pre-PT custody transfer (goods
    ticket 13E), which never has one, the transfer's own document.
    """
    if transfer.custody == PRE_PT:
        document_id = transfer.document_id
    else:
        goods_pt = GoodsPt.objects.filter(transfer=transfer).first()
        if goods_pt is None:
            return None
        document_id = goods_pt.document_id
    head: DocumentHead | None = (
        DocumentHead.objects.select_related("live_version", "draft_revision")
        .filter(document_id=document_id)
        .first()
    )
    return head


def plan_lines(transfer: GoodsTransfer) -> list[dict[str, Any]]:
    """What the movement is for, as its detail shows it (approved, frozen or drafted)."""
    head = _plan_head(transfer)
    return _detail_lines(transfer, head)


def _log(
    run: CommandRun,
    transfer: GoodsTransfer,
    kind: str,
    *,
    site_id: int,
    details: dict[str, Any],
    actual_at: datetime | None = None,
) -> TransferEvent:
    at = actual_at or run.now
    event: TransferEvent = run.record(
        TransferEvent(
            transfer_id=transfer.pk,
            site_id=site_id,
            kind=kind,
            actual_at=at,
            details=details,
        ),
        event_at=at,
    )
    return event


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def visible_transfers(
    tenant_id: uuid.UUID,
    site_ids: frozenset[int] | None,
    *,
    site_id: int | None = None,
    direction: str | None = None,
    state: str | None = None,
) -> list[GoodsTransfer]:
    from django.db.models import Q

    queryset = GoodsTransfer.objects.select_related(
        "document", "source_site", "destination_site"
    ).filter(tenant_id=tenant_id)
    if site_ids is not None:
        queryset = queryset.filter(
            Q(source_site_id__in=sorted(site_ids)) | Q(destination_site_id__in=sorted(site_ids))
        )
    if site_id is not None:
        if direction == "out":
            queryset = queryset.filter(source_site_id=site_id)
        elif direction == "in":
            queryset = queryset.filter(destination_site_id=site_id)
        else:
            queryset = queryset.filter(Q(source_site_id=site_id) | Q(destination_site_id=site_id))
    if state:
        queryset = queryset.filter(state=state)
    return list(queryset.order_by("-document__created_at", "-id"))


def summary_dto(transfer: GoodsTransfer, dispatches: Sequence[TransferDispatch]) -> dict[str, Any]:
    mine = [d for d in dispatches if d.transfer_id == transfer.pk]
    return {
        "id": str(transfer.pk),
        "record_contract": "goods-v1",
        "number": transfer.document.official_number,
        "state": transfer.state,
        "source_site_id": str(transfer.source_site_id),
        "destination_site_id": str(transfer.destination_site_id),
        # Goods ticket 13D: a quarantine transfer moves held goods and never
        # makes them available, at either end.
        "custody": transfer.custody,
        # Both ends by name and code, so a person who can read the transfer can
        # say where it goes without being able to read the other site (§7, Anand,
        # 25 September 2026: being offered a site is not reading it).
        "source_site": site_ref(transfer.source_site),
        "destination_site": site_ref(transfer.destination_site),
        "created_at": transfer.document.created_at.isoformat(),
        "dispatched_at": transfer.dispatched_at.isoformat() if transfer.dispatched_at else None,
        "arrived_at": transfer.arrived_at.isoformat() if transfer.arrived_at else None,
        "dispatch_count": len(mine),
        # Everything still on the road: a whole shipment in transit, what a
        # failed delivery's return has not yet brought back (goods ticket 13C),
        # and a counted shipment's missing pieces until an approved shortage
        # correction resolves them (goods ticket 14) - pending approval they
        # stay here, visibly.
        "in_transit_qty": sum(on_the_road(record) for record in mine),
        "short_qty": sum(int((d.count or {}).get("short_total") or 0) for d in mine),
        "shortage_resolved_qty": _resolved_total(mine),
        # Goods ticket 16: a corrective transfer names the movement whose
        # observed excess it corrects.
        "corrective_for_id": (
            str(transfer.corrective_for_id) if transfer.corrective_for_id else None
        ),
    }


def _resolved_total(records: Sequence[TransferDispatch]) -> int:
    from outbound.transfer_shortages import resolved_qty

    return sum(resolved_qty(record) for record in records if record.count)


def shipped_qty(record: TransferDispatch) -> int:
    return sum(int(line["qty"]) for line in record.lines)


def on_the_road(record: TransferDispatch) -> int:
    """What of one shipment is in transit now.

    All of it until it is counted or anything comes back; after a count, only
    what never arrived and no approved shortage correction has resolved (an
    explicit shortage stays in transit until then); after a return,
    whatever the return receipts have not brought back - nothing once the
    whole shipment is home.
    """
    if record.state == TransferDispatch.State.IN_TRANSIT:
        return shipped_qty(record)
    if record.state in (
        TransferDispatch.State.PARTLY_RETURNED,
        TransferDispatch.State.RETURNED_TO_SOURCE,
    ):
        return shipped_qty(record) - int(record.returned_qty)
    # Counted: what never arrived and no approved shortage has resolved yet
    # (goods ticket 14). A pending proposal leaves it here, visibly in transit.
    from outbound.transfer_shortages import unresolved_qty

    return unresolved_qty(record)


def detail_dto(
    transfer: GoodsTransfer, *, names: dict[uuid.UUID, str], allowed: list[str]
) -> dict[str, Any]:
    goods_pt = GoodsPt.objects.select_related("document").filter(transfer=transfer).first()
    head = _plan_head(transfer)
    from outbound.dispatch_preparation import open_summary
    from outbound.transfer_excess import corrective_dto, excess_qty
    from outbound.transfer_shortages import movement_reconciliation

    dispatches = list(TransferDispatch.objects.filter(transfer=transfer).order_by("sequence_no"))
    version = head.live_version if head is not None else None
    lines = _detail_lines(transfer, head)
    approved = sum(int(line.get("qty") or 0) for line in lines) if version is not None else 0
    cancelled = sum(
        int((event.details or {}).get("quantity") or 0)
        for event in TransferEvent.objects.filter(
            transfer=transfer, kind=TransferEvent.Kind.CANCELLED
        )
    )
    reserved: dict[str, int] = {}
    if version is not None:
        for row in ActiveReservation.objects.filter(transfer_version_id=version.pk):
            reserved[str(row.lot_id)] = reserved.get(str(row.lot_id), 0) + ranges.length(
                bounds(row.portion)
            )
    return {
        **summary_dto(transfer, dispatches),
        "pt_id": str(goods_pt.document_id) if goods_pt is not None else None,
        "pt_number": goods_pt.document.official_number if goods_pt is not None else None,
        # A pre-PT custody transfer (goods ticket 13E) has no transfer PT: its
        # plan is on its own document, and these three stay null.
        "pt_state": head.state if head is not None and goods_pt is not None else None,
        "drafted_by": {
            "id": str(transfer.document.maker_id),
            "name": names.get(transfer.document.maker_id, ""),
        },
        "approved_by": (
            {"id": str(version.approved_by_id), "name": names.get(version.approved_by_id, "")}
            if version is not None
            else None
        ),
        "lines": lines,
        "reserved_qty": sum(reserved.values()),
        "approved_qty": approved,
        "dispatched_qty": sum(shipped_qty(d) for d in dispatches),
        "cancelled_qty": cancelled,
        "returned_qty": sum(int(d.returned_qty) for d in dispatches),
        # Goods ticket 14: the undispatched balance reconciles on its own,
        # separately from each shipment's own conservation.
        "reconciliation": movement_reconciliation(
            approved=approved,
            dispatched=sum(shipped_qty(d) for d in dispatches),
            reserved=sum(reserved.values()),
            cancelled=cancelled,
        ),
        "dispatch_preparation": open_summary(transfer),
        # Goods ticket 16: goods observed that nobody sent (excess and wrong
        # goods), beside - never inside - what was dispatched.
        "excess_qty": excess_qty(dispatches),
        **corrective_dto(transfer, names),
        "dispatches": [dispatch_dto(record, names, transfer) for record in dispatches],
        "events": [event_dto(event) for event in _events(transfer)],
        "allowed_actions": allowed,
    }


def _detail_lines(transfer: GoodsTransfer, head: DocumentHead | None) -> list[dict[str, Any]]:
    """What the movement is for, from the official PT if there is one, else the draft."""
    from core.goods_documents import revision_lines
    from core.kernel_models import OfficialLine

    if head is not None and head.live_version is not None:
        rows = [
            dict(line.payload)
            for line in OfficialLine.objects.filter(version_id=head.live_version_id).order_by(
                "line_no"
            )
        ]
    elif head is not None and head.draft_revision is not None:
        rows = [
            dict(state.payload)
            for state in revision_lines(head.document_id, head.draft_revision.revision)
        ]
    else:
        transfer_head = DocumentHead.objects.select_related("draft_revision").get(
            document_id=transfer.document_id
        )
        rows = (
            [
                dict(state.payload)
                for state in revision_lines(
                    transfer.document_id, transfer_head.draft_revision.revision
                )
            ]
            if transfer_head.draft_revision is not None
            else []
        )
    return rows


#: The stock-read action a value field grant must ride on, as on every stock read.
VALUE_READ = "stock.view"


def priced_lines(
    access: Any, transfer: GoodsTransfer, lines: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each frozen portion's origin unit cost and MRP, for a reader allowed to see them.

    Goods ticket 13A: the plan shows its origins with the value they were frozen
    at. Stock value of either kind - cost or ticket (MRP) - needs the ``cost``
    field grant at the source site for the line's brand (Anand, 15 September
    2026, the same rule stock search applies to its value bases). Without it the
    two properties are left out, never sent as a zero. Both are integer paise as
    a string, and ``None`` only for a portion that carries no origin.
    """
    from masters.goods_identity_models import ProductSku
    from stockledger.goods_models import Origin

    # A pre-PT line (goods ticket 13E) has no origin and no SKU: its value is
    # unknown, and it says so itself rather than carrying a null price.
    frozen = [line for line in lines if line.get("portions") and line.get("sku_id")]
    if not frozen or transfer.custody == PRE_PT:
        return lines
    brands = {
        str(pk): brand
        for pk, brand in ProductSku.objects.filter(
            pk__in={str(line["sku_id"]) for line in frozen}
        ).values_list("pk", "style__brand_id")
    }
    wanted = {
        str(piece["origin_id"])
        for line in frozen
        for piece in line["portions"]
        if piece.get("origin_id")
    }
    prices = {
        str(pk): (str(int(cost)), str(int(mrp)))
        for pk, cost, mrp in Origin.objects.filter(pk__in=sorted(wanted)).values_list(
            "pk", "unit_cost", "mrp"
        )
    }
    out: list[dict[str, Any]] = []
    for line in lines:
        brand = brands.get(str(line.get("sku_id")))
        if not line.get("portions") or "cost" not in access.field_grants(
            site_id=transfer.source_site_id, brand_id=brand, actions={VALUE_READ}
        ):
            out.append(line)
            continue
        portions = []
        for piece in line["portions"]:
            cost, mrp = prices.get(str(piece.get("origin_id")), (None, None))
            portions.append({**piece, "unit_cost_paise": cost, "mrp_paise": mrp})
        out.append({**line, "portions": portions})
    return out


def dispatch_dto(
    record: TransferDispatch, names: dict[uuid.UUID, str], transfer: GoodsTransfer | None = None
) -> dict[str, Any]:
    """One shipment: where from and to, what it carried from which origins, where it is now."""
    from outbound.goods_models import DispatchPreparation
    from outbound.transfer_documents import summary_dto
    from outbound.transfer_excess import shipment_excess
    from outbound.transfer_returns import returns_of
    from outbound.transfer_shipments import eway_dto
    from outbound.transfer_shortages import shipment_reading

    transfer = transfer or record.transfer
    quantity = shipped_qty(record)
    returned = returns_of(record, transfer, names)
    awaiting = _awaiting_putaway(record, transfer)
    preparation = (
        DispatchPreparation.objects.filter(dispatch=record).values_list("pk", flat=True).first()
    )
    return {
        "id": str(record.pk),
        "sequence_no": record.sequence_no,
        "state": record.state,
        "source_site_id": str(transfer.source_site_id),
        "destination_site_id": str(transfer.destination_site_id),
        "dispatched_at": record.dispatched_at.isoformat(),
        "recorded_by": {
            "id": str(record.recorded_by_id),
            "name": names.get(record.recorded_by_id, ""),
        },
        "transport": record.transport,
        "preparation_id": str(preparation) if preparation else None,
        "arrived_at": record.arrived_at.isoformat() if record.arrived_at else None,
        "arrival_recorded_by": (
            {
                "id": str(record.arrival_recorded_by_id),
                "name": names.get(record.arrival_recorded_by_id, ""),
            }
            if record.arrival_recorded_by_id
            else None
        ),
        "counted_at": record.counted_at.isoformat() if record.counted_at else None,
        "accepted_at": record.accepted_at.isoformat() if record.accepted_at else None,
        "returned_at": record.returned_at.isoformat() if record.returned_at else None,
        "return_reason": record.return_reason,
        "quantity": quantity,
        "in_transit_qty": on_the_road(record),
        "returned_qty": int(record.returned_qty),
        "returns": returned["receipts"],
        "lines": [
            {
                "line_key": line["line_key"],
                "sku_id": line["sku_id"],
                "qty": line["qty"],
                "origins": _origin_shares(line.get("portions") or []),
                "returned_qty": returned["by_line"].get(str(line["line_key"]), 0),
                "returned_awaiting_putaway": returned["waiting"].get(str(line["line_key"]), 0),
                "awaiting_putaway": awaiting.get(str(line["line_key"]), 0),
            }
            for line in record.lines
        ],
        "count": record.count or None,
        "eway": eway_dto(record),
        # Store operations ticket 36: the paper it left with, if any. No money.
        "document": summary_dto(record),
        # Goods ticket 16: each excess observation the count recorded, with its
        # corrective decisions and what is still unresolved.
        "excess_observations": shipment_excess(record, transfer, names),
        **shipment_reading(record, names),
    }


def _awaiting_putaway(record: TransferDispatch, transfer: GoodsTransfer) -> dict[str, int]:
    """Per line, the good arrived pieces still unaccepted in the destination's receiving."""
    if record.state != TransferDispatch.State.COUNTED:
        return {}
    site_id = transfer.destination_site_id
    receiving = engine.system_location(site_id, "receiving")
    return {
        str(line["line_key"]): sum(
            ranges.length(piece.interval)
            for piece in _arrived_good(record, line, site_id, receiving)
        )
        for line in record.lines
    }


def _origin_shares(portions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """How many pieces of one shipped line came from each origin. Never merged."""
    shares: dict[str | None, int] = {}
    for piece in portions:
        key = str(piece["origin_id"]) if piece.get("origin_id") else None
        shares[key] = shares.get(key, 0) + ranges.length(_interval(piece))
    return [{"origin_id": key, "qty": qty} for key, qty in sorted(shares.items(), key=str)]


def _events(transfer: GoodsTransfer) -> list[TransferEvent]:
    return list(TransferEvent.objects.filter(transfer=transfer).order_by("recorded_at", "id"))


def event_dto(event: TransferEvent) -> dict[str, Any]:
    return {
        "id": str(event.pk),
        "kind": event.kind,
        "site_id": str(event.site_id),
        "actual_at": event.actual_at.isoformat(),
        "recorded_at": event.recorded_at.isoformat(),
        "details": event.details,
    }


def people_names(ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    from accounts.goods_models import HumanIdentity

    wanted = sorted({i for i in ids if i}, key=str)
    if not wanted:
        return {}
    return dict(HumanIdentity.objects.filter(pk__in=wanted).values_list("pk", "display_name"))


def allowed_actions(access: Any, transfer: GoodsTransfer) -> list[str]:
    """What this person may actually do to this transfer, right now.

    The screen draws its buttons from exactly this, and every command checks the
    same things again itself - a button that is not drawn is a convenience, never
    the boundary.
    """
    out: list[str] = []
    source, destination = transfer.source_site_id, transfer.destination_site_id
    state = transfer.state
    if state == GoodsTransfer.State.DRAFT and access.can(ALLOCATE_ACTION, site_id=source):
        out.append("submit")
    if state == GoodsTransfer.State.SUBMITTED and access.can(APPROVE_ACTION, site_id=source):
        out.append("approve")
    if state in (GoodsTransfer.State.APPROVED, GoodsTransfer.State.DISPATCHING) and _has_balance(
        transfer
    ):
        # Only while something is still reserved: once the balance is sent or
        # cancelled there is nothing left to dispatch or to cancel (goods
        # ticket 13B), and a button for it would only be refused. A corrective
        # transfer is never shipped; it is confirmed (goods ticket 16).
        if access.can(MOVE_ACTION, site_id=source) and transfer.corrective_for_id is None:
            out.append("dispatch")
        if access.can(ALLOCATE_ACTION, site_id=source):
            out.append("cancel_outstanding")
    records = list(TransferDispatch.objects.filter(transfer=transfer))
    if any(r.state == TransferDispatch.State.IN_TRANSIT for r in records) and access.can(
        MOVE_ACTION, site_id=destination
    ):
        out.append("count")
    if any(r.state == TransferDispatch.State.COUNTED for r in records) and access.can(
        ACCEPT_ACTION, site_id=destination
    ):
        out.append("accept")
    from outbound.transfer_excess import excess_actions
    from outbound.transfer_returns import return_actions
    from outbound.transfer_shortages import shortage_actions

    return (
        out
        + return_actions(access, transfer, records)
        + _shipment_actions(access, transfer, records)
        + shortage_actions(access, transfer, records)
        + excess_actions(access, transfer, records)
    )


def _has_balance(transfer: GoodsTransfer) -> bool:
    version = _pt_version(transfer, required=False)
    return (
        version is not None
        and ActiveReservation.objects.filter(transfer_version_id=version.pk).exists()
    )


def _shipment_actions(
    access: Any, transfer: GoodsTransfer, records: Sequence[TransferDispatch]
) -> list[str]:
    """Goods ticket 13B: arrival at the destination, e-way evidence at the source."""
    out: list[str] = []
    if transfer.corrective_for_id is not None:
        # Goods ticket 16: nothing of a corrective transfer is on the road.
        return out
    if any(
        r.state == TransferDispatch.State.IN_TRANSIT and r.arrived_at is None for r in records
    ) and access.can(MOVE_ACTION, site_id=transfer.destination_site_id):
        out.append("record_arrival")
    if records and access.can(MOVE_ACTION, site_id=transfer.source_site_id):
        out.append("attach_eway")
    if records and access.can(APPROVE_ACTION, site_id=transfer.source_site_id):
        out.append("verify_eway")
    return out
