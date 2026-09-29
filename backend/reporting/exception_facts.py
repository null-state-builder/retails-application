"""Copy the counter's exceptions into the reporting store (ticket 48, ST-RPT-6).

One row per exception (``ExceptionFact``), with the store, the day, the login it
is about and the money involved. What each kind is, read from the billing
tables as they stand:

* **Cancelled bill** - a bill cancelled after it was accepted. It is about the
  person who cancelled it where the reversal names them (the reversal's ledger
  legs carry who posted them), else the cashier who billed it. Dated by the day
  of that reversal; its value is the bill's value.
* **Price typed at the counter** - a sold line whose price the cashier typed off
  the tag because the barcode is in no book (``sold_before_inward``, at a store
  on the old stock system). About the cashier; its value is the typed price.
  The counter has no other way to set a price.
* **Manual discount** - a sold line with a manual part to its discount: what the
  server's own rulebook does not explain, as the cap check worked it out when
  the bill arrived (``SaleLine.manual_disc_paise``), never the till's own claim.
  About the cashier who billed it. Lines billed before ticket 48 carry no such
  figure and are left out.
* **Manager PIN use** - a manager's own PIN approving something: an override on
  a bill (``override_by``, whatever its kind), and a cash variance confirmed at
  day close (``CashCount.approved_by``). About the manager.
* **Cash variance** - a day-close count that did not match the expected cash
  (ticket 41). About the person who counted; its value is the difference, short
  or over, as a positive amount.
* **Bill-number hole** - bill numbers the till used that never reached head
  office, as the nightly check still finds them (its standing flag, not yet
  closed). About the store, not a person: a bill that never arrived names
  nobody. ``events`` is how many numbers are missing, dated by the day the gap
  first survived a night.
* **Return without a bill** - a bill that took a piece back against a bill head
  office does not hold (a return line with no original). About the cashier;
  its value is what those pieces gave back. A cancelled bill is left out.
* **Over the no-bill return cap** - a bill flagged by ticket 48's caps.
* **Late sync** - a bill that reached head office more than
  ``KDPS_LATE_SYNC_MINUTES`` after the till printed it. About the cashier.
* **Checklist items missed** - a store's checklist whose time passed with items
  not ticked (ticket 49), as the worker's check recorded it. About the store,
  not a person: nobody ticked them. ``events`` is how many items, dated by the
  day the list was due.

Bills are dated by the day they were billed, India time.

**Kept apart, and incremental.** Runs on the worker's clock and from ``manage.py
refresh_exceptions_report``, never while a report page loads, with plain reads.
The rows that come from a bill are re-read only for bills changed since the last
run began, less an overlap (as the sales copy does): cancelling a bill touches
it, and a cap flag is written with its bill. The few rows that come from no bill
(cash counts, missing numbers, missed checklists) are rebuilt whole every run, so a closed hole
drops out at once. ``full`` rebuilds everything.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from accounts.models import User
from core.commands import database_now
from core.documents import DocStatus
from core.gl import GLEntry
from core.outbox import ANCHOR_INTERVAL
from reporting.models import ExceptionFact, ReportRefresh
from reporting.sales_facts import business_day, changed_sales, chunks, overlap
from sell.cash_models import CashCount
from sell.models import SALE_DOC_TYPE, ContinuityFlag, Sale, SaleLine
from storefront.checklist_models import ChecklistMiss

logger = logging.getLogger(__name__)

#: The ``ReportRefresh`` row this copy keeps its freshness in.
KEY = "exceptions"
#: Its own advisory lock, apart from the other copies'.
LOCK_ID = 7_310_048

Kind = ExceptionFact.Kind
Bills = dict[int, dict[str, Any]]


@dataclass(frozen=True)
class RefreshResult:
    #: False when another run held the lock, so this one did nothing.
    ran: bool
    as_of: datetime | None = None
    documents: int = 0
    took_ms: int = 0
    full: bool = False


def scheduled_refresh(_tenant_id: uuid.UUID) -> None:
    """The worker's call, once per tenant each tick. The copy covers every store,
    so a copy brought up to date within the last half tick is fresh enough."""
    state = ReportRefresh.objects.filter(key=KEY).first()
    if state and state.as_of and timezone.now() - state.as_of < ANCHOR_INTERVAL / 2:
        return
    refresh()


def refresh(*, full: bool = False) -> RefreshResult:
    """Bring the exceptions copy up to date from the bills, counts and flags."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        row = cursor.fetchone()
    if not row or not row[0]:
        return RefreshResult(ran=False)
    try:
        return _refresh(full)
    except Exception as exc:
        ReportRefresh.objects.update_or_create(
            key=KEY,
            defaults={"failed_at": timezone.now(), "failure": f"{type(exc).__name__}: {exc}"[:500]},
        )
        raise
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
        except Exception:  # noqa: BLE001 - logged, the original error stands
            logger.exception("exceptions report refresh: advisory unlock failed")


def _refresh(full: bool) -> RefreshResult:
    clock = time.monotonic()
    started = database_now()
    state, _ = ReportRefresh.objects.get_or_create(key=KEY)
    since = None if full or state.as_of is None else state.as_of - overlap()
    sale_ids = changed_sales(since)
    people = _People()
    with transaction.atomic():
        if since is None:
            ExceptionFact.objects.all().delete()
        else:
            ExceptionFact.objects.filter(bill_id__isnull=True).delete()
        for chunk in chunks(sale_ids):
            if since is not None:
                ExceptionFact.objects.filter(bill_id__in=chunk).delete()
            ExceptionFact.objects.bulk_create(_bill_facts(chunk, people), batch_size=5000)
        ExceptionFact.objects.bulk_create(
            [*_count_facts(people), *_number_holes(), *_checklist_misses()]
        )
        took = int((time.monotonic() - clock) * 1000)
        state.as_of = started
        state.took_ms = took
        state.documents = len(sale_ids)
        state.failed_at = None
        state.failure = ""
        state.save()
    return RefreshResult(
        ran=True, as_of=started, documents=len(sale_ids), took_ms=took, full=since is None
    )


class _People:
    """Logins by id, named as they are named today, read once per run."""

    def __init__(self) -> None:
        self._names: dict[int, str] = {}

    def load(self, ids: Iterable[int | None]) -> None:
        wanted = {pk for pk in ids if pk is not None and pk not in self._names}
        for pk, full_name, username in User.objects.filter(pk__in=wanted).values_list(
            "pk", "full_name", "username"
        ):
            self._names[pk] = (full_name or username)[:160]

    def fact(self, user_id: int | None, **fields: Any) -> ExceptionFact:
        if user_id is None:
            return ExceptionFact(staff_key="", staff_name="", **fields)
        return ExceptionFact(
            staff_key=f"user:{user_id}", staff_name=self._names.get(user_id, ""), **fields
        )


# -- from bills ---------------------------------------------------------------------


def _bill_facts(ids: list[int], people: _People) -> list[ExceptionFact]:
    """Every exception these bills carry, for the numbered ones."""
    bills: Bills = {
        row["id"]: dict(row)
        for row in Sale.objects.filter(id__in=ids, doc_number__isnull=False).values(
            "id",
            "store_id",
            "doc_number",
            "docstatus",
            "billed_at",
            "created_at",
            "updated_at",
            "created_by_id",
            "override_by_id",
            "override_kind",
            "net_paise",
        )
    }
    if not bills:
        return []
    people.load(bill["created_by_id"] for bill in bills.values())
    people.load(bill["override_by_id"] for bill in bills.values())
    live = [pk for pk, bill in bills.items() if bill["docstatus"] != DocStatus.CANCELLED]
    return [
        *_cancelled(bills, people),
        *_overrides(bills, people),
        *_late_syncs(bills, people),
        *_line_facts(bills, live, people),
        *_no_bill_returns(bills, live, people),
        *_cap_flags(bills, people),
    ]


def _from_bill(
    bill: dict[str, Any], kind: str, user_id: int | None, people: _People, **fields: Any
) -> ExceptionFact:
    fields.setdefault("day", business_day(bill["billed_at"]))
    fields.setdefault("reference", bill["doc_number"])
    return people.fact(user_id, store_id=bill["store_id"], bill_id=bill["id"], kind=kind, **fields)


def _cancelled(bills: Bills, people: _People) -> list[ExceptionFact]:
    cancelled = {
        bill["doc_number"]: bill
        for bill in bills.values()
        if bill["docstatus"] == DocStatus.CANCELLED
    }
    if not cancelled:
        return []
    reversed_by: dict[str, tuple[int | None, datetime]] = {}
    for doc_number, posted_by, at in (
        GLEntry.objects.filter(
            doc_type=SALE_DOC_TYPE,
            doc_number__in=list(cancelled),
            memo__startswith="Reversal of",
        )
        .order_by("created_at")
        .values_list("doc_number", "posted_by_id", "created_at")
    ):
        reversed_by.setdefault(doc_number, (posted_by, at))
    people.load(who for who, _at in reversed_by.values())
    out = []
    for doc_number, bill in cancelled.items():
        # A bill whose reversal posted nothing is dated by its own last change.
        canceller, at = reversed_by.get(doc_number, (None, bill["updated_at"]))
        out.append(
            _from_bill(
                bill,
                Kind.CANCELLED_BILL,
                canceller or bill["created_by_id"],
                people,
                day=business_day(at),
                value_paise=abs(int(bill["net_paise"] or 0)),
            )
        )
    return out


def _overrides(bills: Bills, people: _People) -> list[ExceptionFact]:
    return [
        _from_bill(
            bill,
            Kind.PIN_USE,
            bill["override_by_id"],
            people,
            reference=f"{bill['doc_number']} ({bill['override_kind'] or 'override'})",
        )
        for bill in bills.values()
        if bill["override_by_id"] is not None
    ]


def _late_syncs(bills: Bills, people: _People) -> list[ExceptionFact]:
    late_after = timedelta(minutes=int(settings.KDPS_LATE_SYNC_MINUTES))
    out = []
    for bill in bills.values():
        lag = bill["created_at"] - bill["billed_at"]
        if lag > late_after:
            out.append(
                _from_bill(
                    bill,
                    Kind.LATE_SYNC,
                    bill["created_by_id"],
                    people,
                    reference=f"{bill['doc_number']} ({_lag_text(lag)} late)",
                )
            )
    return out


def _line_facts(bills: Bills, live: list[int], people: _People) -> list[ExceptionFact]:
    """Prices typed at the counter and manual discounts, one row per sold line."""
    out: list[ExceptionFact] = []
    for sale_id, manual, typed, mrp, qty, pieces in SaleLine.objects.filter(
        sale_id__in=live, direction=SaleLine.Direction.SALE
    ).values_list(
        "sale_id",
        "manual_disc_paise",
        "sold_before_inward",
        "mrp_paise",
        "qty",
        "goods_allocations",
    ):
        bill = bills[sale_id]
        # A goods-v1 line always names the pieces it took; only an old-system
        # line can be a barcode no book holds, priced off the tag.
        if typed and not pieces:
            out.append(
                _from_bill(
                    bill,
                    Kind.PRICE_TYPED,
                    bill["created_by_id"],
                    people,
                    value_paise=int(mrp) * int(qty),
                )
            )
        if manual:
            out.append(
                _from_bill(
                    bill,
                    Kind.MANUAL_DISCOUNT,
                    bill["created_by_id"],
                    people,
                    value_paise=int(manual),
                )
            )
    return out


def _no_bill_returns(bills: Bills, live: list[int], people: _People) -> list[ExceptionFact]:
    given_back: dict[int, int] = defaultdict(int)
    for sale_id, net in SaleLine.objects.filter(
        sale_id__in=live, direction=SaleLine.Direction.RETURN, original_line__isnull=True
    ).values_list("sale_id", "net_paise"):
        given_back[sale_id] += abs(int(net or 0))
    return [
        _from_bill(
            bills[sale_id],
            Kind.NO_BILL_RETURN,
            bills[sale_id]["created_by_id"],
            people,
            value_paise=value,
        )
        for sale_id, value in given_back.items()
    ]


def _cap_flags(bills: Bills, people: _People) -> list[ExceptionFact]:
    flagged = ContinuityFlag.objects.filter(
        kind=ContinuityFlag.Kind.NO_BILL_RETURN_CAP, sale_id__in=list(bills)
    ).values_list("sale_id", flat=True)
    return [
        _from_bill(bills[sale_id], Kind.NO_BILL_CAP, bills[sale_id]["created_by_id"], people)
        for sale_id in flagged
    ]


def _lag_text(lag: timedelta) -> str:
    minutes = int(lag.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


# -- from no bill ----------------------------------------------------------------------


def _count_facts(people: _People) -> list[ExceptionFact]:
    """Cash variances, and the manager's PIN that confirmed each."""
    counts = list(
        CashCount.objects.exclude(variance_paise=0).values(
            "store_id", "business_day", "counted_by_id", "approved_by_id", "variance_paise"
        )
    )
    people.load(row["counted_by_id"] for row in counts)
    people.load(row["approved_by_id"] for row in counts)
    out: list[ExceptionFact] = []
    for row in counts:
        difference = abs(int(row["variance_paise"]))
        way = "short" if row["variance_paise"] < 0 else "over"
        out.append(
            people.fact(
                row["counted_by_id"],
                store_id=row["store_id"],
                day=row["business_day"],
                kind=Kind.CASH_VARIANCE,
                value_paise=difference,
                reference=f"cash count {row['business_day']} ({way})",
            )
        )
        if row["approved_by_id"] is not None:
            out.append(
                people.fact(
                    row["approved_by_id"],
                    store_id=row["store_id"],
                    day=row["business_day"],
                    kind=Kind.PIN_USE,
                    value_paise=difference,
                    reference=f"cash count {row['business_day']} (variance)",
                )
            )
    return out


def _number_holes() -> list[ExceptionFact]:
    """The nightly check's standing flag per store: numbers still missing."""
    out: list[ExceptionFact] = []
    for flag in (
        ContinuityFlag.objects.filter(kind=ContinuityFlag.Kind.NUMBER_HOLE, sale__isnull=True)
        .exclude(status=ContinuityFlag.Status.RESOLVED)
        .order_by("created_at")
    ):
        details = flag.details or {}
        raw_day = details.get(ContinuityFlag.DAY_KEY)
        day = datetime.fromisoformat(raw_day).date() if raw_day else business_day(flag.created_at)
        out.append(
            ExceptionFact(
                store_id=flag.store_id,
                day=day,
                kind=Kind.NUMBER_HOLE,
                events=int(details.get("count") or 0),
                reference=_hole_text(details),
            )
        )
    return out


def _hole_text(details: dict[str, Any]) -> str:
    named = details.get("missing") or []
    shown = ", ".join(str(seq) for seq in named[:5])
    more = f" and {len(named) - 5} more" if len(named) > 5 else ""
    return f"FY {details.get('fy') or ''}: {shown}{more}"[:128]


def _checklist_misses() -> list[ExceptionFact]:
    """Each store's checklists missed, one row per list and day (ticket 49)."""
    return [
        ExceptionFact(
            store_id=row.store_id,
            day=row.due_on,
            kind=Kind.CHECKLIST_MISSED,
            events=len(row.items),
            reference=f"{row.template.name} {row.due_on.isoformat()}"[:128],
        )
        for row in ChecklistMiss.objects.select_related("template").order_by("due_on", "id")
    ]
