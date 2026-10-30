"""Closing a store's business day (store operations, R-POS-011 / R-FIN-015).

The store works down five steps, and the close is refused while any is open:

1. **All bills synced.** Head office holds every bill the counter numbered: no
   holes below the last accepted number (`sell.services.register`), and the
   counter itself says it has nothing waiting to send. The counter's own queue
   lives on the device; the server checks what it can see and records what the
   counter said.
2. **Held bills cleared.** No parked cart on the counter's mirror
   (``HeldBill``) still waiting for an answer. A cart the store chose to keep
   for tomorrow (``kept``) is an answer and carries over.
3. **Cash counted.** The day's cash count (`sell.services.cash_count`) is
   saved. A count that did not match was already confirmed by a manager when it
   was saved, so a saved count is a count that matches or is accounted for.
4. **Day summary checked.** A person read the Day summary and ticked it, and no
   bill of the day arrived or was cancelled since.
5. **Close the day.** A manager of this store confirms with their own PIN,
   checked on the device and again here. The close is audited, and asking again
   for a closed day answers with the close already recorded.

**Which day.** The business day is the cash count's: the India date a few
hours earlier (`cash_count.business_day`), so a store closing after midnight
closes the day it traded.

**Billing after the close.** The till has no business date of its own: a bill
carries ``billed_at``, the invoice time on the customer's copy, and that never
moves. What moves is which day's close a bill belongs to. A bill belongs to the
day it was billed on, unless the server accepted it after that day was closed;
then it belongs to the next business day (`day_bills`). The closed day's
figures are frozen on the close (``DayClose.facts``) and the late bill is
carried into the next day's close and its cash count (a count takes every bill
no earlier count took). So counters keep billing after a close, into the next
business day, and the closed day's count and summary stay final.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from django.db.models import Max, Q, QuerySet, Sum
from django.utils import timezone

from accounts.till_pin import verify_till_pin
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
from masters.store_feature_registry import CASH_COUNT
from masters.store_features import is_feature_on
from sell.alteration_models import AlterationJob
from sell.cash_models import CashCount
from sell.day_close_models import DayClose
from sell.models import HeldBill, Sale, SaleTender
from sell.reservation_models import CustomerReservation
from sell.services.cash_count import (
    PIN_PAUSE,
    WRONG_PINS_BEFORE_A_PAUSE,
    Saved,
    _principal,
    business_day,
)
from sell.services.register import register_state
from sell.services.resolve import manager_for_override
from sell.services.till_authority import active_till
from sell.special_order_models import SpecialOrder

SUMMARY_ACTION = "sell.day_close.summary_checked"
CLOSE_ACTION = "sell.day_close.close"

_ALREADY = "DAY_ALREADY_RECORDED"


# --- the day's bills ------------------------------------------------------------


@dataclass(frozen=True)
class DayBills:
    """A set of accepted bills, summed by tender."""

    bills: int = 0
    net_paise: int = 0
    tenders: dict[str, int] = field(default_factory=dict)
    #: The highest bill id in the set: a new bill moves it, whatever its amount.
    frontier: int = 0

    def fingerprint(self) -> dict[str, int]:
        return {"bills": self.bills, "frontier": self.frontier, "net_paise": self.net_paise}

    def as_facts(self) -> dict[str, Any]:
        return {**self.fingerprint(), "tenders": dict(self.tenders)}


def _accepted(store: Store) -> QuerySet[Sale]:
    """Bills the server has numbered and not cancelled - a day's trade."""
    return Sale.objects.filter(store=store, doc_number__isnull=False).exclude(
        docstatus=DocStatus.CANCELLED
    )


def _closed(store: Store, day: date) -> DayClose | None:
    return DayClose.objects.filter(store=store, business_day=day, closed_at__isnull=False).first()


def day_bills(store: Store, day: date) -> QuerySet[Sale]:
    """The bills that belong to `day`'s close (see the module docstring).

    Billed on `day` and accepted before `day` closed, plus the bills billed on
    the day before and accepted after that day closed.
    """
    own = Q(billed_at__date=day)
    row = _closed(store, day)
    if row is not None and row.closed_at is not None:
        own &= Q(created_at__lte=row.closed_at)
    before = _closed(store, day - timedelta(days=1))
    if before is not None and before.closed_at is not None:
        own |= Q(billed_at__date=day - timedelta(days=1), created_at__gt=before.closed_at)
    return _accepted(store).filter(own)


def carried_in(store: Store, day: date) -> QuerySet[Sale]:
    """Bills of the day before, accepted after that day closed."""
    before = _closed(store, day - timedelta(days=1))
    if before is None or before.closed_at is None:
        return Sale.objects.none()
    return _accepted(store).filter(
        billed_at__date=day - timedelta(days=1), created_at__gt=before.closed_at
    )


def after_close(store: Store, row: DayClose) -> QuerySet[Sale]:
    """Bills billed on a closed day but accepted after its close: the next day's."""
    if row.closed_at is None:
        return Sale.objects.none()
    return _accepted(store).filter(billed_at__date=row.business_day, created_at__gt=row.closed_at)


def totals(bills: QuerySet[Sale]) -> DayBills:
    ids = list(bills.values_list("id", flat=True))
    if not ids:
        return DayBills()
    tenders = {
        row["mode"]: int(row["total"] or 0)
        for row in SaleTender.objects.filter(sale_id__in=ids)
        .values("mode")
        .annotate(total=Sum("amount_paise"))
    }
    return DayBills(
        bills=len(ids),
        net_paise=sum(tenders.values()),
        tenders=tenders,
        frontier=int(Sale.objects.filter(pk__in=ids).aggregate(top=Max("id"))["top"] or 0),
    )


# --- the five steps -------------------------------------------------------------


@dataclass(frozen=True)
class Blocker:
    """An open step, and the sentence that says what to do about it."""

    step: str
    code: str
    message: str

    def refusal(self) -> Refusal:
        return Refusal(self.code, self.message, status=409)


@dataclass(frozen=True)
class DayCloseState:
    """One store's business day as the server sees it at one moment."""

    store: Store
    day: date
    now: datetime
    row: DayClose | None
    counter_id: str
    hole_count: int
    last_doc_number: str
    held_open: int
    held_kept: int
    switched_on: bool
    count: CashCount | None
    #: Bills the day's count did not take in (they belong to the next count).
    bills_after_count: int
    summary: DayBills
    carried: DayBills
    after: DayBills
    last_closed: date | None
    still_open: dict[str, int]

    @property
    def closed(self) -> bool:
        return self.row is not None and self.row.closed_at is not None

    @property
    def summary_checked(self) -> bool:
        return self.row is not None and self.row.summary_checked_at is not None

    @property
    def summary_changed(self) -> bool:
        """Bills arrived or were cancelled since the summary was ticked."""
        if self.row is None or self.row.summary_checked_at is None or self.closed:
            return False
        return dict(self.row.summary_facts or {}) != self.summary.fingerprint()

    def counter(self) -> str:
        return f"Counter {self.counter_id}" if self.counter_id else "The counter"

    def blockers(self, counter_pending: int = 0) -> list[Blocker]:
        """Every open step, in the order the store works them."""
        out: list[Blocker] = []
        if counter_pending > 0:
            who = self.counter_id or "the counter"
            out.append(Blocker(
                "synced", "BILLS_UNSYNCED",
                f"{self.counter()} has {counter_pending} bill{'' if counter_pending == 1 else 's'} "
                f"waiting. Sync {who}, then come back.",
            ))
        elif self.hole_count:
            out.append(Blocker(
                "synced", "BILLS_UNSYNCED",
                f"Head office is missing {self.hole_count} numbered "
                f"bill{'' if self.hole_count == 1 else 's'}. Send or re-enter "
                f"{'it' if self.hole_count == 1 else 'them'} from Till & sync, then come back.",
            ))
        if self.held_open:
            out.append(Blocker(
                "held", "HELD_BILLS_OPEN",
                f"{self.held_open} bill{' is' if self.held_open == 1 else 's are'} on hold. "
                "Resume or let go of each on Billing, then come back.",
            ))
        if not self.switched_on:
            out.append(Blocker(
                "cash", "CASH_COUNT_OFF",
                "Cash count is switched off for this store, so the day cannot be closed here.",
            ))
        elif self.count is None:
            out.append(Blocker(
                "cash", "CASH_NOT_COUNTED",
                "The drawer is not counted yet. Count it on Cash count, then come back.",
            ))
        if not self.summary_checked:
            out.append(Blocker(
                "summary", "SUMMARY_NOT_CHECKED",
                "Read the Day summary and tick it as checked, then come back.",
            ))
        elif self.summary_changed:
            out.append(Blocker(
                "summary", "SUMMARY_CHANGED",
                "Bills changed since the Day summary was checked. Read it again and tick it again.",
            ))
        return out


def _still_open(store: Store, day: date) -> dict[str, int]:
    """What carries over to tomorrow and does not stop the close."""
    holding = CustomerReservation.objects.filter(
        store=store, status=CustomerReservation.Status.ACTIVE
    )
    return {
        "reservations_holding": holding.count(),
        "reservations_past_date": holding.filter(collect_by__lt=day).count(),
        "alterations_ready": AlterationJob.objects.filter(
            store=store, status=AlterationJob.Status.READY
        ).count(),
        "special_orders_arrived": SpecialOrder.objects.filter(
            store=store, status=SpecialOrder.Status.ARRIVED
        ).count(),
    }


def day_state(store: Store, now: datetime | None = None, day: date | None = None) -> DayCloseState:
    """`store`'s business day (today's, unless `day` says which) as it stands at `now`."""
    now = now or timezone.now()
    day = day or business_day(now)
    row = DayClose.objects.filter(store=store, business_day=day).select_related(
        "summary_checked_by", "closed_by", "confirmed_by", "cash_count"
    ).first()
    register = register_state(store)
    till = active_till(store)
    last = _accepted(store).filter(fy=register.fy).order_by("-till_seq", "-id").first()
    count = (
        row.cash_count
        if row is not None and row.cash_count is not None
        else CashCount.objects.filter(store=store, business_day=day)
        .select_related("counted_by", "approved_by")
        .first()
    )
    after_count = 0
    if count is not None:
        after_count = _accepted(store).filter(
            created_at__gte=count.window_from, cash_count_link__isnull=True
        ).count()
    last_closed = (
        DayClose.objects.filter(store=store, closed_at__isnull=False)
        .order_by("-business_day")
        .values_list("business_day", flat=True)
        .first()
    )
    return DayCloseState(
        store=store,
        day=day,
        now=now,
        row=row,
        counter_id=till.counter_id if till is not None else "",
        hole_count=register.hole_count,
        last_doc_number=(last.doc_number or "") if last is not None else "",
        held_open=HeldBill.objects.filter(store=store)
        .exclude(expires_policy=HeldBill.ExpiresPolicy.KEPT)
        .count(),
        held_kept=HeldBill.objects.filter(
            store=store, expires_policy=HeldBill.ExpiresPolicy.KEPT
        ).count(),
        switched_on=is_feature_on(store, CASH_COUNT),
        count=count,
        bills_after_count=after_count,
        summary=totals(day_bills(store, day)),
        carried=totals(carried_in(store, day)),
        after=totals(after_close(store, row)) if row is not None else DayBills(),
        last_closed=last_closed,
        still_open=_still_open(store, day),
    )


# --- writing ----------------------------------------------------------------------


def _day_changed(asked: date, state: DayCloseState) -> None:
    if asked != state.day:
        raise Refusal(
            "DAY_CHANGED",
            f"The business day is {state.day:%d %b %Y} now, not {asked:%d %b %Y}. "
            "Read the page again.",
            status=409,
        )


def _execute(
    store: Store,
    actor: Any,
    action: str,
    attempt: uuid.UUID,
    stored: dict[str, Any],
    handler: Any,
    day: date,
    principal: Principal | None,
) -> Saved:
    """Run one audited command; a second copy of a completed close answers with it.

    Not nested in the caller's transaction, so a refused attempt keeps its audit
    row - which is what the wrong-PIN pause counts.
    """
    try:
        execute_command(
            principal or _principal(store, actor),
            CommandSpec(
                action=action,
                command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{action}:{attempt}"),
                business_input={
                    key: (str(value) if isinstance(value, uuid.UUID | date) else value)
                    for key, value in stored.items()
                },
                site_id=store.pk,
                subject_key=f"day_close:{store.pk}:{day.isoformat()}",
            ),
            handler,
        )
    except Refusal as refusal:
        if refusal.code != _ALREADY:
            raise
        return Saved(row=DayClose.objects.get(store=store, business_day=day), created=False)
    return Saved(row=DayClose.objects.get(store=store, business_day=day), created=True)


def record_summary_check(
    store: Store, actor: Any, data: dict[str, Any], principal: Principal | None = None
) -> Saved:
    """Tick the day summary as read, once per state of the day's bills, audited."""
    asked: date = data["business_day"]
    state = day_state(store)
    _day_changed(asked, state)
    if state.closed:
        raise Refusal(
            "DAY_CLOSED", "This day is closed. Its summary is final.", status=409
        )
    if state.summary_checked and not state.summary_changed:
        assert state.row is not None
        return Saved(row=state.row, created=False)

    def handler(run: CommandRun) -> CommandResult:
        run.advisory_lock(LockRank.DOCUMENT, [f"day-close:{store.pk}"])
        here = day_state(store, run.now, asked)
        if here.closed:
            raise Refusal("DAY_CLOSED", "This day is closed. Its summary is final.", status=409)
        row, _ = DayClose.objects.get_or_create(store=store, business_day=asked)
        before = {
            "summary_checked": row.summary_checked_at is not None,
            "summary_facts": dict(row.summary_facts or {}),
        }
        row.summary_checked_at = run.now
        row.summary_checked_by = actor
        row.summary_facts = here.summary.fingerprint()
        row.save(update_fields=["summary_checked_at", "summary_checked_by", "summary_facts"])
        run.audit_before = {"business_day": asked.isoformat(), **before}
        run.audit_after = {
            "business_day": asked.isoformat(),
            "summary_checked": True,
            "summary_checked_by_user_id": getattr(actor, "pk", None),
            "summary_facts": row.summary_facts,
        }
        return CommandResult(resource_type="day_close", resource_id=str(row.pk), status_code=201)

    return _execute(
        store, actor, SUMMARY_ACTION, data["id"], dict(data), handler, asked, principal
    )


def _check_manager(run: CommandRun, store: Store, approved_by: int | None, pin: str) -> Any:
    """A manager of this store, by their own PIN - the cash count's own rule set."""
    if approved_by is None or not pin:
        raise Refusal(
            "MANAGER_PIN_NEEDED",
            "Closing the day needs a manager of this store to type their own PIN.",
            status=400,
        )
    wrong = AuditEvent.objects.filter(
        action=CLOSE_ACTION,
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
            "NOT_A_MANAGER", "That person cannot close the day at this store.", status=403
        )
    if not verify_till_pin(pin, manager.till_pin_hash):
        raise Refusal("WRONG_PIN", "That is not a manager's PIN for this store.", status=403)
    return manager


def close_facts(row: DayClose) -> dict[str, Any]:
    """The close as the audit log keeps it."""
    return {
        "closed": row.closed_at is not None,
        "business_day": row.business_day.isoformat(),
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
        "closed_by_user_id": row.closed_by_id,
        "confirmed_by_user_id": row.confirmed_by_id,
        "cash_count_id": str(row.cash_count_id) if row.cash_count_id else None,
        "facts": row.facts,
        "till_number": row.till_number,
    }


def close_day(
    store: Store, actor: Any, data: dict[str, Any], principal: Principal | None = None
) -> Saved:
    """Close the store's business day, once, audited; a repeat answers with the close."""
    asked: date = data["business_day"]
    pending = int(data.get("counter_pending") or 0)
    pin = str(data.get("manager_pin") or "")
    approved_by = data.get("approved_by")
    state = day_state(store)
    _day_changed(asked, state)
    if state.closed:
        assert state.row is not None
        return Saved(row=state.row, created=False)

    def handler(run: CommandRun) -> CommandResult:
        # The drawer's lock too: no count is saved half-way through a close.
        run.advisory_lock(LockRank.DOCUMENT, [f"cash-drawer:{store.pk}", f"day-close:{store.pk}"])
        here = day_state(store, run.now, asked)
        if here.closed:
            raise Refusal(_ALREADY, "This day is already closed.", status=409)
        blocked = here.blockers(pending)
        if blocked:
            raise blocked[0].refusal()
        manager = _check_manager(run, store, approved_by, pin)
        assert here.row is not None and here.count is not None
        row = here.row
        before = close_facts(row)
        row.closed_at = run.now
        row.closed_by = actor
        row.confirmed_by = manager
        row.cash_count = here.count
        row.till_number = str(data.get("till_number") or "")[:64]
        row.facts = {
            **here.summary.as_facts(),
            "carried_in": here.carried.as_facts(),
            "last_doc_number": here.last_doc_number,
            "cash": {
                "counted_paise": here.count.counted_paise,
                "expected_paise": here.count.expected_paise,
                "variance_paise": here.count.variance_paise,
                "bills_after_count": here.bills_after_count,
            },
            "held_kept": here.held_kept,
            "counter_pending": pending,
        }
        row.save(update_fields=[
            "closed_at", "closed_by", "confirmed_by", "cash_count", "till_number", "facts",
        ])
        run.audit_before = before
        run.audit_after = close_facts(row)
        return CommandResult(resource_type="day_close", resource_id=str(row.pk), status_code=201)

    # The PIN never leaves this function: not in the stored input, not in the audit.
    stored = {key: value for key, value in data.items() if key != "manager_pin"}
    return _execute(store, actor, CLOSE_ACTION, data["id"], stored, handler, asked, principal)


def dashboard_state(store: Store) -> dict[str, Any]:
    """The Home row: is today's business day closed yet."""
    day = business_day(timezone.now())
    row = _closed(store, day)
    if row is not None and row.closed_at is not None:
        return {"date": day.isoformat(), "state": "closed", "closed_at": row.closed_at.isoformat()}
    state = "open" if is_feature_on(store, CASH_COUNT) else "off"
    return {"date": day.isoformat(), "state": state, "closed_at": None}
