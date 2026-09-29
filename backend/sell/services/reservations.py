"""Customer reservations (store operations ticket 20, ST-ORD-1, Anand Q2, Q3, T8).

Staff reserve specific accepted pieces for a named customer. It is a stock
reservation, not a sale: the pieces leave available-to-sell under a hold of
their own (R-INV-003) and nothing is billed until the customer collects them.

**How long.** 7 days, or 3 while a sale period is running at the store. A sale
period is running when an approved, live offer tagged "End-of-season sale"
covers the store that day (baseline B93): the one place the app already marks
a sale, per store and per date. Both lengths are settings. The customer can
collect up to and including the collect-by date.

**The advance.** Optional, in cash, card or UPI. It is held as a customer
advance (``GLAccount.CUSTOMER_ADVANCE``) and gets a receipt voucher from the
store's RV series. No GST is charged on it (Notification 66/2017-CT). What
becomes of it is a row per change (``AdvanceMovement``), never an edited total:

* **pickup** - a normal bill is made and the advance pays towards it as an
  ``advance`` tender (``release_for_bill`` / ``record_pickup``);
* **expiry or the customer's cancellation** - the pieces go back to
  available-to-sell, and the advance is refunded or kept by the policy frozen on
  the reservation when it was made (setting ``KDPS_RESERVATION_ADVANCE_POLICY``,
  default refund). Where it is kept the terms read "The advance is forfeited if
  the item is not collected by [date]." - never a cancellation fee (Circular
  178/10/2022) - and no GST is charged (baseline, CA to confirm);
* **the store's cancellation** - always refunded: the customer did nothing wrong.

A refund goes back the way the advance was paid. On a cancellation the
customer is there and it is paid at once; on expiry it waits, still held as the
customer's, until staff record paying it (``refund``).

**Online only.** Every write here is a request to head office; nothing is
queued on a device. The gate (§32) holds the feature off at a real store until
the CA signs off the forfeiture wording.

Every write is one command, audited with the reservation before and after.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.goods_documents import append_revision, new_document, officialise
from core.goods_fields import bounds
from core.kernel_models import DocumentIdentity, OfficialVersion
from core.refusals import Refusal
from masters.document_series import alert_on_refusal
from masters.models import Store
from masters.store_feature_registry import CUSTOMER_RESERVATION
from masters.store_features import require_feature
from offers.models import Offer
from sell.models import Sale, SaleTender
from sell.reservation_models import CustomerReservation, ReservationPiece
from sell.services import advances
from sell.services.advances import NO_GST_NOTE, TENDER_ACCOUNT, TENDER_MODES
from sell.services.customers import normalise_mobile
from sell.services.goods_stock import Piece, is_goods_site, read_shelf
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import ActiveHold, Position

logger = logging.getLogger(__name__)

CREATE_ACTION = "sell.reservation.create"
COLLECT_ACTION = "sell.reservation.collect"
CANCEL_ACTION = "sell.reservation.cancel"
REFUND_ACTION = "sell.reservation.refund"
EXPIRE_ACTION = "sell.reservation.expire"

#: The hold's kind on the stock ledger, and the goods document that authorises it.
HOLD_KIND = "customer_reservation"
DOC_KIND = "RSV"
#: P12 is the design's hold and release posting.
HOLD_POSTING = "P12"
#: The service that expires reservations on the worker's clock.
EXPIRY_SERVICE = "reservation-expiry"

#: The terms, word for word (§9). The forfeiture sentence is the CA-gated one.
FORFEIT_TERMS = "The advance is forfeited if the item is not collected by {date}."
REFUND_TERMS = "The advance is refunded if the item is not collected by {date}."
HELD_TERMS = "The pieces are held for you until {date}."


def _refuse(code: str, message: str, status: int = 422) -> Refusal:
    return Refusal(code, message, status=status)


def date_text(day: date) -> str:
    """The collect-by date as the customer reads it: 5 Oct 2026."""
    return f"{day.day} {day:%b %Y}"


# ---------------------------------------------------------------------------
# Length and terms
# ---------------------------------------------------------------------------


def sale_period_running(store: Store, day: date) -> bool:
    """Is a sale period running at ``store`` on ``day``? (baseline B93)."""
    return Offer.objects.for_tenant(store.tenant_id).live_on(day).for_store(store.code).filter(mode=Offer.Mode.EOSS).exists()


@dataclass(frozen=True)
class Length:
    days: int
    sale_period: bool
    collect_by: date


def length_for(store: Store, day: date) -> Length:
    sale = sale_period_running(store, day)
    days = (
        int(settings.KDPS_RESERVATION_SALE_PERIOD_DAYS)
        if sale
        else int(settings.KDPS_RESERVATION_DAYS)
    )
    return Length(days=days, sale_period=sale, collect_by=day + timedelta(days=days))


def current_policy() -> str:
    value = str(settings.KDPS_RESERVATION_ADVANCE_POLICY or "").strip().lower()
    if value not in CustomerReservation.Policy.values:
        raise ValueError(f"KDPS_RESERVATION_ADVANCE_POLICY must be refund or keep, not {value!r}")
    return value


def terms_for(policy: str, collect_by: date, advance_paise: int) -> str:
    when = date_text(collect_by)
    lines = [HELD_TERMS.format(date=when)]
    if advance_paise > 0:
        lines.append(
            (FORFEIT_TERMS if policy == CustomerReservation.Policy.KEEP else REFUND_TERMS).format(
                date=when
            )
        )
        lines.append(NO_GST_NOTE)
    return " ".join(lines)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def balance_paise(reservation: CustomerReservation) -> int:
    """What the advance still holds for the customer: received less every use."""
    return advances.balance_paise(reservation)


def refund_due(reservation: CustomerReservation) -> bool:
    """Is money still owed back on a reservation that has ended?"""
    return (
        reservation.status != CustomerReservation.Status.ACTIVE and balance_paise(reservation) > 0
    )


def advance_outcome(reservation: CustomerReservation) -> str:
    """What became of the advance, in one word the screen shows."""
    return advances.outcome(
        reservation, open_=reservation.status == CustomerReservation.Status.ACTIVE
    )


def snapshot(reservation: CustomerReservation) -> dict[str, Any]:
    """The reservation as the audit log keeps it. The number shows its last four digits."""
    voucher = getattr(reservation, "voucher", None)
    return {
        "ref": reservation.ref,
        "status": reservation.status,
        "customer_name": reservation.customer_name,
        "customer_mobile": masked(reservation.customer_mobile),
        "collect_by": reservation.collect_by.isoformat(),
        "sale_period": reservation.sale_period,
        "advance_policy": reservation.advance_policy,
        "pieces": [
            {"barcode": p.barcode, "season": p.season, "qty": p.qty}
            for p in reservation.pieces.all()
        ],
        "voucher": voucher.number if voucher is not None else None,
        "advance_paise": voucher.amount_paise if voucher is not None else 0,
        "advance_balance_paise": balance_paise(reservation),
        "close_reason": reservation.close_reason or None,
        "sale": reservation.sale.doc_number if reservation.sale is not None else None,
    }


def masked(mobile: str) -> str:
    return "*" * max(len(mobile) - 4, 0) + mobile[-4:]


# ---------------------------------------------------------------------------
# Who does it
# ---------------------------------------------------------------------------


def _person(store: Store, actor: Any) -> Principal:
    """The named person making the change. A goods document is made by a person."""
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None)
    if human_id is None or tenant_id is None:
        raise _refuse(
            "SCOPE_DENIED",
            "This login is not a person in the goods records, so it cannot hold or release "
            "stock for a customer.",
            403,
        )
    return Principal(tenant_id=tenant_id, human_id=human_id, user_id=getattr(actor, "pk", None))


def _require_goods_store(store: Store) -> None:
    if not is_goods_site(store):
        raise _refuse(
            "RESERVATION_GOODS_ONLY",
            f"{store.code} does not keep piece-level stock, so pieces cannot be held for a "
            "customer here.",
        )


# ---------------------------------------------------------------------------
# Making a reservation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Made:
    reservation: CustomerReservation
    created: bool


def create(store: Store, actor: Any, data: dict[str, Any]) -> Made:
    """Hold the pieces, take the advance, issue the voucher - or refuse whole.

    ``data``: ``id``, ``customer_name``, ``customer_mobile``, ``lines`` (``barcode``,
    ``season``, ``qty``) and an optional ``advance`` (``amount_paise``, ``mode``,
    ``reference``). A replay of the same id answers with the reservation it made.
    """
    reservation_id: uuid.UUID = data["id"]
    existing = CustomerReservation.objects.filter(pk=reservation_id).first()
    if existing is not None:
        if not _same_reservation(existing, store, data):
            raise _refuse(
                "RESERVATION_CONFLICT",
                "This reservation id is already used for a different reservation.",
                409,
            )
        return Made(reservation=existing, created=False)
    _require_goods_store(store)
    require_feature(store, CUSTOMER_RESERVATION)
    name, mobile = _customer(data)
    wanted = _wanted(data.get("lines") or [])
    amount, mode, reference = _advance(data.get("advance") or None)
    principal = _person(store, actor)
    today = timezone.localdate()
    length = length_for(store, today)
    policy = current_policy()

    def handler(run: CommandRun) -> CommandResult:
        run.advisory_lock(LockRank.DOCUMENT, [f"reservations:{store.pk}"])
        chosen = _choose(run, store, wanted)
        mrp_total = sum((piece.mrp_paise or 0) * qty for piece, qty, _allocations in chosen)
        if amount > mrp_total:
            raise _refuse(
                "RESERVATION_ADVANCE_TOO_HIGH",
                f"The advance cannot be more than the pieces' price (Rs {mrp_total / 100:,.2f}).",
            )
        version = _hold_document(run, store, reservation_id, chosen, length)
        reservation = CustomerReservation.objects.create(
            id=reservation_id,
            store=store,
            ref=_next_ref(store),
            customer_name=name,
            customer_mobile=mobile,
            reserved_on=today,
            collect_by=length.collect_by,
            days=length.days,
            sale_period=length.sale_period,
            advance_policy=policy,
            terms=terms_for(policy, length.collect_by, amount),
            hold_version=version,
            created_by=actor,
        )
        plan = engine.Plan(
            HOLD_POSTING, version.pk, engine.event_key(HOLD_KIND, "place", reservation_id)
        )
        for line_no, (piece, qty, allocations) in enumerate(chosen, start=1):
            ReservationPiece.objects.create(
                reservation=reservation,
                line_no=line_no,
                barcode=piece.barcode,
                season=piece.season,
                sku_id=piece.sku_id,
                qty=qty,
                item=_item(piece),
                portions=[
                    {"lot_id": str(lot_id), "lower": lower, "upper": upper}
                    for lot_id, lower, upper in allocations
                ],
            )
            for lot_id, lower, upper in allocations:
                engine.place_hold(
                    run,
                    plan,
                    lot_id=lot_id,
                    interval=(lower, upper),
                    hold_key=reservation.hold_key,
                    kind=HOLD_KIND,
                    site_id=store.pk,
                    source_version_id=version.pk,
                )
        engine.post(run, version, plan)
        if amount:
            _take_advance(store, actor, reservation, amount, mode, reference)
        run.audit_subject_key = f"reservation:{reservation.pk}"
        run.audit_site_id = store.pk
        run.audit_before = None
        run.audit_after = snapshot(reservation)
        return CommandResult(
            resource_type="customer_reservation", resource_id=str(reservation.pk), status_code=201
        )

    with alert_on_refusal():
        execute_command(
            principal,
            CommandSpec(
                action=CREATE_ACTION,
                command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"reservation:{reservation_id}"),
                business_input={
                    "id": str(reservation_id),
                    "store": store.code,
                    "customer_mobile": masked(mobile),
                    "lines": [
                        {"barcode": b, "season": s, "qty": q} for (b, s), q in wanted.items()
                    ],
                    "advance_paise": amount,
                    "mode": mode,
                },
                site_id=store.pk,
                subject_key=f"reservation:{reservation_id}",
            ),
            handler,
        )
    return Made(reservation=CustomerReservation.objects.get(pk=reservation_id), created=True)


def _same_reservation(row: CustomerReservation, store: Store, data: dict[str, Any]) -> bool:
    """Is ``data`` the reservation ``row`` already is? (a replay, not a new one)."""
    wanted: dict[str, int] = {}
    for line in data.get("lines") or []:
        code = str(line.get("barcode") or "").strip()
        wanted[code] = wanted.get(code, 0) + int(line.get("qty") or 0)
    held: dict[str, int] = {}
    for piece in row.pieces.all():
        held[piece.barcode] = held.get(piece.barcode, 0) + piece.qty
    return (
        row.store_id == store.pk
        and row.customer_mobile == normalise_mobile(str(data.get("customer_mobile") or ""))
        and wanted == held
    )


def _customer(data: dict[str, Any]) -> tuple[str, str]:
    name = str(data.get("customer_name") or "").strip()
    if not name:
        raise _refuse("VALIDATION", "A reservation is for a named customer: type their name.", 400)
    mobile = normalise_mobile(str(data.get("customer_mobile") or ""))
    if len(mobile) != 10:
        raise _refuse(
            "VALIDATION",
            "Type the customer's 10-digit mobile number, so the store can reach them.",
            400,
        )
    return name[:120], mobile


def _advance(advance: dict[str, Any] | None) -> tuple[int, str, str]:
    """``(amount, mode, reference)``; no advance is ``(0, "", "")``."""
    if not advance:
        return 0, "", ""
    amount = int(advance.get("amount_paise") or 0)
    mode = str(advance.get("mode") or "")
    if amount <= 0 or mode not in TENDER_MODES:
        raise _refuse("VALIDATION", "An advance is an amount in cash, card or UPI.", 400)
    return amount, mode, str(advance.get("reference") or "")[:64]


def _wanted(lines: Iterable[dict[str, Any]]) -> dict[tuple[str, str], int]:
    wanted: dict[tuple[str, str], int] = {}
    for line in lines:
        barcode = str(line.get("barcode") or "").strip()
        season = str(line.get("season") or "").strip()
        qty = int(line.get("qty") or 0)
        if not barcode or qty < 1:
            raise _refuse("VALIDATION", "Each piece needs its barcode and a quantity.", 400)
        wanted[(barcode, season)] = wanted.get((barcode, season), 0) + qty
    if not wanted:
        raise _refuse("VALIDATION", "Scan at least one piece to reserve.", 400)
    return wanted


Chosen = list[tuple[Piece, int, list[tuple[uuid.UUID, int, int]]]]


def _choose(run: CommandRun, store: Store, wanted: dict[tuple[str, str], int]) -> Chosen:
    """The exact pieces to hold: this store's own sellable stock, oldest first.

    The same shelf and the same selection a bill uses.
    """
    shelf = read_shelf(store)
    by_exact = {(p.barcode, p.season): p for p in shelf.pieces}
    by_code: dict[str, list[Piece]] = {}
    for known in shelf.pieces:
        by_code.setdefault(known.barcode, []).append(known)
    resolved: list[tuple[Piece, int]] = []
    for (barcode, season), qty in wanted.items():
        found = by_exact.get((barcode, season)) or next(
            (p for p in by_code.get(barcode, []) if shelf.quantities.get((p.barcode, p.season))),
            None,
        )
        if found is None:
            raise _refuse(
                "RESERVATION_PIECE_UNKNOWN",
                f"Barcode {barcode} is not a piece this store holds.",
            )
        resolved.append((found, qty))
    # Every lot these SKUs stand in here, locked before choosing (as a transfer
    # does), so a bill or another reservation cannot take the same piece meanwhile.
    engine.lock_lots(
        run,
        sorted(
            set(
                Position.objects.filter(
                    site_id=store.pk,
                    boundary="physical",
                    sku_id__in=[piece.sku_id for piece, _qty in resolved],
                ).values_list("lot_id", flat=True)
            ),
            key=str,
        ),
    )
    chosen: Chosen = []
    for piece, qty in resolved:
        try:
            portions = engine.select_fifo(store.pk, piece.sku_id, qty, purpose="sell")
        except Refusal as refusal:
            if refusal.code != "INSUFFICIENT_ELIGIBLE_STOCK":
                raise
            raise _refuse(
                "INSUFFICIENT_ELIGIBLE_STOCK",
                f"Barcode {piece.barcode}: not enough of it is free to sell here. Held, "
                "reserved and in-transit pieces cannot be reserved.",
            ) from refusal
        chosen.append((piece, qty, [(p.lot_id, p.interval[0], p.interval[1]) for p in portions]))
    return chosen


def _item(piece: Piece) -> dict[str, Any]:
    """The piece as the counter bills it - the till's own item row, with no cost."""
    return {
        "barcode": piece.barcode,
        "season": piece.season,
        "design": piece.dims.get("design", ""),
        "brand": piece.dims.get("brand", ""),
        "item": piece.dims.get("item", ""),
        "size": piece.dims.get("size", ""),
        "color": piece.dims.get("color", ""),
        "hsn": piece.hsn,
        "mrp_paise": piece.mrp_paise,
        "no_discount": False,
        "season_unknown_historical": piece.season_unknown_historical,
    }


def _next_ref(store: Store) -> str:
    """The store code and a running number, under the store's reservation lock."""
    count = CustomerReservation.objects.filter(store=store).count()
    return f"{store.code}-R{count + 1}"


def _hold_document(
    run: CommandRun, store: Store, reservation_id: uuid.UUID, chosen: Chosen, length: Length
) -> OfficialVersion:
    """The frozen goods document the hold, and its end, hang off."""
    identity, head = new_document(
        run,
        kind=DOC_KIND,
        purpose=DocumentIdentity.Purpose.RESERVATION,
        entity_id=store.gstin.legal_entity_id,
        site_id=store.pk,
    )
    lines: list[tuple[uuid.UUID, Mapping[str, Any]]] = [
        (
            uuid.uuid5(identity.pk, f"reserved:{line_no}"),
            {
                "line_no": line_no,
                "sku_id": str(piece.sku_id),
                "qty": qty,
                "portions": [
                    {"lot_id": str(lot_id), "lower": lower, "upper": upper}
                    for lot_id, lower, upper in allocations
                ],
            },
        )
        for line_no, (piece, qty, allocations) in enumerate(chosen, start=1)
    ]
    header = {
        "reservation_id": str(reservation_id),
        "store_code": store.code,
        "collect_by": length.collect_by.isoformat(),
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


def _take_advance(
    store: Store,
    actor: Any,
    reservation: CustomerReservation,
    amount: int,
    mode: str,
    reference: str,
) -> None:
    """The receipt voucher, the customer advance it raises, and the drawer's receipt."""
    advances.take(
        store,
        actor,
        reservation,
        amount=amount,
        mode=mode,
        reference=reference,
        on=reservation.reserved_on,
    )


def _user(actor: Any) -> Any:
    return advances.user_of(actor)


# ---------------------------------------------------------------------------
# Ending the hold
# ---------------------------------------------------------------------------


def _release(run: CommandRun, reservation: CustomerReservation, why: str) -> None:
    """Every piece of the reservation back to available-to-sell, under its own document."""
    version = reservation.hold_version
    plan = engine.Plan(HOLD_POSTING, version.pk, engine.event_key(HOLD_KIND, why, reservation.pk))
    lots: list[uuid.UUID] = []
    for piece in reservation.pieces.all():
        for portion in piece.portions:
            lots.append(uuid.UUID(portion["lot_id"]))
    engine.lock_lots(run, lots)
    for piece in reservation.pieces.all():
        for portion in piece.portions:
            lot_id = uuid.UUID(portion["lot_id"])
            interval: ranges.Interval = (int(portion["lower"]), int(portion["upper"]))
            # Only what is still under this reservation's hold: a piece that has
            # left some other audited way (damage, disposal) has nothing to give back.
            held = ranges.normalise(
                [
                    bounds(h.portion)
                    for h in ActiveHold.objects.filter(lot_id=lot_id, hold_key=reservation.hold_key)
                ]
            )
            for sub in ranges.intersect(held, [interval]):
                engine.release_hold(
                    run,
                    plan,
                    lot_id=lot_id,
                    interval=sub,
                    hold_key=reservation.hold_key,
                    site_id=reservation.store_id,
                    source_version_id=version.pk,
                )
    engine.post(run, version, plan)


def _close(
    reservation: CustomerReservation,
    status: str,
    reason: str,
    actor: Any,
    sale: Sale | None = None,
) -> None:
    reservation.status = status
    reservation.close_reason = reason
    reservation.closed_at = timezone.now()
    reservation.closed_by = _user(actor)
    reservation.sale = sale
    reservation.save(
        update_fields=["status", "close_reason", "closed_at", "closed_by", "sale", "updated_at"]
    )


def _pay_back(reservation: CustomerReservation, actor: Any, amount: int) -> None:
    """Refund ``amount`` the way the advance was paid: the drawer or the machine pays out."""
    advances.pay_back(reservation, actor, amount)


def _forfeit(reservation: CustomerReservation, actor: Any, amount: int) -> None:
    """Keep ``amount`` by the reservation's own terms. Not a sale, so no GST."""
    advances.forfeit(reservation, actor, amount, why="Not collected")


def _locked(store: Store, reservation_id: Any) -> CustomerReservation:
    reservation = (
        CustomerReservation.objects.select_for_update()
        .filter(pk=reservation_id, store=store)
        .first()
    )
    if reservation is None:
        raise _refuse("NOT_FOUND", "That reservation was not found at this store.", 404)
    return reservation


def _write(
    principal: Principal,
    action: str,
    store: Store,
    reservation_id: Any,
    command_id: uuid.UUID,
    business: dict[str, Any],
    body: Any,
) -> None:
    """One audited change to a reservation, under ``command_id``.

    Cancel and refund take the screen's own id for the attempt, so a retry of the
    same tap replays it, while a later, different attempt (a refund after the
    reservation has since expired) is judged afresh rather than answered with an
    old refusal.
    """
    subject = f"reservation:{reservation_id}"

    def handler(run: CommandRun) -> CommandResult:
        reservation = _locked(store, reservation_id)
        before = snapshot(reservation)
        body(run, reservation)
        reservation.refresh_from_db()
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = before
        run.audit_after = snapshot(reservation)
        return CommandResult(
            resource_type="customer_reservation", resource_id=str(reservation_id), status_code=200
        )

    execute_command(
        principal,
        CommandSpec(
            action=action,
            command_id=command_id,
            business_input={"id": str(reservation_id), "store": store.code, **business},
            site_id=store.pk,
            subject_key=subject,
        ),
        handler,
    )


def cancel(
    store: Store,
    actor: Any,
    reservation_id: Any,
    *,
    by_customer: bool,
    command_id: uuid.UUID | None = None,
) -> None:
    """Cancel an active reservation now; the customer is at the counter.

    The pieces go back. The advance is refunded at once, unless the customer
    cancelled and the reservation's policy keeps it.
    """
    who = "customer" if by_customer else "store"

    def body(run: CommandRun, reservation: CustomerReservation) -> None:
        if reservation.status != CustomerReservation.Status.ACTIVE:
            raise _refuse(
                "RESERVATION_CLOSED",
                f"{reservation.ref} is already {reservation.get_status_display().lower()}.",
                409,
            )
        _release(run, reservation, "cancel")
        _close(
            reservation,
            CustomerReservation.Status.CANCELLED,
            CustomerReservation.CloseReason.CUSTOMER_CANCELLED
            if by_customer
            else CustomerReservation.CloseReason.STORE_CANCELLED,
            actor,
        )
        held = balance_paise(reservation)
        if held <= 0:
            return
        if by_customer and reservation.advance_policy == CustomerReservation.Policy.KEEP:
            _forfeit(reservation, actor, held)
        else:
            _pay_back(reservation, actor, held)

    _write(
        _person(store, actor),
        CANCEL_ACTION,
        store,
        reservation_id,
        command_id or uuid.uuid4(),
        {"by": who},
        body,
    )


def refund(
    store: Store, actor: Any, reservation_id: Any, *, command_id: uuid.UUID | None = None
) -> None:
    """Pay back what an ended reservation still holds (expired, or left over after pickup)."""

    def body(run: CommandRun, reservation: CustomerReservation) -> None:
        if reservation.status == CustomerReservation.Status.ACTIVE:
            raise _refuse(
                "RESERVATION_ACTIVE",
                f"{reservation.ref} is still reserved. Cancel it to refund the advance.",
                409,
            )
        held = balance_paise(reservation)
        if held <= 0:
            raise _refuse(
                "NOTHING_TO_REFUND", f"{reservation.ref} holds no advance to refund.", 409
            )
        _pay_back(reservation, actor, held)

    _write(
        _person(store, actor),
        REFUND_ACTION,
        store,
        reservation_id,
        command_id or uuid.uuid4(),
        {},
        body,
    )


# ---------------------------------------------------------------------------
# Expiry, on the worker's clock
# ---------------------------------------------------------------------------


def expire_due(today: date | None = None, tenant_id: Any = None) -> int:
    """End every active reservation whose collect-by date has passed. Returns how many.

    The pieces go back. Where the policy keeps the advance it is forfeited now;
    otherwise it stays held for the customer until staff record the refund. One
    reservation that cannot be ended is logged and left for the next run; it
    never stops the others.
    """
    day = today or timezone.localdate()
    done = 0
    due = CustomerReservation.objects.filter(
        status=CustomerReservation.Status.ACTIVE, collect_by__lt=day
    ).select_related("store")
    if tenant_id is not None:
        due = due.filter(store__tenant_id=tenant_id)
    for reservation in list(due):
        store = reservation.store

        def body(run: CommandRun, locked: CustomerReservation) -> None:
            if locked.status != CustomerReservation.Status.ACTIVE:
                return
            _release(run, locked, "expire")
            _close(
                locked,
                CustomerReservation.Status.EXPIRED,
                CustomerReservation.CloseReason.EXPIRED,
                None,
            )
            held = balance_paise(locked)
            if held > 0 and locked.advance_policy == CustomerReservation.Policy.KEEP:
                _forfeit(locked, None, held)

        try:
            _write(
                Principal(tenant_id=store.tenant_id, service_code=EXPIRY_SERVICE),
                EXPIRE_ACTION,
                store,
                reservation.pk,
                uuid.uuid4(),
                {"collect_by": reservation.collect_by.isoformat()},
                body,
            )
        except Exception:
            logger.exception("reservation %s could not be expired", reservation.ref)
            continue
        done += 1
    return done


def scheduled_expiry(tenant_id: uuid.UUID) -> None:
    """The worker's call: expire what is due, then refresh the expiry alerts."""
    from alerts.checks import sync_reservation_alerts

    try:
        expire_due(tenant_id=tenant_id)
    finally:
        sync_reservation_alerts()


# ---------------------------------------------------------------------------
# Pickup, inside the bill's own transaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pickup:
    reservation: CustomerReservation
    advance_paise: int


def release_for_bill(store: Store, data: dict[str, Any], actor: Any) -> Pickup | None:
    """The reservation this bill collects, its pieces taken off the hold - or ``None``.

    Runs inside the bill's transaction before its pieces are chosen, so the
    released pieces are on the shelf the bill takes from, and a refused bill
    leaves them held. Refused (the bill is not taken) when the reservation is
    not this store's, has ended, is missing pieces from the bill, or the bill
    uses more advance than it holds.
    """
    from sell.services.accept import AcceptError

    reservation_id = data.get("reservation")
    if not reservation_id:
        return None
    advance = sum(
        int(t["amount_paise"]) for t in data["tenders"] if t["mode"] == SaleTender.Mode.ADVANCE
    )
    reservation = (
        CustomerReservation.objects.select_for_update()
        .filter(pk=reservation_id, store=store)
        .first()
    )
    if reservation is None:
        raise AcceptError(
            "RESERVATION_NOT_FOUND", "That reservation is not one of this store's.", 422
        )
    if reservation.status != CustomerReservation.Status.ACTIVE:
        raise AcceptError(
            "RESERVATION_CLOSED",
            f"{reservation.ref} is {reservation.get_status_display().lower()}, so it cannot be "
            "collected on a bill.",
            409,
        )
    held = balance_paise(reservation)
    if advance > held:
        raise AcceptError(
            "RESERVATION_ADVANCE",
            f"{reservation.ref} holds Rs {held / 100:,.2f} of advance; the bill used "
            f"Rs {advance / 100:,.2f}.",
            422,
        )
    _check_pieces_on_bill(reservation, data["all_lines"])

    def handler(run: CommandRun) -> CommandResult:
        before = snapshot(reservation)
        _release(run, reservation, "collect")
        run.audit_subject_key = f"reservation:{reservation.pk}"
        run.audit_site_id = store.pk
        run.audit_before = before
        run.audit_after = {**before, "released_for": "pickup bill"}
        return CommandResult(
            resource_type="customer_reservation", resource_id=str(reservation.pk), status_code=200
        )

    try:
        principal = _person(store, actor)
    except Refusal as refusal:
        raise AcceptError(refusal.code, refusal.message, refusal.status) from refusal
    execute_command(
        principal,
        CommandSpec(
            action="sell.reservation.release_for_bill",
            command_id=uuid.uuid5(
                uuid.NAMESPACE_URL, f"reservation:{reservation.pk}:bill:{data['idempotency_uuid']}"
            ),
            business_input={"id": str(reservation.pk), "bill": str(data["idempotency_uuid"])},
            site_id=store.pk,
            subject_key=f"reservation:{reservation.pk}",
        ),
        handler,
    )
    return Pickup(reservation=reservation, advance_paise=advance)


def _check_pieces_on_bill(
    reservation: CustomerReservation, lines: Iterable[dict[str, Any]]
) -> None:
    from sell.services.accept import AcceptError

    on_bill: dict[str, int] = {}
    for line in lines:
        if line["direction"] == "sale":
            code = str(line["barcode"]).strip()
            on_bill[code] = on_bill.get(code, 0) + int(line["qty"])
    for piece in reservation.pieces.all():
        if on_bill.get(piece.barcode, 0) < piece.qty:
            raise AcceptError(
                "RESERVATION_PIECES_MISSING",
                f"{reservation.ref} holds {piece.qty} of {piece.barcode}; the bill must "
                "carry every reserved piece.",
                422,
            )
        on_bill[piece.barcode] -= piece.qty


def record_pickup(sale: Sale, pickup: Pickup | None, actor: Any) -> None:
    """The reservation collected on ``sale``, and the advance the bill used, audited.

    ``None`` - a bill collecting no reservation - records nothing.
    """
    if pickup is None:
        return
    reservation = pickup.reservation

    def body(run: CommandRun, locked: CustomerReservation) -> None:
        _close(
            locked,
            CustomerReservation.Status.COLLECTED,
            CustomerReservation.CloseReason.COLLECTED,
            actor,
            sale=sale,
        )
        if pickup.advance_paise > 0:
            advances.use(locked, actor, pickup.advance_paise, sale)

    _write(
        _person(sale.store, actor),
        COLLECT_ACTION,
        sale.store,
        reservation.pk,
        uuid.uuid5(
            uuid.NAMESPACE_URL, f"reservation:{reservation.pk}:collect:{sale.idempotency_uuid}"
        ),
        {"sale": sale.doc_number, "advance_paise": pickup.advance_paise},
        body,
    )


__all__ = [
    "NO_GST_NOTE",
    "TENDER_ACCOUNT",
    "TENDER_MODES",
    "Made",
    "Pickup",
    "advance_outcome",
    "balance_paise",
    "cancel",
    "create",
    "expire_due",
    "record_pickup",
    "refund",
    "refund_due",
    "release_for_bill",
    "sale_period_running",
]
