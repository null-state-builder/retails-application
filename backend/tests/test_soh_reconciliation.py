"""C08 repeat SOH: actual approved sources, canonical stock delta, paused till."""

from __future__ import annotations

import copy
import uuid
from typing import Any, cast

import pytest
from django.utils import timezone

from core.canonical import sha256_hex
from core.fiscal import financial_year
from core.numbering import prepare_series
from core.commands import CommandRun, LockRank
from core.refusals import Refusal
from files.goods_services import stage_upload
from masters.goods_models import SiteGuard
from masters.goods_config import withdraw
from outbound import goods_soh_reconciliation as counts
from outbound.goods_soh_models import SohReconciliation, SohReconciliationEvidence
from outbound.goods_soh_views import dto
from ptmapper import soh_services
from ptmapper.soh_parser import parse_soh
from sell.models import Sale
from sell.services.goods_stock import read_shelf
from sell.services.till_authority import issue_allocation, release_allocation
from sell.services.working_set import current_version
from stockledger.goods_models import JournalBatch, Origin, QuantityLeg, ValueLeg
from stockledger import goods_engine as engine
from tests.first_store_goods import _publish_tenant_config, approve, command, live_access, source_cutoff, workbook
from tests.test_first_store_online import online_goods as online_goods
from tests.test_so03_denials import worlds as worlds
from tests.test_so03_denials import _assign, _person

DECLARATIONS = {"full_store_export": True, "whole_store_physically_counted": True, "omissions_are_zero": True, "reason_code": "SOURCE_COUNT"}


def repeat_source(proof: Any, *, qty: int = 2, pause: bool = True, barcode: str = "000123") -> Any:
    """Fresh source uses real import/review commands after an actual numbered pause."""
    site = proof.world.sites[0]
    if pause:
        allocation = issue_allocation(proof.till, current_version(site.pk), {"fixture": "C08 actual writer pause"})
        last = Sale.objects.filter(store=site, fy=financial_year()).order_by("-till_seq").values_list("till_seq", flat=True).first()
        _released, proof.count_pause = release_allocation(site, allocation.version, proof.manager.user,
            "Fresh full-store physical SOH count", financial_year(), int(last or 0) + 1)
    _publish_tenant_config(proof.world.tenant, proof.owner.human_id, kind="reasons",
        payload={"action": "count.run", "codes": [{"code": "SOURCE_COUNT", "label": "Reviewed source and physical stock difference", "retired": False}]}, label="soh-count-reasons")
    _publish_tenant_config(proof.world.tenant, proof.owner.human_id, kind="approval",
        payload={"action": "count.review", "roles": ["owner"], "site_ids": [], "brand_ids": [], "require_distinct": True,
                 "qty_max": 100, "value_max": "10000000", "step_up": True, "unknown_value": "refuse"}, label="soh-count-policy")
    prepare_series(proof.world.tenant.pk, site.gstin.legal_entity, "CNT")
    cutoff = source_cutoff()
    raw = workbook([["Fresh counted shirt", "Old brand text", "M", barcode, "Shirt", "Legacy season", qty, 1000, 500, qty * 500]])
    evidence = stage_upload(proof.manager.principal(), command_id=uuid.uuid4(), data=raw, filename="fresh-full-store-soh.xlsx", kind="manifest",
        scope={"scope_kind": "sites", "site_ids": [site.pk], "brand_ids": [], "sensitive_fields": ["cost", "financial"]},
        expected_sha256=sha256_hex(raw), contains_fields=["cost", "financial"])
    metadata, parsed = parse_soh(raw)
    source = command(proof.manager, soh_services.STAGE, lambda run: soh_services.create_source(run, site_id=site.pk, evidence=evidence, metadata=metadata, parsed=parsed))
    source = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.claim_preparation(run, proof.warehouse, source, source.revision))
    config = copy.deepcopy(proof.config)
    config["cutoff_at"] = cutoff.isoformat()
    config["external_reconciliation_note"] = "This exact fresh export was physically counted after all tills paused. Existing layers keep their recorded value."
    source = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.prepare(run, proof.warehouse, source, config, source.revision))
    source = command(proof.manager, soh_services.STAGE, lambda run: soh_services.verify_rows(run, proof.manager, source,
        [{"barcode": barcode, "observed_qty": qty, "observed_condition": "good", "reason": "Actual fresh whole-store count"}], source.revision))
    source = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.submit(run, proof.warehouse, source, source.revision))
    approve(proof.owner, source.approval_request)
    source.refresh_from_db()
    return source


def start(proof: Any, source: Any) -> SohReconciliation:
    return cast(SohReconciliation, command(proof.manager, counts.RUN, lambda run: counts.begin(run, proof.manager, source, DECLARATIONS, source.revision)))


def submit_and_decide(proof: Any, row: SohReconciliation) -> SohReconciliation:
    row = command(proof.manager, counts.RUN, lambda run: counts.submit(run, proof.manager, row, row.revision, row.content_hash))
    assert row.approval_request is not None
    approve(proof.owner, row.approval_request)
    row.refresh_from_db()
    return row


def test_reduction_posts_precise_count_delta_and_preserves_opening_history(online_goods: Any) -> None:
    proof = online_goods
    source = repeat_source(proof, qty=2)
    before = JournalBatch.objects.count(), Origin.objects.count()
    row = start(proof, source)
    assert row.payload["differences"][0]["delta"] == -1 and not row.payload["problems"]
    assert dto(proof.manager, row)["data"]["review"] is None
    assert dto(proof.owner, row)["data"]["review"]["book"]["positions"]
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 0, "Freeze makes book unavailable to sale"
    row = submit_and_decide(proof, row)
    assert row.state == "closed" and row.journal_batch_id and row.official_version_id
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 2
    assert JournalBatch.objects.count() == before[0] + 1 and Origin.objects.count() == before[1]
    assert JournalBatch.objects.get(pk=row.journal_batch_id).posting_kind == "P13"
    assert sum(QuantityLeg.objects.filter(batch_id=row.journal_batch_id, side="source").values_list("qty", flat=True)) == -1
    assert sum(ValueLeg.objects.filter(batch_id=row.journal_batch_id, side="source").values_list("amount", flat=True)) == -50000
    assert proof.source.batches.count() == 1 and source.batches.count() == 0
    replay = start(proof, source)
    assert replay.pk == row.pk and JournalBatch.objects.count() == before[0] + 1
    assert SohReconciliationEvidence.objects.filter(reconciliation=row).count() == 3
    assert SiteGuard.objects.get(site=proof.world.sites[0]).freeze_id is None


def test_matching_snapshot_closes_with_no_inventory_or_value_posting(online_goods: Any) -> None:
    proof = online_goods
    source = repeat_source(proof, qty=3)
    before = JournalBatch.objects.count(), QuantityLeg.objects.count(), ValueLeg.objects.count()
    row = submit_and_decide(proof, start(proof, source))
    assert row.state == "closed" and row.journal_batch_id is None
    assert before == (JournalBatch.objects.count(), QuantityLeg.objects.count(), ValueLeg.objects.count())
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 3


def test_unpaused_and_gain_snapshots_stay_inactive_without_stock_change(online_goods: Any) -> None:
    proof = online_goods
    source = repeat_source(proof, qty=4, pause=False)
    before = JournalBatch.objects.count()
    with pytest.raises(Refusal):
        start(proof, source)
    assert not SohReconciliation.objects.exists() and SiteGuard.objects.get(site=proof.world.sites[0]).freeze_id is None
    allocation = issue_allocation(proof.till, current_version(proof.world.sites[0].pk), {"fixture": "late pause"})
    release_allocation(proof.world.sites[0], allocation.version, proof.manager.user, "Pause", financial_year(), 1)
    with pytest.raises(Refusal) as caught:
        start(proof, source)
    assert caught.value.code == "SOH_CUTOFF_STALE" and JournalBatch.objects.count() == before


def test_gain_requires_owned_found_custody_and_cancel_retains_evidence(online_goods: Any) -> None:
    proof = online_goods
    source = repeat_source(proof, qty=4)
    before = JournalBatch.objects.count()
    row = start(proof, source)
    assert {problem["code"] for problem in row.payload["problems"]} == {"GAIN_REQUIRES_FOUND_CUSTODY"}
    with pytest.raises(Refusal) as caught:
        command(proof.manager, counts.RUN, lambda run: counts.submit(run, proof.manager, row, row.revision, row.content_hash))
    assert caught.value.code == "SOH_VARIANCE_PENDING" and JournalBatch.objects.count() == before
    row = command(proof.manager, counts.RUN, lambda run: counts.cancel(run, proof.manager, row, row.revision, "Gain awaits owned custody/value evidence"))
    assert row.state == "cancelled" and row.evidence.count() == 2
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 3


def test_explicit_zero_and_full_store_omission_remove_original_portions(online_goods: Any) -> None:
    proof = online_goods
    source = repeat_source(proof, qty=0, barcode="UNSTOCKED_ZERO_CATALOGUE")
    row = start(proof, source)
    assert row.payload["differences"][0]["observed_qty"] == 0
    assert row.payload["differences"][0]["source_row_keys"] == []
    row = submit_and_decide(proof, row)
    assert row.state == "closed" and row.journal_batch_id
    assert not read_shelf(proof.world.sites[0]).quantities
    assert Origin.objects.get(pk=next(iter(Origin.objects.values_list("pk", flat=True)))).opening_qty == 3


@pytest.mark.parametrize("missing", ["full_store_export", "whole_store_physically_counted", "omissions_are_zero"])
def test_partial_or_unaffirmed_snapshot_never_installs_freeze(online_goods: Any, missing: str) -> None:
    proof = online_goods
    source = repeat_source(proof)
    declarations = {**DECLARATIONS, missing: False}
    before = JournalBatch.objects.count()
    with pytest.raises(Refusal) as caught:
        command(proof.manager, counts.RUN, lambda run: counts.begin(run, proof.manager, source, declarations, source.revision))
    assert caught.value.code == "INVALID_REQUEST"
    assert not SohReconciliation.objects.exists() and SiteGuard.objects.get(site=proof.world.sites[0]).freeze_id is None
    assert JournalBatch.objects.count() == before


def test_changed_source_after_freeze_refuses_and_cancellation_releases_it(online_goods: Any) -> None:
    proof = online_goods
    source = repeat_source(proof)
    row = start(proof, source)
    before = JournalBatch.objects.count()
    command(proof.manager, soh_services.STAGE, lambda run: soh_services.verify_rows(run, proof.manager, source,
        [{"barcode": proof.barcode, "observed_qty": 2, "observed_condition": "good", "reason": "Physical recount supersedes prior evidence"}], source.revision))
    with pytest.raises(Refusal) as caught:
        command(proof.manager, counts.RUN, lambda run: counts.submit(run, proof.manager, row, row.revision, row.content_hash))
    assert caught.value.code == "REVISION_SUPERSEDED"
    assert JournalBatch.objects.count() == before and SiteGuard.objects.get(site=proof.world.sites[0]).freeze_id == row.pk
    row = command(proof.manager, counts.RUN, lambda run: counts.cancel(run, proof.manager, row, row.revision, "Cancel stale source revision"))
    assert row.state == "cancelled" and sum(read_shelf(proof.world.sites[0]).quantities.values()) == 3


def test_count_maker_and_wrong_scope_owner_cannot_review(online_goods: Any) -> None:
    proof = online_goods
    source = repeat_source(proof)
    row = start(proof, source)
    row = command(proof.manager, counts.RUN, lambda run: counts.submit(run, proof.manager, row, row.revision, row.content_hash))
    _assign(proof.world, proof.manager.user.human, "owner", all_sites=True, all_brands=True)
    maker_owner = live_access(proof.manager.user)
    assert dto(maker_owner, row)["data"]["review"] is None
    with pytest.raises(Refusal) as self_check:
        approve(maker_owner, row.approval_request)
    assert self_check.value.code == "SELF_APPROVAL"
    user, human = _person(proof.world, "wrong-site-count-owner")
    _assign(proof.world, human, "owner", sites=(proof.world.sites[1],), all_brands=True)
    with pytest.raises(Refusal):
        approve(live_access(user), row.approval_request)
    assert row.approval_request.decisions.count() == 0 and JournalBatch.objects.filter(posting_kind="P13").count() == 0


def test_withdrawn_count_policy_and_missing_step_up_cannot_move_stock(online_goods: Any) -> None:
    proof = online_goods
    source = repeat_source(proof)
    row = start(proof, source)
    row = command(proof.manager, counts.RUN, lambda run: counts.submit(run, proof.manager, row, row.revision, row.content_hash))
    assert proof.owner.session is not None
    proof.owner.session.step_up_at = None
    proof.owner.session.save(update_fields=["step_up_at"])
    with pytest.raises(Refusal) as no_confirmation:
        approve(live_access_without_confirmation(proof), row.approval_request)
    assert no_confirmation.value.code == "STEP_UP_REQUIRED"
    proof.owner.session.step_up_at = timezone.now()
    proof.owner.session.save(update_fields=["step_up_at"])
    version = row.approval_request.policy_version
    command(proof.owner, "config.withdraw", lambda run: withdraw(run, version, reason_code="PROOF_POLICY_WITHDRAWN"))
    with pytest.raises(Refusal) as stale:
        approve(proof.owner, row.approval_request)
    assert stale.value.code == "APPROVAL_STALE" and not JournalBatch.objects.filter(posting_kind="P13").exists()


def live_access_without_confirmation(proof: Any) -> Any:
    from accounts.principal import resolve_access
    from types import SimpleNamespace
    return resolve_access(SimpleNamespace(user=proof.owner.user, auth=proof.owner.session))


def test_encumbered_reduction_stays_owned_pending(online_goods: Any) -> None:
    proof = online_goods
    site = proof.world.sites[0]
    origin = Origin.objects.get()
    assert origin.official_line is not None
    origin_version_id = origin.official_line.version_id
    from stockledger.goods_models import Position
    position = Position.objects.get(boundary="physical", site=site)
    def hold(run: CommandRun) -> None:
        run.lock(LockRank.SITE, SiteGuard.objects.filter(site=site))
        engine.lock_lots(run, [position.lot_id])
        plan = engine.Plan("P07", origin_version_id, engine.event_key("proof_count_hold", uuid.uuid4()))
        engine.place_hold(run, plan, lot_id=position.lot_id, interval=(0, 1), hold_key=uuid.uuid4(), kind="Proof reviewed hold",
                          site_id=site.pk, source_version_id=origin_version_id)
        engine.post(run, proof.official_version, plan)
    command(proof.owner, "proof.count.hold", hold)
    source = repeat_source(proof)
    row = start(proof, source)
    assert "ENCUMBERED_REDUCTION" in {problem["code"] for problem in row.payload["problems"]}
    with pytest.raises(Refusal) as pending:
        command(proof.manager, counts.RUN, lambda run: counts.submit(run, proof.manager, row, row.revision, row.content_hash))
    assert pending.value.code == "SOH_VARIANCE_PENDING" and not JournalBatch.objects.filter(posting_kind="P13").exists()


def test_interruption_after_real_position_writer_rolls_back_and_resumes_once(online_goods: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    proof = online_goods
    source = repeat_source(proof)
    row = start(proof, source)
    row = command(proof.manager, counts.RUN, lambda run: counts.submit(run, proof.manager, row, row.revision, row.content_hash))
    before = JournalBatch.objects.count(), QuantityLeg.objects.count(), ValueLeg.objects.count()
    original = engine.end_positions
    def interrupted(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("Proof interrupt after real position delta before commit")
    monkeypatch.setattr(engine, "end_positions", interrupted)
    with pytest.raises(RuntimeError, match="Proof interrupt"):
        approve(proof.owner, row.approval_request)
    assert before == (JournalBatch.objects.count(), QuantityLeg.objects.count(), ValueLeg.objects.count())
    row.refresh_from_db()
    assert row.state == "submitted" and row.journal_batch_id is None
    monkeypatch.setattr(engine, "end_positions", original)
    approve(proof.owner, row.approval_request)
    row.refresh_from_db()
    assert row.state == "closed" and sum(read_shelf(proof.world.sites[0]).quantities.values()) == 2
    assert JournalBatch.objects.count() == before[0] + 1
