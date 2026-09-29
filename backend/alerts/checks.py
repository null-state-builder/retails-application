"""The checks the alerts job runs, and the sync that keeps their open
alerts matching what is true right now (#77).

Both checks return every currently-true instance of their kind, keyed by a
dedupe key; ``sync_kind`` opens what's new, leaves what's still open alone,
and resolves what stopped being true. A later alert kind (dead stock, another
deadline) is another check function returning the same ``AlertHit`` shape, not
a new mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.utils import timezone

from core.documents import DocStatus
from masters.models import Brand
from outbound import size_balancing
from outbound.models import ReturnSource, StoreTransfer
from outbound.returnable import returnable_pool_all

from .models import Alert, AlertKind, AlertPolicy, AlertStatus


@dataclass(frozen=True)
class AlertHit:
    """One currently-true instance of an alert kind, before it becomes a row."""

    dedupe_key: str
    title: str
    store_id: int | None
    brand: str
    object_id: int | None
    due_date: date | None
    threshold_days: int | None


def _thresholds(kind: str) -> list[int]:
    """This kind's configured thresholds, largest first — data, not code
    (Rule 12). No policy row means the check is switched off, not defaulted."""
    policy = AlertPolicy.objects.filter(kind=kind).first()
    return sorted(policy.thresholds_days, reverse=True) if policy else []


def check_in_transit_aging(today: date) -> list[AlertHit]:
    """Dispatched transfers with pieces still on the road past the configured
    number of days — transit loss without anyone remembering to check.

    One threshold for this kind: "not received within N days" (the corpus's own
    words). A transfer with a receipt is done; a transfer whose scan gap has
    already been closed (nothing left ``in_transit``) has nothing to chase.
    """
    thresholds = _thresholds(AlertKind.IN_TRANSIT_AGING)
    if not thresholds:
        return []
    days = thresholds[0]
    cutoff = today - timedelta(days=days)

    hits: list[AlertHit] = []
    qs = (
        StoreTransfer.objects.filter(docstatus=DocStatus.SUBMITTED, receipt__isnull=True)
        .exclude(dispatch_date__isnull=True)
        .filter(dispatch_date__date__lte=cutoff)
        .select_related("destination_store")
        .prefetch_related("lines")
    )
    for transfer in qs:
        qty_stuck = sum(line.qty_in_transit for line in transfer.lines.all())
        if qty_stuck <= 0:
            continue
        dispatched_on = timezone.localtime(transfer.dispatch_date).date()
        overdue_by = (today - dispatched_on).days - days
        hits.append(
            AlertHit(
                dedupe_key=f"transfer:{transfer.id}",
                title=(
                    f"{transfer.doc_number or 'Transfer'} to {transfer.destination_store.name} — "
                    f"{qty_stuck} pc(s) still in transit, {overdue_by} day(s) past the "
                    f"{days}-day limit"
                ),
                store_id=transfer.destination_store_id,
                brand="",
                object_id=transfer.id,
                due_date=dispatched_on + timedelta(days=days),
                threshold_days=days,
            )
        )
    return hits


def check_return_window(today: date) -> list[AlertHit]:
    """Season-end holdings inside a brand's negotiated return window, counting
    down through the configured days-left thresholds (30/15/7).

    Confirmed-damage holdings carry no window (returnable.py) and are never
    alerted here; an already-expired holding is not offered back to the brand
    at all, so it stops crossing new thresholds.

    One alert per holding, not one per threshold: a holding may have crossed
    more than one threshold by the time the job first sees it (a first-ever
    run, or one that missed a few days), but a countdown is one reminder that
    tightens, not three permanent ones stacked on top of each other — so only
    the tightest threshold already crossed is reported, and it re-fires under
    the *same* dedupe key as the countdown continues, refreshing the alert
    already open rather than opening a second one beside it.
    """
    thresholds = _thresholds(AlertKind.RETURN_WINDOW)
    if not thresholds:
        return []

    hits: list[AlertHit] = []
    # `takes_returns` is derived from the two commercial-model axes, not a
    # column — filtered in Python, the same as `returnable_pool_all` does it.
    for brand in Brand.objects.filter(is_active=True):
        if not brand.takes_returns:
            continue
        for row in returnable_pool_all(brand, today=today):
            if row.source != ReturnSource.SEASON_END or row.expired or row.days_left is None:
                continue
            crossed = [t for t in thresholds if row.days_left <= t]
            if not crossed:
                continue
            tightest = min(crossed)
            hits.append(
                AlertHit(
                    dedupe_key=f"return_window:{brand.id}:{row.store_id}:{row.sku_code}",
                    title=(
                        f"{brand.name} at {row.store_code} — {row.sku_code} "
                        f"({row.qty} pc(s)) must go back within {row.days_left} day(s) — "
                        f"past the {tightest}-day mark"
                    ),
                    store_id=row.store_id,
                    brand=brand.name,
                    object_id=None,
                    due_date=row.window_date,
                    threshold_days=tightest,
                )
            )
    return hits


def check_stock_ageing(today: date) -> list[AlertHit]:
    """Aged stock at each store where season-aware ageing is switched on (ticket 33).

    Three alerts per store at most, not one per item: in-season stock with no
    sale for the policy's days, stock whose season has ended, and stock of a
    closed season whose end nobody has recorded - that stock cannot be aged until
    somebody does, and it must not sit there unnoticed. Each names how many pieces
    and items, and opens the store's Stock Ageing page. The unknown historical
    season is never aged and raises nothing. A store with the switch off raises
    nothing, and its open ageing alerts resolve on the next run.
    """
    from masters.models import Store
    from masters.store_feature_registry import SEASON_AGEING
    from masters.store_features import feature, switch_states
    from stockledger.goods_ageing import IN_SEASON, SEASON_ENDED, SEASON_UNCLEAR, store_ageing

    thresholds = _thresholds(AlertKind.STOCK_AGEING)
    if not thresholds:
        return []
    idle = min(thresholds)
    stores = list(Store.objects.filter(is_active=True, store_type=Store.StoreType.STORE))
    states = switch_states(stores, [feature(SEASON_AGEING)])
    hits: list[AlertHit] = []
    for store, state in zip(stores, states, strict=True):
        if not state.enabled:
            continue
        report = store_ageing(store, today, idle)
        totals = {total.group: total for total in report.totals()}
        in_season = totals[IN_SEASON]
        if in_season.aged_qty:
            aged_items = len({r.sku_id for r in report.rows if r.group == IN_SEASON and r.aged})
            hits.append(
                AlertHit(
                    dedupe_key=f"stock_ageing:{store.pk}:{IN_SEASON}",
                    title=(
                        f"{store.code}: {in_season.aged_qty} pc(s) of in-season stock "
                        f"({aged_items} item(s)) with no sale for {idle} days or more"
                    ),
                    store_id=store.pk,
                    brand="",
                    object_id=store.pk,
                    due_date=None,
                    threshold_days=idle,
                )
            )
        ended = totals[SEASON_ENDED]
        if ended.qty:
            hits.append(
                AlertHit(
                    dedupe_key=f"stock_ageing:{store.pk}:{SEASON_ENDED}",
                    title=(
                        f"{store.code}: {ended.qty} pc(s) ({ended.items} item(s)) from seasons "
                        f"that have ended, the oldest {ended.oldest_days} day(s) ago"
                    ),
                    store_id=store.pk,
                    brand="",
                    object_id=store.pk,
                    due_date=None,
                    threshold_days=None,
                )
            )
        unclear = totals[SEASON_UNCLEAR]
        if unclear.qty:
            hits.append(
                AlertHit(
                    dedupe_key=f"stock_ageing:{store.pk}:{SEASON_UNCLEAR}",
                    title=(
                        f"{store.code}: {unclear.qty} pc(s) ({unclear.items} item(s)) cannot be "
                        "aged until their season's end date is recorded"
                    ),
                    store_id=store.pk,
                    brand="",
                    object_id=store.pk,
                    due_date=None,
                    threshold_days=None,
                )
            )
    return hits


def check_reservation_expiry(today: date) -> list[AlertHit]:
    """Customer reservations about to reach their collect-by date (ticket 20, ST-OPS-2).

    One alert per active reservation within the policy's days of its last day,
    so staff can call the customer before the pieces go back. It resolves on the
    next run once the reservation is collected, cancelled or expired. The
    customer's name and number stay off the title: the inbox is read by people
    who only need the reference.
    """
    from sell.reservation_models import CustomerReservation

    thresholds = _thresholds(AlertKind.RESERVATION_EXPIRY)
    if not thresholds:
        return []
    window = max(thresholds)
    due = CustomerReservation.objects.filter(
        status=CustomerReservation.Status.ACTIVE,
        collect_by__lte=today + timedelta(days=window),
    ).select_related("store")
    hits: list[AlertHit] = []
    for reservation in due:
        left = (reservation.collect_by - today).days
        when = "today" if left <= 0 else ("tomorrow" if left == 1 else f"in {left} days")
        hits.append(
            AlertHit(
                dedupe_key=f"reservation:{reservation.pk}",
                title=(
                    f"{reservation.ref}: reservation ends {when} "
                    f"({reservation.collect_by:%d %b}) - call the customer to collect"
                ),
                store_id=reservation.store_id,
                brand="",
                object_id=None,
                due_date=reservation.collect_by,
                threshold_days=window,
            )
        )
    return hits


def check_broken_sizes(now: datetime) -> list[AlertHit]:
    """Broken sizes at each selling store (ticket 32, ST-INV-1).

    Each style-colour's own alert is a ``BrokenSizeAlert``, opened, refreshed and
    closed here (``stockledger.broken_size.refresh_store``); that is the row that
    records when it was acted on. The alerts centre gets one alert per store
    naming how many are open and how many nobody has acted on, and it opens the
    store's Broken Sizes page: a store can hold hundreds of style-colours, and a
    bell with one line each would bury everything else. A store with the switch
    off has its open broken-size alerts closed and raises nothing.
    """
    from masters.models import Store
    from masters.store_feature_registry import BROKEN_SIZE
    from masters.store_features import feature, switch_states
    from stockledger.broken_size import refresh_store, rules_in_force
    from stockledger.broken_size_models import BrokenSizeAlert

    stores = list(Store.objects.filter(is_active=True, store_type=Store.StoreType.STORE))
    states = switch_states(stores, [feature(BROKEN_SIZE)])
    # A store closed, or no longer a selling store, since its alerts opened: they
    # close too, so nothing is left open for ever.
    gone = Store.objects.filter(
        pk__in=BrokenSizeAlert.objects.filter(closed_at__isnull=True).values("site_id")
    ).exclude(pk__in=[store.pk for store in stores])
    for store in gone:
        refresh_store(store, on=False, now=now, closing=BrokenSizeAlert.Closed.NOT_CHECKED)
    rules = rules_in_force()
    hits: list[AlertHit] = []
    for store, state in zip(stores, states, strict=True):
        count = refresh_store(store, on=state.enabled, now=now, rules=rules)
        if not count.open:
            continue
        hits.append(
            AlertHit(
                dedupe_key=f"broken_size:{store.pk}",
                title=(
                    f"{store.code}: {count.open} style-colour(s) missing too many core sizes, "
                    f"{count.not_acted} not acted on yet"
                ),
                store_id=store.pk,
                brand="",
                object_id=store.pk,
                due_date=None,
                threshold_days=None,
            )
        )
    return hits


def check_sor_ageing(today: date) -> list[AlertHit]:
    """SOR stock that needs settling, at each site where SOR ageing is on (ticket 24).

    Three alerts per site at most, not one per piece: SOR pieces at 5 months from
    the brand's dispatch date (a month left to sell them, return them or get the
    brand's invoice), SOR pieces already past the 6 months the brand had to
    invoice by, and SOR pieces with no dispatch date - they cannot be aged until
    somebody records it, and must not sit there unnoticed. Each names how many
    pieces and opens the site's SOR Ageing page. Pieces whose brand model is
    unknown are listed on the page and raise nothing: nobody knows they are SOR.
    Every active site counts, warehouses too (SOR stock ages wherever it stands);
    a site with the switch off raises nothing, and its open alerts resolve.
    """
    from masters.models import Store
    from masters.store_feature_registry import SOR_AGEING
    from masters.store_features import feature, switch_states
    from stockledger.sor_ageing import DUE, NO_DISPATCH_DATE, OVERDUE, store_sor_ageing

    stores = list(Store.objects.filter(is_active=True).order_by("code"))
    states = switch_states(stores, [feature(SOR_AGEING)])
    hits: list[AlertHit] = []
    for store, state in zip(stores, states, strict=True):
        if not state.enabled:
            continue
        report = store_sor_ageing(store, today)
        for group in (OVERDUE, DUE):
            rows = [row for row in report.rows if row.state == group]
            if not rows:
                continue
            qty = sum(row.qty for row in rows)
            first = min(row.invoice_by for row in rows if row.invoice_by is not None)
            title = (
                f"{store.code}: {qty} pc(s) of SOR stock past 6 months from the brand's "
                f"dispatch with no brand invoice, the oldest due by {first:%d %b %Y}"
                if group == OVERDUE
                else f"{store.code}: {qty} pc(s) of SOR stock at 5 months from the brand's "
                f"dispatch - sell, return or get the brand's invoice by {first:%d %b %Y}"
            )
            hits.append(
                AlertHit(
                    dedupe_key=f"sor_ageing:{store.pk}:{group}",
                    title=title,
                    store_id=store.pk,
                    brand="",
                    object_id=store.pk,
                    due_date=first,
                    threshold_days=None,
                )
            )
        undated = sum(row.qty for row in report.rows if row.state == NO_DISPATCH_DATE)
        if undated:
            hits.append(
                AlertHit(
                    dedupe_key=f"sor_ageing:{store.pk}:{NO_DISPATCH_DATE}",
                    title=(
                        f"{store.code}: {undated} pc(s) of SOR stock cannot be aged until "
                        "the brand's dispatch date is recorded"
                    ),
                    store_id=store.pk,
                    brand="",
                    object_id=store.pk,
                    due_date=None,
                    threshold_days=None,
                )
            )
    return hits


def sync_sor_alerts(today: date | None = None) -> int:
    """Refresh the SOR ageing alerts now (a recorded dispatch date or brand invoice
    calls this, so a settled delivery leaves its alert at once)."""
    hits = check_sor_ageing(today or timezone.localdate())
    sync_kind(AlertKind.SOR_AGEING, AlertKind.SOR_AGEING.label, hits)
    return len(hits)


def sync_reservation_alerts(today: date | None = None) -> int:
    """Refresh the reservation-expiry alerts now (the expiry job calls this)."""
    hits = check_reservation_expiry(today or timezone.localdate())
    sync_kind(AlertKind.RESERVATION_EXPIRY, AlertKind.RESERVATION_EXPIRY.label, hits)
    return len(hits)


def sync_kind(kind: str, kind_label: str, hits: list[AlertHit]) -> None:
    """Open what's new, resolve what stopped being true, and refresh what's
    still open — a holding's countdown moves even while its alert stays open,
    so the title/threshold on an existing row must track it, not freeze at
    whatever it said the day the row was born."""
    current_keys = {hit.dedupe_key for hit in hits}
    (
        Alert.objects.filter(kind=kind, status=AlertStatus.OPEN)
        .exclude(dedupe_key__in=current_keys)
        .update(status=AlertStatus.RESOLVED, resolved_at=timezone.now())
    )
    for hit in hits:
        Alert.objects.update_or_create(
            kind=kind,
            dedupe_key=hit.dedupe_key,
            status=AlertStatus.OPEN,
            defaults={
                "kind_label": kind_label,
                "title": hit.title,
                "store_id": hit.store_id,
                "brand": hit.brand,
                "object_id": hit.object_id,
                "due_date": hit.due_date,
                "threshold_days": hit.threshold_days,
            },
        )


def run_alert_checks(today: date | None = None) -> dict[str, int]:
    """Run every check and sync the alerts table. Returns the hit counts, for
    the job's own log line."""
    today = today or timezone.localdate()
    aging = check_in_transit_aging(today)
    window = check_return_window(today)
    ageing = check_stock_ageing(today)
    reservations = check_reservation_expiry(today)
    broken = check_broken_sizes(timezone.now())
    sor = check_sor_ageing(today)
    sync_kind(AlertKind.IN_TRANSIT_AGING, AlertKind.IN_TRANSIT_AGING.label, aging)
    sync_kind(AlertKind.RETURN_WINDOW, AlertKind.RETURN_WINDOW.label, window)
    sync_kind(AlertKind.STOCK_AGEING, AlertKind.STOCK_AGEING.label, ageing)
    sync_kind(AlertKind.RESERVATION_EXPIRY, AlertKind.RESERVATION_EXPIRY.label, reservations)
    sync_kind(AlertKind.BROKEN_SIZE, AlertKind.BROKEN_SIZE.label, broken)
    sync_kind(AlertKind.SOR_AGEING, AlertKind.SOR_AGEING.label, sor)
    # Ticket 34 (ST-TRF-1): suggested transfers read the broken sizes just checked.
    balanced = size_balancing.refresh(timezone.now())
    return {
        "reservation_expiry": len(reservations),
        "in_transit_aging": len(aging),
        "return_window": len(window),
        "stock_ageing": len(ageing),
        "broken_size": len(broken),
        "sor_ageing": len(sor),
        "size_balancing": balanced.waiting,
    }
