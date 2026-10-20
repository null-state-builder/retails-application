"""Store test run: the nightly legacy sync (P22) on a synthetic tenant.

Both options - the SOH replace and the sales-report import - built on the real
online-sale proof: three accepted pieces of one barcode, a paired till, and a
test bill issued through the actual finalise endpoint.
"""
from __future__ import annotations

import hashlib
import io
import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from django.utils import timezone
from openpyxl import Workbook

from core.refusals import Refusal
from files.goods_services import stage_upload
from ptmapper import legacy_sales_services as sales
from ptmapper import stock_sync_services as sync_services
from ptmapper.legacy_sales_models import LegacySalesImport, LegacySalesRow
from ptmapper.legacy_sales_parser import parse_sales
from ptmapper.soh_parser import parse_soh
from ptmapper.soh_services import requires_inventory_reconciliation
from ptmapper.stock_sync_models import StockSync
from sell.models import Sale
from sell.services.goods_stock import read_shelf
from sell.views import OnlineFinaliseView
from stockledger.goods_models import JournalBatch, QuantityLeg
from tests.first_store_goods import command
from tests.test_first_store_online import bill, online_goods, post  # noqa: F401
from tests.test_so03_denials import worlds as worlds  # noqa: F401

SALES_HEADER = ["", "Bill Date", "Bill No", "Customer", "Phone", "Item", "Brand", "Barcode", "SalesMan", "Qty", "Rate", "Net Amount"]
SOH_HEADER = ["Item Name", "Brand", "Barcode", "Tqty", "Mrp", "Rate", "Amount"]


def workbook(header: list[Any], rows: list[list[Any]]) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def line(day: date | None, bill_no: str | None, barcode: str, qty: int, rate: int = 1000) -> list[Any]:
    return [None, day, bill_no, "A CUSTOMER" if bill_no else None, 9999999999 if bill_no else None,
            "SHIRT", "BRAND", barcode, "SALESPERSON", qty, rate, rate * qty]


def shelf(proof: Any) -> int:
    return sum(read_shelf(proof.world.sites[0]).quantities.values())


def issue_test_bill(proof: Any) -> None:
    wire, _ = bill(proof)
    assert post(OnlineFinaliseView, proof, wire).status_code == 201


# -- SOH replace -------------------------------------------------------------


def soh(proof: Any, stock: dict[str, int], *, cutoff: Any = None) -> StockSync:
    data = workbook(SOH_HEADER, [["SHIRT", "BRAND", code, qty, 1000, 500, 500 * qty] for code, qty in stock.items()])
    metadata, parsed = parse_soh(data)
    site = proof.world.sites[0]
    evidence = stage_upload(proof.manager.principal(), command_id=uuid.uuid4(), data=data, filename="SOH.xlsx",
                            kind="manifest", scope={"scope_kind": "sites", "site_ids": [site.pk], "brand_ids": [],
                                                    "sensitive_fields": ["cost", "financial"]},
                            expected_sha256=hashlib.sha256(data).hexdigest(), contains_fields=["cost", "financial"])
    return command(proof.manager, sync_services.ACTION, lambda run: sync_services.create_sync(
        run, site_id=site.pk, evidence=evidence, cutoff_at=cutoff or timezone.now(), metadata=metadata, parsed=parsed))


def apply_soh(proof: Any, sync: StockSync) -> StockSync:
    return command(proof.manager, sync_services.ACTION, lambda run: sync_services.apply(run, sync))


def test_soh_puts_test_bill_back_then_matches_lower_soh(online_goods: Any) -> None:
    proof = online_goods
    issue_test_bill(proof)
    assert shelf(proof) == 2
    sync = soh(proof, {proof.barcode: 1})
    applied = apply_soh(proof, sync)
    assert shelf(proof) == 1
    assert applied.outcome["test_bills_reversed"] == 1 and applied.outcome["removed_qty"] == 2
    journal = JournalBatch.objects.get(pk=applied.journal_batch_id)
    assert journal.posting_kind == "P22"
    reasons = {leg.position.get("reason") for leg in QuantityLeg.objects.filter(batch=journal, side="destination")}
    assert "legacy_soh_lower" in reasons
    assert Sale.objects.count() == 1


def test_soh_adds_sellable_pieces_of_a_known_item(online_goods: Any) -> None:
    proof = online_goods
    applied = apply_soh(proof, soh(proof, {proof.barcode: 5}))
    assert applied.outcome["added_qty"] == 2
    assert shelf(proof) == 5
    row = applied.rows.get()
    assert (row.book_qty, row.soh_qty, row.applied_qty, row.outcome) == (3, 5, 2, "added")


def test_soh_missing_barcode_goes_to_zero_and_new_item_is_staged_for_review(online_goods: Any) -> None:
    proof = online_goods
    applied = apply_soh(proof, soh(proof, {"NEW-ITEM-1": 4}))
    assert shelf(proof) == 0
    outcomes = {row.barcode: row.outcome for row in applied.rows.all()}
    assert outcomes == {proof.barcode: "removed", "NEW-ITEM-1": "new_item"}
    top_up = applied.top_up_import
    assert top_up is not None and list(top_up.rows.values_list("barcode", "quantity")) == [("NEW-ITEM-1", 4)]
    assert not requires_inventory_reconciliation(top_up)


def test_soh_older_than_the_last_applied_is_refused(online_goods: Any) -> None:
    proof = online_goods
    apply_soh(proof, soh(proof, {proof.barcode: 3}))
    stale = soh(proof, {proof.barcode: 2}, cutoff=timezone.now() - timedelta(hours=1))
    with pytest.raises(Refusal) as refused:
        apply_soh(proof, stale)
    assert refused.value.code == "STOCK_SYNC_STALE"
    assert shelf(proof) == 3


# -- Sales report ------------------------------------------------------------


def upload_sales(proof: Any, rows: list[list[Any]], *, name: str = "sale.xlsx") -> LegacySalesImport:
    data = workbook(SALES_HEADER, rows)
    metadata, parsed = parse_sales(data)
    return command(proof.manager, sales.ACTION, lambda run: sales.create_import(
        run, site_id=proof.world.sites[0].pk, source_hash=hashlib.sha256(data).hexdigest(), source_name=name,
        metadata=metadata, rows=parsed))


def apply_sales(proof: Any, source: LegacySalesImport) -> LegacySalesImport:
    return command(proof.manager, sales.ACTION, lambda run: sales.apply(run, source))


def test_parser_inherits_bills_drops_personal_data_and_keeps_returns() -> None:
    day = date(2026, 6, 12)
    metadata, rows = parse_sales(workbook(SALES_HEADER, [
        line(day, "26-27/SGMR/1", "111", 1),
        line(None, None, "222", 2),
        line(day, "26-27/SGMR/2", "111", -1),
        [None, None, None, None, None, None, None, None, None, 2, None, 2000],  # totals footer
    ]))
    assert [(r.bill_no, r.barcode, r.quantity) for r in rows] == [
        ("26-27/SGMR/1", "111", 1), ("26-27/SGMR/1", "222", 2), ("26-27/SGMR/2", "111", -1)]
    assert metadata["sold_quantity"] == 3 and metadata["returned_quantity"] == 1
    assert not set(rows[0].__dataclass_fields__) & {"customer", "phone", "salesman"}
    assert "9999999999" not in repr(rows) and "A CUSTOMER" not in repr(metadata)


def test_sales_report_reverses_test_bill_then_consumes(online_goods: Any) -> None:
    proof = online_goods
    issue_test_bill(proof)
    today = timezone.localdate()
    applied = apply_sales(proof, upload_sales(proof, [line(today, "L/1", proof.barcode, 2),
                                                      line(today, "L/2", "NOT-A-BARCODE", 1)]))
    assert shelf(proof) == 1
    assert applied.outcome["test_bills_reversed"] == 1 and applied.outcome["legacy_sold_qty"] == 2
    missing = LegacySalesRow.objects.get(source_import=applied, barcode="NOT-A-BARCODE")
    assert missing.outcome == "exception" and missing.exception_code == "LINE_UNRESOLVED"


def test_sales_short_stock_and_overlap(online_goods: Any) -> None:
    proof = online_goods
    today = timezone.localdate()
    applied = apply_sales(proof, upload_sales(proof, [line(today, "L/1", proof.barcode, 5)]))
    row = applied.rows.get()
    assert row.applied_qty == 3 and row.exception_code == "SHORT_STOCK"
    with pytest.raises(Refusal) as again:
        apply_sales(proof, upload_sales(proof, [line(today, "L/9", proof.barcode, 1)], name="again.xlsx"))
    assert again.value.code == "LEGACY_SALES_OVERLAP"


def test_sales_return_restores_only_an_earlier_legacy_sale(online_goods: Any) -> None:
    proof = online_goods
    today = timezone.localdate()
    apply_sales(proof, upload_sales(proof, [line(today, "L/1", proof.barcode, 2)]))
    tomorrow = today + timedelta(days=1)
    returned = apply_sales(proof, upload_sales(proof, [line(tomorrow, "L/2", proof.barcode, -1),
                                                       line(tomorrow, "L/3", "UNSEEN", -1)]))
    assert shelf(proof) == 2
    assert {row.barcode: row.outcome for row in returned.rows.all()} == {proof.barcode: "restored", "UNSEEN": "exception"}


def test_sales_already_covered_by_an_applied_soh_are_refused(online_goods: Any) -> None:
    proof = online_goods
    apply_soh(proof, soh(proof, {proof.barcode: 3}))
    with pytest.raises(Refusal) as refused:
        apply_sales(proof, upload_sales(proof, [line(timezone.localdate(), "L/1", proof.barcode, 1)]))
    assert refused.value.code == "LEGACY_SALES_COVERED"
    assert shelf(proof) == 3


def test_a_test_bill_is_never_put_back_twice(online_goods: Any) -> None:
    proof = online_goods
    issue_test_bill(proof)
    yesterday = timezone.localdate() - timedelta(days=1)
    # A sales report for an earlier day reverses nothing; then the SOH puts the bill back once.
    apply_sales(proof, upload_sales(proof, [line(yesterday, "L/0", "OTHER", 1)]))
    applied = apply_soh(proof, soh(proof, {proof.barcode: 3}))
    assert applied.outcome["test_bills_reversed"] == 1 and applied.outcome["removed_qty"] == 0
    assert shelf(proof) == 3


def test_real_store_is_refused(online_goods: Any) -> None:
    proof = online_goods
    proof.world.tenant.synthetic = False
    proof.world.tenant.save(update_fields=["synthetic"])
    with pytest.raises(Refusal) as refused:
        upload_sales(proof, [line(timezone.localdate(), "L/1", proof.barcode, 1)])
    assert refused.value.code == "TEST_TENANT_ONLY"
    with pytest.raises(Refusal) as refused_soh:
        soh(proof, {proof.barcode: 1})
    assert refused_soh.value.code == "TEST_TENANT_ONLY"
    assert not LegacySalesImport.objects.exists() and not StockSync.objects.exists()
