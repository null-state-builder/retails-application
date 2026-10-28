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
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from openpyxl import Workbook
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from alerts.goods_models import GoodsException
from core.kernel_models import OutboxIntent
from core.refusals import Refusal
from files.goods_services import stage_upload
from masters.goods_identity_models import SkuAlias
from ptmapper import legacy_sales_services as sales
from ptmapper import stock_sync_services as sync_services
from ptmapper.legacy_sales_models import LegacySalesImport, LegacySalesRow
from ptmapper.legacy_sales_parser import parse_sales
from ptmapper.soh_models import SohImport
from ptmapper.soh_parser import parse_soh
from ptmapper.stock_sync_models import StockSync
from ptmapper.stock_sync_views import StockSyncAcknowledgeView, StockSyncRowsView
from sell.models import Sale
from sell.services.goods_stock import read_shelf
from sell.views import OnlineFinaliseView
from stockledger.goods_models import CustodyLot, JournalBatch, Origin, QuantityLeg
from tests.first_store_goods import command, live_access
from tests.test_first_store_online import bill, post
from tests.test_first_store_online import online_goods as online_goods
from tests.test_so03_denials import _assign, _person
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


def book_mrp_rupees(proof: Any) -> int:
    """The fixture barcode's MRP as the counter prices it, in rupees as the SOH writes it."""
    piece = next(p for p in read_shelf(proof.world.sites[0]).pieces if p.barcode == proof.barcode)
    assert piece.mrp_paise and piece.mrp_paise % 100 == 0
    return piece.mrp_paise // 100


#: The same SOH content is the same file: an XLSX stamps its save time, so keep the first bytes.
_SOH_FILES: dict[tuple[tuple[str, int, int], ...], bytes] = {}


def soh_workbook(proof: Any, stock: dict[str, int], mrp: dict[str, int] | None = None) -> bytes:
    same = book_mrp_rupees(proof)
    rows = tuple((code, qty, (mrp or {}).get(code, same)) for code, qty in stock.items())
    if rows not in _SOH_FILES:
        _SOH_FILES[rows] = workbook(SOH_HEADER, [["SHIRT", "BRAND", code, qty, price, 500, 500 * qty]
                                                 for code, qty, price in rows])
    return _SOH_FILES[rows]


def soh(proof: Any, stock: dict[str, int], *, cutoff: Any = None, mrp: dict[str, int] | None = None) -> StockSync:
    data = soh_workbook(proof, stock, mrp)
    metadata, parsed = parse_soh(data)
    site = proof.world.sites[0]
    evidence = stage_upload(proof.manager.principal(), command_id=uuid.uuid4(), data=data, filename="SOH.xlsx",
                            kind="manifest", scope={"scope_kind": "sites", "site_ids": [site.pk], "brand_ids": [],
                                                    "sensitive_fields": ["cost", "financial"]},
                            expected_sha256=hashlib.sha256(data).hexdigest(), contains_fields=["cost", "financial"])
    return cast(StockSync, command(proof.manager, sync_services.ACTION, lambda run: sync_services.create_sync(
        run, site_id=site.pk, evidence=evidence, cutoff_at=cutoff or timezone.now(), metadata=metadata, parsed=parsed)))


def apply_soh(proof: Any, sync: StockSync) -> StockSync:
    return cast(StockSync, command(proof.manager, sync_services.ACTION, lambda run: sync_services.apply(run, sync)))


def test_soh_puts_test_bill_back_then_matches_lower_soh(online_goods: Any) -> None:
    proof = online_goods
    issue_test_bill(proof)
    assert shelf(proof) == 2
    sync = soh(proof, {proof.barcode: 1})
    applied = apply_soh(proof, sync)
    assert shelf(proof) == 1
    assert applied.outcome["test_bills_reversed"] == 1 and applied.outcome["removed_qty"] == 2
    assert applied.journal_batch_id is not None
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


def flags(sync: StockSync) -> dict[str, tuple[str | None, bool, str]]:
    return {row.barcode: (row.category, row.mrp_differs, row.outcome) for row in sync.rows.all()}


def review_exceptions() -> Any:
    return GoodsException.objects.filter(kind=sync_services.EXCEPTION_KIND)


def test_soh_missing_barcode_goes_to_zero_and_soh_only_barcode_is_flagged_not_created(online_goods: Any) -> None:
    proof = online_goods
    site = proof.world.sites[0]
    before = (SkuAlias.objects.count(), CustodyLot.objects.count(), Origin.objects.count(), SohImport.objects.count())
    applied = apply_soh(proof, soh(proof, {"NEW-ITEM-1": 4}))
    assert shelf(proof) == 0
    assert flags(applied) == {proof.barcode: ("PT_ONLY", False, "removed"), "NEW-ITEM-1": ("SOH_ONLY", False, "flagged")}
    soh_only = applied.rows.get(barcode="NEW-ITEM-1")
    assert (soh_only.soh_qty, soh_only.book_qty, soh_only.applied_qty) == (4, 0, 0)
    assert "not sellable until its PT is uploaded" in soh_only.exception_note
    # Nothing is invented: no identity, no lot, no origin, no top-up import - and nothing to sell.
    assert applied.top_up_import is None
    assert (SkuAlias.objects.count(), CustodyLot.objects.count(), Origin.objects.count(),
            SohImport.objects.count()) == before
    assert "NEW-ITEM-1" not in {code for code, _season in read_shelf(site).quantities}
    assert applied.outcome["soh_only_qty"] == 4 and applied.outcome["soh_only_barcodes"] == 1
    assert applied.outcome["category_counts"]["SOH_ONLY"] == 1 and applied.outcome["category_counts"]["PT_ONLY"] == 1


def test_every_category_is_flagged_and_known_stock_equals_the_soh(online_goods: Any) -> None:
    proof = online_goods
    # Below: the SOH holds fewer than the book.
    below = apply_soh(proof, soh(proof, {proof.barcode: 1}))
    assert flags(below) == {proof.barcode: ("SOH_BELOW", False, "removed")}
    assert shelf(proof) == 1
    # Above: more than the book, for an item that came in on a PT.
    above = apply_soh(proof, soh(proof, {proof.barcode: 4}))
    assert flags(above) == {proof.barcode: ("SOH_ABOVE", False, "added")}
    assert shelf(proof) == 4
    # Same quantity, different MRP: flagged only, stock unchanged.
    mrp = book_mrp_rupees(proof) + 1
    priced = apply_soh(proof, soh(proof, {proof.barcode: 4}, mrp={proof.barcode: mrp}))
    row = priced.rows.get()
    assert (row.category, row.mrp_differs, row.outcome, row.applied_qty) == ("MRP_DIFFERS", True, "flagged", 0)
    assert (row.soh_mrp_paise, row.book_mrp_paise) == (mrp * 100, (mrp - 1) * 100)
    assert shelf(proof) == 4
    # A quantity and an MRP difference together: the quantity is the category, the MRP a flag.
    both = apply_soh(proof, soh(proof, {proof.barcode: 2}, mrp={proof.barcode: mrp}))
    assert flags(both) == {proof.barcode: ("SOH_BELOW", True, "removed")}
    assert both.outcome["category_counts"] == {"SOH_ONLY": 0, "PT_ONLY": 0, "SOH_ABOVE": 0, "SOH_BELOW": 1,
                                               "MRP_DIFFERS": 1}
    assert shelf(proof) == 2
    # A matching SOH flags nothing and raises nothing.
    quiet = apply_soh(proof, soh(proof, {proof.barcode: 2}))
    assert not quiet.rows.exists() and quiet.outcome["flagged_rows"] == 0
    assert review_exceptions().count() == 4


def test_one_review_exception_and_notification_per_sync_for_the_store_manager(online_goods: Any) -> None:
    proof = online_goods
    applied = apply_soh(proof, soh(proof, {proof.barcode: 1, "NEW-1": 2, "NEW-2": 3, "NEW-3": 1}))
    assert applied.rows.count() == 4
    exception = review_exceptions().get()
    assert (exception.owner_role, exception.site_id, exception.subject_key) == (
        "M-STR", proof.world.sites[0].pk, f"stock_sync:{applied.pk}")
    intents = [i for i in OutboxIntent.objects.filter(kind="notification")
               if (i.payload or {}).get("template_key") == "exception.stock_sync_review"]
    assert len(intents) == 1 and intents[0].payload["recipient_role"] == "M-STR"


def test_preview_counts_categories_before_apply(online_goods: Any) -> None:
    proof = online_goods
    sync = soh(proof, {proof.barcode: 5, "NEW-1": 2}, mrp={proof.barcode: book_mrp_rupees(proof) + 3})
    preview = sync_services.preview(sync)
    assert (preview["book_qty"], preview["soh_qty"], preview["add_qty"], preview["soh_only_qty"]) == (3, 7, 2, 2)
    assert preview["flagged_rows"] == 2
    assert preview["category_counts"] == {"SOH_ONLY": 1, "PT_ONLY": 0, "SOH_ABOVE": 1, "SOH_BELOW": 0,
                                          "MRP_DIFFERS": 1}


def test_soh_older_than_the_last_applied_is_refused_at_upload_and_at_apply(online_goods: Any) -> None:
    proof = online_goods
    earlier = timezone.now()
    waiting = soh(proof, {proof.barcode: 2}, cutoff=earlier)
    apply_soh(proof, soh(proof, {proof.barcode: 3}))
    with pytest.raises(Refusal) as at_upload:
        soh(proof, {proof.barcode: 1}, cutoff=earlier)
    assert at_upload.value.code == "STOCK_SYNC_STALE"
    with pytest.raises(Refusal) as at_apply:
        apply_soh(proof, waiting)
    assert at_apply.value.code == "STOCK_SYNC_STALE"
    assert shelf(proof) == 3 and StockSync.objects.count() == 2


def test_the_same_soh_again_is_a_no_op(online_goods: Any) -> None:
    proof = online_goods
    cutoff = timezone.now()
    first = soh(proof, {proof.barcode: 1}, cutoff=cutoff)
    applied = apply_soh(proof, first)
    again = soh(proof, {proof.barcode: 1}, cutoff=cutoff)
    assert again.pk == first.pk and again.state == StockSync.State.APPLIED
    assert StockSync.objects.count() == 1 and applied.rows.count() == 1 and review_exceptions().count() == 1
    assert shelf(proof) == 1


# -- Review: acknowledge -----------------------------------------------------


def person(proof: Any, role: str, site_index: int = 0) -> Any:
    world = proof.world
    user, human = _person(world, f"sync-{role}-{site_index}")
    _assign(world, human, role, sites=(world.sites[site_index],), all_brands=True)
    return live_access(user)


def call(view: Any, access: Any, path: str, *, data: dict[str, Any] | None = None, **kwargs: Any) -> Response:
    factory = APIRequestFactory()
    request = (factory.post(path, data, format="json") if data is not None else factory.get(path))
    force_authenticate(request, user=access.user, token=access.session)
    return cast(Response, view.as_view()(request, **kwargs))


def acknowledge(access: Any, sync: StockSync, row_ids: list[str]) -> Response:
    return call(StockSyncAcknowledgeView, access, f"/api/goods-v1/ptmapper/stock-syncs/{sync.pk}/rows/acknowledge",
                data={"command_id": str(uuid.uuid4()), "contract_version": "goods-v1", "row_ids": row_ids}, pk=sync.pk)


def test_review_list_filters_by_category_and_store_manager_acknowledges(online_goods: Any) -> None:
    proof = online_goods
    applied = apply_soh(proof, soh(proof, {proof.barcode: 1, "NEW-1": 2, "NEW-2": 1},
                                   mrp={proof.barcode: book_mrp_rupees(proof) + 1}))
    path = f"/api/goods-v1/ptmapper/stock-syncs/{applied.pk}/rows"
    listed = call(StockSyncRowsView, proof.manager, f"{path}?category=SOH_ONLY&limit=1", pk=applied.pk)
    assert listed.status_code == 200, listed.data
    assert listed.data["total"] == 2 and len(listed.data["rows"]) == 1
    assert listed.data["counts"]["categories"] == {"SOH_ONLY": 2, "PT_ONLY": 0, "SOH_ABOVE": 0, "SOH_BELOW": 1,
                                                   "MRP_DIFFERS": 1}
    mrp_rows = call(StockSyncRowsView, proof.manager, f"{path}?category=MRP_DIFFERS", pk=applied.pk)
    assert [r["barcode"] for r in mrp_rows.data["rows"]] == [proof.barcode]
    stock_before = shelf(proof)

    ids = [str(pk) for pk in applied.rows.values_list("pk", flat=True)]
    first = acknowledge(proof.manager, applied, ids[:2])
    assert first.status_code == 200, first.data
    assert (first.data["acknowledged"], first.data["open_rows"]) == (2, 1)
    assert review_exceptions().get().state == "open"
    done = acknowledge(proof.manager, applied, ids)
    assert (done.data["acknowledged"], done.data["open_rows"]) == (1, 0)
    assert review_exceptions().get().state == "resolved"
    reviewed = applied.rows.first()
    assert reviewed is not None and reviewed.reviewed_by_id == proof.manager.human_id and reviewed.reviewed_at
    # Acknowledging changes no stock and no item.
    assert shelf(proof) == stock_before and not SkuAlias.objects.filter(value__startswith="NEW-").exists()
    opened = call(StockSyncRowsView, proof.manager, f"{path}?review_state=open", pk=applied.pk)
    assert opened.data["total"] == 0 and opened.data["counts"]["acknowledged"] == 3


def test_acknowledge_is_denied_without_opening_import_stage_or_at_another_store(online_goods: Any) -> None:
    proof = online_goods
    applied = apply_soh(proof, soh(proof, {proof.barcode: 1}))
    ids = [str(pk) for pk in applied.rows.values_list("pk", flat=True)]
    for outsider in (person(proof, "accounts"), person(proof, "store_person", site_index=1)):
        refused = acknowledge(outsider, applied, ids)
        assert refused.status_code == 404, refused.data
        listed = call(StockSyncRowsView, outsider, f"/api/goods-v1/ptmapper/stock-syncs/{applied.pk}/rows",
                      pk=applied.pk)
        assert listed.status_code == 404
    assert not applied.rows.filter(review_state="acknowledged").exists()
    assert review_exceptions().get().state == "open"


# -- sync_soh command --------------------------------------------------------


def test_sync_soh_command_stages_previews_and_applies(online_goods: Any, worlds: Any, tmp_path: Any) -> None:
    proof = online_goods
    other = worlds[1].tenant
    other.synthetic = False
    other.save(update_fields=["synthetic"])
    user = proof.manager.user
    user.email = "sync-manager@test.example"
    user.save(update_fields=["email"])
    path = tmp_path / "SOH.xlsx"
    path.write_bytes(soh_workbook(proof, {proof.barcode: 1, "NEW-1": 2}))
    exported = timezone.localtime().isoformat()
    args = ["--store", proof.world.sites[0].code, "--soh", str(path), "--exported-at", exported,
            "--uploader", user.email]
    out = io.StringIO()
    call_command("sync_soh", *args, stdout=out)
    assert "Flagged rows 2: SOH_ONLY 1, PT_ONLY 0, SOH_ABOVE 0, SOH_BELOW 1, MRP_DIFFERS 0." in out.getvalue()
    assert StockSync.objects.get().state == StockSync.State.PREVIEWED and shelf(proof) == 3
    out = io.StringIO()
    call_command("sync_soh", *args, "--apply", stdout=out)
    text = out.getvalue()
    assert "book units 3 -> 1, SOH total 3" in text and "SOH-only 2 pieces over 1 barcodes" in text
    sync = StockSync.objects.get()
    assert sync.state == StockSync.State.APPLIED and sync.uploaded_by_id == proof.manager.human_id
    assert shelf(proof) == 1 and review_exceptions().count() == 1
    # Run again: the same file is the same, already-applied sync.
    out = io.StringIO()
    call_command("sync_soh", *args, "--apply", stdout=out)
    assert "Already applied" in out.getvalue() and StockSync.objects.count() == 1


def test_sync_soh_command_refuses_a_login_without_the_action(online_goods: Any, worlds: Any, tmp_path: Any) -> None:
    proof = online_goods
    worlds[1].tenant.synthetic = False
    worlds[1].tenant.save(update_fields=["synthetic"])
    outsider = person(proof, "accounts")
    outsider.user.email = "sync-accounts@test.example"
    outsider.user.save(update_fields=["email"])
    path = tmp_path / "SOH.xlsx"
    path.write_bytes(soh_workbook(proof, {proof.barcode: 1}))
    with pytest.raises(CommandError):
        call_command("sync_soh", "--store", proof.world.sites[0].code, "--soh", str(path),
                     "--exported-at", timezone.localtime().isoformat(), "--uploader", outsider.user.email,
                     stdout=io.StringIO())
    assert not StockSync.objects.exists()


def test_soh_exported_before_the_stock_was_loaded_is_refused(online_goods: Any) -> None:
    """Items loaded after the export would read as absent and come in again as new items."""
    proof = online_goods
    with pytest.raises(Refusal) as refused:
        soh(proof, {proof.barcode: 3}, cutoff=timezone.now() - timedelta(days=30))
    assert refused.value.code == "SOH_BEFORE_STOCK"
    assert "Export the SOH again" in refused.value.message
    assert not StockSync.objects.exists() and shelf(proof) == 3


def test_a_discarded_soh_can_be_uploaded_again_with_another_export_time(online_goods: Any) -> None:
    proof = online_goods
    first = soh(proof, {proof.barcode: 3})
    with pytest.raises(Refusal) as refused:
        soh(proof, {proof.barcode: 3}, cutoff=first.cutoff_at + timedelta(minutes=1))
    assert refused.value.code == "SOH_ALREADY_UPLOADED"
    command(proof.manager, sync_services.ACTION, lambda run: sync_services.discard(run, first))
    again = soh(proof, {proof.barcode: 3}, cutoff=first.cutoff_at + timedelta(minutes=1))
    assert again.pk != first.pk and again.state == StockSync.State.PREVIEWED


# -- Sales report ------------------------------------------------------------


def upload_sales(proof: Any, rows: list[list[Any]], *, name: str = "sale.xlsx") -> LegacySalesImport:
    data = workbook(SALES_HEADER, rows)
    metadata, parsed = parse_sales(data)
    return cast(LegacySalesImport, command(proof.manager, sales.ACTION, lambda run: sales.create_import(
        run, site_id=proof.world.sites[0].pk, source_hash=hashlib.sha256(data).hexdigest(), source_name=name,
        metadata=metadata, rows=parsed)))


def apply_sales(proof: Any, source: LegacySalesImport) -> LegacySalesImport:
    return cast(LegacySalesImport, command(proof.manager, sales.ACTION, lambda run: sales.apply(run, source)))


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
