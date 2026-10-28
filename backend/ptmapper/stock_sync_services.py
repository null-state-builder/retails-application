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
4. **Barcode never held here:** not invented. Those rows are staged together as
   a reviewed top-up opening import (`ptmapper.soh_services`), which brings in
   identity, HSN and cost through the normal approval path.

Anything that cannot be applied - held or reserved stock, an SKU with no
scannable barcode - is kept as an exception row and never blocks the night.
Nothing here creates a bill, a tax document or a GL posting, and the whole
module refuses at a real (non-synthetic) store.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from core.commands import CommandRun, LockRank
from core.documents import DocStatus
from core.goods_documents import append_revision, new_document, officialise
from core.goods_fields import bounds, portion
from core.kernel_models import DocumentIdentity, OfficialLine
from core.operational import LEGACY_SYNC_POSTING, ValuePair
from core.refusals import Refusal
from files.goods_models import EvidenceObject
from masters.goods_models import Location
from masters.models import Store
from masters.store_features import is_real_store
from ptmapper.soh_models import SohImport, SohImportRow
from ptmapper.soh_parser import SourceRow
from ptmapper.stock_sync_models import StockSync, StockSyncRow
from sell.services.goods_sale import SOLD_REASON, GoodsSaleError, plan_sale
from sell.services.goods_stock import barcode_aliases, is_goods_site, read_shelf
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
    found = StockSync.objects.filter(tenant_id=run.tenant_id, site_id=site_id, source_hash=evidence.sha256).first()
    if found is not None:
        run.audit_after = {"stock_sync_id": str(found.pk), "unchanged": True}
        return found
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


def preview(sync: StockSync) -> dict[str, Any]:
    """What an apply would do now, read-only (before test bills are put back)."""
    store = sync.site
    counts, by_barcode, unlabelled = book(store, sync.cutoff_at)
    soh = soh_quantities(sync)
    lower = higher = new = 0
    for barcode in set(counts) | set(soh):
        delta = soh.get(barcode, 0) - counts.get(barcode, 0)
        if delta < 0:
            lower += -delta
        elif delta > 0 and barcode in by_barcode:
            higher += delta
        elif delta > 0:
            new += delta
    return {"test_bills_to_reverse": len(test_bills(store, previous_cutoff(store), sync.cutoff_at)),
            "book_qty": sum(counts.values()), "soh_qty": sum(soh.values()),
            "remove_qty": lower, "add_qty": higher, "new_item_qty": new, "unlabelled_skus": len(unlabelled)}


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
    return sum(qty for (code, _season), qty in read_shelf(store, barcodes={barcode}).quantities.items() if code == barcode)


def _remove(run: CommandRun, batch: engine.Plan, store: Store, row: StockSyncRow, qty: int) -> None:
    take = min(qty, _sellable(store, row.barcode))
    if take > 0:
        try:
            plan = plan_sale(store, [{"line_no": 1, "barcode": row.barcode, "season": "", "qty": take, "direction": "sale"}])
        except GoodsSaleError as refusal:
            row.outcome, row.exception_code, row.exception_note = "exception", refusal.code, str(refusal)[:300]
            return
        for allocation in plan.sold[1]:
            engine.lock_lots(run, [allocation.lot_id])
            engine.end_positions(run, batch, allocation.lot_id, allocation.interval, "consumed", SYNC_REMOVED_REASON)
            if allocation.cost_paise > 0:
                batch.value.append(ValuePair(origin_id=allocation.origin_id, amount=allocation.cost_paise,
                                             source_bucket="stock", destination_bucket="external",
                                             source_site_id=store.pk, destination_site_id=None,
                                             lot_id=allocation.lot_id, lower=allocation.lower, upper=allocation.upper))
    row.applied_qty = -take
    if take == qty:
        row.outcome = "removed"
    else:
        row.outcome, row.exception_code = "exception", "STOCK_NOT_FREE"
        row.exception_note = (f"{qty - take} of {qty} to remove are not on a selling shelf "
                              "(receiving, quarantine, held or reserved); settle them where they stand.")


def _value_origin(store: Store, sku_id: uuid.UUID) -> Origin | None:
    """The cost a new-found piece is valued at: this SKU's latest origin, this store's first."""
    origins = Origin.objects.filter(tenant_id=store.tenant_id, sku_id=sku_id).order_by("-source_time")
    origin: Origin | None = origins.filter(site_id=store.pk).first() or origins.first()
    return origin


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


def _top_up(run: CommandRun, sync: StockSync, barcodes: set[str]) -> SohImport | None:
    """Stage the barcodes this store has never held as a reviewed opening import."""
    rows = [row for row in sync.metadata.get("rows", []) if row["barcode"] in barcodes and int(row["qty"]) > 0]
    if not rows:
        return None
    assert run.principal.human_id is not None
    digest = hashlib.sha256(f"{sync.source_hash}:top-up".encode()).hexdigest()
    quantity = sum(int(row["qty"]) for row in rows)
    metadata = {**{k: v for k, v in sync.metadata.items() if k in {"sheet", "header_row", "columns", "preamble"}},
                "row_count": len(rows), "stocked_rows": len(rows), "quantity": quantity, "zero_stock_rows": 0,
                "negative_rows": 0, "rate_meaning": "unconfirmed",
                "stock_mrp_paise": str(sum((row["mrp"] or 0) * int(row["qty"]) for row in rows)),
                "stock_sync_top_up": str(sync.pk)}
    source = SohImport.objects.create(tenant_id=run.tenant_id, site_id=sync.site_id, source_evidence=sync.source_evidence,
                                      source_hash=digest, source_name=f"New items from {sync.source_name}"[:240],
                                      source_metadata=metadata, uploaded_by_id=run.principal.human_id)
    SohImportRow.objects.bulk_create([
        SohImportRow(tenant_id=run.tenant_id, source_import=source, ordinal=row["ordinal"],
                     source_row_key=f"{metadata.get('sheet', 'SOH')}:{row['ordinal']}", barcode=row["barcode"],
                     source_brand=row["brand"] or "", quantity=int(row["qty"]), mrp_paise=row["mrp"],
                     source_rate_paise=row["rate"], source=row["source"])
        for row in rows
    ], batch_size=1000)
    return source


def apply(run: CommandRun, sync: StockSync) -> StockSync:
    # Store before document: the command kernel's lock order.
    store = run.lock(LockRank.SITE, Store.objects.filter(tenant_id=run.tenant_id, pk=sync.site_id))[0]
    sync = _locked(run, sync)
    require_test_store(store)
    after = previous_cutoff(store)
    if after is not None and sync.cutoff_at <= after:
        raise Refusal("STOCK_SYNC_STALE",
                      f"A newer SOH (exported {after.isoformat()}) is already applied at this store.", status=409)
    assert run.principal.human_id is not None
    batch = engine.Plan(LEGACY_SYNC_POSTING, uuid.uuid4(), engine.event_key(PURPOSE, sync.pk))
    reversed_ = _reverse_test_bills(run, batch, store, test_bills(store, after, sync.cutoff_at))

    counts, by_barcode, unlabelled = book(store, run.now)
    soh = soh_quantities(sync)
    rows: list[StockSyncRow] = []
    gains: list[tuple[StockSyncRow, dict[str, Any]]] = []
    new_items: set[str] = set()
    for barcode in sorted(set(counts) | set(soh)):
        held, wanted = counts.get(barcode, 0), soh.get(barcode, 0)
        if held == wanted:
            continue
        row = StockSyncRow(tenant_id=run.tenant_id, sync=sync, barcode=barcode, soh_qty=wanted, book_qty=held,
                           outcome="exception")
        rows.append(row)
        if wanted < held:
            _remove(run, batch, store, row, held - wanted)
            continue
        sku_id = by_barcode.get(barcode)
        origin = _value_origin(store, sku_id) if sku_id is not None else None
        if origin is None:
            row.outcome = "new_item"
            row.exception_note = "Never held here: staged in the new-items opening import for review."
            new_items.add(barcode)
            continue
        gains.append((row, {"barcode": barcode, "sku_id": str(sku_id), "qty": wanted - held,
                            "origin_id": str(origin.pk), "unit_cost_paise": int(origin.unit_cost or 0)}))
    for sku in unlabelled:
        rows.append(StockSyncRow(tenant_id=run.tenant_id, sync=sync, barcode=f"sku:{sku}"[:128], soh_qty=0,
                                 book_qty=0, outcome="exception", exception_code="NO_BARCODE",
                                 exception_note="Pieces of an item with no single scannable barcode; the SOH cannot match them."))

    identity, head = new_document(run, kind=DOC_KIND, purpose=DocumentIdentity.Purpose.LEGACY_SYNC,
                                  entity_id=store.gstin.legal_entity_id, site_id=store.pk)
    summary = {"test_bills_reversed": reversed_["bills"], "test_pieces_restored": reversed_["pieces"],
               "test_pieces_already_back": reversed_["already_back"],
               "removed_qty": -sum(r.applied_qty for r in rows if r.applied_qty < 0),
               "added_qty": sum(int(body["qty"]) for _r, body in gains),
               "new_item_qty": sum(soh[b] for b in new_items), "new_item_barcodes": len(new_items),
               "exceptions": sum(r.outcome == "exception" for r in rows)}
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
            row.outcome = "added"
    StockSyncRow.objects.bulk_create(rows, batch_size=1000)

    batch_id = None
    if not batch.empty():
        batch = engine.Plan(LEGACY_SYNC_POSTING, version.pk, batch.event_key, quantity=batch.quantity,
                            value=batch.value, encumbrance=batch.encumbrance)
        batch_id = engine.post(run, version, batch)
    sync.top_up_import = _top_up(run, sync, new_items)
    sync.state = StockSync.State.APPLIED
    sync.applied_by_id = run.principal.human_id
    sync.applied_at = run.now
    sync.outcome = summary
    sync.document = identity
    sync.journal_batch_id = batch_id
    sync.save()
    run.audit_subject_key = f"stock_sync:{sync.pk}"
    run.audit_site_id = store.pk
    run.audit_after = {"stock_sync_id": str(sync.pk), **summary}
    return sync
