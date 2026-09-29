"""Copy stock and receipts into the reporting store (store operations ticket 43, ST-RPT-2).

Two copies, both goods-v1 stores only (a legacy store keeps no origins or
acceptance, so its snapshot says so and holds no pieces):

* **Stock snapshots.** Each run writes the day's snapshot of every selling
  store (``InventorySnapshot`` and its ``InventoryStockFact`` rows), replacing
  that day's earlier one, so the day's last run stands as its closing stock.
  The pieces are exactly those ticket 33 ages (``store_ageing``): good, accepted
  pieces standing at the store. A piece is **dead** when ticket 33 flags it in
  season (no sale of its item here for the ageing policy's days). Cost is each
  piece's recorded layer cost. For ticket 45's stock age, each row also keeps
  its pieces of an ended season and their days at the store added up. Past
  days are never rewritten: stock at an earlier day is only known where a
  snapshot was taken that day.
* **Received.** Pieces put away good at a store (acceptance events, append-only)
  from a receipt PT, an opening PT or a transfer. A customer return put back
  (the bill's own acceptance) and goods back from a vendor return (a movement)
  are not received. Each event's pieces are traced to their origin through the
  lot's coverage, which names the item and its season; pieces no coverage
  explains keep no item and still count at their store.

* **Stock by barcode** (ticket 29, the brands' SOH reports). The same pieces as
  the snapshot, one row per barcode and MRP (``InventoryItemFact``). Only each
  month's last snapshot is kept per store: today's replaces this month's
  earlier one, so a past month keeps its closing stock.

**Kept apart.** Runs on the worker's clock and from ``manage.py
refresh_inventory_report``, never while a report page loads. It only reads the
goods tables, with plain reads, and writes only its own tables, in one
transaction. Received is incremental: each run re-reads the events recorded
since the last run began, less the report overlap, and replaces them; the
day's first run re-reads every event, so a piece covered anew is named anew.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from core.commands import database_now
from core.goods_fields import bounds
from core.kernel_models import DocumentIdentity
from core.outbox import ANCHOR_INTERVAL
from core.tenancy import tenant_context
from masters.goods_identity_services import candidates_for
from masters.goods_models import Tenant
from masters.models import Season, Store
from offers.resolution import normalise
from reporting.models import (
    InventoryItemFact,
    InventoryReceiptFact,
    InventorySnapshot,
    InventoryStockFact,
    ReportRefresh,
)
from stockledger.goods_ageing import IN_SEASON, SEASON_ENDED, idle_days_policy, store_ageing
from stockledger.goods_models import AcceptanceEvent, CoverageEvent, Origin
from stockledger.goods_reads import origin_seasons

logger = logging.getLogger(__name__)

#: A covered stretch of a lot: its lower and upper piece bounds, and its origin.
Span = tuple[int, int, uuid.UUID]

#: The ``ReportRefresh`` row this copy keeps its freshness in.
KEY = "inventory"
#: Its own advisory lock, apart from the other copies'.
LOCK_ID = 7_310_043

#: What an acceptance was of, by its source document's purpose.
RECEIVED_FROM: dict[str, str] = {
    DocumentIdentity.Purpose.RECEIPT.value: InventoryReceiptFact.Source.RECEIPT.value,
    DocumentIdentity.Purpose.OPENING.value: InventoryReceiptFact.Source.OPENING.value,
    DocumentIdentity.Purpose.TRANSFER.value: InventoryReceiptFact.Source.TRANSFER.value,
}


@dataclass(frozen=True)
class RefreshResult:
    #: False when another run held the lock, so this one did nothing.
    ran: bool
    as_of: datetime | None = None
    stores: int = 0
    events: int = 0
    took_ms: int = 0


def scheduled_refresh(_tenant_id: uuid.UUID) -> None:
    """The worker's call, once per tenant each tick. The copy covers every tenant,
    so a copy refreshed within the last half tick is fresh enough; a run already
    in progress elsewhere is simply skipped."""
    state = ReportRefresh.objects.filter(key=KEY).first()
    if state and state.as_of and timezone.now() - state.as_of < ANCHOR_INTERVAL / 2:
        return
    refresh()


def refresh(*, full: bool = False, today: date | None = None) -> RefreshResult:
    """Take today's stock snapshot and bring received up to date; ``full`` re-reads
    every acceptance event. ``today`` is for tests: the snapshot's day."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        row = cursor.fetchone()
    if not row or not row[0]:
        return RefreshResult(ran=False)
    try:
        return _refresh(full, today or timezone.localdate())
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
            logger.exception("inventory report refresh: advisory unlock failed")


def _refresh(full: bool, today: date) -> RefreshResult:
    clock = time.monotonic()
    started = database_now()
    state = ReportRefresh.objects.filter(key=KEY).first()
    since = None
    # The first run of a day re-reads every received piece, so a later change to
    # how a piece is covered (and so its item and season) reaches the copy daily.
    if (
        not full
        and state is not None
        and state.as_of is not None
        and timezone.localtime(state.as_of).date() >= today
    ):
        since = state.as_of - timedelta(minutes=int(settings.KDPS_REPORT_REFRESH_OVERLAP_MINUTES))
    idle = idle_days_policy()
    snapshots: list[InventorySnapshot] = []
    stock: list[InventoryStockFact] = []
    items: list[InventoryItemFact] = []
    receipts: list[InventoryReceiptFact] = []
    events: set[uuid.UUID] = set()
    for tenant_id in Tenant.objects.order_by("id").values_list("id", flat=True):
        with tenant_context(tenant_id):
            found_snapshots, found_stock, found_items = _snapshots(tenant_id, today, idle, started)
            found_receipts, found_events = _receipts(tenant_id, since)
        snapshots += found_snapshots
        stock += found_stock
        items += found_items
        receipts += found_receipts
        events |= found_events
    with transaction.atomic():
        state, _ = ReportRefresh.objects.select_for_update().get_or_create(key=KEY)
        stores = [s.store_id for s in snapshots]
        InventoryStockFact.objects.filter(day=today, store_id__in=stores).delete()
        InventorySnapshot.objects.filter(day=today, store_id__in=stores).delete()
        InventorySnapshot.objects.bulk_create(snapshots, batch_size=5000)
        InventoryStockFact.objects.bulk_create(stock, batch_size=5000)
        # One snapshot by barcode per store and month: this month's earlier days go.
        InventoryItemFact.objects.filter(
            store_id__in=stores, day__gte=today.replace(day=1), day__lte=today
        ).delete()
        InventoryItemFact.objects.bulk_create(items, batch_size=5000)
        if since is None:
            InventoryReceiptFact.objects.all().delete()
        else:
            InventoryReceiptFact.objects.filter(event_id__in=sorted(events, key=str)).delete()
        InventoryReceiptFact.objects.bulk_create(receipts, batch_size=5000)
        took = int((time.monotonic() - clock) * 1000)
        state.as_of = started
        state.took_ms = took
        state.documents = len(events)
        state.failed_at = None
        state.failure = ""
        state.save()
    return RefreshResult(
        ran=True, as_of=started, stores=len(snapshots), events=len(events), took_ms=took
    )


# -- stock --------------------------------------------------------------------------


def _snapshots(
    tenant_id: Any, today: date, idle: int | None, taken_at: datetime
) -> tuple[list[InventorySnapshot], list[InventoryStockFact], list[InventoryItemFact]]:
    stores = Store.objects.filter(
        tenant_id=tenant_id, is_active=True, store_type=Store.StoreType.STORE
    ).order_by("id")
    snapshots: list[InventorySnapshot] = []
    facts: list[InventoryStockFact] = []
    items: list[InventoryItemFact] = []
    season_ids = dict(Season.objects.values_list("code", "pk"))
    for store in stores:
        ageing = store_ageing(store, today, idle)
        snapshots.append(
            InventorySnapshot(
                store=store,
                day=today,
                taken_at=taken_at,
                goods_records=ageing.goods_records,
                idle_days=idle,
                items_kept=True,
            )
        )
        if not ageing.rows:
            continue
        priced = {
            pk: (cost, mrp)
            for pk, cost, mrp in Origin.objects.filter(
                pk__in=sorted({row.origin_id for row in ageing.rows}, key=str)
            ).values_list("pk", "unit_cost", "mrp")
        }
        costs = {pk: cost for pk, (cost, _mrp) in priced.items()}
        # One row per brand, category and season: all a report reads, and a day's
        # snapshot of a store stays a few hundred rows however many items it holds.
        grouped: dict[tuple[int | None, str, str, str], InventoryStockFact] = {}
        for row in ageing.rows:
            key = (row.brand_id, row.brand[:120], row.item[:120], row.season_code[:120])
            fact = grouped.get(key)
            if fact is None:
                fact = grouped[key] = InventoryStockFact(
                    store=store,
                    day=today,
                    brand=row.brand[:120],
                    brand_ref_id=row.brand_id,
                    category=row.item[:120],
                    season_id=season_ids.get(row.season_code) if row.season_code else None,
                    season=row.season_code[:120],
                    pieces=0,
                    cost_paise=0,
                    ended_pieces=0,
                    piece_days=0,
                )
            cost = row.qty * int(costs[row.origin_id])
            fact.pieces += row.qty
            fact.cost_paise += cost
            fact.piece_days = int(fact.piece_days or 0) + row.qty * row.days_here
            if row.group == SEASON_ENDED:
                fact.ended_pieces = int(fact.ended_pieces or 0) + row.qty
            if row.group == IN_SEASON and row.aged:
                fact.dead_pieces += row.qty
                fact.dead_cost_paise += cost
        facts += grouped.values()
        items += _items(store, today, ageing.rows, {pk: mrp for pk, (_c, mrp) in priced.items()})
    return snapshots, facts, items


def _items(
    store: Store, today: date, rows: Iterable[Any], mrps: dict[Any, Any]
) -> list[InventoryItemFact]:
    """The pieces by barcode and MRP, named as the ageing rows name them."""
    grouped: dict[tuple[str, ...], InventoryItemFact] = {}
    for row in rows:
        mrp = int(mrps[row.origin_id]) if mrps.get(row.origin_id) else None
        key = (str(row.brand_id), row.barcode, row.brand, row.item, row.design, row.size, row.colour, row.season_code)
        fact = grouped.get((*key, str(mrp)))
        if fact is None:
            fact = grouped[(*key, str(mrp))] = InventoryItemFact(
                store=store,
                day=today,
                brand=row.brand[:120],
                    brand_ref_id=row.brand_id,
                brand_key=normalise(row.brand)[:120],
                item=row.item[:120],
                design=row.design[:120],
                size=row.size[:24],
                color=row.colour[:60],
                barcode=row.barcode[:64],
                season=row.season_code[:120],
                mrp_paise=mrp,
                pieces=0,
            )
        fact.pieces += row.qty
    return list(grouped.values())


# -- received -----------------------------------------------------------------------


def _receipts(
    tenant_id: Any, since: datetime | None
) -> tuple[list[InventoryReceiptFact], set[uuid.UUID]]:
    rows = AcceptanceEvent.objects.filter(
        tenant_id=tenant_id,
        outcome=AcceptanceEvent.Outcome.ACCEPTED_GOOD,
        session__source_version__document__purpose__in=list(RECEIVED_FROM),
    )
    if since is not None:
        rows = rows.filter(recorded_at__gte=since)
    found = list(
        rows.order_by("recorded_at", "id").values_list(
            "pk",
            "site_id",
            "event_at",
            "lot_id",
            "portion",
            "session__source_version__document__purpose",
        )
    )
    if not found:
        return [], set()
    covers = _covers({lot for _pk, _site, _at, lot, _portion, _purpose in found})
    wanted = {o for pair in covers.values() for spans in pair for *_, o in spans}
    origins = {
        str(pk): sku
        for pk, sku in Origin.objects.filter(pk__in=sorted(wanted, key=str)).values_list(
            "pk", "sku_id"
        )
    }
    seasons = origin_seasons(list(origins))
    described = {
        row["sku_id"]: row for row in candidates_for(tenant_id, {str(s) for s in origins.values()})
    }
    out: list[InventoryReceiptFact] = []
    for pk, site_id, event_at, lot_id, portion, purpose in found:
        lower, upper = bounds(portion)
        day = timezone.localtime(event_at).date()
        explained = 0
        standing, fallen = covers.get(lot_id, ([], []))
        for low, high, origin_id in [*standing, *fallen]:
            pieces = min(upper - lower - explained, min(upper, high) - max(lower, low))
            if pieces <= 0:
                continue
            explained += pieces
            sku_id = origins.get(str(origin_id))
            season = (seasons.get(str(origin_id)) or {}).get("now") or {}
            identity = described.get(str(sku_id), {})
            out.append(
                InventoryReceiptFact(
                    store_id=site_id,
                    day=day,
                    event_id=pk,
                    origin_id=origin_id,
                    source=RECEIVED_FROM[purpose],
                    sku_id=sku_id,
                    brand=str(identity.get("brand") or "")[:120],
                    brand_ref_id=identity.get("brand_id"),
                    category=str(identity.get("grade") or "")[:120],
                    season_id=int(season["season_id"]) if season.get("season_id") else None,
                    season=str(season.get("code") or "")[:120],
                    pieces=pieces,
                )
            )
        if explained < upper - lower:
            out.append(
                InventoryReceiptFact(
                    store_id=site_id,
                    day=day,
                    event_id=pk,
                    origin_id=None,
                    source=RECEIVED_FROM[purpose],
                    pieces=upper - lower - explained,
                )
            )
    return out, {row[0] for row in found}


def _covers(
    lot_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, tuple[list[Span], list[Span]]]:
    """Each lot's portions and the origin that covers them: the covers that still
    stand, then those later countered. A piece is named by a standing cover first;
    a countered one names only what no standing cover explains."""
    rows = list(
        CoverageEvent.objects.filter(lot_id__in=sorted(set(lot_ids), key=str))
        .order_by("recorded_at", "id")
        .values_list("pk", "lot_id", "portion", "origin_id", "effect", "counter_of_id")
    )
    countered = {counter for *_, effect, counter in rows if effect == "counter" and counter}
    out: dict[uuid.UUID, tuple[list[Span], list[Span]]] = defaultdict(lambda: ([], []))
    for pk, lot_id, portion, origin_id, effect, _counter in rows:
        if effect != "cover":
            continue
        lower, upper = bounds(portion)
        out[lot_id][1 if pk in countered else 0].append((lower, upper, origin_id))
    return dict(out)
