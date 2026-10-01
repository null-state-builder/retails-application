"""What a bill does to goods-v1 stock (OPS-07, PRD §8 and §10.4, overall PRD R-INV-002).

At a legacy store a bill line moves a projection row: a number goes down, and the
cost of record comes off the cohort. At a goods-v1 store there is no such number
and no cohort. A sold piece is a *portion of a custody lot*, valued by the origin
that received it, and selling it is a journal posting like every other movement
those goods have ever made.

Three things follow, and each is the whole reason a separate module exists.

**FIFO is not a preference, it is the rule.** R-INV-002 says sales consume
eligible receipt layers oldest first. So the pieces are chosen by
`engine.select_fifo`, which is the same selection a transfer's allocation uses -
and "eligible" therefore means exactly what it means everywhere else: accepted at
this store, good, officially valued, standing in a selling location, under no
hold and reserved to nobody. Nothing here re-states that sentence, because a
second spelling of it is how a quarantined piece eventually gets sold.

**The bill keeps its own origin outcome.** Which lots and which origins a line
actually consumed, and what each cost, is written onto the line and never
recomputed (R-POS-009). An exchange months later gives back *those* pieces at
*those* costs; a re-derivation from today's masters would give back a different
piece at a different cost and nothing downstream would ever notice.

**A returned piece is not stock.** It comes back into the store's custody -
which is a real fact, and the pieces are really in the shop - but it comes back
*unaccepted*, standing in receiving, and it is invisible to the counter until
somebody has physically looked at it and put it away (PRD §10.4). Returned
damaged, it goes to quarantine and opens the same pending damage report damage
anywhere else in the system opens.

Nothing here writes to the legacy stock ledger, and the contract fence still
refuses any attempt to (`CONTRACT_DISABLED`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.goods_documents import append_revision, new_document, officialise
from core.kernel_models import DocumentIdentity, OfficialVersion
from core.operational import SALE_POSTING, ValuePair
from core.refusals import Refusal
from masters.models import Store
from sell.services.goods_stock import Piece, read_shelf
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import AcceptanceEvent, AcceptanceSession, Position

#: The document kind a store's bill writes into the goods record. It is not a PT
#: and never appears in the receipt acceptance queue; it exists so the sale's
#: journal legs hang off a frozen official version like every other posting's do.
SALE_DOC_KIND = "SAL"

#: Why a sold piece left: the reason written on its consumed position.
SOLD_REASON = "sold"


class GoodsSaleError(Exception):
    """A bill the goods records will not take, carrying the code the till routes on."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Allocation:
    """One portion of one custody lot, and what the origin that valued it says it cost."""

    lot_id: uuid.UUID
    lower: int
    upper: int
    origin_id: uuid.UUID
    unit_cost_paise: int

    @property
    def interval(self) -> ranges.Interval:
        return (self.lower, self.upper)

    @property
    def qty(self) -> int:
        return self.upper - self.lower

    @property
    def cost_paise(self) -> int:
        return self.qty * self.unit_cost_paise

    def as_json(self) -> dict[str, Any]:
        return {
            "lot_id": str(self.lot_id),
            "lower": self.lower,
            "upper": self.upper,
            "origin_id": str(self.origin_id),
            "unit_cost_paise": self.unit_cost_paise,
        }

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> Allocation:
        return cls(
            lot_id=uuid.UUID(str(row["lot_id"])),
            lower=int(row["lower"]),
            upper=int(row["upper"]),
            origin_id=uuid.UUID(str(row["origin_id"])),
            unit_cost_paise=int(row["unit_cost_paise"]),
        )


@dataclass(frozen=True)
class Plan:
    """What this bill will do to this store's goods, settled before anything is written."""

    store: Store
    #: Per payload line number: what the scan resolved to.
    pieces: dict[int, Piece]
    #: Per payload line number: the pieces a sold line consumes, oldest origin first.
    sold: dict[int, list[Allocation]]


# ---------------------------------------------------------------------------
# Deciding which pieces the bill takes, before anything is written
# ---------------------------------------------------------------------------


def plan_sale(store: Store, payload_lines: Sequence[dict[str, Any]]) -> Plan:
    """Resolve every scan and choose the exact pieces a sale takes.

    Sold quantities are pooled per SKU before the selection runs. Two lines of the
    same piece on one bill are one demand on one shelf: selecting for each line
    separately would hand both lines the same oldest portion, and the second
    posting would fail - or worse, would not.
    """
    shelf = read_shelf(store, barcodes={str(p["barcode"]).strip() for p in payload_lines if str(p["barcode"]).strip()})
    by_barcode = {(piece.barcode, piece.season): piece for piece in shelf.pieces}
    by_code: dict[str, list[Piece]] = {}
    for piece in shelf.pieces:
        by_code.setdefault(piece.barcode, []).append(piece)

    pieces: dict[int, Piece] = {}
    wanted: dict[uuid.UUID, int] = {}
    order: list[tuple[int, uuid.UUID, int]] = []
    for payload in payload_lines:
        line_no = int(payload["line_no"])
        barcode = str(payload["barcode"]).strip()
        season = str(payload["season"]).strip()
        found = _resolve(store, by_barcode, by_code, barcode, season)
        if found is not None:
            pieces[line_no] = found
        if payload["direction"] != "sale":
            continue
        if found is None:
            raise GoodsSaleError(
                "LINE_UNRESOLVED",
                f"Line {line_no}: barcode '{barcode}' is not a piece this store holds, "
                "so there is nothing to sell.",
            )
        qty = int(payload["qty"])
        wanted[found.sku_id] = wanted.get(found.sku_id, 0) + qty
        order.append((line_no, found.sku_id, qty))

    chosen = {sku_id: _select(store, sku_id, qty, pieces) for sku_id, qty in wanted.items()}
    sold: dict[int, list[Allocation]] = {}
    for line_no, sku_id, qty in order:
        sold[line_no] = _take(chosen[sku_id], qty)
    return Plan(store=store, pieces=pieces, sold=sold)


def _resolve(
    store: Store,
    by_barcode: dict[tuple[str, str], Piece],
    by_code: dict[str, list[Piece]],
    barcode: str,
    season: str,
) -> Piece | None:
    """The piece a scan means. An exact (barcode, season) wins; otherwise the oldest.

    "Oldest" is the shelf's own order, which `read_shelf` already sorted: the same
    rule the legacy counter uses, so a till that does not send a season gets the
    same piece it always got.
    """
    if not barcode:
        return None
    exact = by_barcode.get((barcode, season))
    if exact is not None:
        return exact
    candidates = by_code.get(barcode) or []
    return candidates[0] if candidates else None


def _select(
    store: Store, sku_id: uuid.UUID, qty: int, pieces: dict[int, Piece]
) -> list[Allocation]:
    barcode = next(
        (piece.barcode for piece in pieces.values() if piece.sku_id == sku_id), str(sku_id)
    )
    try:
        chosen = engine.select_fifo(store.pk, sku_id, qty, purpose="sell")
    except Refusal as refusal:
        if refusal.code != "INSUFFICIENT_ELIGIBLE_STOCK":
            raise
        raise GoodsSaleError(
            "INSUFFICIENT_ELIGIBLE_STOCK",
            f"Barcode {barcode}: {refusal.message} Held, quarantined, reserved and "
            "in-transit pieces are not among them.",
        ) from refusal
    out: list[Allocation] = []
    for item in chosen:
        origin_id = item.address.origin_id or item.address.value_basis_origin_id
        assert origin_id is not None, "an eligible portion is valued by construction"
        out.append(
            Allocation(
                lot_id=item.lot_id,
                lower=item.interval[0],
                upper=item.interval[1],
                origin_id=uuid.UUID(str(origin_id)),
                unit_cost_paise=int(item.unit_cost or 0),
            )
        )
    return out


def _take(pool: list[Allocation], qty: int) -> list[Allocation]:
    """Cut `qty` pieces off the front of an oldest-first pool, splitting a portion if needed."""
    taken: list[Allocation] = []
    left = qty
    while left > 0 and pool:
        head = pool[0]
        if head.qty <= left:
            taken.append(pool.pop(0))
            left -= head.qty
            continue
        taken.append(replace(head, upper=head.lower + left))
        pool[0] = replace(head, lower=head.lower + left)
        left = 0
    assert left == 0, "the pool was selected for exactly this demand"
    return taken


# ---------------------------------------------------------------------------
# What comes back: an exchange's return leg
# ---------------------------------------------------------------------------


def returned_allocations(original: Any, qty: int) -> list[Allocation]:
    """The exact pieces a return gives back, oldest of that line's own pieces first.

    Read off the original line and nowhere else. Anything already given back on an
    earlier exchange is skipped, so the same piece cannot come back twice even
    across two bills (PRD §10.4).
    """
    from sell.services.refunds import returned_so_far

    stamped = [Allocation.from_json(row) for row in (original.goods_allocations or [])]
    if not stamped:
        raise GoodsSaleError(
            "ORIGINAL_REQUIRED",
            "That piece has no recorded origin on its original bill, so there is no cost "
            "to take it back at. A goods store takes a return against the bill that sold it.",
        )
    pool = list(stamped)
    already = int(returned_so_far(original)[0])
    if already:
        _take(pool, already)
    if sum(row.qty for row in pool) < qty:
        raise GoodsSaleError(
            "ALREADY_RETURNED", "Those pieces have already been given back on an earlier bill."
        )
    return _take(pool, qty)


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReturnLeg:
    """One piece coming back inside a bill, and where it is going."""

    line_no: int
    condition: str
    allocations: list[Allocation]


def post_goods_sale(
    sale: Any,
    store: Store,
    *,
    actor: Any,
    plan: Plan,
    returns: Sequence[ReturnLeg],
) -> uuid.UUID | None:
    """One journal posting for the whole bill: what left, and what came back.

    One batch rather than one per line, because a bill is one business event: a
    half-posted exchange is not a state any reader of these records should ever
    have to interpret.
    """
    principal = _principal(actor)
    holder: dict[str, Any] = {}

    def handler(run: CommandRun) -> CommandResult:
        version = _official_version(run, sale, store, plan, returns)
        lots = sorted(
            {a.lot_id for rows in plan.sold.values() for a in rows}
            | {a.lot_id for leg in returns for a in leg.allocations},
            key=str,
        )
        engine.lock_lots(run, lots)
        batch = engine.Plan(
            SALE_POSTING, version.pk, engine.event_key("sale", sale.idempotency_uuid)
        )
        for rows in plan.sold.values():
            for allocation in rows:
                _consume(run, batch, store, allocation)
        for leg in returns:
            for allocation in leg.allocations:
                _take_back(run, batch, store, allocation, leg.condition)
        holder["batch_id"] = engine.post(run, version, batch)
        if any(leg.condition == "damaged" for leg in returns):
            _open_damage_report(run, store, version.document_id, returns)
        run.audit_subject_key = f"sale:{sale.pk}"
        run.audit_site_id = store.pk
        run.audit_after = {"doc_number": sale.doc_number, "posting": SALE_POSTING}
        return CommandResult(resource_type="sale", resource_id=str(sale.pk), status_code=201)

    execute_command(
        principal,
        CommandSpec(
            action="sell.sale.post",
            # Derived from the bill's own idempotency key, so a replay that somehow
            # reaches here is the same command rather than a second one.
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"sale:{sale.idempotency_uuid}"),
            business_input={"idempotency_uuid": str(sale.idempotency_uuid)},
            site_id=store.pk,
            subject_key=f"sale:{sale.pk}",
        ),
        handler,
    )
    found: uuid.UUID | None = holder.get("batch_id")
    return found


def _principal(actor: Any) -> Principal:
    """Who is billing, as the goods records name people.

    A till's right to bill at all is checked before this - the `sell` section gate
    on the endpoint and the store scope in the accept pipeline - so what is needed
    here is the person's goods identity to sign the evidence with. A login with no
    such identity cannot sign anything, and a bill nobody signed is not a record.
    """
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None)
    if human_id is None or tenant_id is None:
        raise GoodsSaleError(
            "SCOPE_DENIED",
            "This login is not a person in the goods records, so it cannot bill goods stock.",
            status=403,
        )
    from accounts.principal import access_for_user
    access = access_for_user(actor)
    if access.session is not None:
        return access.principal()
    return Principal(tenant_id=tenant_id, human_id=human_id, user_id=getattr(actor, "pk", None))


def _official_version(
    run: CommandRun,
    sale: Any,
    store: Store,
    plan: Plan,
    returns: Sequence[ReturnLeg],
) -> OfficialVersion:
    """The frozen document this bill's journal legs hang off.

    A bill is already official the moment it is printed - it is in a customer's
    hand - so there is no draft anybody reviews and no second approver. The
    document exists because every operational posting names the version that
    authorised it, and a sale's authority is the bill.
    """
    identity, head = new_document(
        run,
        kind=SALE_DOC_KIND,
        purpose=DocumentIdentity.Purpose.SALE,
        entity_id=store.gstin.legal_entity_id,
        site_id=store.pk,
    )
    lines: list[tuple[uuid.UUID, Mapping[str, Any]]] = []
    for line_no, rows in sorted(plan.sold.items()):
        lines.append(
            (
                uuid.uuid5(identity.pk, f"sold:{line_no}"),
                {
                    "direction": "sale",
                    "line_no": line_no,
                    "sku_id": str(plan.pieces[line_no].sku_id),
                    "qty": sum(row.qty for row in rows),
                    "portions": [row.as_json() for row in rows],
                },
            )
        )
    for leg in sorted(returns, key=lambda r: r.line_no):
        lines.append(
            (
                uuid.uuid5(identity.pk, f"returned:{leg.line_no}"),
                {
                    "direction": "return",
                    "line_no": leg.line_no,
                    "condition": leg.condition,
                    "qty": sum(row.qty for row in leg.allocations),
                    "portions": [row.as_json() for row in leg.allocations],
                },
            )
        )
    header = {
        "bill_number": sale.doc_number or "",
        "store_code": store.code,
        "billed_at": sale.billed_at.isoformat(),
        "idempotency_uuid": str(sale.idempotency_uuid),
    }
    append_revision(run, head, header=header, replace_lines=lines)
    assert run.principal.human_id is not None
    version, _lines = officialise(
        run,
        head,
        approved_by_id=run.principal.human_id,
        canonical_header=header,
        lines=lines,
        authority=run.authority,
    )
    return version


def _consume(run: CommandRun, batch: engine.Plan, store: Store, allocation: Allocation) -> None:
    """One sold portion leaves the shop, at the cost its own origin froze."""
    engine.end_positions(
        run, batch, allocation.lot_id, allocation.interval, "consumed", SOLD_REASON
    )
    batch.value.append(
        ValuePair(
            origin_id=allocation.origin_id,
            amount=allocation.cost_paise,
            source_bucket="stock",
            destination_bucket="external",
            source_site_id=store.pk,
            destination_site_id=None,
            lot_id=allocation.lot_id,
            lower=allocation.lower,
            upper=allocation.upper,
        )
    )


def _take_back(
    run: CommandRun,
    batch: engine.Plan,
    store: Store,
    allocation: Allocation,
    condition: str,
) -> None:
    """One returned piece comes back into custody - and only into custody.

    Back at its own origin and its own cost, because the piece is that piece: a
    return creates no purchase and no new value, it unwinds the one the sale made.
    It lands unaccepted in receiving (or, damaged, in quarantine), which is what
    keeps it off the shelf until somebody has physically looked at it.
    """
    damaged = condition == "damaged"
    location = engine.system_location(store.pk, "quarantine" if damaged else "receiving")
    engine.change_address(
        run,
        batch,
        allocation.lot_id,
        allocation.interval,
        lambda old: replace(
            old,
            boundary="physical",
            site_id=store.pk,
            location_id=location.pk,
            transfer_id=None,
            condition="damaged" if damaged else "good",
            # Never accepted by coming back. Somebody has to put it away first
            # (PRD §10.4, overall PRD R-INV-002).
            accepted_event_id=None,
            reason=None,
        ),
    )
    batch.value.append(
        ValuePair(
            origin_id=allocation.origin_id,
            amount=allocation.cost_paise,
            source_bucket="external",
            destination_bucket="stock",
            source_site_id=None,
            destination_site_id=store.pk,
            lot_id=allocation.lot_id,
            lower=allocation.lower,
            upper=allocation.upper,
        )
    )


def _open_damage_report(
    run: CommandRun,
    store: Store,
    document_id: uuid.UUID,
    returns: Sequence[ReturnLeg],
) -> None:
    """A piece taken back damaged waits for a different person, like all damage does."""
    from outbound.damage_review import open_for_movement

    damaged = [leg for leg in returns if leg.condition == "damaged"]
    positions = {
        str(row.lot_id): row
        for row in Position.objects.filter(
            lot_id__in=[a.lot_id for leg in damaged for a in leg.allocations]
        )
    }
    resolved: list[tuple[Any, Sequence[Any], dict[str, Any]]] = [
        (
            _ReportLine(qty=sum(a.qty for a in leg.allocations)),
            [_ReportPiece(lot_id=a.lot_id, interval=a.interval) for a in leg.allocations],
            {
                "line_key": str(uuid.uuid5(document_id, f"damaged:{leg.line_no}")),
                "lot_id": str(leg.allocations[0].lot_id),
                "sku_id": str(getattr(positions.get(str(leg.allocations[0].lot_id)), "sku_id", "")),
                "origin_id": str(leg.allocations[0].origin_id),
                "source_location_id": None,
                "hold_keys": [],
            },
        )
        for leg in damaged
        if leg.allocations
    ]
    if not resolved:
        return
    open_for_movement(
        run,
        document=DocumentIdentity.objects.get(pk=document_id),
        site_id=store.pk,
        reason_code="CUSTOMER_RETURN_DAMAGED",
        evidence_ids=[],
        resolved=resolved,
    )


@dataclass(frozen=True)
class _ReportLine:
    """The shape `snapshot_movement_lines` reads a quantity off."""

    qty: int


@dataclass(frozen=True)
class _ReportPiece:
    """The shape it reads a portion and its condition off."""

    lot_id: uuid.UUID
    interval: ranges.Interval

    @property
    def address(self) -> Any:
        return engine.Address(boundary="physical", condition="damaged")


# ---------------------------------------------------------------------------
# Putting a returned piece away, which is what makes it sellable again
# ---------------------------------------------------------------------------


def accept_returned_pieces(
    run: CommandRun,
    *,
    store: Store,
    sale_line: Any,
    location_id: uuid.UUID,
) -> int:
    """The store's acceptance of pieces a customer brought back (PRD §10.4).

    The same evidence a transfer's arrival records - an `AcceptanceSession` against
    the document that brought the goods, and an `AcceptanceEvent` naming who put
    which portion where - because "who accepted these pieces, where and when" has
    to be one kind of record in this system rather than three. Only after this does
    the piece appear on the counter's shelf again.
    """
    assert run.principal.human_id is not None
    version = _sale_version(sale_line)
    receiving = engine.system_location(store.pk, "receiving")
    waiting = [
        Allocation.from_json(row)
        for row in (sale_line.goods_allocations or [])
        if _is_waiting(row, store, receiving.pk)
    ]
    if not waiting:
        raise Refusal("NOT_FOUND", "None of that line's pieces are waiting to be put away.")
    engine.lock_lots(run, sorted({a.lot_id for a in waiting}, key=str))
    session = _acceptance_session(run, store.pk, version)
    batch = engine.Plan(
        "P09", version.pk, engine.event_key("sale_return_accept", sale_line.pk, run.key_id)
    )
    accepted = 0
    for allocation in waiting:
        event: AcceptanceEvent = run.record(
            AcceptanceEvent(
                session_id=session.pk,
                site_id=store.pk,
                scan_key=uuid.uuid5(session.pk, f"{allocation.lot_id}:{allocation.lower}"),
                official_line=None,
                lot_id=allocation.lot_id,
                portion=_portion(allocation),
                destination_location_id=location_id,
                observed_alias="customer return",
                observed_ticket_mrp=None,
                label_evidence_ref=None,
                tag_verdict="matched",
                outcome=AcceptanceEvent.Outcome.ACCEPTED_GOOD,
            )
        )
        engine.change_address(
            run,
            batch,
            allocation.lot_id,
            allocation.interval,
            lambda old, loc=location_id, ev=event.pk: replace(
                old, location_id=loc, accepted_event_id=ev
            ),
        )
        accepted += allocation.qty
    engine.post(run, version, batch)
    run.audit_subject_key = f"sale_line:{sale_line.pk}"
    run.audit_site_id = store.pk
    run.audit_after = {"accepted_qty": accepted}
    return accepted


def _portion(allocation: Allocation) -> Any:
    from core.goods_fields import portion

    return portion(allocation.lower, allocation.upper)


def _is_waiting(row: dict[str, Any], store: Store, receiving_id: uuid.UUID) -> bool:
    """Is this exact portion still standing unaccepted in the store's receiving?"""
    from core.goods_fields import portion

    return Position.objects.filter(
        lot_id=uuid.UUID(str(row["lot_id"])),
        portion__overlap=portion(int(row["lower"]), int(row["upper"])),
        site_id=store.pk,
        location_id=receiving_id,
        boundary="physical",
        condition="good",
        accepted_event__isnull=True,
    ).exists()


def _sale_version(sale_line: Any) -> OfficialVersion:
    version = (
        OfficialVersion.objects.filter(
            document__kind=SALE_DOC_KIND,
            canonical_payload__idempotency_uuid=str(sale_line.sale.idempotency_uuid),
        )
        .order_by("-version")
        .first()
    )
    if version is None:
        raise Refusal("NOT_FOUND", "That bill posted no goods movement to accept against.")
    return version


def _acceptance_session(
    run: CommandRun, site_id: int, version: OfficialVersion
) -> AcceptanceSession:
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
