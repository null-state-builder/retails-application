"""Actual blind captures, independent review and original-layer count correction.

These tests use only the isolated proof DB and synthetic three-unit shop fixture.
Every mutation enters its mounted handler and command kernel.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.sessions import revoke_session
from approvals.goods_models import ApprovalRequest
from approvals.goods_views import GoodsApprovalDecideView
from core.commands import database_now
from core.fiscal import financial_year
from core.numbering import prepare_series
from masters.goods_config import withdraw
from masters.goods_models import SiteGuard
from outbound.goods_count_views import (
    CountPassScanView, CountPassSubmitView, StocktakeCloseView, StocktakeDetailView,
    StocktakeListCreateView, StocktakePassOpenView, StocktakeSubmitReviewView, StocktakeVarianceView,
)
from outbound.goods_models import CountDecision, GoodsStocktake
from sell.services.goods_stock import read_shelf
from sell.services.till_authority import TillError, issue_allocation, release_allocation, resume_till
from sell.services.working_set import current_version
from stockledger.goods_models import JournalBatch, Origin, QuantityLeg, ValueLeg
from tests.first_store_goods import _publish_tenant_config, command, live_access
from tests.test_first_store_goods_operations import post, wire
from tests.test_first_store_online import online_goods as online_goods
from tests.test_so03_denials import _assign, _person
from tests.test_so03_denials import worlds as worlds


def test_approval_inbox_rechecks_session_before_protected_delivery(online_goods: Any, monkeypatch: Any) -> None:
    proof = setup(online_goods)
    row, report = captured(proof, 2)
    assert submit(proof, row, report).status_code == 200
    from accounts.principal import AccessContext
    from approvals.goods_views import GoodsApprovalInboxView
    original = AccessContext.revalidate_delivery

    def withdraw(access: AccessContext) -> None:
        revoke_session(proof.checker.session)
        original(access)

    monkeypatch.setattr(AccessContext, "revalidate_delivery", withdraw)
    request = APIRequestFactory().get("/proof/count-approval-inbox")
    force_authenticate(request, proof.checker.user, proof.checker.session)
    response = GoodsApprovalInboxView.as_view()(request)
    assert response.status_code == 401 and "reconciliation" not in str(response.data)


def get(view: Any, actor: Any, pk: uuid.UUID) -> Any:
    request = APIRequestFactory().get("/proof/trading-count")
    force_authenticate(request, actor.user, actor.session)
    return view.as_view()(request, pk=pk)


def setup(proof: Any, *, pause: bool = True) -> Any:
    site = proof.world.sites[0]
    if pause:
        allocation = issue_allocation(proof.till, current_version(site.pk), {"fixture": "Blind count real persisted pause"})
        release_allocation(site, allocation.version, proof.manager.user, "Independent physical count", financial_year(), 1)
    for kind in ("CNT", "HLD", "REL"):
        prepare_series(proof.world.tenant.pk, site.gstin.legal_entity, kind)
    _publish_tenant_config(proof.world.tenant, proof.owner.human_id, kind="reasons",
        payload={"action": "count.run", "codes": [{"code": "SOURCE_COUNT", "label": "Reviewed physical difference", "retired": False}]}, label="blind-count-reason")
    proof.policy = _publish_tenant_config(proof.world.tenant, proof.owner.human_id, kind="approval",
        payload={"action": "count.review", "roles": ["owner"], "require_distinct": True, "site_ids": [], "brand_ids": [],
                 "qty_max": 100, "value_max": "10000000", "step_up": True, "unknown_value": "refuse"}, label="blind-count-review")
    user, human = _person(proof.world, "independent-count-checker")
    _assign(proof.world, human, "owner", all_sites=True, all_brands=True)
    proof.checker = live_access(user)
    return proof


def start(proof: Any) -> Any:
    return post(StocktakeListCreateView, proof.manager, wire(site_id=proof.world.sites[0].pk, scope={"kind": "site", "count_kind": "full"}))


def captured(proof: Any, qty: int) -> tuple[GoodsStocktake, dict[str, Any]]:
    response = start(proof)
    assert response.status_code == 201, response.data
    row = GoodsStocktake.objects.get(pk=response.data["id"])
    assert row.till_pause_evidence and row.non_trading_event_id is None
    opened = post(StocktakePassOpenView, proof.manager, wire(), pk=row.pk)
    assert opened.status_code in (200, 201), opened.data
    body = opened.data
    assert "book_qty" not in str(body) and "removed_value_paise" not in str(body)
    if qty:
        scanned = post(CountPassScanView, proof.manager, wire(expected_revision=body["revision"], observations=[{
            "scan_key": str(uuid.uuid4()), "sku_id": str(proof.sku_id), "alias_value": proof.barcode,
            "location_id": str(proof.location.pk), "qty": qty, "condition": "good", "actual_at": database_now().isoformat(),
        }]), pk=uuid.UUID(body["id"]))
        assert scanned.status_code == 200, scanned.data
        body = scanned.data
    submitted = post(CountPassSubmitView, proof.manager, wire(expected_revision=body["revision"],
        reviewed_hash=body["observation_hash"], scope_complete=True), pk=uuid.UUID(body["id"]))
    assert submitted.status_code == 200, submitted.data
    report = get(StocktakeVarianceView, proof.owner, row.pk)
    assert report.status_code == 200 and report.data["complete"], report.data
    row.refresh_from_db()
    return row, report.data


def submit(proof: Any, row: GoodsStocktake, report: dict[str, Any]) -> Any:
    return post(StocktakeSubmitReviewView, proof.owner, wire(expected_revision=row.revision,
        reviewed_hash=report["variance_hash"], selected_pass_ids=report["selected_pass_ids"], reason_code="SOURCE_COUNT"), pk=row.pk)


def decision(request: ApprovalRequest) -> dict[str, Any]:
    return wire(expected_revision=request.revision, reviewed_hash=request.reviewed_hash, decision="approve")


def pending(row: GoodsStocktake) -> ApprovalRequest:
    return ApprovalRequest.objects.get(subject_kind="count", subject_key=str(row.pk), state="pending")


@pytest.mark.parametrize("qty", [0, 2, 3])
def test_independent_closure_posts_only_exact_shortage_once(online_goods: Any, qty: int) -> None:
    proof = setup(online_goods)
    before = JournalBatch.objects.count(), Origin.objects.count()
    row, report = captured(proof, qty)
    with pytest.raises(TillError) as frozen:
        resume_till(proof.world.sites[0], proof.manager.user)
    assert frozen.value.code == "UNDER_COUNT"
    if qty == 3:
        direct = post(StocktakeCloseView, proof.owner, wire(expected_revision=row.revision,
            reviewed_hash=report["variance_hash"], selected_pass_ids=report["selected_pass_ids"]), pk=row.pk)
        assert direct.status_code == 409 and direct.data["code"] == "APPROVAL_REQUIRED", direct.data
    response = submit(proof, row, report)
    assert response.status_code == 200, response.data
    request = pending(row)
    before_review = get(StocktakeDetailView, proof.checker, row.pk)
    assert before_review.data["approval"]["removed_qty"] == 3 - qty
    assert before_review.data["approval"]["removed_value_paise"] == str((3 - qty) * 50000)
    assert {"approve", "reject", "variance"} <= set(before_review.data["allowed_actions"])
    self_review = post(GoodsApprovalDecideView, proof.owner, decision(request), pk=request.pk)
    assert self_review.status_code in (403, 422) and self_review.data["code"] == "SELF_APPROVAL", self_review.data
    body = decision(request)
    approved = post(GoodsApprovalDecideView, proof.checker, body, pk=request.pk)
    assert approved.status_code == 200, approved.data
    row.refresh_from_db()
    assert row.state == "closed" and CountDecision.objects.filter(stocktake=row).count() == 1
    assert SiteGuard.objects.get(site=proof.world.sites[0]).freeze_id is None
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == qty
    assert Origin.objects.count() == before[1]
    assert JournalBatch.objects.count() == before[0] + (qty != 3)
    if qty != 3:
        batch = JournalBatch.objects.get(posting_kind="P13")
        assert sum(QuantityLeg.objects.filter(batch=batch, side="source").values_list("qty", flat=True)) == qty - 3
        assert sum(ValueLeg.objects.filter(batch=batch, side="source").values_list("amount", flat=True)) == (qty - 3) * 50000
    after = JournalBatch.objects.count()
    assert post(GoodsApprovalDecideView, proof.checker, body, pk=request.pk).status_code == 200
    assert JournalBatch.objects.count() == after and CountDecision.objects.filter(stocktake=row).count() == 1
    assert resume_till(proof.world.sites[0], proof.manager.user) is not None


def test_unpaused_store_cannot_start_trading_count(online_goods: Any) -> None:
    proof = setup(online_goods, pause=False)
    before = JournalBatch.objects.count()
    response = start(proof)
    assert response.status_code == 409, response.data
    assert not GoodsStocktake.objects.exists() and JournalBatch.objects.count() == before
    assert SiteGuard.objects.get(site=proof.world.sites[0]).freeze_id is None


def test_found_gains_remain_inactive_with_evidence_and_freeze(online_goods: Any) -> None:
    proof = setup(online_goods)
    row, report = captured(proof, 4)
    before = JournalBatch.objects.count()
    response = submit(proof, row, report)
    assert response.status_code == 422 and response.data["code"] == "COUNT_GAIN_PENDING", response.data
    row.refresh_from_db()
    assert row.state == "open" and not ApprovalRequest.objects.filter(subject_kind="count").exists()
    assert JournalBatch.objects.count() == before and SiteGuard.objects.get(site=proof.world.sites[0]).freeze_id == row.pk


@pytest.mark.parametrize("case", ["withdrawn_policy", "revoked_session", "fields", "mixed_assignments", "wrong_site"])
def test_stale_or_incomplete_authority_cannot_close_or_post(online_goods: Any, case: str) -> None:
    proof = setup(online_goods)
    row, report = captured(proof, 2)
    assert submit(proof, row, report).status_code == 200
    request = pending(row)
    actor = proof.checker
    if case == "withdrawn_policy":
        command(proof.owner, "proof.withdraw", lambda run: withdraw(run, proof.policy, reason_code="REVIEWED_WITHDRAWAL"))
    elif case == "revoked_session":
        revoke_session(actor.session)
    else:
        user, human = _person(proof.world, "incomplete-count-checker")
        if case == "wrong_site":
            _assign(proof.world, human, "owner", sites=(proof.world.sites[1],), all_brands=True)
        else:
            role = proof.world.roles["owner"]
            role.field_access = [field for field in role.field_access if field != "cost"]
            role.save(update_fields=["field_access"])
            _assign(proof.world, human, "owner", all_sites=True, all_brands=True)
            if case == "mixed_assignments":
                _assign(proof.world, human, "accounts", all_sites=True, all_brands=True)
        actor = live_access(user)
    before = JournalBatch.objects.count()
    response = post(GoodsApprovalDecideView, actor, decision(request), pk=request.pk)
    assert response.status_code in (401, 403, 404, 409, 422), response.data
    row.refresh_from_db()
    assert row.state == "review" and JournalBatch.objects.count() == before
    assert SiteGuard.objects.get(site=proof.world.sites[0]).freeze_id == row.pk


def test_delivery_revocation_withholds_review_cost_and_variance(online_goods: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from outbound import goods_count_reads

    proof = setup(online_goods)
    row, report = captured(proof, 2)
    assert submit(proof, row, report).status_code == 200
    original = goods_count_reads.count_detail
    def late_revoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
        body = original(*args, **kwargs)
        assert body["approval"]["removed_value_paise"] == "50000"
        revoke_session(proof.checker.session)
        return body
    monkeypatch.setattr(goods_count_reads, "count_detail", late_revoke)
    response = get(StocktakeDetailView, proof.checker, row.pk)
    assert response.status_code == 401, response.data
    assert "removed_value_paise" not in response.data and "passes" not in response.data
    row.refresh_from_db()
    assert row.state == "review"


def test_damage_after_matching_capture_refuses_independent_closure(online_goods: Any) -> None:
    from outbound.goods_views import MarkDamagedView
    from tests.test_first_store_goods_operations import damage_wire

    proof = setup(online_goods)
    row, report = captured(proof, 3)
    assert submit(proof, row, report).status_code == 200
    request = pending(row)
    marked = post(MarkDamagedView, proof.manager, damage_wire(proof))
    assert marked.status_code == 201, marked.data
    before = JournalBatch.objects.count()
    response = post(GoodsApprovalDecideView, proof.checker, decision(request), pk=request.pk)
    assert response.status_code == 422 and response.data["details"]["domain_code"] == "COUNT_SNAPSHOT_STALE", response.data
    assert JournalBatch.objects.count() == before and not CountDecision.objects.exists()
    row.refresh_from_db()
    assert row.state == "review" and SiteGuard.objects.get(site=proof.world.sites[0]).freeze_id == row.pk
