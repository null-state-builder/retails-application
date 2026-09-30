"""SOH input reconciliation, protected projection and canonical stock posting."""
from __future__ import annotations

import uuid
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from django.conf import UserSettingsHolder
from core.commands import CommandRun, database_now

import pytest

from core.refusals import Refusal
from core.tenancy import tenant_context
from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from core.canonical import sha256_hex
from files.goods_services import stage_upload
from ptmapper import soh_services
from ptmapper.soh_models import SohImport, SohImportBatch, SohImportRow
from ptmapper.soh_parser import parse_soh
from ptmapper.soh_views import source_dto
from stockledger.goods_models import JournalBatch
from masters.goods_identity_models import ProductSku, SkuAlias
from masters.goods_config import withdraw as withdraw_config
from ptmapper import goods_manifest_services
from stockledger import goods_acceptance
from tests.first_store_goods import actors, approve, command, live_access, opened_goods, reviewed_source, workbook
from tests.test_so03_denials import TenantWorld, _assign, _person, worlds as worlds


@pytest.fixture(autouse=True)
def offbox(settings: UserSettingsHolder, tmp_path: Path) -> None:
    settings.KDPS_OFFBOX_ROOT = str(tmp_path / "write-once")


@pytest.fixture
def disposable_transactional_flush(django_db_blocker: Any) -> None:
    """Allow Django's teardown only on its positively named disposable proof DB."""
    with django_db_blocker.unblock():
        if os.environ.get("KDPS_PROOF_MODE") != "1" or connection.settings_dict["NAME"] != "kdps_proof_test":
            raise AssertionError("Concurrent import testing requires the disposable proof test database.")
        with connection.cursor() as cursor:
            cursor.execute("SET kdps.allow_truncate = 'on'")


@pytest.mark.django_db(transaction=True)
def test_concurrent_source_claims_bind_one_snapshot(worlds: tuple[TenantWorld, TenantWorld], disposable_transactional_flush: None) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        _, _, manager = actors(world)
        access = live_access(manager)
        raw = workbook()
        metadata, parsed = parse_soh(raw)
        evidence = stage_upload(access.principal(), command_id=uuid.uuid4(), data=raw, filename="proof-concurrent-soh.xlsx", kind="manifest",
            scope={"scope_kind": "sites", "site_ids": [world.sites[0].pk], "brand_ids": [], "sensitive_fields": ["cost", "financial"]}, expected_sha256=sha256_hex(raw), contains_fields=["cost", "financial"])
    barrier = threading.Barrier(2)
    def stage() -> str:
        close_old_connections()
        try:
            with tenant_context(world.tenant.pk):
                access = live_access(manager)
                barrier.wait(timeout=10)
                parent = command(access, soh_services.STAGE, lambda run: soh_services.create_source(run, site_id=world.sites[0].pk, evidence=evidence, metadata=metadata, parsed=parsed))
                return str(parent.pk)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: stage(), range(2)))
    with tenant_context(world.tenant.pk):
        assert results[0] == results[1]
        assert SohImport.objects.count() == 1 and SohImportRow.objects.count() == 1
        assert not JournalBatch.objects.exists()


def test_current_quantity_uses_tqty_and_preserves_exact_barcode_and_raw_source() -> None:
    metadata, rows = parse_soh(workbook(footer=[None, None, None, None, None, None, 3, None, None, 1500]))
    assert metadata["row_count"] == 1 and metadata["quantity"] == 3
    assert metadata["source_rate_difference_paise"] == "0"
    assert rows[0].barcode == "000123" and rows[0].rate == 50000
    assert metadata["rate_meaning"] == "unconfirmed"


def test_counter_item_name_uses_reviewed_source_without_changing_identity_or_history(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    from sell.services.goods_stock import read_shelf
    from stockledger.goods_descriptions import origin_item_names
    from stockledger.goods_models import Origin
    world, other = worlds
    with tenant_context(world.tenant.pk):
        proof = opened_goods(world, *actors(world))
        origin = Origin.objects.get(sku_id=proof.sku_id)
        sku = ProductSku.objects.select_related("style").get(pk=proof.sku_id)
        before = (origin.frozen_evidence, origin.unit_cost, sku.identity_key, sku.style.style_code,
                  JournalBatch.objects.count())
        piece = read_shelf(world.sites[0]).pieces[0]
        assert piece.dims["item"] == "Shirt" and piece.dims["design"] == sku.style.style_code
        assert origin_item_names(other.tenant.pk, [origin.pk]) == {}
        raw = proof.source.rows.get()
        raw.source = {**raw.source, "itemname": "Unreviewed replacement"}
        raw.save(update_fields=["source"])
        assert origin_item_names(world.tenant.pk, [origin.pk]) == {}
        assert "Unreviewed replacement" not in str(read_shelf(world.sites[0]).pieces)
        origin.refresh_from_db()
        sku.refresh_from_db()
        assert before == (origin.frozen_evidence, origin.unit_cost, sku.identity_key, sku.style.style_code,
                          JournalBatch.objects.count())


def test_zero_negative_and_unpriced_rows_are_retained_for_review() -> None:
    metadata, rows = parse_soh(workbook([
        ["Catalogue", "Old label", "M", "000001", "Shirt", "2026-09-28", 0, 100, 50, 0],
        ["Unresolved negative", "Old label", "M", "000002", "Shirt", "14", -1, 100, 50, 0],
        ["Unpriced stock", "Old label", "M", "000003", "Shirt", None, 1, 0, 50, 50],
    ]))
    assert metadata["row_count"] == 3 and metadata["zero_stock_rows"] == 1
    assert metadata["negative_rows"] == 1 and metadata["quantity"] == 1
    assert [row.quantity for row in rows] == [0, -1, 1]
    assert rows[0].source["season"] == "2026-09-28" and rows[1].source["season"] == "14"
    assert rows[2].mrp == 0, "The parser preserves invalid MRP; reviewed inclusion must refuse it"


def test_full_size_source_has_deterministic_stocked_batches_and_retains_catalogue() -> None:
    stocked = 11_161
    double_quantity = 19_896 - stocked
    rows = [["Shirt", "Old label", "M", f"000{index:06}", "Shirt", "Legacy season",
             2 if index < double_quantity else 1 if index < stocked else 0,
             1000, 500, 1000 if index < double_quantity else 500 if index < stocked else 0]
            for index in range(25_689)]
    metadata, parsed = parse_soh(workbook(rows, [None, None, None, None, None, None, 19_896, None, None, 9_948_000]))
    assert metadata["row_count"] == 25_689 and metadata["stocked_rows"] == stocked
    assert metadata["quantity"] == 19_896 and metadata["zero_stock_rows"] == 14_528
    included = [row for row in parsed if row.quantity > 0]
    children = [included[start:start + soh_services.MAX_BATCH] for start in range(0, len(included), soh_services.MAX_BATCH)]
    assert [len(batch) for batch in children] == [5000, 5000, 1161]
    assert sum(row.quantity for batch in children for row in batch) == 19_896
    assert len({row.barcode for batch in children for row in batch}) == stocked


@pytest.mark.parametrize("rows,footer", [
    ([["No identity", "Brand", "M", None, "Shirt", "Season", 3, 100, 50, 150]], None),
    ([["Shirt", "Brand", "M", "A", "Shirt", "Season", "1.5", 100, 50, 75]], None),
    (None, [None, None, None, None, None, None, 4, None, None, 1500]),
    (None, [None, None, None, None, None, None, 3, None, None, 1501]),
])
def test_invalid_source_never_silently_drops_a_row_or_bad_footer(rows: list[list[Any]] | None, footer: list[Any] | None) -> None:
    with pytest.raises(Refusal):
        parse_soh(workbook(rows, footer))


def test_real_source_requires_independent_exact_review_and_hides_cost_from_manager(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        owner, warehouse, manager = actors(world)
        proof = reviewed_source(world, owner, warehouse, manager)
        projected = source_dto(proof.manager, proof.source)
        assert projected["source_evidence_id"] is None
        assert not projected["data"]["field_access"]["readable_fields"]
        assert "valuation_evidence_id" not in projected["data"]["configuration"]
        assert source_dto(proof.warehouse, proof.source)["data"]["field_access"]["readable_fields"] == ["cost"]
        with pytest.raises(Refusal, match="approved"):
            command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 1))
        assert manager.human is not None
        _assign(world, manager.human, "owner", all_sites=True, all_brands=True)
        from tests.first_store_goods import live_access
        with pytest.raises(Refusal, match="physical verifier"):
            approve(live_access(manager), proof.source.approval_request)
        approve(proof.owner, proof.source.approval_request)
        proof.source.refresh_from_db()
        batch = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 1))
        public_row = batch.manifest.current_version.rows.get().payload
        assert public_row["identity"]["description"] == "Shirt"
        assert public_row["source_row_key"] == proof.source.rows.get().source_row_key
        replay = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 1))
        assert batch.pk == replay.pk and SohImportBatch.objects.count() == 1
        assert not JournalBatch.objects.exists(), "Staging/materialising a manifest does not post inventory"


def test_own_pt_cost_requires_recorded_preparation_and_cannot_be_claimed_by_manager(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        _, preparer_user, manager_user = actors(world)
        preparer, manager = live_access(preparer_user), live_access(manager_user)
        raw = workbook()
        metadata, rows = parse_soh(raw)
        evidence = stage_upload(manager.principal(), command_id=uuid.uuid4(), data=raw, filename="proof-unclaimed-soh.xlsx", kind="manifest",
            scope={"scope_kind": "sites", "site_ids": [world.sites[0].pk], "brand_ids": [], "sensitive_fields": ["cost", "financial"]}, expected_sha256=sha256_hex(raw), contains_fields=["cost", "financial"])
        source = command(manager, soh_services.STAGE, lambda run: soh_services.create_source(run, site_id=world.sites[0].pk, evidence=evidence, metadata=metadata, parsed=rows))
        before = source_dto(preparer, source)
        assert before["data"]["field_access"]["readable_fields"] == []
        assert "claim" in before["allowed_actions"] and "source_rate_difference_paise" not in before["data"]["metadata"]
        with pytest.raises(Refusal):
            command(manager, soh_services.PREPARE, lambda run: soh_services.claim_preparation(run, manager, source, source.revision))
        source = command(preparer, soh_services.PREPARE, lambda run: soh_services.claim_preparation(run, preparer, source, source.revision))
        assert source.prepared_by_id == preparer.human_id
        assert source_dto(preparer, source)["data"]["field_access"]["readable_fields"] == ["cost"]
        other, human = _person(world, "other-soh-warehouse")
        _assign(world, human, "warehouse", sites=(world.sites[0],), all_brands=True)
        other_access = live_access(other)
        assert source_dto(other_access, source)["data"]["field_access"]["readable_fields"] == []
        with pytest.raises(Refusal):
            command(other_access, soh_services.PREPARE, lambda run: soh_services.claim_preparation(run, other_access, source, source.revision))


def test_source_financial_evidence_requires_joint_fields_and_hidden_notes_survive_mapping_edit(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = reviewed_source(world, *actors(world))
        assert proof.source.prepared_by is not None
        _assign(world, proof.source.prepared_by, "owner", sites=(world.sites[1],), all_brands=True)
        assert proof.warehouse.refresh()
        own_cost = source_dto(proof.warehouse, proof.source)
        assert own_cost["source_evidence_id"] is None
        assert "source_rate_difference_paise" not in own_cost["data"]["metadata"]
        assert not soh_services.FINANCIAL_NOTES.intersection(own_cost["data"]["configuration"])
        owner = source_dto(proof.owner, proof.source)
        assert owner["source_evidence_id"] == str(proof.source.source_evidence_id)
        assert "source_rate_difference_paise" in owner["data"]["metadata"]
        assert set(owner["data"]["field_access"]["readable_fields"]) == {"cost", "financial"}
        original_notes = {key: proof.source.configuration[key] for key in soh_services.FINANCIAL_NOTES}
        source = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.prepare(run, proof.warehouse, proof.source,
            own_cost["data"]["configuration"], proof.source.revision))
        assert {key: source.configuration[key] for key in soh_services.FINANCIAL_NOTES} == original_notes
        assert source.revision == proof.source.revision + 1
        source.refresh_from_db()
        assert source.state == "review" and source.approved_by_id is None


def test_child_materialisation_reuses_pinned_config_without_rowwise_config_queries(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        rows = [["Shirt", "Old brand text", "M", f"QUERY{index:05}", "Shirt", "Legacy season", 1, 1000, 500, 500] for index in range(16)]
        proof = reviewed_source(world, *actors(world), rows=rows)
        approve(proof.owner, proof.source.approval_request)
        proof.source.refresh_from_db()
        with CaptureQueriesContext(connection) as queries:
            batch = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 1))
        config_reads = [query["sql"] for query in queries.captured_queries if 'FROM "masters_configversion"' in query["sql"]
                        and ("'identity_profile'" in query["sql"] or "'vocabulary'" in query["sql"])]
        assert len(config_reads) <= 8, "Config resolution must be command-bound, not multiplied by 5,000 rows"
        assert batch.row_count == 16 and ProductSku.objects.count() == 16 and SkuAlias.objects.count() == 16
        assert not JournalBatch.objects.exists(), "The retained manifest writer remains the only child writer"


def _next_reviewed_snapshot(world: TenantWorld, proof: Any, barcode: str) -> SohImport:
    raw = workbook([["Shirt", "Old brand text", "M", barcode, "Shirt", "Legacy season", 2, 1000, 500, 1000]])
    metadata, rows = parse_soh(raw)
    evidence = stage_upload(proof.manager.principal(), command_id=uuid.uuid4(), data=raw, filename="proof-next-snapshot.xlsx", kind="manifest",
        scope={"scope_kind": "sites", "site_ids": [world.sites[0].pk], "brand_ids": [], "sensitive_fields": ["cost", "financial"]}, expected_sha256=sha256_hex(raw), contains_fields=["cost", "financial"])
    source: SohImport = command(proof.manager, soh_services.STAGE, lambda run: soh_services.create_source(run, site_id=world.sites[0].pk, evidence=evidence, metadata=metadata, parsed=rows))
    source = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.claim_preparation(run, proof.warehouse, source, source.revision))
    configuration = {**proof.source.configuration, "cutoff_at": database_now().isoformat()}
    source = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.prepare(run, proof.warehouse, source, configuration, source.revision))
    source = command(proof.manager, soh_services.STAGE, lambda run: soh_services.verify_rows(run, proof.manager, source,
        [{"barcode": barcode, "observed_qty": 2, "observed_condition": "good", "reason": "Actual count in this isolated proof"}], source.revision))
    source = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.submit(run, proof.warehouse, source, source.revision))
    assert source.approval_request is not None
    approve(proof.owner, source.approval_request)
    source.refresh_from_db()
    return source


def test_later_full_snapshot_cannot_duplicate_opening_or_disable_established_reconciliation(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = opened_goods(world, *actors(world))
        original = soh_services.opening_reconciliation(world.sites[0])
        assert original["passed"] and original["quantity"] == 3
        source = _next_reviewed_snapshot(world, proof, "NEW-SNAPSHOT-ITEM")
        assert soh_services.is_reconciled(world.sites[0]), "Staging later evidence alone must not revoke the established opening"
        assert "apply" not in source_dto(proof.warehouse, source)["allowed_actions"]
        with pytest.raises(Refusal) as refused:
            command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, source, 1))
        assert refused.value.code == "SOH_RECONCILIATION_REQUIRED"
        assert SohImportBatch.objects.count() == 1 and ProductSku.objects.count() == 1 and SkuAlias.objects.count() == 1
        assert JournalBatch.objects.filter(posting_kind="P05").count() == 1
        assert JournalBatch.objects.filter(posting_kind="P09").count() == 1
        assert soh_services.opening_reconciliation(world.sites[0]) == original
        source.refresh_from_db()
        assert source.state == "approved" and not source.batches.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_snapshot_application_selects_one_initial_parent(worlds: tuple[TenantWorld, TenantWorld], disposable_transactional_flush: None) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        people = actors(world)
        proof = reviewed_source(world, *people)
        approve(proof.owner, proof.source.approval_request)
        proof.source.refresh_from_db()
        other_user, other_human = _person(world, "other-initial-source-preparer")
        _assign(world, other_human, "it_admin", all_sites=True, all_brands=True)
        _assign(world, other_human, "warehouse", sites=(world.sites[0],), all_brands=True)
        other_proof = SimpleNamespace(**{**vars(proof), "warehouse": live_access(other_user)})
        other = _next_reviewed_snapshot(world, other_proof, "CONCURRENT-OTHER")
        source_ids = (proof.source.pk, other.pk)
        preparers = {proof.source.pk: people[1], other.pk: other_user}
    barrier = threading.Barrier(2)
    def apply(source_id: uuid.UUID) -> str:
        close_old_connections()
        try:
            with tenant_context(world.tenant.pk):
                access = live_access(preparers[source_id])
                source = SohImport.objects.get(pk=source_id)
                barrier.wait(timeout=10)
                try:
                    command(access, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, access, source, 1))
                except Refusal as refused:
                    return refused.code
                return "APPLIED"
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(apply, source_ids))
    with tenant_context(world.tenant.pk):
        assert sorted(results) == ["APPLIED", "SOH_RECONCILIATION_REQUIRED"]
        assert SohImportBatch.objects.count() == 1 and ProductSku.objects.count() == 1 and SkuAlias.objects.count() == 1
        assert not JournalBatch.objects.exists(), "Only one initial parent is materialised; neither source silently posts stock"


def test_exact_source_posts_once_through_opt_and_acceptance_and_reconciles(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = opened_goods(world, *actors(world))
        result = soh_services.opening_reconciliation(world.sites[0])
        assert result["passed"] is True and result["quantity"] == 3
        assert proof.source.state == "applied"
        assert JournalBatch.objects.filter(posting_kind="P05").count() == 1
        assert JournalBatch.objects.filter(posting_kind="P09").count() == 1
        replay = command(proof.manager, "stock.accept", lambda run: goods_acceptance.scan(run,
            proof.acceptance_session.pk, proof.acceptance_scans, proof.acceptance_session.revision))
        assert replay.pk == proof.acceptance_session.pk
        assert JournalBatch.objects.filter(posting_kind="P09").count() == 1


def test_source_input_revision_change_cannot_use_previous_approval(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = reviewed_source(world, *actors(world))
        row = proof.source.rows.get()
        row.quantity += 1
        row.save(update_fields=["quantity"])
        with pytest.raises(Refusal, match="changed after review"):
            approve(proof.owner, proof.source.approval_request)
        assert not SohImportBatch.objects.exists() and not JournalBatch.objects.exists()


def test_withdrawn_profile_refuses_unapplied_source_without_catalogue_or_stock_changes(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = reviewed_source(world, *actors(world))
        approve(proof.owner, proof.source.approval_request)
        proof.source.refresh_from_db()
        proof.owner.require_action("config.approve")
        command(proof.owner, "config.withdraw", lambda run: withdraw_config(run, proof.profile, reason_code="PROOF_INVALID_PROFILE"))
        with pytest.raises(Refusal, match="not currently effective"):
            command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 1))
        assert not SohImportBatch.objects.exists() and not ProductSku.objects.exists()
        assert not JournalBatch.objects.exists()


def test_interrupted_child_batch_rolls_back_partial_catalogue_and_can_resume(worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        rows = [["Shirt", "Old brand text", "M", f"00012{index}", "Shirt", "Legacy season", 1, 1000, 500, 500] for index in range(3)]
        proof = reviewed_source(world, *actors(world), rows=rows)
        approve(proof.owner, proof.source.approval_request)
        proof.source.refresh_from_db()
        monkeypatch.setattr(soh_services, "MAX_BATCH", 2)
        first = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 1))
        assert first.row_count == 2 and first.quantity == 2
        original_writer = goods_manifest_services.create_manifest
        def interrupted(run: CommandRun, **kwargs: Any) -> None:
            raise RuntimeError("Proof interruption after catalogue preparation, before manifest commit")
        monkeypatch.setattr(goods_manifest_services, "create_manifest", interrupted)
        with pytest.raises(RuntimeError, match="Proof interruption"):
            command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 2))
        assert SohImportBatch.objects.count() == 1 and ProductSku.objects.count() == 2 and SkuAlias.objects.count() == 2
        monkeypatch.setattr(goods_manifest_services, "create_manifest", original_writer)
        resumed = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 2))
        replay = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 1))
        proof.source.refresh_from_db()
        assert resumed.row_count == 1 and replay.pk == first.pk and proof.source.state == "applied"
        assert SohImportBatch.objects.count() == 2 and ProductSku.objects.count() == 3 and SkuAlias.objects.count() == 3
        assert not JournalBatch.objects.exists(), "Only retained OPT approval and acceptance may post inventory"


def test_unmapped_staged_source_remains_inactive_and_can_be_independently_withdrawn(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = reviewed_source(world, *actors(world))
        assert not soh_services.is_reconciled(world.sites[0])
        with pytest.raises(Refusal):
            command(proof.manager, soh_services.APPROVE, lambda run: soh_services.withdraw(run, proof.manager, proof.source, "Superseded", proof.source.revision))
        withdrawn = command(proof.owner, soh_services.APPROVE, lambda run: soh_services.withdraw(run, proof.owner, proof.source, "Fresh source replaces this abandoned draft", proof.source.revision))
        assert withdrawn.state == "withdrawn" and withdrawn.reviews.count() >= 2


def test_mixed_assignment_fields_and_scope_do_not_grant_source_valuation(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = reviewed_source(world, *actors(world))
        wrong, human = _person(world, f"wrong-{uuid.uuid4().hex[:4]}")
        _assign(world, human, "warehouse", sites=(world.sites[1],), all_brands=True)
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        from tests.first_store_goods import live_access
        projected = source_dto(live_access(wrong), proof.source)
        assert not projected["data"]["field_access"]["readable_fields"]
