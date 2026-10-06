"""Read-only stock and money reconciliation for the owned receiving browser sibling.

This proves the exercised SOH/receipt/damage slice. The incoming damaged piece
has no approved value; it remains an explicit quarantined exclusion. It does
not claim full-product REC-01/02 or an operational day close.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def helper() -> Any:
    spec = importlib.util.spec_from_file_location("receiving_reconciliation_fixture", Path(__file__).with_name("first-store-receiving-fixture.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("The owned receiving verifier is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    fixture = helper()
    _proof, claim, record = fixture.initialise()
    from core.gl import GLEntry
    from core.tenancy import tenant_context
    from django.db.models import Count, Sum
    from inbound.goods_models import Disposition, GoodsGrn
    from inbound.goods_services import grn_comparison
    from outbound.goods_models import DamageReport
    from outbound.goods_soh_models import SohReconciliation
    from sell.cash_models import CashCount, CashCountBill
    from sell.models import Sale
    from stockledger.goods_models import QuantityLeg, ValueLeg

    with tenant_context(claim.tenant_id):
        evidence = fixture.ROOT / ".local/first-store-receiving-evidence.jsonl"
        baselines = [row for line in evidence.read_text().splitlines()
                     if (row := json.loads(line)).get("label") == "SOH baseline after intervening first sale"
                     and row["rehearsal_identity"]["database"] == record["receiving_rehearsal"]["database"]]
        assert baselines, "Retain the browser's exact before evidence."
        before = baselines[0]
        after = fixture.snapshot(claim, record)
        assert after["sale_hash"] == before["sale_hash"] and after["sales"] == before["sales"]
        assert after["tenders"] == before["tenders"] and after["cash_hash"] == before["cash_hash"]
        assert after["freeze_id"] is None
        original_origins = {row["id"]: row for row in before["origins"]}
        assert {row["id"]: row for row in after["origins"] if row["id"] in original_origins} == original_origins
        receipt_origins = [row for row in after["origins"] if row["id"] not in original_origins]
        assert len(receipt_origins) == 1 and receipt_origins[0]["opening_qty"] == 2
        assert int(receipt_origins[0]["unit_cost"]) == 50_000 and int(receipt_origins[0]["mrp"]) == 100_000
        physical = [row for row in after["positions"] if row["boundary"] == "physical" and row["origin_id"]]
        assert sum(row["qty"] for row in physical) == 2
        assert after["sellable_qty"] == 1 and after["stock_cost_paise"] == "100000"
        unvalued_damage = [row for row in after["positions"] if not row["origin_id"] and row["condition"] == "damaged"]
        assert sum(row["qty"] for row in unvalued_damage) == 1
        counts = list(SohReconciliation.objects.filter(site_id=claim.first_store_id))
        closed = [row for row in counts if row.state == "closed"]
        cancelled = [row for row in counts if row.state == "cancelled"]
        assert len(closed) == 2 and len(cancelled) == 3 and len(counts) == 5
        assert all(row.journal_batch_id for row in closed)
        assert all(row.journal_batch_id is None for row in cancelled)
        old_journals = {row["id"] for row in before["journals"]}
        delta_journals = [row for row in after["journals"] if row["id"] not in old_journals and row["posting_kind"] == "P13"]
        assert len(delta_journals) == 2
        reductions = ValueLeg.objects.filter(batch_id__in=[row.journal_batch_id for row in closed], bucket="stock").aggregate(total=Sum("amount"))["total"]
        assert reductions == -100_000
        grn, = GoodsGrn.objects.filter(document__site_id=claim.first_store_id)
        comparison, = grn_comparison(grn)
        assert {key: comparison[key] for key in ("claimed_qty", "counted_qty", "difference", "remaining_shortage_qty")} == {
            "claimed_qty": 4, "counted_qty": 3, "difference": -1, "remaining_shortage_qty": 0}
        shortage, = Disposition.objects.filter(document=grn.document, kind="accept_shortage")
        assert shortage.qty == 1 and shortage.journal_batch_id is None
        pending, = DamageReport.objects.filter(site_id=claim.first_store_id, state="pending")
        confirmed, = DamageReport.objects.filter(site_id=claim.first_store_id, state="confirmed")
        assert pending.quantity == confirmed.quantity == 1
        assert pending.disposition_id is not None and pending.reviewer_id is None
        assert confirmed.movement_id is not None and confirmed.reporter_id != confirmed.reviewer_id
        for model, amount in ((QuantityLeg, "qty"), (ValueLeg, "amount")):
            pairs = model.objects.values("batch_id", "pair_key").annotate(total=Sum(amount), sides=Count("side"))
            assert all(row["total"] == 0 and row["sides"] == 2 for row in pairs)
        bill, = Sale.objects.filter(store_id=claim.first_store_id)
        assert bill.net_paise == 90_000
        assert GLEntry.objects.filter(doc_number=bill.doc_number).aggregate(total=Sum("amount"))["total"] == 0
        saved, = CashCount.objects.filter(store_id=claim.first_store_id)
        assert saved.counted_paise == saved.expected_paise == 90_000 and saved.variance_paise == 0
        assert CashCountBill.objects.filter(count=saved).count() == 1
        assert fixture.snapshot(claim, record) == after
        print(json.dumps({"synthetic_only": True, "database": record["receiving_rehearsal"]["database"],
                          "opening_accepted_qty": 3, "original_sale_qty": 1, "reviewed_soh_reduction_qty": 2,
                          "receipt_accepted_good_qty": 2, "ending_valued_physical_qty": 2, "ending_sellable_qty": 1,
                          "valued_quarantine_qty": 1, "incoming_unvalued_quarantine_qty": 1,
                          "stock_cost_paise": 100_000, "soh_value_removed_paise": 100_000,
                          "original_bill_tenders_and_cash_unchanged": True, "quantity_and_value_pairs_balanced": True,
                          "unexplained_exercised_slice_difference": 0, "full_REC01_REC02_verified": False,
                          "new_day_close_verified": False}, sort_keys=True))


if __name__ == "__main__":
    main()
