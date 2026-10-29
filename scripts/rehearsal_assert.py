"""Check a rehearsed store opening and SOH sync; print a PASS/FAIL table, exit 1 on any FAIL.

Runs inside the API container (scripts/rehearse_store_opening.sh mounts it), with no
management command:

    python /rehearsal/rehearsal_assert.py --phase opening --store WRR --pt PT.xlsx
    python /rehearsal/rehearsal_assert.py --phase sync --store WRR --pt PT.xlsx --soh SOH.xlsx

``opening`` checks the store right after its PT opening; ``sync`` re-checks the opening
evidence and then the applied SOH sync. The expected categories come from reading the
two workbooks directly with openpyxl, not from the product's parsers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Any

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


# -- the files, read independently ------------------------------------------------


def _barcode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _sheet(path: str) -> list[dict[str, Any]]:
    import openpyxl

    sheet = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = [str(cell).strip().lower() if cell is not None else "" for cell in next(rows)]
    return [dict(zip(header, row)) for row in rows]


def _paise(value: Any) -> int:
    return round(float(value) * 100)


def read_pt(path: str) -> tuple[dict[str, int], dict[str, set[int]]]:
    qty: dict[str, int] = defaultdict(int)
    mrp: dict[str, set[int]] = defaultdict(set)
    for row in _sheet(path):
        barcode = _barcode(row.get("barcode"))
        if not barcode:
            continue
        qty[barcode] += int(row.get("qty") or 0)
        if row.get("mrp"):
            mrp[barcode].add(_paise(row["mrp"]))
    return dict(qty), dict(mrp)


def read_soh(path: str) -> tuple[dict[str, int], dict[str, set[int]]]:
    qty: dict[str, int] = defaultdict(int)
    mrp: dict[str, set[int]] = defaultdict(set)
    for row in _sheet(path):
        barcode = _barcode(row.get("barcode"))
        if not barcode:  # the footer total
            continue
        qty[barcode] += max(int(row.get("tqty") or 0), 0)
        if row.get("mrp"):
            mrp[barcode].add(_paise(row["mrp"]))
    return dict(qty), dict(mrp)


def expected_categories(pt: tuple[dict[str, int], dict[str, set[int]]],
                        soh: tuple[dict[str, int], dict[str, set[int]]]) -> dict[str, list[int]]:
    """{category: [barcodes, pieces]} for the PT book against the SOH, as the sync defines them."""
    held_by, held_mrp = pt
    wanted_by, soh_mrp = soh
    out: dict[str, list[int]] = {name: [0, 0] for name in
                                 ("SOH_ONLY", "PT_ONLY", "SOH_ABOVE", "SOH_BELOW", "MRP_DIFFERS")}
    for barcode in set(held_by) | set(wanted_by):
        held, wanted = held_by.get(barcode, 0), wanted_by.get(barcode, 0)
        if held != wanted:
            if held > 0 and wanted == 0:
                out["PT_ONLY"][0] += 1
                out["PT_ONLY"][1] += held
            elif wanted < held:
                out["SOH_BELOW"][0] += 1
                out["SOH_BELOW"][1] += held - wanted
            elif held > 0:
                out["SOH_ABOVE"][0] += 1
                out["SOH_ABOVE"][1] += wanted - held
            else:
                out["SOH_ONLY"][0] += 1
                out["SOH_ONLY"][1] += wanted
        theirs, ours = soh_mrp.get(barcode) or set(), held_mrp.get(barcode) or set()
        if held > 0 and theirs and ours and theirs - ours:
            out["MRP_DIFFERS"][0] += 1
    return out


# -- the database ------------------------------------------------------------------


def setup_django() -> None:
    sys.path.insert(0, "/app")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def floor_units(store: Any) -> int:
    from core.goods_fields import bounds
    from stockledger import ranges
    from stockledger.goods_models import Position

    return sum(ranges.length(bounds(span)) for span in Position.objects.filter(
        tenant_id=store.tenant_id, site_id=store.pk, boundary="physical").values_list("portion", flat=True))


def pt_source(store: Any) -> Any:
    from ptmapper.soh_models import SohImport

    sources = [s for s in SohImport.objects.filter(tenant_id=store.tenant_id, site=store).exclude(state="withdrawn")
               if (s.source_metadata or {}).get("pt_opening")]
    check("one PT source at the store", len(sources) == 1, f"{len(sources)} sources")
    return sources[-1] if sources else None


def opening_checks(store: Any, source: Any, *, floor: bool) -> None:
    from alerts.goods_models import GoodsException
    from approvals.goods_models import ApprovalDecision, ApprovalRequest
    from ptmapper import pt_opening_services as opening
    from ptmapper import soh_services
    from ptmapper.goods_models import OpeningManifest

    state = opening.status(source)
    check("quick_load state is done", state.get("state") == "done",
          f"state={state.get('state')} error={state.get('error') or '-'}")
    recon = soh_services.opening_reconciliation(store)
    check("opening_reconciliation passed", recon["passed"] is True,
          f"quantity={recon['quantity']} reasons={recon['reasons'][:3]}")
    included = source.rows.filter(quantity__gt=0, exclusion_reason="")
    included_qty = sum(included.values_list("quantity", flat=True))
    preview_qty = int((source.source_metadata or {}).get("quantity") or 0)
    check("PT preview total = included rows", preview_qty == included_qty,
          f"preview={preview_qty} included={included_qty} excluded_rows="
          f"{source.rows.filter(quantity__gt=0).exclude(exclusion_reason='').count()}")
    check("reconciled quantity = PT preview total", recon["quantity"] == preview_qty,
          f"reconciled={recon['quantity']} preview={preview_qty}")
    if floor:
        on_floor = floor_units(store)
        check("floor pieces after opening = PT preview total", on_floor == preview_qty,
              f"floor={on_floor} preview={preview_qty}")
    unavailable = GoodsException.objects.filter(tenant_id=store.tenant_id, site=store,
                                                kind="opening_origin_unavailable").count()
    check("zero opening_origin_unavailable exceptions", unavailable == 0, f"{unavailable} found")
    batches = (included.count() + soh_services.MAX_BATCH - 1) // soh_services.MAX_BATCH
    manifests = list(OpeningManifest.objects.filter(tenant_id=store.tenant_id, site=store)
                     .values_list("batch_key", flat=True))
    check("exactly one manifest per batch key",
          len(manifests) == len(set(manifests)) == batches == source.batches.count(),
          f"manifests={len(manifests)} distinct keys={len(set(manifests))} batches expected={batches} "
          f"source batches={source.batches.count()}")
    batch_ids = [str(b.manifest_id) for b in source.batches.all()]
    requests = ApprovalRequest.objects.filter(tenant_id=store.tenant_id, site=store) | ApprovalRequest.objects.filter(
        tenant_id=store.tenant_id, subject_key__in=[str(source.pk), *batch_ids])
    decisions = list(ApprovalDecision.objects.filter(request__in=requests.distinct())
                     .values_list("outcome", "reason_code"))
    odd = [(o, r) for o, r in decisions if r != "NO_APPROVAL_REQUIRED"]
    pending = requests.distinct().filter(state="pending").count()
    check("all store approval decisions are NO_APPROVAL_REQUIRED", bool(decisions) and not odd and not pending,
          f"decisions={len(decisions)} other={odd[:3]} pending requests={pending}")


def sync_checks(store: Any, pt_path: str, soh_path: str) -> None:
    from alerts.goods_models import GoodsException
    from ptmapper import stock_sync_services as services
    from ptmapper.stock_sync_models import StockSync, StockSyncRow
    from sell.services.goods_stock import read_shelf
    from django.utils import timezone

    syncs = list(StockSync.objects.filter(tenant_id=store.tenant_id, site=store).order_by("created_at"))
    applied = [s for s in syncs if s.state == StockSync.State.APPLIED]
    check("exactly one applied stock sync", len(applied) == 1,
          f"syncs={[(str(s.pk)[:8], s.state) for s in syncs]}")
    if not applied:
        return
    sync = applied[0]
    soh = services.soh_quantities(sync)
    rows = list(StockSyncRow.objects.filter(sync=sync))
    soh_only = {r.barcode for r in rows if r.category == "SOH_ONLY"}
    sellable = services.sellable_by_barcode(read_shelf(store, timezone.now()))
    wrong = [(b, sellable.get(b, 0), soh.get(b, 0)) for b in sorted((set(soh) | set(sellable)) - soh_only)
             if sellable.get(b, 0) != soh.get(b, 0)]
    check("sellable = SOH qty for every known barcode", not wrong,
          f"{len(wrong)} differ {wrong[:3]}; SOH-only barcodes {len(soh_only)}")
    stocked_soh_only = [b for b in soh_only if sellable.get(b, 0)]
    check("SOH-only barcodes are not sellable", not stocked_soh_only, f"{len(stocked_soh_only)} sellable")
    known_total = sum(q for b, q in soh.items() if b not in soh_only)
    check("floor pieces = SOH total for known items", floor_units(store) == known_total,
          f"floor={floor_units(store)} soh_known={known_total}")

    expected = expected_categories(read_pt(pt_path), read_soh(soh_path))
    outcome = (sync.outcome or {}).get("category_counts") or {}
    by_rows: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        if r.category and r.category != "MRP_DIFFERS":
            by_rows[r.category][0] += 1
            by_rows[r.category][1] += abs(r.soh_qty - r.book_qty)
        if r.mrp_differs:
            by_rows["MRP_DIFFERS"][0] += 1
    for name, (barcodes, pieces) in expected.items():
        got_b, got_p = by_rows[name]
        same = got_b == barcodes and outcome.get(name, 0) == barcodes and (name == "MRP_DIFFERS" or got_p == pieces)
        check(f"category {name} matches file diff", same,
              f"expected {barcodes} barcodes/{pieces} pcs; rows {got_b}/{got_p}; outcome {outcome.get(name, 0)}")

    reviews = GoodsException.objects.filter(tenant_id=store.tenant_id, site=store, kind=services.EXCEPTION_KIND)
    for_sync = reviews.filter(subject_key=services.review_subject(sync)).count()
    check("exactly one stock_sync_review exception for the sync", for_sync == 1 and reviews.count() == 1,
          f"for this sync={for_sync} at store={reviews.count()}")
    print("CATEGORIES " + json.dumps({name: {"expected": expected[name], "actual_rows": by_rows[name],
                                              "actual_outcome": outcome.get(name, 0)} for name in expected}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["opening", "sync", "expected"], required=True)
    parser.add_argument("--store", default="WRR")
    parser.add_argument("--pt", required=True)
    parser.add_argument("--soh")
    args = parser.parse_args()
    if args.phase == "expected":
        print(json.dumps(expected_categories(read_pt(args.pt), read_soh(args.soh))))
        return 0
    setup_django()
    from core.tenancy import tenant_context
    from masters.goods_models import Tenant
    from masters.models import Store

    tenant = Tenant.objects.get(synthetic=True)
    with tenant_context(tenant.pk):
        store = Store.objects.get(tenant_id=tenant.pk, code=args.store)
        source = pt_source(store)
        if source is not None:
            opening_checks(store, source, floor=args.phase == "opening")
        if args.phase == "sync":
            if not args.soh:
                parser.error("--soh is needed for --phase sync")
            sync_checks(store, args.pt, args.soh)
    width = max(len(name) for name, _ok, _detail in RESULTS)
    print(f"\n{'CHECK'.ljust(width)}  RESULT  DETAIL")
    for name, ok, detail in RESULTS:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL'}    {detail}")
    failed = sum(not ok for _name, ok, _detail in RESULTS)
    print(f"\n{len(RESULTS) - failed} PASS, {failed} FAIL ({args.phase})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
