"""Scheduled counts: when each store's blind count is due (store operations PRD
ST-INV-3; ticket 35).

The Owner sets a ``CountSchedule`` per store: a brand (or the whole store), how
often, and the first day it is due. Everything else is worked out here:

* **Due dates** follow from the first one: every 7 days, or on the same day of
  every month or third month (the last day of a shorter month).
* **Done** is read from the store's existing blind counts, never recorded apart:
  a count of that brand, or of the whole store, submitted on any day since the
  previous due date up to and including this one. A legacy count session and a
  goods-v1 count pass both count; a cancelled goods-v1 count does not. A whole
  store schedule is done only by a whole store count.
* **Due today** and not done is the store's task on Today.
* **Missed**: once its day has passed with no count, the worker's check opens a
  ``scheduled_count_missed`` exception for the store. It closes when that brand
  (or the whole store) is counted, or when the Owner stops the schedule. A due
  date before the day the schedule was set or last changed is never judged.
* **The count-due alert** is open while a count is due today or missed.

Where the ``scheduled-counts`` switch is off nothing is due and nothing new is
raised; what was already raised stays until it is counted or stopped.
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from django.utils import timezone

from alerts.checks import AlertHit, sync_kind
from alerts.goods_models import GoodsException
from alerts.goods_services import open_exception, resolve_exceptions
from alerts.models import Alert, AlertKind, AlertStatus
from core.commands import (
    CommandResult,
    CommandRun,
    CommandSpec,
    LockRank,
    Principal,
    execute_command,
)
from masters.goods_models import SiteGuard
from masters.models import Store
from masters.store_feature_registry import SCHEDULED_COUNTS
from masters.store_features import feature, switch_states
from outbound.count_schedule_models import CountEvery, CountSchedule
from outbound.goods_models import GoodsCountPass, GoodsStocktake
from outbound.models import CountScope, CountSession

FEATURE_KEY = SCHEDULED_COUNTS
WEEK = CountEvery.WEEK.value
MONTH = CountEvery.MONTH.value
QUARTER = CountEvery.QUARTER.value
MONTHS_IN = {MONTH: 1, QUARTER: 3}

MISSED_KIND = "scheduled_count_missed"
MISSED_REASON = "COUNT_NOT_DONE"
CHECK_SERVICE = "count-schedule-check"
CHECK_ACTION = "stock.count_schedule.check"
SUBJECT_PREFIX = "count_schedule"
_MISSED_NAMESPACE = uuid.UUID("3d7c1f35-2f8e-4d6b-9a35-0c35c0a1e035")


# ---------------------------------------------------------------------------
# The rule: when a count is due, and which days count for it
# ---------------------------------------------------------------------------


def nth_due(first: date, every: str, n: int) -> date:
    """The ``n``-th due date counting ``first`` as 0 (a negative ``n`` looks back)."""
    if every == WEEK:
        return first + timedelta(days=7 * n)
    years, month_index = divmod(first.month - 1 + n * MONTHS_IN[every], 12)
    year, month = first.year + years, month_index + 1
    return date(year, month, min(first.day, calendar.monthrange(year, month)[1]))


def due_on_or_before(first: date, every: str, day: date) -> tuple[int, date] | None:
    """The latest due date on or before ``day``, with its number; None before the first."""
    if day < first:
        return None
    if every == WEEK:
        n = (day - first).days // 7
    else:
        n = ((day.year - first.year) * 12 + day.month - first.month) // MONTHS_IN[every]
        while nth_due(first, every, n) > day:
            n -= 1
    return n, nth_due(first, every, n)


def window(first: date, every: str, n: int) -> tuple[date, date]:
    """The days a count done on counts for the ``n``-th due date, both included:
    the day after the previous due date up to the due date itself."""
    return nth_due(first, every, n - 1) + timedelta(days=1), nth_due(first, every, n)


def next_due(first: date, every: str, today: date) -> date:
    """Today if a count is due today, else the next due date."""
    hit = due_on_or_before(first, every, today)
    if hit is None:
        return first
    n, due = hit
    return due if due == today else nth_due(first, every, n + 1)


# ---------------------------------------------------------------------------
# Done: read from the store's existing blind counts
# ---------------------------------------------------------------------------


def _day_start(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min))


@dataclass(frozen=True)
class _Count:
    """One submitted blind count: when, and what it covered."""

    site_id: int
    at: datetime
    whole: bool
    brand_id: int | None
    brand_name: str


class CountLog:
    """Every submitted blind count at some stores, read in two queries.

    Counts are rare (a store counts a brand a few times a year), so reading them
    all once and judging each schedule in memory costs less than asking the
    database once per schedule. A legacy count session names its brand by the
    name typed; a goods-v1 count by the brand's id.
    """

    def __init__(self, site_ids: Iterable[int]) -> None:
        ids = sorted(set(site_ids))
        rows: list[_Count] = []
        for site_id, scope, value, at in CountSession.objects.filter(
            stocktake__store_id__in=ids,
            submitted_at__isnull=False,
            scope__in=[CountScope.STORE, CountScope.BRAND],
        ).values_list("stocktake__store_id", "scope", "scope_value", "submitted_at"):
            whole = scope == CountScope.STORE
            if at is None:  # pragma: no cover - filtered out above
                continue
            rows.append(_Count(site_id, at, whole, None, "" if whole else value.strip().lower()))
        for site_id, scope, at in (
            GoodsCountPass.objects.filter(stocktake__site_id__in=ids, submitted_at__isnull=False)
            .exclude(stocktake__state=GoodsStocktake.State.CANCELLED)
            .values_list("stocktake__site_id", "stocktake__scope", "submitted_at")
        ):
            kind = (scope or {}).get("kind")
            if at is not None and kind in ("site", "brand"):
                brand = scope.get("brand_id") if kind == "brand" else None
                rows.append(_Count(site_id, at, kind == "site", brand, ""))
        self._by_site: dict[int, list[_Count]] = {}
        for row in rows:
            self._by_site.setdefault(row.site_id, []).append(row)

    def _covering(self, schedule: CountSchedule) -> list[datetime]:
        """When this schedule's brand (or its whole store) was counted."""
        brand = schedule.brand
        name = brand.name.strip().lower() if brand else ""
        return [
            row.at
            for row in self._by_site.get(schedule.site_id, [])
            if row.whole
            or (
                brand is not None
                and (row.brand_id == brand.pk or (row.brand_id is None and row.brand_name == name))
            )
        ]

    def counted_between(self, schedule: CountSchedule, opens: date, closes: date) -> bool:
        """Counted on a day from ``opens`` to ``closes`` (India), both included?"""
        start, end = _day_start(opens), _day_start(closes + timedelta(days=1))
        return any(start <= at < end for at in self._covering(schedule))

    def last_counted_on(self, schedule: CountSchedule) -> date | None:
        moments = self._covering(schedule)
        return timezone.localdate(max(moments)) if moments else None


def counted_between(schedule: CountSchedule, opens: date, closes: date) -> bool:
    return CountLog([schedule.site_id]).counted_between(schedule, opens, closes)


def last_counted_on(schedule: CountSchedule) -> date | None:
    return CountLog([schedule.site_id]).last_counted_on(schedule)


# ---------------------------------------------------------------------------
# Due today, and missed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Due:
    schedule: CountSchedule
    due_on: date


def switched_on(stores: list[Store]) -> list[Store]:
    states = switch_states(stores, [feature(FEATURE_KEY)])
    return [store for store, state in zip(stores, states, strict=True) if state.enabled]


def live_schedules(stores: list[Store]) -> list[CountSchedule]:
    return list(
        CountSchedule.objects.filter(site__in=stores, active=True)
        .select_related("site", "brand")
        .order_by("site__code", "brand__name")
    )


def due_today(
    stores: list[Store], today: date, schedules: list[CountSchedule] | None = None
) -> list[Due]:
    """The counts due today and not yet done, at the stores given (switch on only).

    ``schedules`` are those stores' live schedules when the caller already has them.
    """
    schedules = live_schedules(switched_on(stores)) if schedules is None else schedules
    due_now = []
    for schedule in schedules:
        hit = due_on_or_before(schedule.first_due_on, schedule.every, today)
        if hit is not None and hit[1] == today:
            due_now.append((schedule, window(schedule.first_due_on, schedule.every, hit[0])))
    if not due_now:
        return []
    log = CountLog(schedule.site_id for schedule, _ in due_now)
    return [
        Due(schedule, due)
        for schedule, (opens, due) in due_now
        if not log.counted_between(schedule, opens, due)
    ]


def missed(schedule: CountSchedule, today: date, log: CountLog | None = None) -> Due | None:
    """The latest due date before today, if its count was not done in its days.

    Only the latest is judged: the check runs every few minutes, and a due date
    passed while the switch was off (or before the schedule was set or last
    changed) is not the store's to answer for.
    """
    hit = due_on_or_before(schedule.first_due_on, schedule.every, today - timedelta(days=1))
    if hit is None:
        return None
    n, due = hit
    if due < schedule.judged_from:
        return None
    opens, _ = window(schedule.first_due_on, schedule.every, n)
    log = log or CountLog([schedule.site_id])
    return None if log.counted_between(schedule, opens, due) else Due(schedule, due)


def subject_key(schedule_id: Any, due_on: date) -> str:
    return f"{SUBJECT_PREFIX}:{schedule_id}:{due_on.isoformat()}"


def parse_subject(key: str) -> tuple[uuid.UUID, date] | None:
    prefix, _, rest = key.partition(":")
    schedule_id, _, due = rest.rpartition(":")
    if prefix != SUBJECT_PREFIX:
        return None
    try:
        return uuid.UUID(schedule_id), date.fromisoformat(due)
    except ValueError:
        return None


def missed_event_key(schedule_id: Any, due_on: date) -> uuid.UUID:
    """One occurrence per schedule and due date: the check never opens it twice."""
    return uuid.uuid5(_MISSED_NAMESPACE, f"{schedule_id}:{due_on.isoformat()}")


def open_missed(tenant_id: uuid.UUID, site_ids: list[int] | None = None) -> list[GoodsException]:
    rows = GoodsException.objects.filter(tenant_id=tenant_id, kind=MISSED_KIND, state="open")
    if site_ids is not None:
        rows = rows.filter(site_id__in=site_ids)
    return list(rows.order_by("opened_at"))


def _label(schedule: CountSchedule) -> str:
    return schedule.brand.name if schedule.brand else "the whole store"


def _alert_hits(dues: list[Due], missed_rows: list[GoodsException]) -> list[AlertHit]:
    hits = [
        AlertHit(
            dedupe_key=f"count_due:{due.schedule.pk}:{due.due_on.isoformat()}",
            title=f"{due.schedule.site.code}: count of {_label(due.schedule)} is due today",
            store_id=due.schedule.site_id,
            brand=due.schedule.brand.name if due.schedule.brand else "",
            object_id=due.schedule.site_id,
            due_date=due.due_on,
            threshold_days=None,
        )
        for due in dues
    ]
    parsed = {row.pk: parse_subject(row.subject_key) for row in missed_rows}
    schedules = {
        s.pk: s
        for s in CountSchedule.objects.filter(
            pk__in=[p[0] for p in parsed.values() if p is not None]
        ).select_related("site", "brand")
    }
    for row in missed_rows:
        found = parsed[row.pk]
        schedule = schedules.get(found[0]) if found else None
        if found is None or schedule is None:
            continue
        hits.append(
            AlertHit(
                dedupe_key=f"count_due:{schedule.pk}:{found[1].isoformat()}",
                title=(
                    f"{schedule.site.code}: count of {_label(schedule)} was due on "
                    f"{found[1]:%d %b %Y} and is not done"
                ),
                store_id=schedule.site_id,
                brand=schedule.brand.name if schedule.brand else "",
                object_id=schedule.site_id,
                due_date=found[1],
                threshold_days=None,
            )
        )
    return hits


def run_check(tenant_id: uuid.UUID, today: date | None = None) -> dict[str, int]:
    """The worker's check: open what was missed, close what was since counted, and
    keep the count-due alerts matching. Safe to run as often as it likes."""
    today = today or timezone.localdate()
    stores = switched_on(list(Store.objects.filter(is_active=True, tenant_id=tenant_id)))
    schedules = live_schedules(stores)
    missed_rows = open_missed(tenant_id)
    parsed = {row.pk: parse_subject(row.subject_key) for row in missed_rows}
    missed_schedules = {
        s.pk: s
        for s in CountSchedule.objects.filter(
            pk__in=[p[0] for p in parsed.values() if p is not None]
        ).select_related("site", "brand")
    }
    log = CountLog([s.site_id for s in schedules] + [s.site_id for s in missed_schedules.values()])

    newly_missed = [
        found
        for found in (missed(schedule, today, log) for schedule in schedules)
        if found is not None
    ]
    known = set(
        GoodsException.objects.filter(
            tenant_id=tenant_id,
            kind=MISSED_KIND,
            source_event_key__in=[missed_event_key(d.schedule.pk, d.due_on) for d in newly_missed],
        ).values_list("source_event_key", flat=True)
    )
    to_open = [d for d in newly_missed if missed_event_key(d.schedule.pk, d.due_on) not in known]
    to_resolve = []
    for row in missed_rows:
        found = parsed[row.pk]
        schedule = missed_schedules.get(found[0]) if found else None
        # Counted on any day after the one it was due: late, but done.
        if found and schedule and log.counted_between(schedule, found[1] + timedelta(1), today):
            to_resolve.append(row)

    if to_open or to_resolve:
        _write_check(tenant_id, today, to_open, to_resolve)

    dues = due_today(stores, today, schedules)
    sync_kind(
        AlertKind.COUNT_DUE, AlertKind.COUNT_DUE.label, _alert_hits(dues, open_missed(tenant_id))
    )
    return {"due_today": len(dues), "opened": len(to_open), "resolved": len(to_resolve)}


def _write_check(
    tenant_id: uuid.UUID, today: date, to_open: list[Due], to_resolve: list[GoodsException]
) -> None:
    opened: list[str] = []
    resolved: list[str] = []

    def handler(run: CommandRun) -> CommandResult:
        # The worker and a hand-run check may overlap: one at a time, and what the
        # other already opened is not opened again.
        run.advisory_lock(LockRank.DOCUMENT, [f"{SUBJECT_PREFIX}:check"])
        existing = set(
            GoodsException.objects.filter(
                tenant_id=run.tenant_id,
                kind=MISSED_KIND,
                source_event_key__in=[missed_event_key(d.schedule.pk, d.due_on) for d in to_open],
            ).values_list("source_event_key", flat=True)
        )
        run.audit_before = {
            "opened": [],
            "open": sorted(row.subject_key for row in to_resolve),
        }
        for due in to_open:
            if missed_event_key(due.schedule.pk, due.due_on) in existing:
                continue
            key = subject_key(due.schedule.pk, due.due_on)
            open_exception(
                run,
                kind=MISSED_KIND,
                site_id=due.schedule.site_id,
                subject_key=key,
                reason_code=MISSED_REASON,
                source_event_key=missed_event_key(due.schedule.pk, due.due_on),
                # Counted from the schedule's page (its "count now" link), or stopped there.
                allowed_resolution_actions=[
                    f"POST /api/goods-v1/outbound/count-schedules/{due.schedule.pk}/stop"
                ],
                note=(
                    f"The count of {_label(due.schedule)} at {due.schedule.site.code} was due "
                    f"on {due.due_on.isoformat()} and was not done. Count it, or the Owner "
                    "stops the schedule."
                ),
            )
            opened.append(key)
        for row in to_resolve:
            resolve_exceptions(
                run, kind=MISSED_KIND, subject_key=row.subject_key, reason_code="COUNTED"
            )
            resolved.append(row.subject_key)
        run.audit_subject_key = f"{SUBJECT_PREFIX}:check"
        run.audit_after = {"opened": opened, "resolved": resolved}
        return CommandResult(resource_type="count_schedule_check")

    execute_command(
        Principal(tenant_id=tenant_id, service_code=CHECK_SERVICE),
        CommandSpec(
            action=CHECK_ACTION,
            command_id=uuid.uuid4(),
            business_input={
                "today": today.isoformat(),
                "open": sorted(subject_key(d.schedule.pk, d.due_on) for d in to_open),
                "resolve": sorted(row.subject_key for row in to_resolve),
            },
            subject_key=f"{SUBJECT_PREFIX}:check",
        ),
        handler,
    )


def close_for_stopped(run: CommandRun, schedule: CountSchedule) -> list[str]:
    """Stopping a schedule closes its missed counts and its count-due alerts."""
    keys = [
        row.subject_key
        for row in GoodsException.objects.filter(
            tenant_id=run.tenant_id,
            kind=MISSED_KIND,
            state="open",
            subject_key__startswith=f"{SUBJECT_PREFIX}:{schedule.pk}:",
        )
    ]
    for key in keys:
        resolve_exceptions(run, kind=MISSED_KIND, subject_key=key, reason_code="SCHEDULE_STOPPED")
    Alert.objects.filter(
        kind=AlertKind.COUNT_DUE,
        status=AlertStatus.OPEN,
        dedupe_key__startswith=f"count_due:{schedule.pk}:",
    ).update(status=AlertStatus.RESOLVED, resolved_at=timezone.now())
    return keys


def goods_v1_sites(site_ids: list[int]) -> set[int]:
    """Sites whose blind count is the goods-v1 one (the rest use the legacy count)."""
    return set(
        SiteGuard.objects.filter(
            site_id__in=site_ids, stock_contract=SiteGuard.StockContract.GOODS_V1
        ).values_list("site_id", flat=True)
    )
