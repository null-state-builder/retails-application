"""The day-close cash count by note and coin (store operations ticket 41, ST-MNY-2).

At the Z-report the cashier counts the drawer by note and coin and the count is
compared with the cash the system expected to be there (overall PRD R-POS-011,
R-FIN-015):

    expected = opening + cash sales - cash refunds - deposits and handovers
               - petty cash spent + petty cash brought from head office

**What a count takes in.** Every accepted, not cancelled bill of the store that
no earlier count took in (``CashCountBill``: a bill is in one count at most),
received on or after the day the store started counting. Bills are chosen by
whether they are counted yet, never by a clock: a bill that reaches the server
after a count - a slow sync, a second device, a till clock running behind - is
taken in by the next count, and none is counted twice. Movements (deposits and
handovers) are written under the same lock as counts, so they are taken by the
time they were recorded: after the previous count, up to this one. The opening
is what the previous count found in the drawer. A store's first count has no
previous one, so the cashier declares the opening float
(``opening_declared``) and the count starts from midnight of its business day.

**The business day** of a count is the India date a few hours earlier
(``KDPS_CASH_DAY_CUTOFF_HOURS``, default 4): a store closing after midnight
closes the day it traded, not tomorrow.

**What each figure is, today.**

* Cash sales: cash tenders on the bills taken in. Tenders are the amount the
  bill took, not what the customer handed over, so change given is already out.
* Cash refunds: nought. The counter has no cash refund path: a return nets
  inside an exchange bill, and a negative exchange issues a credit note (a
  cash refund for a cheaper exchange is baseline B81, not built yet). The
  figure is kept on every count so a later refund path fills it in.
* Deposits and handovers: ``CashMovement`` rows in the window.
* Petty cash (ticket 42): the count is of the store's cash, the drawer and the
  petty cash box together (baseline B125). Money spent from the box in the
  window comes off; cash head office brought for the box comes on. A top-up
  from the till moves cash inside the store and changes nothing.

**A variance** (counted minus expected, to the paisa) is kept on the count and
confirmed by a manager of this store with their own PIN, never by the person
who counted. The till checks the PIN on the device, as a bill's override does
(ticket 06), and - because this request is online - the server checks it again
against the manager's hash; the PIN itself is never stored or audited. Three
wrong PINs pause the store's confirmations for half a minute, as the till does.
A variance opens an owned ``cash_variance`` exception and a cash-variance alert.
It is never a balancing entry: nothing here writes to any ledger.

**Online only.** The count and the movements are saved straight to the server,
never queued on the device: the expected cash is only true once the server
holds every bill, and the till refuses to count while it still has bills to
send. A request sent twice (the line dropped before the answer came back) is
recognised by the till's own id and answers with what was saved; the same id
with different figures is refused.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from django.conf import settings
from django.db.models import QuerySet, Sum
from django.utils import timezone

from accounts.till_pin import verify_till_pin
from alerts.goods_services import open_exception
from alerts.models import Alert, AlertKind, AlertStatus
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from core.documents import DocStatus
from core.kernel_models import AuditEvent
from core.refusals import Refusal
from masters.models import Store
from sell.cash_models import CashCount, CashCountBill, CashMovement
from sell.models import Sale, SaleTender
from sell.services import petty_cash
from sell.services.resolve import manager_for_override

#: The notes counted one by one, by face value in rupees (ST-MNY-2). Coins are
#: counted as one amount beside them (ticket 41 baseline B90).
DENOMINATIONS: tuple[int, ...] = (500, 200, 100, 50, 20, 10)

#: The most pieces of one note a count may say. Far above any drawer; it only
#: keeps a typing slip from reaching the database as an overflow.
MAX_PIECES = 100_000

COUNT_ACTION = "sell.cash_count.record"
MOVE_ACTION = "sell.cash_movement.record"
EXCEPTION_KIND = "cash_variance"

#: Wrong PINs before the store's confirmations pause, and for how long - the
#: till's own numbers (`ManagerPin.WRONG_PINS_BEFORE_A_PAUSE`, `PAUSE_MS`).
WRONG_PINS_BEFORE_A_PAUSE = 3
PIN_PAUSE = timedelta(seconds=30)

#: Every tender mode the Z-report shows, in the order it reads them.
_TENDER_MODES: tuple[str, ...] = (
    SaleTender.Mode.CASH,
    SaleTender.Mode.CARD,
    SaleTender.Mode.UPI,
    SaleTender.Mode.CREDIT_NOTE,
)

_ALREADY = "CASH_ALREADY_RECORDED"


def business_day(moment: datetime) -> date:
    """The trading day a count at `moment` closes (see the module docstring)."""
    hours = int(getattr(settings, "KDPS_CASH_DAY_CUTOFF_HOURS", 4))
    return timezone.localdate(moment - timedelta(hours=hours))


@dataclass(frozen=True)
class CashPosition:
    """The drawer as the server sees it at one moment, since the last count."""

    store: Store
    business_day: date
    window_from: datetime
    window_to: datetime
    #: No earlier count: the opening is declared and the count starts at midnight.
    first: bool
    previous: CashCount | None
    #: The bills this count would take in.
    sale_ids: list[int]
    cash_sales_paise: int
    cash_refunds_paise: int
    movements: list[CashMovement]
    petty_cash_paise: int
    #: Petty cash brought from head office in the window (ticket 42).
    petty_top_ups_paise: int
    tenders: dict[str, int]
    #: The day's count, if it has been saved.
    counted: CashCount | None
    #: Whether the store's switch is on. Off, the page shows saved counts only.
    switched_on: bool = True

    @property
    def bills(self) -> int:
        return len(self.sale_ids)

    @property
    def opening_paise(self) -> int | None:
        return self.previous.counted_paise if self.previous is not None else None

    @property
    def movements_paise(self) -> int:
        return sum(int(m.amount_paise) for m in self.movements)

    def expected(self, opening_paise: int) -> int:
        return (
            opening_paise
            + self.cash_sales_paise
            - self.cash_refunds_paise
            - self.movements_paise
            - self.petty_cash_paise
            + self.petty_top_ups_paise
        )

    @property
    def expected_paise(self) -> int | None:
        """The expected cash, or ``None`` while the opening is still to be declared."""
        opening = self.opening_paise
        return None if opening is None else self.expected(opening)


def _uncounted_bills(store: Store, since: datetime) -> QuerySet[Sale]:
    return (
        Sale.objects.filter(
            store=store,
            doc_number__isnull=False,
            created_at__gte=since,
            cash_count_link__isnull=True,
        )
        .exclude(docstatus=DocStatus.CANCELLED)
        .order_by("id")
    )


def cash_position(
    store: Store, now: datetime | None = None, *, switched_on: bool = True
) -> CashPosition:
    """`store`'s drawer since its previous count, as it stands at `now`."""
    now = now or timezone.now()
    day = business_day(now)
    previous = CashCount.objects.filter(store=store, counted_at__lte=now).first()
    if previous is not None:
        window_from = previous.counted_at
        since = (
            CashCount.objects.filter(store=store)
            .order_by("counted_at")
            .values_list("window_from", flat=True)[0]
        )
        moves = CashMovement.objects.filter(recorded_at__gt=window_from)
    else:
        window_from = timezone.make_aware(datetime.combine(day, time.min))
        since = window_from
        moves = CashMovement.objects.filter(recorded_at__gte=window_from)
    sale_ids = list(_uncounted_bills(store, since).values_list("id", flat=True))
    tenders = {mode: 0 for mode in _TENDER_MODES}
    rows = (
        SaleTender.objects.filter(sale_id__in=sale_ids, mode__in=_TENDER_MODES)
        .values("mode")
        .annotate(total=Sum("amount_paise"))
    )
    for row in rows:
        tenders[row["mode"]] = int(row["total"] or 0)
    movements = list(
        moves.filter(store=store, recorded_at__lte=now)
        .select_related("given_by")
        .order_by("recorded_at")
    )
    petty_spent, petty_brought = petty_cash.for_cash_count(
        store, window_from, now, first=previous is None
    )
    return CashPosition(
        store=store,
        business_day=day,
        window_from=window_from,
        window_to=now,
        first=previous is None,
        previous=previous,
        sale_ids=sale_ids,
        cash_sales_paise=tenders[SaleTender.Mode.CASH],
        cash_refunds_paise=0,
        movements=movements,
        petty_cash_paise=petty_spent,
        petty_top_ups_paise=petty_brought,
        tenders=tenders,
        counted=CashCount.objects.filter(store=store, business_day=day).first(),
        switched_on=switched_on,
    )


# --- saving a count -------------------------------------------------------------


@dataclass(frozen=True)
class Saved:
    """A row the request wrote, or found already written (a replay)."""

    row: Any
    created: bool


def _validation(message: str) -> Refusal:
    return Refusal("VALIDATION", message, status=400)


def counted_paise(notes: dict[str, int], coins_paise: int) -> int:
    """What the cashier counted: each note times its face value, plus the coins."""
    return sum(int(notes[str(face)]) * face * 100 for face in DENOMINATIONS) + coins_paise


def _check_notes(notes: dict[str, Any], coins_paise: int) -> dict[str, int]:
    wanted = {str(face) for face in DENOMINATIONS}
    if set(notes) != wanted:
        raise _validation(
            "Count every note: Rs " + ", ".join(str(face) for face in DENOMINATIONS) + "."
        )
    clean = {key: int(notes[key]) for key in sorted(wanted, key=int, reverse=True)}
    if any(not 0 <= pieces <= MAX_PIECES for pieces in clean.values()):
        raise _validation(f"A note count is a whole number from 0 to {MAX_PIECES:,}.")
    if not 0 <= coins_paise <= MAX_PIECES * 100:
        raise _validation("Coins cannot be below nought.")
    return clean


def _principal(store: Store, actor: Any) -> Principal:
    """The person at the till, or - for a login that is not a person - the till."""
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None)
    user_id = getattr(actor, "pk", None)
    if human_id is not None and tenant_id is not None:
        from accounts.principal import access_for_user
        access = access_for_user(actor)
        if access.session is not None:
            return access.principal()
        return Principal(tenant_id=tenant_id, human_id=human_id, user_id=user_id)
    raise Refusal("AUTH_REQUIRED", "A cash close requires the authenticated person at the counter.")


def _check_approver(
    run: CommandRun, store: Store, actor: Any, approved_by: int | None, pin: str
) -> None:
    """A variance needs a manager of this store, not the cashier, and their own PIN."""
    from sell.services.online import online_alpha
    if online_alpha(store):
        raise Refusal("INDEPENDENT_APPROVAL_REQUIRED",
                      "A cash variance requires a recorded independent approval before this online store can close it. Keep the count draft and arrange an authorised review.",
                      status=409)
    if approved_by is None or not pin:
        raise Refusal(
            "MANAGER_PIN_NEEDED",
            "The count does not match the expected cash. A manager of this store has to "
            "type their own PIN to confirm it.",
            status=400,
        )
    if approved_by == getattr(actor, "pk", None):
        raise Refusal(
            "SELF_APPROVAL",
            "You cannot confirm your own count. Another manager of this store has to "
            "type their PIN.",
            status=403,
        )
    wrong = AuditEvent.objects.filter(
        action=COUNT_ACTION,
        site_id=store.pk,
        outcome="refused",
        reason_code="WRONG_PIN",
        recorded_at__gte=run.now - PIN_PAUSE,
    ).count()
    if wrong >= WRONG_PINS_BEFORE_A_PAUSE:
        raise Refusal(
            "PIN_PAUSED",
            "Too many wrong PINs. This waits half a minute before it will take another.",
            status=429,
        )
    manager = manager_for_override(approved_by, store, own_pin_rules=True)
    if manager is None or not manager.till_pin_hash:
        raise Refusal(
            "NOT_A_MANAGER", "That person cannot confirm a count at this store.", status=403
        )
    if not verify_till_pin(pin, manager.till_pin_hash):
        raise Refusal("WRONG_PIN", "That is not a manager's PIN for this store.", status=403)


def _replay(model: type[Any], row_id: uuid.UUID, store: Store, same: Any) -> Saved | None:
    """The row this id already saved, if the request is the same one again."""
    row = model.objects.filter(pk=row_id).first()
    if row is None:
        return None
    if row.store_id != store.pk or not same(row):
        raise Refusal(
            "COMMAND_CONFLICT",
            "This was already saved with different figures. Read the page again.",
            status=409,
        )
    return Saved(row=row, created=False)


def _rupees(paise: int) -> str:
    """Rupees in Indian grouping (Rs 1,23,456.50), for the alert's frozen title."""
    rupees, rest = divmod(abs(paise), 100)
    digits = str(rupees)
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    grouped = ",".join(filter(None, [head, *groups, tail]))
    return f"Rs {grouped}" + (f".{rest:02d}" if rest else "")


def count_facts(row: CashCount) -> dict[str, Any]:
    """The count as the audit log keeps it."""
    return {
        "counted": True,
        "business_day": row.business_day.isoformat(),
        "opening_paise": row.opening_paise,
        "opening_declared": row.opening_declared,
        "cash_sales_paise": row.cash_sales_paise,
        "cash_refunds_paise": row.cash_refunds_paise,
        "movements_paise": row.movements_paise,
        "petty_cash_paise": row.petty_cash_paise,
        "petty_top_ups_paise": row.petty_top_ups_paise,
        "expected_paise": row.expected_paise,
        "notes": row.notes,
        "coins_paise": row.coins_paise,
        "counted_paise": row.counted_paise,
        "variance_paise": row.variance_paise,
        "bills": row.bills.count(),
        "counted_by_user_id": row.counted_by_id,
        "approved_by_user_id": row.approved_by_id,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "till_number": row.till_number,
    }


def _opening(here: CashPosition, declared: Any) -> int:
    """The previous count's cash, or - on a store's first count - the float typed."""
    if not here.first:
        return int(here.opening_paise or 0)
    if declared is None or int(declared) < 0:
        raise _validation("This is the store's first count: type the opening float.")
    return int(declared)


def record_count(store: Store, actor: Any, data: dict[str, Any]) -> Saved:
    """Save the day's count once, audited; a variance opens its exception and alert."""
    count_id: uuid.UUID = data["id"]
    coins = int(data.get("coins_paise") or 0)
    notes = _check_notes(dict(data.get("notes") or {}), coins)
    pin = str(data.get("manager_pin") or "")

    def same(row: CashCount) -> bool:
        return row.notes == notes and row.coins_paise == coins

    replayed = _replay(CashCount, count_id, store, same)
    if replayed is not None:
        return replayed
    subject = f"cash_count:{count_id}"

    def handler(run: CommandRun) -> CommandResult:
        run.advisory_lock(LockRank.DOCUMENT, [f"cash-drawer:{store.pk}"])
        if CashCount.objects.filter(pk=count_id).exists():
            raise Refusal(_ALREADY, "This count is already saved.", status=409)
        now = run.now
        here = cash_position(store, now)
        if here.counted is not None:
            raise Refusal(
                "ALREADY_COUNTED",
                "Today's cash at this store is already counted.",
                status=409,
            )
        opening = _opening(here, data.get("opening_paise"))
        expected = here.expected(opening)
        if int(data["expected_paise"]) != expected:
            raise Refusal(
                "CASH_CHANGED",
                "A bill or a deposit arrived since this screen was opened. Look at the "
                "expected cash again before saving.",
                status=409,
            )
        counted = counted_paise(notes, coins)
        variance = counted - expected
        approved_by = data.get("approved_by") if variance else None
        if variance:
            _check_approver(run, store, actor, approved_by, pin)
        row = CashCount.objects.create(
            id=count_id,
            store=store,
            business_day=here.business_day,
            counted_at=now,
            window_from=here.window_from,
            previous=here.previous,
            opening_paise=opening,
            opening_declared=here.first,
            cash_sales_paise=here.cash_sales_paise,
            cash_refunds_paise=here.cash_refunds_paise,
            movements_paise=here.movements_paise,
            petty_cash_paise=here.petty_cash_paise,
            petty_top_ups_paise=here.petty_top_ups_paise,
            expected_paise=expected,
            notes=notes,
            coins_paise=coins,
            counted_paise=counted,
            variance_paise=variance,
            counted_by=actor,
            approved_by_id=approved_by,
            # The server's clock: the PIN was checked here, now.
            approved_at=now if approved_by else None,
            till_number=str(data.get("till_number") or "")[:64],
        )
        CashCountBill.objects.bulk_create(
            [CashCountBill(count=row, sale_id=sale_id) for sale_id in here.sale_ids]
        )
        if variance:
            _raise_variance(run, row, store)
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = {"business_day": here.business_day.isoformat(), "counted": False}
        run.audit_after = count_facts(row)
        return CommandResult(resource_type="cash_count", resource_id=str(row.pk), status_code=201)

    # The PIN never leaves this function: not in the command's stored input, not
    # in the audit record.
    stored = {key: value for key, value in data.items() if key != "manager_pin"}
    return _run(store, actor, COUNT_ACTION, count_id, subject, stored, handler, CashCount, same)


def _raise_variance(run: CommandRun, row: CashCount, store: Store) -> None:
    """The owned exception and the alert for a count that did not match."""
    short = row.variance_paise < 0
    open_exception(
        run,
        kind=EXCEPTION_KIND,
        site_id=store.pk,
        subject_key=f"cash_count:{row.pk}",
        reason_code="SHORT" if short else "OVER",
        source_event_key=row.pk,
    )
    Alert.objects.create(
        kind=AlertKind.CASH_VARIANCE,
        kind_label=AlertKind.CASH_VARIANCE.label,
        title=(
            f"Cash {'short' if short else 'over'} by {_rupees(row.variance_paise)} at "
            f"{store.code} on {row.business_day:%d %b %Y}"
        )[:240],
        dedupe_key=f"cash_count:{row.pk}",
        store=store,
        due_date=row.business_day,
        status=AlertStatus.OPEN,
    )


def _run(
    store: Store,
    actor: Any,
    action: str,
    row_id: uuid.UUID,
    subject: str,
    data: dict[str, Any],
    handler: Any,
    model: type[Any],
    same: Any,
) -> Saved:
    try:
        execute_command(
            _principal(store, actor),
            CommandSpec(
                action=action,
                command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{action}:{row_id}"),
                business_input={
                    key: (str(value) if isinstance(value, uuid.UUID | datetime) else value)
                    for key, value in data.items()
                },
                site_id=store.pk,
                subject_key=subject,
            ),
            handler,
        )
    except Refusal as refusal:
        if refusal.code != _ALREADY:
            raise
        # Another copy of the same request got there first, under the same lock.
        again = _replay(model, row_id, store, same)
        if again is None:  # pragma: no cover - the handler just saw it
            raise
        return again
    return Saved(row=model.objects.get(pk=row_id), created=True)


# --- deposits and handovers -----------------------------------------------------


def record_movement(store: Store, actor: Any, data: dict[str, Any]) -> Saved:
    """Record cash leaving the drawer, with both sides named, once, audited."""
    move_id: uuid.UUID = data["id"]
    kind = str(data.get("kind") or "")
    amount = int(data.get("amount_paise") or 0)
    received_by = str(data.get("received_by") or "").strip()[:120]
    reference = str(data.get("reference") or "").strip()[:64]

    def same(row: CashMovement) -> bool:
        return (row.kind, row.amount_paise, row.received_by, row.reference) == (
            kind,
            amount,
            received_by,
            reference,
        )

    replayed = _replay(CashMovement, move_id, store, same)
    if replayed is not None:
        return replayed
    if kind not in CashMovement.Kind.values:
        raise _validation("Say whether this is a bank deposit or a handover.")
    if amount <= 0:
        raise _validation("The amount must be more than nought.")
    if not received_by:
        raise _validation(
            "Name the bank." if kind == CashMovement.Kind.DEPOSIT else "Name who received it."
        )
    if not reference:
        raise _validation(
            "Type the deposit slip number."
            if kind == CashMovement.Kind.DEPOSIT
            else "Type the receipt number they signed."
        )
    subject = f"cash_movement:{move_id}"

    def handler(run: CommandRun) -> CommandResult:
        run.advisory_lock(LockRank.DOCUMENT, [f"cash-drawer:{store.pk}"])
        if CashMovement.objects.filter(pk=move_id).exists():
            raise Refusal(_ALREADY, "This is already recorded.", status=409)
        row = CashMovement.objects.create(
            id=move_id,
            store=store,
            kind=kind,
            amount_paise=amount,
            given_by=actor,
            received_by=received_by,
            reference=reference,
            recorded_at=run.now,
            till_number=str(data.get("till_number") or "")[:64],
        )
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = {"recorded": False}
        run.audit_after = {
            "recorded": True,
            "kind": row.kind,
            "amount_paise": row.amount_paise,
            "given_by_user_id": row.given_by_id,
            "received_by": row.received_by,
            "reference": row.reference,
            "till_number": row.till_number,
        }
        return CommandResult(
            resource_type="cash_movement", resource_id=str(row.pk), status_code=201
        )

    return _run(store, actor, MOVE_ACTION, move_id, subject, data, handler, CashMovement, same)


def recent_counts(store: Store, limit: int = 7) -> list[CashCount]:
    """The store's latest counts, newest first, for the screen's history."""
    return list(
        CashCount.objects.filter(store=store).select_related("counted_by", "approved_by")[:limit]
    )
