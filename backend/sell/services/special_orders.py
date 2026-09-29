"""Special orders (store operations ticket 21, ST-ORD-2; overall PRD R-POS-014, R-BUY-001).

For an item no store has. Staff record the customer, brand, style, size and
colour, and an optional advance with a receipt voucher (the RV series, no GST:
Notification 66/2017-CT - the same advance as ticket 20's reservations,
``sell.services.advances``). The order is then tracked to collection:

    asked -> ordered -> arrived -> customer told -> collected, or cancelled

* **ordered** - it becomes a transfer request or a booking line, whichever staff
  choose. A *transfer request* is raised here, from the site that holds the
  item to this store (``outbound.transfers.create_request``); it needs the
  person's own ``transfer.allocate`` grant at the store, as any request does. A
  *booking line* is the line of a booking a buyer has confirmed with the brand
  (R-BUY-001): the order names the booking, and the line is found by its brand
  and style. The store places no booking itself - that is the buyer's grant.
* **arrived** - the piece is at the store; staff scan it. Nothing is held: the
  item is sold on a normal bill like any other.
* **customer told** - staff record how they told the customer.
* **collected** - on a normal bill that names the order and carries the piece.
  The advance is used as a tender (``check_for_bill`` / ``record_collection``,
  called by ``accept``). Advance left over is refunded from the screen.
* **cancelled** - at any open step. The advance follows ticket 20's policy
  (``KDPS_RESERVATION_ADVANCE_POLICY``, frozen on the order when taken): the
  customer's cancellation forfeits it where the policy keeps it; otherwise, and
  always on the store's cancellation, it is refunded at once.

**Online only.** Every write is a request to head office; nothing is queued on
a device. Every write is one command, audited with the order before and after.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from django.utils import timezone

from accounts.principal import AccessContext, effective_grants
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.kernel_models import DocumentIdentity
from core.refusals import Refusal
from masters.document_series import alert_on_refusal
from masters.models import Brand, Store
from masters.store_feature_registry import SPECIAL_ORDERS
from masters.store_features import require_feature
from sell.models import Sale, SaleTender
from sell.services import advances
from sell.services.advances import NO_GST_NOTE, TENDER_MODES
from sell.services.customers import normalise_mobile
from sell.services.goods_stock import is_goods_site, sku_for_barcode
from sell.special_order_models import SpecialOrder

CREATE_ACTION = "sell.special_order.create"
ORDER_ACTION = "sell.special_order.order"
ARRIVE_ACTION = "sell.special_order.arrive"
TELL_ACTION = "sell.special_order.tell"
COLLECT_ACTION = "sell.special_order.collect"
CANCEL_ACTION = "sell.special_order.cancel"
REFUND_ACTION = "sell.special_order.refund"

#: The terms, word for word. The forfeiture sentence joins ticket 20's on the
#: CA's list (§32); the default policy refunds, so it is not shown until then.
ASKED_TERMS = "We will tell you when your order arrives."
REFUND_TERMS = "If the order is cancelled, the advance is refunded."
FORFEIT_TERMS = "If you cancel the order, the advance is forfeited."

OPEN = (
    SpecialOrder.Status.ASKED,
    SpecialOrder.Status.ORDERED,
    SpecialOrder.Status.ARRIVED,
    SpecialOrder.Status.TOLD,
)
#: The step it is collected from: the piece is at the store and the customer
#: was told (a customer who walks in is told "in person" first). No step is skipped.
COLLECTABLE = (SpecialOrder.Status.TOLD,)


def _refuse(code: str, message: str, status: int = 422) -> Refusal:
    return Refusal(code, message, status=status)


# ---------------------------------------------------------------------------
# Policy and terms
# ---------------------------------------------------------------------------


def current_policy() -> str:
    """Ticket 20's advance policy: ``refund`` or ``keep``."""
    from sell.services.reservations import current_policy as reservation_policy

    return reservation_policy()


def terms_for(policy: str, advance_paise: int) -> str:
    lines = [ASKED_TERMS]
    if advance_paise > 0:
        lines.append(FORFEIT_TERMS if policy == "keep" else REFUND_TERMS)
        lines.append(NO_GST_NOTE)
    return " ".join(lines)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def balance_paise(order: SpecialOrder) -> int:
    return advances.balance_paise(order)


def refund_due(order: SpecialOrder) -> bool:
    """Is money still owed back on an order that has ended?"""
    return not order.is_open and balance_paise(order) > 0


def advance_outcome(order: SpecialOrder) -> str:
    return advances.outcome(order, open_=order.is_open)


def masked(mobile: str) -> str:
    return "*" * max(len(mobile) - 4, 0) + mobile[-4:]


def booking_number(order: SpecialOrder) -> str | None:
    if order.booking is None:
        return None
    return order.booking.document.official_number


def snapshot(order: SpecialOrder) -> dict[str, Any]:
    """The order as the audit log keeps it. The number shows its last four digits."""
    voucher = getattr(order, "voucher", None)
    return {
        "ref": order.ref,
        "status": order.status,
        "customer_name": order.customer_name,
        "customer_mobile": masked(order.customer_mobile),
        "brand": order.brand.name,
        "style": order.style,
        "size": order.size,
        "colour": order.colour,
        "advance_policy": order.advance_policy,
        "route": order.route or None,
        "transfer_request": str(order.transfer_request_id) if order.transfer_request_id else None,
        "ordered_barcode": order.ordered_barcode or None,
        "booking": booking_number(order),
        "booking_line_key": str(order.booking_line_key) if order.booking_line_key else None,
        "arrived_barcode": order.arrived_barcode or None,
        "told_how": order.told_how or None,
        "voucher": voucher.number if voucher is not None else None,
        "advance_paise": voucher.amount_paise if voucher is not None else 0,
        "advance_balance_paise": balance_paise(order),
        "close_reason": order.close_reason or None,
        "sale": order.sale.doc_number if order.sale is not None else None,
    }


# ---------------------------------------------------------------------------
# Who does it
# ---------------------------------------------------------------------------


def _person(actor: Any) -> Principal:
    """The named person making the change: a transfer request is made by a person."""
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None)
    if human_id is None or tenant_id is None:
        raise _refuse(
            "SCOPE_DENIED",
            "This login is not a person in the goods records, so it cannot take a special order.",
            403,
        )
    return Principal(tenant_id=tenant_id, human_id=human_id, user_id=getattr(actor, "pk", None))


def _transfer_access(store: Store, actor: Any, principal: Principal, session: Any) -> Principal:
    """The person's own ``transfer.allocate`` at this store (as any request needs),
    and the principal that carries the grants the command relied on."""
    from outbound.transfers import ALLOCATE_ACTION

    if session is None or not hasattr(session, "token_hash"):
        raise _refuse("SCOPE_DENIED", "Sign in to request a transfer.", 403)
    assert principal.human_id is not None
    access = AccessContext(
        user=actor,
        human_id=principal.human_id,
        tenant_id=principal.tenant_id,
        session=session,
        grants=effective_grants(principal.human_id),
    )
    if not access.can(ALLOCATE_ACTION, site_id=store.pk):
        raise _refuse(
            "ACTION_DENIED",
            f"You may not request transfers for {store.code}. Ask the store manager, or "
            "choose a booking line.",
            403,
        )
    return access.principal()


# ---------------------------------------------------------------------------
# Taking the order
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Made:
    order: SpecialOrder
    created: bool


def create(store: Store, actor: Any, data: dict[str, Any]) -> Made:
    """Record what the customer wants and take any advance - or refuse whole.

    ``data``: ``id``, ``customer_name``, ``customer_mobile``, ``brand`` (id),
    ``style_code``, ``size``, ``colour``, optional ``note`` and ``advance``
    (``amount_paise``, ``mode``, ``reference``). A replay of the same id answers
    with the order it made.
    """
    order_id: uuid.UUID = data["id"]
    existing = SpecialOrder.objects.filter(pk=order_id).first()
    if existing is not None:
        if not _same_order(existing, store, data):
            raise _refuse(
                "SPECIAL_ORDER_CONFLICT",
                "This order id is already used for a different special order.",
                409,
            )
        return Made(order=existing, created=False)
    require_feature(store, SPECIAL_ORDERS)
    name, mobile = _customer(data)
    brand = _brand(data)
    style, size, colour = _item(data)
    note = str(data.get("note") or "").strip()[:200]
    amount, mode, reference = _advance(data.get("advance") or None)
    principal = _person(actor)
    today = timezone.localdate()
    policy = current_policy()

    def handler(run: CommandRun) -> CommandResult:
        run.advisory_lock(LockRank.DOCUMENT, [f"special-orders:{store.pk}"])
        order = SpecialOrder.objects.create(
            id=order_id,
            store=store,
            ref=_next_ref(store),
            customer_name=name,
            customer_mobile=mobile,
            brand=brand,
            style=style,
            size=size,
            colour=colour,
            note=note,
            asked_on=today,
            advance_policy=policy,
            terms=terms_for(policy, amount),
            created_by=actor,
        )
        if amount:
            advances.take(
                store, actor, order, amount=amount, mode=mode, reference=reference, on=today
            )
        run.audit_subject_key = f"special_order:{order.pk}"
        run.audit_site_id = store.pk
        run.audit_before = None
        run.audit_after = snapshot(order)
        return CommandResult(
            resource_type="special_order", resource_id=str(order.pk), status_code=201
        )

    with alert_on_refusal():
        execute_command(
            principal,
            CommandSpec(
                action=CREATE_ACTION,
                command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"special-order:{order_id}"),
                business_input={
                    "id": str(order_id),
                    "store": store.code,
                    "customer_mobile": masked(mobile),
                    "brand": brand.pk,
                    "style": style,
                    "size": size,
                    "colour": colour,
                    "advance_paise": amount,
                    "mode": mode,
                },
                site_id=store.pk,
                subject_key=f"special_order:{order_id}",
            ),
            handler,
        )
    return Made(order=SpecialOrder.objects.get(pk=order_id), created=True)


def _same_order(row: SpecialOrder, store: Store, data: dict[str, Any]) -> bool:
    """Is ``data`` the order ``row`` already is? (a replay, not a new one)."""
    return (
        row.store_id == store.pk
        and row.customer_mobile == normalise_mobile(str(data.get("customer_mobile") or ""))
        and str(row.brand_id) == str(data.get("brand") or "")
        and row.style == str(data.get("style_code") or "").strip()
        and row.size == str(data.get("size") or "").strip()
        and row.colour == str(data.get("colour") or "").strip()
        and _voucher_amount(row) == int((data.get("advance") or {}).get("amount_paise") or 0)
    )


def _voucher_amount(row: SpecialOrder) -> int:
    voucher = getattr(row, "voucher", None)
    return voucher.amount_paise if voucher is not None else 0


def _customer(data: dict[str, Any]) -> tuple[str, str]:
    name = str(data.get("customer_name") or "").strip()
    if not name:
        raise _refuse(
            "VALIDATION", "A special order is for a named customer: type their name.", 400
        )
    mobile = normalise_mobile(str(data.get("customer_mobile") or ""))
    if len(mobile) != 10:
        raise _refuse(
            "VALIDATION",
            "Type the customer's 10-digit mobile number, so the store can tell them it arrived.",
            400,
        )
    return name[:120], mobile


def _brand(data: dict[str, Any]) -> Brand:
    try:
        brand_id = int(data.get("brand") or 0)
    except (TypeError, ValueError):
        brand_id = 0
    brand = Brand.objects.filter(pk=brand_id, is_active=True).first() if brand_id else None
    if brand is None:
        raise _refuse("VALIDATION", "Choose the brand the customer wants.", 400)
    return brand


def _item(data: dict[str, Any]) -> tuple[str, str, str]:
    style = str(data.get("style_code") or "").strip()[:80]
    size = str(data.get("size") or "").strip()[:24]
    colour = str(data.get("colour") or "").strip()[:40]
    if not (style and size and colour):
        raise _refuse("VALIDATION", "Type the style, size and colour the customer wants.", 400)
    return style, size, colour


def _advance(advance: dict[str, Any] | None) -> tuple[int, str, str]:
    """``(amount, mode, reference)``; no advance is ``(0, "", "")``."""
    if not advance:
        return 0, "", ""
    amount = int(advance.get("amount_paise") or 0)
    mode = str(advance.get("mode") or "")
    if amount <= 0 or mode not in TENDER_MODES:
        raise _refuse("VALIDATION", "An advance is an amount in cash, card or UPI.", 400)
    return amount, mode, str(advance.get("reference") or "")[:64]


def _next_ref(store: Store) -> str:
    """The store code and a running number (``SO``), under the store's order lock."""
    count = SpecialOrder.objects.filter(store=store).count()
    return f"{store.code}-SO{count + 1}"


# ---------------------------------------------------------------------------
# One audited change
# ---------------------------------------------------------------------------


def _locked(store: Store, order_id: Any) -> SpecialOrder:
    order = SpecialOrder.objects.select_for_update().filter(pk=order_id, store=store).first()
    if order is None:
        raise _refuse("NOT_FOUND", "That special order was not found at this store.", 404)
    return order


def _write(
    principal: Principal,
    action: str,
    store: Store,
    order_id: Any,
    command_id: uuid.UUID,
    business: dict[str, Any],
    body: Callable[[CommandRun, SpecialOrder], None],
    *,
    before_lock: Callable[[CommandRun], None] | None = None,
) -> None:
    """One audited change to an order, under ``command_id``.

    Each step takes the screen's own id for the attempt, so a retry of the same
    tap replays it while a later, different attempt is judged afresh.
    ``before_lock`` runs first in the command, for work that locks sites (a
    transfer request) and must come before the order's row lock.
    """
    subject = f"special_order:{order_id}"

    def handler(run: CommandRun) -> CommandResult:
        if before_lock is not None:
            before_lock(run)
        order = _locked(store, order_id)
        before = snapshot(order)
        body(run, order)
        order.refresh_from_db()
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = before
        run.audit_after = snapshot(order)
        return CommandResult(
            resource_type="special_order", resource_id=str(order_id), status_code=200
        )

    execute_command(
        principal,
        CommandSpec(
            action=action,
            command_id=command_id,
            business_input={"id": str(order_id), "store": store.code, **business},
            site_id=store.pk,
            subject_key=subject,
        ),
        handler,
    )


def _require_status(order: SpecialOrder, allowed: Iterable[str], doing: str) -> None:
    if order.status not in allowed:
        raise _refuse(
            "SPECIAL_ORDER_STEP",
            f"{order.ref} is {order.get_status_display().lower()}, so it cannot be {doing}.",
            409,
        )


# ---------------------------------------------------------------------------
# Ordered: a transfer request or a booking line
# ---------------------------------------------------------------------------


def order_by_transfer(
    store: Store,
    actor: Any,
    order_id: Any,
    *,
    source_code: str,
    barcode: str,
    session: Any,
    command_id: uuid.UUID | None = None,
) -> None:
    """Raise a transfer request for one piece from ``source_code`` to this store."""
    from outbound import transfers

    principal = _person(actor)
    order = SpecialOrder.objects.filter(pk=order_id, store=store).first()
    if order is None:
        raise _refuse("NOT_FOUND", "That special order was not found at this store.", 404)
    # The step itself is checked inside the command, so a retry of the same tap
    # replays its answer instead of being refused as "already ordered".
    if not is_goods_site(store):
        raise _refuse(
            "SPECIAL_ORDER_GOODS_ONLY",
            f"{store.code} does not take transfers on goods records yet. Choose a booking line.",
        )
    principal = _transfer_access(store, actor, principal, session)
    code = (source_code or "").strip().upper()
    source = (
        Store.objects.filter(code=code, is_active=True, tenant_id=store.tenant_id)
        .exclude(pk=store.pk)
        .first()
    )
    if source is None or not is_goods_site(source):
        raise _refuse(
            "SPECIAL_ORDER_SOURCE",
            "Choose the store or warehouse that holds the item, other than this one.",
        )
    tag = (barcode or "").strip()
    sku_id = sku_for_barcode(store, tag) if tag else None
    if sku_id is None:
        raise _refuse(
            "SPECIAL_ORDER_ITEM_UNKNOWN",
            f"Barcode {tag or '(blank)'} is not one item in the catalogue. Scan the item's tag.",
        )
    request_body = {
        "source_site_id": source.pk,
        "destination_site_id": store.pk,
        "note": f"Special order {order.ref} for a customer.",
        "lines": [
            {
                "line_key": str(uuid.uuid5(order.pk, "transfer")),
                "sku_id": str(sku_id),
                "qty": 1,
                "note": f"{order.brand.name} {order.style} {order.colour} {order.size}"[:240],
            }
        ],
    }
    made: dict[str, Any] = {}

    def raise_request(run: CommandRun) -> None:
        made["request"] = transfers.create_request(run, request_body)

    def body(run: CommandRun, locked: SpecialOrder) -> None:
        _require_status(locked, [SpecialOrder.Status.ASKED], "ordered again")
        _mark_ordered(
            locked,
            actor,
            route=SpecialOrder.Route.TRANSFER,
            transfer_request=made["request"],
            ordered_barcode=tag,
        )

    _write(
        principal,
        ORDER_ACTION,
        store,
        order.pk,
        command_id or uuid.uuid4(),
        {"route": "transfer", "source": source.code, "barcode": tag},
        body,
        before_lock=raise_request,
    )


def order_by_booking(
    store: Store,
    actor: Any,
    order_id: Any,
    *,
    number: str,
    command_id: uuid.UUID | None = None,
) -> None:
    """Put the order on the line of a confirmed booking with the same brand and style."""
    from vendors.goods_services import (
        booking_by_document,
        booking_head,
        booking_lines,
        booking_state,
    )

    principal = _person(actor)
    wanted = (number or "").strip()

    def body(run: CommandRun, order: SpecialOrder) -> None:
        _require_status(order, [SpecialOrder.Status.ASKED], "ordered again")
        identity = DocumentIdentity.objects.filter(
            tenant_id=store.tenant_id, purpose="booking", official_number__iexact=wanted
        ).first()
        booking = booking_by_document(identity.pk) if identity is not None and wanted else None
        if booking is None:
            raise _refuse(
                "SPECIAL_ORDER_BOOKING_UNKNOWN",
                f"No confirmed booking is numbered {wanted or '(blank)'}.",
            )
        head = booking_head(booking)
        if booking_state(booking, head) != "confirmed":
            raise _refuse(
                "SPECIAL_ORDER_BOOKING_UNKNOWN",
                f"Booking {wanted} is not open for goods: it is a draft, closed or cancelled.",
            )
        if booking.brand_id != order.brand_id:
            raise _refuse(
                "SPECIAL_ORDER_BOOKING_MISMATCH",
                f"Booking {wanted} is for another brand, not {order.brand.name}.",
            )
        line_key = _booking_line_for(store, order, *booking_lines(booking, head)[:2])
        if line_key is None:
            raise _refuse(
                "SPECIAL_ORDER_BOOKING_MISMATCH",
                f"Booking {wanted} has no line for style {order.style} coming to {store.code}. "
                "Ask the buyer to add one.",
            )
        _mark_ordered(
            order,
            actor,
            route=SpecialOrder.Route.BOOKING,
            booking=booking,
            booking_line_key=uuid.UUID(line_key),
        )

    _write(
        principal,
        ORDER_ACTION,
        store,
        order_id,
        command_id or uuid.uuid4(),
        {"route": "booking", "booking": wanted},
        body,
    )


def _booking_line_for(
    store: Store, order: SpecialOrder, header: dict[str, Any], lines: list[dict[str, Any]]
) -> str | None:
    """The booking line this order is: same style, coming to this store (or not
    yet placed). A line addressed to this store is preferred."""
    here = str(store.pk)
    default = header.get("destination_site_id")
    matches: list[tuple[int, str]] = []
    for line in lines:
        if str(line.get("style_code") or "").strip().casefold() != order.style.casefold():
            continue
        going = line.get("destination_site_id") or default
        if going is not None and str(going) != here:
            continue
        matches.append((0 if going is not None else 1, str(line["line_key"])))
    return min(matches)[1] if matches else None


def _mark_ordered(order: SpecialOrder, actor: Any, **fields: Any) -> None:
    order.status = SpecialOrder.Status.ORDERED
    order.ordered_at = timezone.now()
    order.ordered_by = advances.user_of(actor)
    for name, value in fields.items():
        setattr(order, name, value)
    order.save()


# ---------------------------------------------------------------------------
# Arrived, customer told
# ---------------------------------------------------------------------------


def arrive(
    store: Store,
    actor: Any,
    order_id: Any,
    *,
    barcode: str,
    command_id: uuid.UUID | None = None,
) -> None:
    """The piece is at the store: record its barcode, which the bill must carry."""
    tag = (barcode or "").strip()[:64]
    if not tag:
        raise _refuse("VALIDATION", "Scan the tag of the piece that arrived.", 400)

    def body(run: CommandRun, order: SpecialOrder) -> None:
        _require_status(order, [SpecialOrder.Status.ORDERED], "marked arrived")
        _check_arrived_piece(store, order, tag)
        order.status = SpecialOrder.Status.ARRIVED
        order.arrived_barcode = tag
        order.arrived_at = timezone.now()
        order.arrived_by = advances.user_of(actor)
        order.save(
            update_fields=["status", "arrived_barcode", "arrived_at", "arrived_by", "updated_at"]
        )

    _write(
        _person(actor),
        ARRIVE_ACTION,
        store,
        order_id,
        command_id or uuid.uuid4(),
        {"barcode": tag},
        body,
    )


def _check_arrived_piece(store: Store, order: SpecialOrder, tag: str) -> None:
    """At a goods store the tag must name one catalogue item of the brand asked
    for - and, on a transfer request, the item requested. A mistyped tag would
    leave the order impossible to collect."""
    from masters.goods_identity_models import ProductSku

    if order.route == SpecialOrder.Route.TRANSFER and tag != order.ordered_barcode:
        raise _refuse(
            "SPECIAL_ORDER_WRONG_PIECE",
            f"{order.ref} asked for {order.ordered_barcode} on its transfer request; "
            f"{tag} is another item.",
        )
    if not is_goods_site(store):
        return
    sku_id = sku_for_barcode(store, tag)
    brand_id = (
        ProductSku.objects.filter(pk=sku_id).values_list("style__brand_id", flat=True).first()
        if sku_id is not None
        else None
    )
    if brand_id is None:
        raise _refuse(
            "SPECIAL_ORDER_ITEM_UNKNOWN",
            f"Barcode {tag} is not one item in the catalogue. Scan the piece's tag.",
        )
    if brand_id != order.brand_id:
        raise _refuse(
            "SPECIAL_ORDER_WRONG_PIECE",
            f"{tag} is not a {order.brand.name} item, which is what {order.ref} asked for.",
        )


def tell(
    store: Store,
    actor: Any,
    order_id: Any,
    *,
    how: str,
    command_id: uuid.UUID | None = None,
) -> None:
    """Record that the customer was told their order arrived, and how."""
    if how not in SpecialOrder.Told.values:
        raise _refuse(
            "VALIDATION", "Say how the customer was told: call, message or in person.", 400
        )

    def body(run: CommandRun, order: SpecialOrder) -> None:
        _require_status(order, [SpecialOrder.Status.ARRIVED], "marked told")
        order.status = SpecialOrder.Status.TOLD
        order.told_how = how
        order.told_at = timezone.now()
        order.told_by = advances.user_of(actor)
        order.save(update_fields=["status", "told_how", "told_at", "told_by", "updated_at"])

    _write(
        _person(actor),
        TELL_ACTION,
        store,
        order_id,
        command_id or uuid.uuid4(),
        {"how": how},
        body,
    )


# ---------------------------------------------------------------------------
# Cancelled, refunded
# ---------------------------------------------------------------------------


def _close(
    order: SpecialOrder, status: str, reason: str, actor: Any, sale: Sale | None = None
) -> None:
    order.status = status
    order.close_reason = reason
    order.closed_at = timezone.now()
    order.closed_by = advances.user_of(actor)
    order.sale = sale
    order.save(
        update_fields=["status", "close_reason", "closed_at", "closed_by", "sale", "updated_at"]
    )


def cancel(
    store: Store,
    actor: Any,
    order_id: Any,
    *,
    by_customer: bool,
    command_id: uuid.UUID | None = None,
) -> None:
    """Cancel an open order now; the customer is at the counter or on the phone.

    The advance is refunded at once, unless the customer cancelled and the
    order's policy keeps it (ticket 20's policy). A transfer request already
    raised stays with the sending site to answer.
    """

    def body(run: CommandRun, order: SpecialOrder) -> None:
        _require_status(order, OPEN, "cancelled")
        _close(
            order,
            SpecialOrder.Status.CANCELLED,
            SpecialOrder.CloseReason.CUSTOMER_CANCELLED
            if by_customer
            else SpecialOrder.CloseReason.STORE_CANCELLED,
            actor,
        )
        held = balance_paise(order)
        if held <= 0:
            return
        if by_customer and order.advance_policy == "keep":
            advances.forfeit(order, actor, held, why="Order cancelled")
        else:
            advances.pay_back(order, actor, held)

    _write(
        _person(actor),
        CANCEL_ACTION,
        store,
        order_id,
        command_id or uuid.uuid4(),
        {"by": "customer" if by_customer else "store"},
        body,
    )


def refund(store: Store, actor: Any, order_id: Any, *, command_id: uuid.UUID | None = None) -> None:
    """Pay back what an ended order still holds (left over after collection)."""

    def body(run: CommandRun, order: SpecialOrder) -> None:
        if order.is_open:
            raise _refuse(
                "SPECIAL_ORDER_OPEN",
                f"{order.ref} is still open. Cancel it to refund the advance.",
                409,
            )
        held = balance_paise(order)
        if held <= 0:
            raise _refuse("NOTHING_TO_REFUND", f"{order.ref} holds no advance to refund.", 409)
        advances.pay_back(order, actor, held)

    _write(
        _person(actor),
        REFUND_ACTION,
        store,
        order_id,
        command_id or uuid.uuid4(),
        {},
        body,
    )


# ---------------------------------------------------------------------------
# Collection, inside the bill's own transaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Collection:
    order: SpecialOrder
    advance_paise: int


def check_for_bill(store: Store, data: dict[str, Any], actor: Any) -> Collection | None:
    """The special order this bill collects, checked and locked - or ``None``.

    Refused (the bill is not taken) when the login is not a person, the order is
    not this store's, the customer has not been told it arrived, it has ended,
    the bill does not carry the piece that arrived, or it uses more advance than
    the order holds.
    """
    from sell.services.accept import AcceptError

    order_id = data.get("special_order")
    if not order_id:
        return None
    try:
        _person(actor)
    except Refusal as refusal:
        raise AcceptError(refusal.code, refusal.message, refusal.status) from refusal
    order = SpecialOrder.objects.select_for_update().filter(pk=order_id, store=store).first()
    if order is None:
        raise AcceptError(
            "SPECIAL_ORDER_NOT_FOUND", "That special order is not one of this store's.", 422
        )
    if order.status not in COLLECTABLE:
        raise AcceptError(
            "SPECIAL_ORDER_CLOSED" if not order.is_open else "SPECIAL_ORDER_NOT_READY",
            f"{order.ref} is {order.get_status_display().lower()}, so it cannot be collected on "
            "a bill.",
            409,
        )
    advance = sum(
        int(t["amount_paise"]) for t in data["tenders"] if t["mode"] == SaleTender.Mode.ADVANCE
    )
    held = balance_paise(order)
    if advance > held:
        raise AcceptError(
            "SPECIAL_ORDER_ADVANCE",
            f"{order.ref} holds Rs {held / 100:,.2f} of advance; the bill used "
            f"Rs {advance / 100:,.2f}.",
            422,
        )
    on_bill = any(
        line["direction"] == "sale" and str(line["barcode"]).strip() == order.arrived_barcode
        for line in data["all_lines"]
    )
    if not on_bill:
        raise AcceptError(
            "SPECIAL_ORDER_PIECE_MISSING",
            f"{order.ref} is collected with the piece that arrived ({order.arrived_barcode}); "
            "scan it onto the bill.",
            422,
        )
    return Collection(order=order, advance_paise=advance)


def record_collection(sale: Sale, collection: Collection | None, actor: Any) -> None:
    """The order collected on ``sale``, and the advance the bill used, audited.

    ``None`` - a bill collecting no special order - records nothing.
    """
    if collection is None:
        return

    def body(run: CommandRun, locked: SpecialOrder) -> None:
        _close(
            locked,
            SpecialOrder.Status.COLLECTED,
            SpecialOrder.CloseReason.COLLECTED,
            actor,
            sale=sale,
        )
        if collection.advance_paise > 0:
            advances.use(locked, actor, collection.advance_paise, sale)

    order = collection.order
    _write(
        _person(actor),
        COLLECT_ACTION,
        sale.store,
        order.pk,
        uuid.uuid5(uuid.NAMESPACE_URL, f"special-order:{order.pk}:collect:{sale.idempotency_uuid}"),
        {"sale": sale.doc_number, "advance_paise": collection.advance_paise},
        body,
    )


__all__ = [
    "Collection",
    "Made",
    "advance_outcome",
    "arrive",
    "balance_paise",
    "cancel",
    "check_for_bill",
    "create",
    "order_by_booking",
    "order_by_transfer",
    "record_collection",
    "refund",
    "refund_due",
    "tell",
]
