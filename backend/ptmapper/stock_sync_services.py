"""Nightly stock sync to the legacy SOH for a synthetic test tenant (store test run, P22).

During the test run the legacy software is the only real book. Staff also bill
and move goods in this system to try it, and the shop really sells, receives
and transfers on the legacy software. Each night the legacy SOH is uploaded and
one apply brings this store to it, in one journal batch:

1. **Test bills go back.** Every goods-v1 bill at this store after the previous
   sync's cutoff and up to this one has its sold portions put back exactly where
   its own journal says they came from. The bill row stays, as test history.
2. **Fewer in the SOH:** the difference leaves oldest-first by the same FIFO
   selection a real bill uses (`goods_sale.plan_sale`), as ``consumed``.
3. **More in the SOH, item already known:** a new adjustment lot arrives valued
   at that SKU's most recent recorded origin cost and is accepted straight onto
   the floor, so the counter can sell it in the morning.
4. **Barcode in no PT:** flagged only. Nothing is created, stocked or made
   sellable; it becomes real when a PT carrying it is uploaded, and a later
   SOH sync then matches it.

The legacy software is master for quantities during the alpha, so known items
are adjusted automatically, and every row that differs is kept with a review
category (``StockSyncRow.Category``) for the store manager, who is told once
per sync through one ``stock_sync_review`` exception. Acknowledging a row only
marks it reviewed; nothing here lets anyone add or edit items or stock by hand.
Anything that cannot be applied - held or reserved stock, an SKU with no
scannable barcode - is kept as an exception row and never blocks the night.
Nothing here creates a bill, a tax document or a GL posting, and the whole
module refuses at a real (non-synthetic) store.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Max
from django.utils import timezone

from accounts.goods_models import HumanIdentity
from core.commands import CommandRun, LockRank
from core.documents import DocStatus
from core.goods_documents import append_revision, new_document, officialise
from core.goods_fields import bounds, portion
from core.kernel_models import DocumentIdentity, OfficialLine
from core.operational import LEGACY_SYNC_POSTING, ValuePair
from core.refusals import Refusal
from files.goods_models import EvidenceObject
from masters.goods_identity_models import SkuAlias
from masters.goods_models import Location
from masters.models import Store
from masters.store_features import is_real_store
from ptmapper.soh_parser import SourceRow
from ptmapper.stock_sync_models import StockSync, StockSyncRow
from sell.services.goods_sale import SOLD_REASON, GoodsSaleError, plan_sale
from sell.services.goods_stock import Shelf, barcode_aliases, is_goods_site, read_shelf
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import (
    AcceptanceEvent,
    AcceptanceSession,
    CustodyLot,
    JournalBatch,
    Origin,
    Position,
    QuantityLeg,
)

#: Who may upload and apply: the same people who may stage an opening import
#: at the store (owner, store person, warehouse).
ACTION = "opening.import.stage"
DOC_KIND = "SYN"
PURPOSE = "stock_sync"
#: Why a piece left: the reason on its consumed quantity leg.
SYNC_REMOVED_REASON = "legacy_soh_lower"
FOUND_BOUNDARY = "legacy_soh"
#: One owned exception per applied sync with flagged rows, for the store manager.
EXCEPTION_KIND = "stock_sync_review"
SOH_ONLY_NOTE = "Not in any PT - not sellable until its PT is uploaded."
Category = StockSyncRow.Category


def require_test_store(store: Store) -> None:
    if is_real_store(store):
        raise Refusal("TEST_TENANT_ONLY",
                      "Stock sync runs only on a test tenant beside the legacy software.", status=409)
    if not is_goods_site(store):
        raise Refusal("CONTRACT_DISABLED", "This store does not keep goods-v1 stock.", status=409)


def require_opening(store: Store) -> None:
    """A store that has never held stock opens first (PT files or an SOH at /goods/opening).

    Synced first, every barcode would become a top-up opening, and the store's own
    opening would then be refused as a second one.
    """
    if not (CustodyLot.objects.filter(tenant_id=store.tenant_id, initial_site_id=store.pk).exists()
            or Origin.objects.filter(tenant_id=store.tenant_id, site_id=store.pk).exists()
            or Position.objects.filter(tenant_id=store.tenant_id, site_id=store.pk, boundary="physical").exists()):
        raise Refusal("OPENING_REQUIRED", "Load this store's opening stock first (Opening stock: upload its PT files).",
                      status=409)


def require_soh_after_stock(store: Store, cutoff_at: datetime, now: datetime) -> None:
    """An SOH exported before some of the store's stock was loaded here cannot be matched to it.

    The sync reads the store's barcodes as they stood at the export time. Items loaded later (an
    opening or a top-up) would then look absent - never taken out - and come in again as new items,
    doubling them. The store manager exports the SOH again instead.
    """
    held = {sku for sku in Position.objects.filter(tenant_id=store.tenant_id, site_id=store.pk, boundary="physical")
            .values_list("sku_id", flat=True) if sku is not None}
    late = (held - set(barcode_aliases(store, cutoff_at))) & set(barcode_aliases(store, now))
    if not late:
        return
    since = SkuAlias.objects.filter(sku_id__in=late).aggregate(at=Max("effective_from"))["at"]
    at = timezone.localtime(since).strftime("%d %b %Y, %H:%M") if since else "after the export"
    raise Refusal("SOH_BEFORE_STOCK",
                  f"This SOH was exported before {len(late)} of this store's items were loaded here (last at {at}). "
                  "Export the SOH again now and upload that one.", status=409)


def previous_cutoff(store: Store) -> datetime | None:
    last = (StockSync.objects.filter(tenant_id=store.tenant_id, site=store, state=StockSync.State.APPLIED)
            .order_by("-cutoff_at").first())
    return last.cutoff_at if last else None


def create_sync(run: CommandRun, *, site_id: int, evidence: EvidenceObject, cutoff_at: datetime,
                metadata: dict[str, Any], parsed: list[SourceRow]) -> StockSync:
    stores = run.lock(LockRank.SITE, Store.objects.filter(tenant_id=run.tenant_id, pk=site_id, is_active=True))
    if not stores:
        raise Refusal("NOT_FOUND", "That store was not found.")
    require_test_store(stores[0])
    require_opening(stores[0])
    # App and database clocks may differ slightly; a cutoff minutes ahead is a mistake.
    if cutoff_at > run.now + timedelta(minutes=5):
        raise Refusal("INVALID_REQUEST", "The SOH export time cannot be in the future.")
    found = (StockSync.objects.filter(tenant_id=run.tenant_id, site_id=site_id, source_hash=evidence.sha256)
             .exclude(state=StockSync.State.DISCARDED).first())
    if found is not None and found.cutoff_at != cutoff_at:
        raise Refusal("SOH_ALREADY_UPLOADED",
                      f"This SOH is already uploaded as exported at {timezone.localtime(found.cutoff_at):%d %b %Y, %H:%M}. "
                      "Discard that one to give it a different export time.", status=409)
    if found is not None:
        # The same file at the same export time is the same sync: nothing happens again.
        run.audit_after = {"stock_sync_id": str(found.pk), "unchanged": True}
        return found
    require_newer_than_applied(stores[0], cutoff_at)
    require_soh_after_stock(stores[0], cutoff_at, run.now)
    assert run.principal.human_id is not None
    rows = [{"ordinal": r.ordinal, "barcode": r.barcode, "qty": r.quantity, "brand": r.brand,
             "mrp": r.mrp, "rate": r.rate, "source": r.source} for r in parsed]
    sync = StockSync.objects.create(
        tenant_id=run.tenant_id, site_id=site_id, source_evidence=evidence, source_hash=evidence.sha256,
        source_name=evidence.filename[:240], cutoff_at=cutoff_at, metadata={**metadata, "rows": rows},
        uploaded_by_id=run.principal.human_id)
    run.audit_subject_key = f"stock_sync:{sync.pk}"
    run.audit_site_id = site_id
    run.audit_after = {"stock_sync_id": str(sync.pk), "rows": len(rows), "cutoff_at": cutoff_at.isoformat()}
    return sync


def require_newer_than_applied(store: Store, cutoff_at: datetime) -> None:
    """An SOH exported at or before the last applied one would take the store back in time."""
    after = previous_cutoff(store)
    if after is not None and cutoff_at <= after:
        raise Refusal("STOCK_SYNC_STALE",
                      f"A newer SOH (exported {timezone.localtime(after):%d %b %Y, %H:%M}) is already applied "
                      "at this store. Export the SOH again now and upload that one.", status=409)


def discard(run: CommandRun, sync: StockSync) -> StockSync:
    sync = _locked(run, sync)
    sync.state = StockSync.State.DISCARDED
    sync.save(update_fields=["state"])
    run.audit_subject_key = f"stock_sync:{sync.pk}"
    run.audit_after = {"state": sync.state}
    return sync


def _locked(run: CommandRun, sync: StockSync) -> StockSync:
    found = run.lock(LockRank.DOCUMENT, StockSync.objects.filter(tenant_id=run.tenant_id, pk=sync.pk))
    if not found:
        raise Refusal("NOT_FOUND", "That stock sync was not found.")
    locked: StockSync = found[0]
    if locked.state != StockSync.State.PREVIEWED:
        raise Refusal("STOCK_SYNC_DONE", f"This stock sync is already {locked.state}.", status=409)
    return locked


# ---------------------------------------------------------------------------
# Reading: the book, the SOH and the difference
# ---------------------------------------------------------------------------


def test_bills(store: Store, after: datetime | None, until: datetime) -> list[Any]:
    from sell.models import Sale

    bills = Sale.objects.filter(store=store, billed_at__lte=until, docstatus=DocStatus.SUBMITTED)
    if after is not None:
        bills = bills.filter(billed_at__gt=after)
    return list(bills.prefetch_related("lines").order_by("billed_at"))


def book(store: Store, at: datetime) -> tuple[dict[str, int], dict[str, uuid.UUID], list[uuid.UUID]]:
    """Physical pieces at this store per barcode, the barcode→SKU map, and SKUs with no barcode."""
    held: dict[uuid.UUID, int] = defaultdict(int)
    for sku_id, span in Position.objects.filter(site_id=store.pk, boundary="physical").values_list("sku_id", "portion"):
        if sku_id is not None:
            held[sku_id] += ranges.length(bounds(span))
    aliases = barcode_aliases(store, at)
    by_barcode = {barcode: sku for sku, barcode in aliases.items()}
    counts = {aliases[sku]: qty for sku, qty in held.items() if sku in aliases and qty}
    unlabelled = [sku for sku, qty in held.items() if qty and sku not in aliases]
    return counts, by_barcode, unlabelled


def soh_quantities(sync: StockSync) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for row in sync.metadata.get("rows", []):
        out[str(row["barcode"])] += max(int(row["qty"]), 0)
    return dict(out)


def soh_mrps(sync: StockSync) -> dict[str, set[int]]:
    """Every MRP (paise) the SOH gives each barcode; a barcode may repeat across rows."""
    out: dict[str, set[int]] = defaultdict(set)
    for row in sync.metadata.get("rows", []):
        if row.get("mrp"):
            out[str(row["barcode"])].add(int(row["mrp"]))
    return dict(out)


def book_mrps(shelf: Shelf) -> dict[str, set[int]]:
    """The MRP each barcode's pieces carry here (one per season, from its newest origin)."""
    out: dict[str, set[int]] = defaultdict(set)
    for piece in shelf.pieces:
        if piece.mrp_paise:
            out[piece.barcode].add(int(piece.mrp_paise))
    return dict(out)


def sellable_by_barcode(shelf: Shelf) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for (barcode, _season), qty in shelf.quantities.items():
        out[barcode] += qty
    return dict(out)


def valuation_origins(store: Store, sku_ids: set[uuid.UUID]) -> dict[uuid.UUID, Origin]:
    """Per SKU, the origin a found piece is valued at: its latest here, else its latest anywhere.

    One read for every SKU. A SKU with no origin at all came in on no PT, so an SOH
    barcode naming it is ``SOH_ONLY``.
    """
    here: dict[uuid.UUID, Origin] = {}
    anywhere: dict[uuid.UUID, Origin] = {}
    for origin in (Origin.objects.filter(tenant_id=store.tenant_id, sku_id__in=list(sku_ids))
                   .order_by("sku_id", "-source_time", "pk")):
        if origin.site_id == store.pk:
            here.setdefault(origin.sku_id, origin)
        anywhere.setdefault(origin.sku_id, origin)
    return {sku: here.get(sku) or origin for sku, origin in anywhere.items()}


def categorise(held: int, wanted: int, *, known: bool) -> str | None:
    """The quantity category of one barcode, or ``None`` when the book and the SOH agree.

    ``known`` means the barcode names a SKU that came in on a PT (it has an origin).
    """
    if held == wanted:
        return None
    if held > 0 and wanted == 0:
        return Category.PT_ONLY
    if wanted < held:
        return Category.SOH_BELOW
    return Category.SOH_ABOVE if known else Category.SOH_ONLY


def mrp_check(barcode: str, theirs: Mapping[str, set[int]],
              ours: Mapping[str, set[int]]) -> tuple[bool, int | None, int | None]:
    """(differs, SOH MRP, book MRP). Judged only when both sides name an MRP.

    It differs when the SOH names an MRP that none of this barcode's pieces carry.
    """
    soh, held = theirs.get(barcode) or set(), ours.get(barcode) or set()
    if not soh or not held:
        return False, min(soh) if soh else None, min(held) if held else None
    odd = sorted(soh - held)
    if odd:
        return True, odd[0], min(held)
    shared = min(soh & held)
    return False, shared, shared


def category_counts(rows: list[tuple[str | None, bool]]) -> dict[str, int]:
    """Rows per category from (category, mrp_differs) pairs.

    ``MRP_DIFFERS`` counts every row whose MRP differs, whether or not its
    quantity is flagged too, so the counts can add up to more than the rows.
    """
    out = {category.value: 0 for category in Category}
    for category, differs in rows:
        if category and category != Category.MRP_DIFFERS:
            out[category] += 1
        if differs:
            out[Category.MRP_DIFFERS] += 1
    return out


def preview(sync: StockSync) -> dict[str, Any]:
    """What an apply would do now, read-only (before test bills are put back)."""
    store = sync.site
    counts, by_barcode, unlabelled = book(store, sync.cutoff_at)
    soh, theirs = soh_quantities(sync), soh_mrps(sync)
    ours = book_mrps(read_shelf(store, sync.cutoff_at))
    known = valuation_origins(store, set(by_barcode.values()))
    lower = higher = soh_only = 0
    flags: list[tuple[str | None, bool]] = []
    for barcode in set(counts) | set(soh):
        held, wanted = counts.get(barcode, 0), soh.get(barcode, 0)
        sku = by_barcode.get(barcode)
        category = categorise(held, wanted, known=sku is not None and sku in known)
        differs = mrp_check(barcode, theirs, ours)[0]
        if category is None and not differs:
            continue
        flags.append((category or Category.MRP_DIFFERS, differs))
        if wanted < held:
            lower += held - wanted
        elif category == Category.SOH_ABOVE:
            higher += wanted - held
        elif category == Category.SOH_ONLY:
            soh_only += wanted
    return {"test_bills_to_reverse": len(test_bills(store, previous_cutoff(store), sync.cutoff_at)),
            "book_qty": sum(counts.values()), "soh_qty": sum(soh.values()),
            "remove_qty": lower, "add_qty": higher, "soh_only_qty": soh_only, "unlabelled_skus": len(unlabelled),
            "flagged_rows": len(flags), "category_counts": category_counts(flags)}


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _address(raw: Mapping[str, Any]) -> engine.Address:
    ids = {"location_id", "transfer_id", "sku_id", "origin_id", "value_basis_origin_id", "accepted_event_id"}
    values: dict[str, Any] = {key: (uuid.UUID(str(value)) if key in ids and value else value)
                              for key, value in raw.items() if key != "reason"}
    return engine.Address(**values)


def _still_consumed(lot_id: uuid.UUID, interval: ranges.Interval) -> bool:
    held = engine.positions_of(lot_id, interval)
    covered = ranges.intersect([bounds(p.portion) for p in held if p.boundary == "consumed"], [interval])
    return ranges.total(covered) == ranges.length(interval)


def _reverse_test_bills(run: CommandRun, batch: engine.Plan, store: Store, bills: list[Any]) -> dict[str, int]:
    """Put each test bill's sold portions back where the bill's own journal took them from."""
    counts = {"bills": 0, "pieces": 0, "already_back": 0}
    for sale in bills:
        journal = JournalBatch.objects.filter(tenant_id=run.tenant_id,
                                              event_key=engine.event_key("sale", sale.idempotency_uuid)).first()
        if journal is None:
            continue
        costs = [(uuid.UUID(str(row["lot_id"])), int(row["lower"]), int(row["upper"]),
                  uuid.UUID(str(row["origin_id"])), int(row["unit_cost_paise"]))
                 for line in sale.lines.all() for row in (line.goods_allocations or [])]
        pairs: dict[uuid.UUID, dict[str, QuantityLeg]] = defaultdict(dict)
        for leg in QuantityLeg.objects.filter(batch=journal):
            pairs[leg.pair_key][leg.side] = leg
        touched = False
        for pair in pairs.values():
            source, destination = pair.get("source"), pair.get("destination")
            if source is None or destination is None:
                continue
            if destination.position.get("boundary") != "consumed" or destination.position.get("reason") != SOLD_REASON:
                continue
            interval = bounds(source.portion)
            if not _still_consumed(source.lot_id, interval):
                counts["already_back"] += ranges.length(interval)
                continue
            engine.lock_lots(run, [source.lot_id])
            address = _address(source.position)
            engine.change_address(run, batch, source.lot_id, interval, lambda _old, a=address: a)
            cost = next((c for c in costs if c[0] == source.lot_id and c[1] <= interval[0] and interval[1] <= c[2]), None)
            if cost is not None and cost[4] > 0:
                batch.value.append(ValuePair(origin_id=cost[3], amount=ranges.length(interval) * cost[4],
                                             source_bucket="external", destination_bucket="stock",
                                             source_site_id=None, destination_site_id=store.pk,
                                             lot_id=source.lot_id, lower=interval[0], upper=interval[1]))
            counts["pieces"] += ranges.length(interval)
            touched = True
        counts["bills"] += int(touched)
    return counts


def _sellable(store: Store, barcode: str) -> int:
    """One barcode's sellable pieces here (the sales-report import reads them line by line)."""
    return sum(qty for (code, _season), qty in read_shelf(store, barcodes={barcode}).quantities.items() if code == barcode)


def _removed(row: StockSyncRow, qty: int, take: int) -> None:
    row.applied_qty = -take
    if take == qty:
        row.outcome = "removed"
    else:
        row.outcome, row.exception_code = "exception", "STOCK_NOT_FREE"
        row.exception_note = (f"{qty - take} of {qty} to remove are not on a selling shelf "
                              "(receiving, quarantine, held or reserved); settle them where they stand.")


def _end(run: CommandRun, batch: engine.Plan, store: Store, allocations: list[Any]) -> None:
    for allocation in allocations:
        engine.lock_lots(run, [allocation.lot_id])
        engine.end_positions(run, batch, allocation.lot_id, allocation.interval, "consumed", SYNC_REMOVED_REASON)
        if allocation.cost_paise > 0:
            batch.value.append(ValuePair(origin_id=allocation.origin_id, amount=allocation.cost_paise,
                                         source_bucket="stock", destination_bucket="external",
                                         source_site_id=store.pk, destination_site_id=None,
                                         lot_id=allocation.lot_id, lower=allocation.lower, upper=allocation.upper))


def _remove_all(run: CommandRun, batch: engine.Plan, store: Store, removals: list[tuple[StockSyncRow, int]],
                sellable: Mapping[str, int]) -> None:
    """Take every lower barcode out oldest-first by the bill's own FIFO (``plan_sale``), in one plan.

    Each barcode names its own SKU, so one plan over all of them chooses exactly
    what one plan per barcode would, with one shelf read instead of thousands. If
    the pooled plan is refused, each barcode is planned alone so only the barcode
    at fault becomes an exception row.
    """
    lines: dict[int, tuple[StockSyncRow, int, int]] = {}
    for row, qty in removals:
        take = min(qty, sellable.get(row.barcode, 0))
        if take > 0:
            lines[len(lines) + 1] = (row, qty, take)
        else:
            _removed(row, qty, 0)
    if not lines:
        return
    try:
        pooled = plan_sale(store, [{"line_no": no, "barcode": row.barcode, "season": "", "qty": take,
                                    "direction": "sale"} for no, (row, _qty, take) in lines.items()])
    except GoodsSaleError:
        pooled = None
    for no, (row, qty, take) in lines.items():
        if pooled is not None:
            allocations = pooled.sold[no]
        else:
            try:
                allocations = plan_sale(store, [{"line_no": 1, "barcode": row.barcode, "season": "", "qty": take,
                                                 "direction": "sale"}]).sold[1]
            except GoodsSaleError as refusal:
                row.outcome, row.exception_code, row.exception_note = "exception", refusal.code, str(refusal)[:300]
                continue
        _end(run, batch, store, allocations)
        _removed(row, qty, take)


def _selling_location(store: Store) -> Location:
    for kind in ("floor", "backstore"):
        found = Location.objects.filter(tenant_id=store.tenant_id, site=store, kind=kind).order_by("created_at").first()
        if found is not None:
            return found
    raise Refusal("STOCK_SYNC_NOT_READY", "This store has no floor or backstore location to put found pieces on.", status=422)


def _add(run: CommandRun, batch: engine.Plan, store: Store, version: Any, session: AcceptanceSession,
         line: OfficialLine, floor: Location) -> int:
    body = line.payload
    qty, sku_id, origin_id = int(body["qty"]), uuid.UUID(body["sku_id"]), uuid.UUID(body["origin_id"])
    receiving = engine.system_location(store.pk, "receiving")
    lot = engine.open_lot(
        run, batch, source_kind=CustodyLot.SourceKind.ADJUSTMENT, site_id=store.pk, qty=qty,
        identity={"sku_id": str(sku_id), "description": body.get("barcode") or "", "condition": "good",
                  "adjustment": "legacy_soh"},
        source_time=run.now,
        address=engine.Address(boundary="physical", site_id=store.pk, location_id=receiving.pk, condition="good",
                               sku_id=sku_id, value_basis_origin_id=origin_id),
        adjustment_line_id=line.pk, from_boundary=FOUND_BOUNDARY)
    unit_cost = int(body.get("unit_cost_paise") or 0)
    if unit_cost > 0:
        batch.value.append(ValuePair(origin_id=origin_id, amount=qty * unit_cost,
                                     source_bucket="adjustment_evidence", destination_bucket="stock",
                                     source_site_id=None, destination_site_id=store.pk,
                                     lot_id=lot.pk, lower=0, upper=qty))
    event: AcceptanceEvent = run.record(AcceptanceEvent(
        session_id=session.pk, site_id=store.pk, scan_key=uuid.uuid5(session.pk, str(lot.pk)), official_line=line,
        lot_id=lot.pk, portion=portion(0, qty), destination_location_id=floor.pk,
        observed_alias=str(body.get("barcode") or "")[:128], observed_ticket_mrp=None, label_evidence_ref=None,
        tag_verdict="matched", outcome=AcceptanceEvent.Outcome.ACCEPTED_GOOD))
    engine.change_address(run, batch, lot.pk, (0, qty),
                          lambda old: replace(old, location_id=floor.pk, accepted_event_id=event.pk))
    return qty


def apply(run: CommandRun, sync: StockSync) -> StockSync:
    # Store before document: the command kernel's lock order.
    store = run.lock(LockRank.SITE, Store.objects.filter(tenant_id=run.tenant_id, pk=sync.site_id))[0]
    sync = _locked(run, sync)
    require_test_store(store)
    after = previous_cutoff(store)
    require_newer_than_applied(store, sync.cutoff_at)
    require_soh_after_stock(store, sync.cutoff_at, run.now)
    assert run.principal.human_id is not None
    batch = engine.Plan(LEGACY_SYNC_POSTING, uuid.uuid4(), engine.event_key(PURPOSE, sync.pk))
    reversed_ = _reverse_test_bills(run, batch, store, test_bills(store, after, sync.cutoff_at))

    # One read of the whole store, after the test bills are back, serves every barcode.
    counts, by_barcode, unlabelled = book(store, run.now)
    shelf = read_shelf(store, run.now)
    sellable, ours = sellable_by_barcode(shelf), book_mrps(shelf)
    soh, theirs = soh_quantities(sync), soh_mrps(sync)
    origins = valuation_origins(store, set(by_barcode.values()))
    rows: list[StockSyncRow] = []
    removals: list[tuple[StockSyncRow, int]] = []
    gains: list[tuple[StockSyncRow, dict[str, Any]]] = []
    for barcode in sorted(set(counts) | set(soh)):
        held, wanted = counts.get(barcode, 0), soh.get(barcode, 0)
        sku_id = by_barcode.get(barcode)
        origin = origins.get(sku_id) if sku_id is not None else None
        category = categorise(held, wanted, known=origin is not None)
        differs, soh_mrp, book_mrp = mrp_check(barcode, theirs, ours)
        if category is None and not differs:
            continue
        row = StockSyncRow(tenant_id=run.tenant_id, sync=sync, barcode=barcode, soh_qty=wanted, book_qty=held,
                           outcome=StockSyncRow.Outcome.FLAGGED, category=category or Category.MRP_DIFFERS,
                           mrp_differs=differs, soh_mrp_paise=soh_mrp, book_mrp_paise=book_mrp)
        rows.append(row)
        if category == Category.SOH_ONLY:
            row.exception_note = SOH_ONLY_NOTE  # flagged only: no item, no stock, nothing to sell
        elif wanted < held:
            removals.append((row, held - wanted))
        elif wanted > held and origin is None:
            # Held here but valued by no origin: there is no cost to bring a found piece in at.
            row.outcome, row.exception_code = StockSyncRow.Outcome.EXCEPTION, "NO_ORIGIN"
            row.exception_note = "No purchase record to value the extra pieces at; they were not added."
        elif wanted > held:
            assert sku_id is not None and origin is not None
            gains.append((row, {"barcode": barcode, "sku_id": str(sku_id), "qty": wanted - held,
                                "origin_id": str(origin.pk), "unit_cost_paise": int(origin.unit_cost or 0)}))
    _remove_all(run, batch, store, removals, sellable)
    for sku in unlabelled:
        rows.append(StockSyncRow(tenant_id=run.tenant_id, sync=sync, barcode=f"sku:{sku}"[:128], soh_qty=0,
                                 book_qty=0, outcome="exception", exception_code="NO_BARCODE",
                                 exception_note="Pieces of an item with no single scannable barcode; the SOH cannot match them."))

    identity, head = new_document(run, kind=DOC_KIND, purpose=DocumentIdentity.Purpose.LEGACY_SYNC,
                                  entity_id=store.gstin.legal_entity_id, site_id=store.pk)
    soh_only = [r for r in rows if r.category == Category.SOH_ONLY]
    summary = {"test_bills_reversed": reversed_["bills"], "test_pieces_restored": reversed_["pieces"],
               "test_pieces_already_back": reversed_["already_back"],
               "book_qty_before": sum(counts.values()), "soh_qty": sum(soh.values()),
               "removed_qty": -sum(r.applied_qty for r in rows if r.applied_qty < 0),
               "added_qty": sum(int(body["qty"]) for _r, body in gains),
               "soh_only_qty": sum(r.soh_qty for r in soh_only), "soh_only_barcodes": len(soh_only),
               "flagged_rows": len(rows), "exceptions": sum(r.outcome == "exception" for r in rows),
               "category_counts": category_counts([(r.category, r.mrp_differs) for r in rows])}
    header = {"kind": PURPOSE, "sync_id": str(sync.pk), "store_code": store.code,
              "cutoff_at": sync.cutoff_at.isoformat(), "source_hash": sync.source_hash}
    lines: list[tuple[uuid.UUID, Mapping[str, Any]]] = [(uuid.uuid5(identity.pk, "summary"), summary)]
    lines += [(uuid.uuid5(identity.pk, f"gain:{body['barcode']}"), body) for _row, body in gains]
    append_revision(run, head, header=header, replace_lines=lines)
    version, official = officialise(run, head, approved_by_id=run.principal.human_id, canonical_header=header,
                                    lines=lines, authority=run.authority)
    if gains:
        floor = _selling_location(store)
        session = AcceptanceSession.objects.create(tenant_id=run.tenant_id, site_id=store.pk, source_version_id=version.pk,
                                                   opened_by_id=run.principal.human_id, last_activity_at=run.now,
                                                   state=AcceptanceSession.State.COMPLETED)
        by_key = {line.stable_line_key: line for line in official}
        for row, body in gains:
            row.applied_qty = _add(run, batch, store, version, session,
                                   by_key[uuid.uuid5(identity.pk, f"gain:{body['barcode']}")], floor)
            row.outcome = StockSyncRow.Outcome.ADDED
    StockSyncRow.objects.bulk_create(rows, batch_size=1000)

    batch_id = None
    if not batch.empty():
        batch = engine.Plan(LEGACY_SYNC_POSTING, version.pk, batch.event_key, quantity=batch.quantity,
                            value=batch.value, encumbrance=batch.encumbrance)
        batch_id = engine.post(run, version, batch)
    sync.state = StockSync.State.APPLIED
    sync.applied_by_id = run.principal.human_id
    sync.applied_at = run.now
    sync.outcome = summary
    sync.document = identity
    sync.journal_batch_id = batch_id
    sync.save()
    if rows:
        _raise_review(run, store, sync, summary)
    run.audit_subject_key = review_subject(sync)
    run.audit_site_id = store.pk
    run.audit_after = {"stock_sync_id": str(sync.pk), **summary}
    return sync


def review_subject(sync: StockSync) -> str:
    return f"stock_sync:{sync.pk}"


def _raise_review(run: CommandRun, store: Store, sync: StockSync, summary: Mapping[str, Any]) -> None:
    """One exception - so one notification - per sync for the store manager, never one per row."""
    from alerts.goods_services import open_exception

    flagged = ", ".join(f"{count} {name}" for name, count in summary["category_counts"].items() if count)
    open_exception(run, kind=EXCEPTION_KIND, site_id=store.pk, subject_key=review_subject(sync),
                   reason_code="SOH_MISMATCH", source_event_key=sync.pk, allowed_resolution_actions=[ACTION],
                   note=f"{summary['flagged_rows']} rows differ from the legacy SOH ({flagged}). Stock was brought "
                        "to the SOH; review and acknowledge each row at Legacy sync.")


# ---------------------------------------------------------------------------
# Review: acknowledging flagged rows marks them seen and changes nothing else
# ---------------------------------------------------------------------------

MAX_ACKNOWLEDGE = 1000


def acknowledge(run: CommandRun, sync: StockSync, row_ids: set[uuid.UUID]) -> dict[str, int]:
    """Mark rows of an applied sync reviewed. Never edits stock, items, or what a row records.

    When the last open row is acknowledged, the sync's review exception closes:
    its cause - differences nobody has looked at - is gone.
    """
    if not row_ids or len(row_ids) > MAX_ACKNOWLEDGE:
        raise Refusal("INVALID_REQUEST", f"Give between 1 and {MAX_ACKNOWLEDGE} row ids to acknowledge.")
    # Store before document: the command kernel's lock order.
    run.lock(LockRank.SITE, Store.objects.filter(tenant_id=run.tenant_id, pk=sync.site_id))
    found = run.lock(LockRank.DOCUMENT, StockSync.objects.filter(tenant_id=run.tenant_id, pk=sync.pk))
    if not found:
        raise Refusal("NOT_FOUND", "That stock sync was not found.")
    locked: StockSync = found[0]
    if locked.state != StockSync.State.APPLIED:
        raise Refusal("STATE_CONFLICT", "Only an applied stock sync has rows to review.", status=409)
    rows = StockSyncRow.objects.filter(tenant_id=run.tenant_id, sync=locked, pk__in=list(row_ids))
    if rows.count() != len(row_ids):
        raise Refusal("NOT_FOUND", "Some of those rows are not in this stock sync.")
    assert run.principal.human_id is not None
    reviewer = HumanIdentity.objects.get(pk=run.principal.human_id)
    changed = rows.filter(review_state=StockSyncRow.ReviewState.OPEN).update(
        review_state=StockSyncRow.ReviewState.ACKNOWLEDGED, reviewed_by=reviewer, reviewed_at=run.now)
    remaining = StockSyncRow.objects.filter(tenant_id=run.tenant_id, sync=locked,
                                            review_state=StockSyncRow.ReviewState.OPEN).count()
    if remaining == 0:
        from alerts.goods_services import resolve_exceptions

        resolve_exceptions(run, kind=EXCEPTION_KIND, subject_key=review_subject(locked),
                           reason_code="ALL_ROWS_REVIEWED", source_event_key=locked.pk)
    run.audit_subject_key = review_subject(locked)
    run.audit_site_id = locked.site_id
    run.audit_after = {"acknowledged": changed, "open_rows": remaining, "row_ids": sorted(str(pk) for pk in row_ids)}
    return {"acknowledged": changed, "open_rows": remaining}


