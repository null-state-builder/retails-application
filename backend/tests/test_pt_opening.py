"""Store test run: a store opened from its PT files, then brought to the legacy SOH.

The owner's process (28 Oct 2026): set up the store empty, upload its KDPS PT
workbooks, "Approve and load" (no scanning, cost = P RATE), then upload the
legacy SOH at the stock sync. Synthetic tenant only.
"""
from __future__ import annotations

import hashlib
import io
import uuid
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone
from openpyxl import Workbook

from accounts.goods_demo import seed_working_calendar
from accounts.goods_setup import bootstrap_deployment
from accounts.models import User
from accounts.registration_services import deployment_key
from accounts.test_store_loader import Loader, OpeningDriver, Plan, access_for, act
from core.refusals import Refusal
from core.tenancy import tenant_context
from files.goods_services import stage_upload
from masters.models import Season, Store
from ptmapper import pt_opening_services as quick
from ptmapper import stock_sync_services as sync_services
from ptmapper.pt_opening import evidence_workbook, parse_pt_files
from ptmapper.soh_models import SohImport
from ptmapper.soh_parser import parse_soh
from sell.services.goods_stock import read_shelf

pytestmark = pytest.mark.django_db

PT_HEADER = ["SEASON", "BRAND", "COLOR", "GENDER", "SUB CATEGORY", "TYPE", "ITEM", "FIT", "SIZE", "BARCODE", "DESIGN",
             "HSN", "QTY", "MRP", "BASIC", "P RATE", "INPUT TAX", "OUTPUT TAX", "NAG", "MARGIN"]
OWNER = "syn.owner@synthetic.demo"


def pt_row(barcode: Any, qty: Any, mrp: Any = 1399, prate: Any = Decimal("1091.22"), hsn: Any = 61091000,
           brand: str = "STATUS QUO", size: str = "M") -> list[Any]:
    return ["SPRING SUMMER(May-26)", brand, "TEAL", "MALE", "CASUAL WEAR", "TOP WEAR", "T-SHIRT", "SLIM", size,
            barcode, "SQI-RN-26193", hsn, qty, mrp, Decimal("909.35"), prate, 5, 5, qty, 22]


def pt_book(sheets: dict[str, list[list[Any]]]) -> bytes:
    book = Workbook()
    first = book.active
    assert first is not None
    book.remove(first)
    master = book.create_sheet("Master Sheet")
    master.append(["SEASON", "BRAND", "COLOR", "SIZE"])
    master.append(["SPRING SUMMER(May-26)", "STATUS QUO", "TEAL", "M"])
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        sheet.append([sum(r[12] for r in rows if isinstance(r[12], int)), None, None])  # the work sheet's totals row
        sheet.append(PT_HEADER)
        for row in rows:
            sheet.append([float(v) if isinstance(v, Decimal) else v for v in row])
        sheet.append([None] * 7 + ["SLIM"])  # a fill-down stray cell, not a piece
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def soh_book(stock: dict[str, int]) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["Item Name", "Brand", "Size", "Barcode", "Category", "Season", "Tqty", "Mrp", "Rate", "Amount"])
    for code, qty in stock.items():
        sheet.append(["T-SHIRT", "STATUS QUO", "M", code, "CASUAL WEAR", "SPRING SUMMER(May-26)", qty, 1399, 900, 900 * qty])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# -- reader --------------------------------------------------------------------


def test_reader_adds_up_barcodes_across_sheets_and_files_and_lists_problems() -> None:
    one = pt_book({'"NARESH" Work Sheet': [pt_row(8905699672984, 2), pt_row(8905699673004, 1, prate=1500)],
                   '"ANKIT" Work Sheet': [pt_row(8905699672984, 1), pt_row(None, 3), pt_row(111, 1, hsn=6109)]})
    two = pt_book({"Sheet1": [pt_row("8905699672984", 4, mrp=1499), pt_row(222, 1, hsn="61X")]})
    metadata, rows, problems = parse_pt_files([("a.xlsx", one), ("b.xlsx", two)])
    by = {row.barcode: row for row in rows}
    assert by["8905699672984"].quantity == 7  # 2 + 1 + 4, numeric and text barcodes are one barcode
    assert by["8905699672984"].mrp == 149900 and by["8905699672984"].rate == 109122  # the last row wins ...
    assert metadata["pt_opening"]["conflicts"][0]["barcode"] == "8905699672984"  # ... and the MRP conflict is listed
    assert by["8905699672984"].source["hsn"] == "61091000" and by["8905699672984"].source["itemname"] == "T-SHIRT"
    assert problems == {"8905699673004": "PT: P RATE is above MRP", "222": "PT: HSN is not 4, 6 or 8 digits"}
    assert by["8905699673004"].source["problem"] == "PT: P RATE is above MRP"
    assert by["111"].source.get("problem") is None  # a 4-digit HSN is fine
    assert metadata["pt_opening"]["unreadable"] == [{"where": 'a.xlsx › "ANKIT" Work Sheet › row 4', "reason": "No barcode"}]
    assert [s["sheet"] for f in metadata["pt_opening"]["files"] for s in f["sheets"]] == [
        '"NARESH" Work Sheet', '"ANKIT" Work Sheet', "Sheet1"]  # Master Sheet has no PT header
    assert metadata["quantity"] == 7 + 1 + 1 + 1 and metadata["rate_meaning"] == "pt_prate"
    assert parse_soh(evidence_workbook(rows))[0]["quantity"] == metadata["quantity"]


def test_reader_refuses_a_file_with_no_pt_sheet() -> None:
    book = Workbook()
    assert book.active is not None
    book.active.append(["Barcode", "Tqty", "Mrp"])
    buffer = io.BytesIO()
    book.save(buffer)
    with pytest.raises(Refusal) as refused:
        parse_pt_files([("soh.xlsx", buffer.getvalue())])
    assert refused.value.code == "PT_SOURCE_INVALID"


# -- the store -----------------------------------------------------------------


@pytest.fixture
def store() -> Store:
    Season.objects.get_or_create(historical_unknown=True,
                                 defaults={"code": "UNKNOWN-HIST", "name": "Unknown historical season", "status": "closed"})
    tenant = bootstrap_deployment(deployment_key=deployment_key(), code="KDPS-TEST", name="KDPS Lifestyle (test)",
                                  timezone_name="Asia/Kolkata", currency="INR", locale="en-IN", synthetic=True,
                                  admin_email=OWNER, admin_name="Synthetic Company Owner", admin_staff_code="OWN-01",
                                  admin_password="Bootstrap@123")
    with tenant_context(tenant.pk):
        seed_working_calendar(tenant, User.objects.get(email=OWNER).human_id)
    plan = Plan(store_code="WRR", store_name="Wardrobe Ratu Road (test)", warehouse_code="RNC-WH",
                warehouse_name="Warehouse Ranchi (test)", entity_code="syn-vas", entity_name="KDPS test entity",
                pan="AAAVT4444D", state_code="20", state_name="Jharkhand (test)", city="Ranchi", prefix="wrr")
    _warehouse, built = Loader(tenant, plan, None, lambda _line: None).run()
    with tenant_context(tenant.pk):
        assert sum(read_shelf(built).quantities.values()) == 0  # sell-ready, no stock yet
    return built


def stage_pt(store: Store, files: list[tuple[str, bytes]], who: str = "wrr.store@synthetic.demo") -> SohImport:
    metadata, rows, _problems = parse_pt_files(files)
    driver = OpeningDriver(store.tenant, store, stager=lambda: access_for(who), preparer=lambda: access_for(who),
                           approver=lambda: access_for(OWNER), log=lambda _line: None)
    return driver.stage(evidence_workbook(rows), "PT opening.xlsx", metadata, rows)


def load(source: SohImport) -> dict[str, Any]:
    stager, preparer, approver = quick.check(access_for(OWNER), source)
    quick.load(source.pk, stager=stager, preparer=preparer, approver=approver)
    source.refresh_from_db()
    return quick.status(source)


def soh_sync(store: Store, stock: dict[str, int]) -> Any:
    data = soh_book(stock)
    metadata, parsed = parse_soh(data)
    who = access_for("wrr.store@synthetic.demo")
    evidence = stage_upload(who.principal(), command_id=uuid.uuid4(), data=data, filename="SOH.xlsx", kind="manifest",
                            scope={"scope_kind": "sites", "site_ids": [store.pk], "brand_ids": [],
                                   "sensitive_fields": ["cost", "financial"]},
                            expected_sha256=hashlib.sha256(data).hexdigest(), contains_fields=["cost", "financial"])
    sync = act(who, sync_services.ACTION, lambda run: sync_services.create_sync(
        run, site_id=store.pk, evidence=evidence, cutoff_at=timezone.now(), metadata=metadata, parsed=parsed))
    return act(who, sync_services.ACTION, lambda run: sync_services.apply(run, sync))


def test_pt_files_open_a_test_store_and_the_soh_brings_it_to_the_floor(store: Store) -> None:
    with tenant_context(store.tenant_id):
        with pytest.raises(Refusal) as refused:
            soh_sync(store, {"A1": 1})
        assert refused.value.code == "OPENING_REQUIRED"  # the SOH never stands in for the opening

        book = pt_book({"Sheet1": [pt_row("A1", 3), pt_row("A2", 2, size="L"), pt_row("BAD", 1, prate=2000)]})
        source = stage_pt(store, [("ratu road.xlsx", book)])
        state = load(source)
        assert state["state"] == "done", state
        assert state["loaded_qty"] == 5 and state["excluded_rows"] == 1
        assert read_shelf(store).quantities and sum(read_shelf(store).quantities.values()) == 5
        row = source.rows.get(barcode="A1")
        assert row.mapping["basic_paise"] == "109122" and row.mapping["hsn"] == "61091000"  # P RATE, the PT's HSN
        assert source.rows.get(barcode="BAD").exclusion_reason == "PT: P RATE is above MRP"
        assert source.state == "applied"

        with pytest.raises(Refusal) as again:
            quick.check(access_for(OWNER), source)
        assert again.value.code == "STATE_CONFLICT"  # loaded once

        sync = soh_sync(store, {"A1": 1, "A2": 4, "N1": 2})  # A1 sold 2, A2 two more found, N1 never in a PT
        assert sync.outcome["removed_qty"] == 2 and sync.outcome["added_qty"] == 2
        assert sync.outcome["new_item_barcodes"] == 1
        assert sum(read_shelf(store).quantities.values()) == 5

        top_up = sync.top_up_import
        assert top_up is not None
        state = load(top_up)  # the same one-click load takes the new items in, at the SOH Rate
        assert state["state"] == "done", state
        assert sum(read_shelf(store).quantities.values()) == 7
        assert top_up.rows.get(barcode="N1").mapping["basic_paise"] == "90000"


def test_load_needs_a_different_approving_owner_and_a_test_store(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    with tenant_context(store.tenant_id):
        source = stage_pt(store, [("pt.xlsx", pt_book({"Sheet1": [pt_row("A1", 1)]}))])
        with pytest.raises(Refusal) as denied:
            quick.check(access_for("wrr.cashier@synthetic.demo"), source)
        assert denied.value.code == "ACTION_DENIED"

        own = stage_pt(store, [("own.xlsx", pt_book({"Sheet1": [pt_row("B1", 1)]}))], who=OWNER)
        with pytest.raises(Refusal) as self_approval:
            quick.check(access_for(OWNER), own)
        assert self_approval.value.code == "SELF_APPROVAL"

        monkeypatch.setattr(sync_services, "is_real_store", lambda _store: True)
        with pytest.raises(Refusal) as real:
            quick.check(access_for(OWNER), source)
        assert real.value.code == "TEST_TENANT_ONLY"


def test_a_second_pt_opens_only_the_barcodes_the_store_never_opened(store: Store) -> None:
    from ptmapper import soh_services

    with tenant_context(store.tenant_id):
        assert load(stage_pt(store, [("first.xlsx", pt_book({"Sheet1": [pt_row("A1", 2)]}))]))["state"] == "done"
        source = stage_pt(store, [("second.xlsx", pt_book({"Sheet1": [pt_row("A1", 5), pt_row("C1", 1, size="L")]}))])
        assert load(source)["state"] == "done"
        assert source.rows.get(barcode="A1").exclusion_reason == soh_services.ALREADY_OPENED
        assert sum(read_shelf(store).quantities.values()) == 3  # A1's balance is the SOH sync's to correct


def _alias_queries_per_batch(store: Store, monkeypatch: pytest.MonkeyPatch, barcodes: list[str]) -> int:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from ptmapper import soh_services

    counts: list[int] = []
    real = soh_services.apply_batch

    def counted(*args: Any, **kwargs: Any) -> Any:
        with CaptureQueriesContext(connection) as captured:
            batch = real(*args, **kwargs)
        counts.append(sum('FROM "masters_skualias"' in q["sql"] for q in captured.captured_queries))
        return batch

    monkeypatch.setattr(soh_services, "apply_batch", counted)
    book = pt_book({"Sheet1": [pt_row(code, 1, size=f"S{n}") for n, code in enumerate(barcodes)]})
    assert load(stage_pt(store, [("pt.xlsx", book)]))["state"] == "done"
    assert len(counts) == 1
    return counts[0]


def test_a_batch_looks_its_barcodes_up_together_not_one_by_one(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # One lookup per row was quadratic on the fresh server database (28 Oct 2026).
    with tenant_context(store.tenant_id):
        queries = _alias_queries_per_batch(store, monkeypatch, [f"Q{n}" for n in range(8)])
    assert queries <= 2  # was one per row (8 here, 5,000 in a full batch)


# -- load_pt_opening (the same load from a server shell) ------------------------


def test_load_pt_opening_stages_and_loads_then_resumes_nothing_twice(store: Store, tmp_path: Any) -> None:
    from django.core.management import CommandError, call_command

    path = tmp_path / "pt.xlsx"
    path.write_bytes(pt_book({"Sheet1": [pt_row("A1", 3), pt_row("A2", 2, size="L")]}))
    out = io.StringIO()
    with pytest.raises(CommandError, match="needs --stager"):
        call_command("load_pt_opening", "--store", store.code, "--pt", str(path))
    with pytest.raises(CommandError, match="cannot also upload"):
        call_command("load_pt_opening", "--store", store.code, "--pt", str(path), "--stager", OWNER)
    call_command("load_pt_opening", "--store", store.code, "--pt", str(path), "--stager", "wrr.store@synthetic.demo",
                 stdout=out)
    assert "Done: 5 pieces on the floor" in out.getvalue()
    with tenant_context(store.tenant_id):
        assert sum(read_shelf(store).quantities.values()) == 5
        assert SohImport.objects.filter(site=store).count() == 1
    with pytest.raises(CommandError, match="no PT source waiting"):
        call_command("load_pt_opening", "--store", store.code)  # a loaded source is not loaded again


def test_load_pt_opening_resumes_the_uploaded_source(store: Store) -> None:
    from django.core.management import CommandError, call_command

    with tenant_context(store.tenant_id):
        source = stage_pt(store, [("pt.xlsx", pt_book({"Sheet1": [pt_row("A1", 2)]}))])
        quick._record(source.pk, state="failed", error="RETRY_EXHAUSTED: busy")  # the web load that gave up
    with pytest.raises(CommandError, match="already has a PT source"):
        call_command("load_pt_opening", "--store", store.code, "--pt", __file__, "--stager", "wrr.store@synthetic.demo")
    out = io.StringIO()
    call_command("load_pt_opening", "--store", store.code, stdout=out)
    assert "Done: 2 pieces on the floor" in out.getvalue()
    with tenant_context(store.tenant_id):
        source.refresh_from_db()
        assert quick.status(source)["state"] == "done"
        assert SohImport.objects.filter(site=store).count() == 1


def test_load_pt_opening_keeps_the_page_guards(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    from django.core.management import CommandError, call_command

    with tenant_context(store.tenant_id):
        stage_pt(store, [("own.xlsx", pt_book({"Sheet1": [pt_row("B1", 1)]}))], who=OWNER)
    with pytest.raises(CommandError, match="SELF_APPROVAL"):
        call_command("load_pt_opening", "--store", store.code)
    monkeypatch.setattr(sync_services, "is_real_store", lambda _store: True)
    with pytest.raises(CommandError, match="TEST_TENANT_ONLY"):
        call_command("load_pt_opening", "--store", store.code)
    with pytest.raises(CommandError, match="No store NOPE"):
        call_command("load_pt_opening", "--store", "NOPE")
