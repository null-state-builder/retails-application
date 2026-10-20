"""Nightly legacy sales import for a synthetic test tenant (store test run, P22).

The incremental companion of the SOH stock sync (`ptmapper.stock_sync_services`).
When only the day's sales report is to hand, one apply, in one journal batch:

1. **Test bills go back** - the same reversal the SOH sync uses, for bills in
   the report's dates after the last applied SOH. A bill already put back is
   never put back twice.
2. **Legacy sales leave.** Each sold barcode is consumed oldest-first, by the
   same FIFO selection a real bill uses (`goods_sale.plan_sale`).
3. **Legacy returns come back** only where an earlier legacy sale consumed that
   barcode, to the exact place that sale took it from.

A report for days the last applied SOH already covers is refused: that SOH
already holds those sales. Anything that cannot be matched - an unknown
barcode, short stock, a return with no recorded sale - is kept as an exception
row and never blocks the rest of the night. Nothing here creates a bill, a tax
document or a GL posting, and the module refuses at a real store.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any

from django.utils import timezone

from core.commands import CommandRun, LockRank
from core.goods_documents import append_revision, new_document, officialise
from core.kernel_models import DocumentIdentity
from core.operational import LEGACY_SYNC_POSTING, ValuePair
from core.refusals import Refusal
from masters.models import Store
from ptmapper.legacy_sales_models import LegacySalesImport, LegacySalesRow
from ptmapper.legacy_sales_parser import SaleRow
from ptmapper.stock_sync_services import (
    _address,
    _reverse_test_bills,
    _sellable,
    _still_consumed,
    previous_cutoff,
    require_test_store,
    test_bills,
)
from sell.services.goods_sale import GoodsSaleError, plan_sale
from stockledger import goods_engine as engine
from stockledger import ranges

#: Who may upload and apply: the same people who may stage an opening import.
ACTION = "opening.import.stage"
DOC_KIND = "LSI"
PURPOSE = "legacy_sales"
#: Why a piece left: the reason on its consumed quantity leg.
LEGACY_SOLD_REASON = "legacy_sold"


def create_import(run: CommandRun, *, site_id: int, source_hash: str, source_name: str,
                  metadata: dict[str, Any], rows: list[SaleRow]) -> LegacySalesImport:
    stores = run.lock(LockRank.SITE, Store.objects.filter(tenant_id=run.tenant_id, pk=site_id, is_active=True))
    if not stores:
        raise Refusal("NOT_FOUND", "That store was not found.")
    require_test_store(stores[0])
    found = LegacySalesImport.objects.filter(tenant_id=run.tenant_id, site_id=site_id, source_hash=source_hash).first()
    if found is not None:
        run.audit_after = {"legacy_sales_import_id": str(found.pk), "unchanged": True}
        return found
    assert run.principal.human_id is not None
    source = LegacySalesImport.objects.create(
        tenant_id=run.tenant_id, site_id=site_id, source_hash=source_hash, source_name=source_name[:240],
        date_from=date.fromisoformat(metadata["date_from"]), date_to=date.fromisoformat(metadata["date_to"]),
        metadata=metadata, uploaded_by_id=run.principal.human_id)
    LegacySalesRow.objects.bulk_create([
        LegacySalesRow(tenant_id=run.tenant_id, source_import=source, ordinal=row.ordinal,
                       business_date=row.business_date, bill_no=row.bill_no[:60], barcode=row.barcode,
                       quantity=row.quantity, rate_paise=row.rate_paise, net_paise=row.net_paise)
        for row in rows
    ], batch_size=1000)
    run.audit_subject_key = f"legacy_sales:{source.pk}"
    run.audit_site_id = site_id
    run.audit_after = {"legacy_sales_import_id": str(source.pk), "rows": len(rows),
                       "date_from": metadata["date_from"], "date_to": metadata["date_to"]}
    return source


def discard(run: CommandRun, source: LegacySalesImport) -> LegacySalesImport:
    source = _locked(run, source)
    source.state = LegacySalesImport.State.DISCARDED
    source.save(update_fields=["state"])
    run.audit_subject_key = f"legacy_sales:{source.pk}"
    run.audit_after = {"state": source.state}
    return source


def _locked(run: CommandRun, source: LegacySalesImport) -> LegacySalesImport:
    found = run.lock(LockRank.DOCUMENT, LegacySalesImport.objects.filter(tenant_id=run.tenant_id, pk=source.pk))
    if not found:
        raise Refusal("NOT_FOUND", "That sales import was not found.")
    locked: LegacySalesImport = found[0]
    if locked.state != LegacySalesImport.State.PREVIEWED:
        raise Refusal("LEGACY_SALES_DONE", f"This import is already {locked.state}.", status=409)
    return locked


def day_bounds(first: date, last: date) -> tuple[datetime, datetime]:
    zone = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(first, time.min), zone),
            timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min), zone))


def bills_for(store: Store, first: date, last: date) -> list[Any]:
    start, end = day_bounds(first, last)
    after = previous_cutoff(store)
    after = max(after, start - timedelta(microseconds=1)) if after else start - timedelta(microseconds=1)
    return [bill for bill in test_bills(store, after, end) if bill.billed_at < end]


def _consume(run: CommandRun, batch: engine.Plan, store: Store, row: LegacySalesRow) -> None:
    wanted = row.quantity
    try:
        plan = plan_sale(store, [{"line_no": 1, "barcode": row.barcode, "season": "", "qty": wanted, "direction": "sale"}])
    except GoodsSaleError as refusal:
        if refusal.code != "INSUFFICIENT_ELIGIBLE_STOCK":
            row.outcome, row.exception_code, row.exception_note = "exception", refusal.code, str(refusal)[:300]
            return
        wanted = _sellable(store, row.barcode)
        if wanted <= 0:
            row.outcome, row.exception_code = "exception", "SHORT_STOCK"
            row.exception_note = f"No sellable stock of {row.barcode} left to take {row.quantity}."
            return
        plan = plan_sale(store, [{"line_no": 1, "barcode": row.barcode, "season": "", "qty": wanted, "direction": "sale"}])
        row.exception_code = "SHORT_STOCK"
        row.exception_note = f"Only {wanted} of {row.quantity} sellable; the rest was not taken."
    portions: list[dict[str, Any]] = []
    for allocation in plan.sold[1]:
        engine.lock_lots(run, [allocation.lot_id])
        for slice_, old in engine.end_positions(run, batch, allocation.lot_id, allocation.interval,
                                                "consumed", LEGACY_SOLD_REASON):
            portions.append({"lot_id": str(allocation.lot_id), "lower": slice_[0], "upper": slice_[1],
                             "origin_id": str(allocation.origin_id), "unit_cost_paise": allocation.unit_cost_paise,
                             "address": old.as_json(), "restored": False})
        if allocation.cost_paise > 0:
            batch.value.append(ValuePair(origin_id=allocation.origin_id, amount=allocation.cost_paise,
                                         source_bucket="stock", destination_bucket="external",
                                         source_site_id=store.pk, destination_site_id=None,
                                         lot_id=allocation.lot_id, lower=allocation.lower, upper=allocation.upper))
    row.portions = portions
    row.applied_qty = sum(p["upper"] - p["lower"] for p in portions)
    row.outcome = "consumed" if row.applied_qty == row.quantity else "exception"


def _restore(run: CommandRun, batch: engine.Plan, store: Store, row: LegacySalesRow) -> None:
    """A legacy return: undo an earlier legacy sale of the same barcode, newest first."""
    wanted = -row.quantity
    earlier = (LegacySalesRow.objects.filter(tenant_id=run.tenant_id, source_import__site=store,
                                             source_import__state=LegacySalesImport.State.APPLIED,
                                             barcode=row.barcode, quantity__gt=0, applied_qty__gt=0)
               .order_by("-business_date", "-ordinal"))
    portions: list[dict[str, Any]] = []
    for sold in earlier:
        changed = False
        for piece in sold.portions:
            if wanted == 0:
                break
            done = int(piece.get("restored_qty", piece["upper"] - piece["lower"] if piece.get("restored") else 0))
            left = piece["upper"] - piece["lower"] - done
            if left <= 0:
                continue
            lot_id = uuid.UUID(piece["lot_id"])
            start = piece["lower"] + done
            interval: ranges.Interval = (start, start + min(left, wanted))
            if not _still_consumed(lot_id, interval):
                continue
            engine.lock_lots(run, [lot_id])
            address = _address(piece["address"])
            engine.change_address(run, batch, lot_id, interval, lambda _old, a=address: a)
            if piece["unit_cost_paise"] > 0:
                batch.value.append(ValuePair(origin_id=uuid.UUID(piece["origin_id"]),
                                             amount=ranges.length(interval) * piece["unit_cost_paise"],
                                             source_bucket="external", destination_bucket="stock",
                                             source_site_id=None, destination_site_id=store.pk,
                                             lot_id=lot_id, lower=interval[0], upper=interval[1]))
            piece["restored_qty"] = done + ranges.length(interval)
            piece["restored"] = piece["restored_qty"] == piece["upper"] - piece["lower"]
            portions.append({**piece, "lower": interval[0], "upper": interval[1], "from_row_id": str(sold.pk)})
            wanted -= ranges.length(interval)
            changed = True
        if changed:
            sold.save(update_fields=["portions"])
        if wanted == 0:
            break
    row.portions = portions
    row.applied_qty = -sum(p["upper"] - p["lower"] for p in portions)
    if row.applied_qty == row.quantity:
        row.outcome = "restored"
    else:
        row.outcome, row.exception_code = "exception", "RETURN_UNMATCHED"
        row.exception_note = (f"Only {-row.applied_qty} of {-row.quantity} matched an earlier legacy sale here; "
                              "the next SOH upload will settle the rest.")


def apply(run: CommandRun, source: LegacySalesImport) -> LegacySalesImport:
    # Store before document: the command kernel's lock order.
    store = run.lock(LockRank.SITE, Store.objects.filter(tenant_id=run.tenant_id, pk=source.site_id))[0]
    source = _locked(run, source)
    require_test_store(store)
    covered = previous_cutoff(store)
    if covered is not None and source.date_from <= timezone.localtime(covered).date():
        raise Refusal("LEGACY_SALES_COVERED",
                      f"The SOH applied for {timezone.localtime(covered):%d %b %Y %H:%M} already includes sales up to "
                      "that day. Upload only later days' sales reports.", status=409)
    overlap = LegacySalesImport.objects.filter(
        tenant_id=run.tenant_id, site_id=store.pk, state=LegacySalesImport.State.APPLIED,
        date_from__lte=source.date_to, date_to__gte=source.date_from).first()
    if overlap is not None:
        raise Refusal("LEGACY_SALES_OVERLAP",
                      f"Sales for {overlap.date_from}..{overlap.date_to} were already applied at this store "
                      f"from {overlap.source_name}.", status=409)
    assert run.principal.human_id is not None
    identity, head = new_document(run, kind=DOC_KIND, purpose=DocumentIdentity.Purpose.LEGACY_SYNC,
                                  entity_id=store.gstin.legal_entity_id, site_id=store.pk)
    batch = engine.Plan(LEGACY_SYNC_POSTING, uuid.uuid4(), engine.event_key(PURPOSE, source.pk))
    reversed_ = _reverse_test_bills(run, batch, store, bills_for(store, source.date_from, source.date_to))
    rows = list(source.rows.order_by("ordinal"))
    # Sales before returns: a return only takes back a piece an earlier night
    # recorded as a legacy sale, never one this same file is still to consume.
    for row in [r for r in rows if r.quantity > 0]:
        _consume(run, batch, store, row)
    for row in [r for r in rows if r.quantity < 0]:
        _restore(run, batch, store, row)
    LegacySalesRow.objects.bulk_update(rows, ["outcome", "applied_qty", "exception_code", "exception_note", "portions"])
    summary = {
        "test_bills_reversed": reversed_["bills"],
        "test_pieces_restored": reversed_["pieces"],
        "test_pieces_already_back": reversed_["already_back"],
        "legacy_sold_qty": sum(r.applied_qty for r in rows if r.quantity > 0),
        "legacy_returned_qty": -sum(r.applied_qty for r in rows if r.quantity < 0),
        "exceptions": sum(r.outcome == "exception" for r in rows),
    }
    header = {"kind": PURPOSE, "import_id": str(source.pk), "store_code": store.code,
              "date_from": source.date_from.isoformat(), "date_to": source.date_to.isoformat(),
              "source_hash": source.source_hash}
    lines: list[tuple[uuid.UUID, Mapping[str, Any]]] = [(uuid.uuid5(identity.pk, "summary"), summary)]
    append_revision(run, head, header=header, replace_lines=lines)
    version, _official = officialise(run, head, approved_by_id=run.principal.human_id, canonical_header=header,
                                     lines=lines, authority=run.authority)
    batch_id = None
    if not batch.empty():
        batch = engine.Plan(LEGACY_SYNC_POSTING, version.pk, batch.event_key, quantity=batch.quantity,
                            value=batch.value, encumbrance=batch.encumbrance)
        batch_id = engine.post(run, version, batch)
    source.state = LegacySalesImport.State.APPLIED
    source.applied_by_id = run.principal.human_id
    source.applied_at = run.now
    source.outcome = summary
    source.document = identity
    source.journal_batch_id = batch_id
    source.save()
    run.audit_subject_key = f"legacy_sales:{source.pk}"
    run.audit_site_id = store.pk
    run.audit_after = {"legacy_sales_import_id": str(source.pk), **summary}
    return source
