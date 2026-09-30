"""Actual online writer/pause contracts on the isolated proof database."""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.commands import CommandRun, LockRank
from core.fiscal import financial_year
from core.refusals import Refusal
from masters.goods_models import SiteGuard
from sell.models import OnlineSaleSubmission, Sale, TillAllocation, TillNumberBlock
from sell.services.goods_stock import read_shelf
from sell.services.invoice_numbers import till_numbering
from sell.services.till_authority import (
    TillError, issue_allocation, live_pause, register_till, release_allocation,
    renew_authority, resume_till, snapshot_pause_evidence,
)
from sell.views import OnlineFinaliseView, _with_till_allocation
from stockledger.goods_models import JournalBatch
from tests.first_store_goods import command
from tests.test_first_store_online import bill, online_goods as online_goods, post
from tests.test_so03_denials import worlds as worlds

pytestmark = pytest.mark.django_db


def pause(proof: Any, *, next_seq: int = 1) -> Any:
    from sell.services.dataset import build_dataset

    site = proof.world.sites[0]
    payload = _with_till_allocation(site, build_dataset(site, ""))
    _allocation, paused = release_allocation(site, payload["till"]["allocation_version"],
        proof.manager.user, "Full store snapshot proof", financial_year(), next_seq)
    return paused


def snapshot(proof: Any) -> dict[str, Any]:
    site = proof.world.sites[0]
    def read(run: CommandRun) -> dict[str, Any]:
        run.lock(LockRank.SITE, SiteGuard.objects.filter(site=site, tenant_id=run.tenant_id))
        return snapshot_pause_evidence(run, site)
    return dict(command(proof.owner, "proof.snapshot.pause", read))


def install_boundary_fixture(proof: Any) -> uuid.UUID:
    """Boundary-only fixture; this is not evidence of count/delta acceptance."""
    freeze_id = uuid.uuid4()
    site = proof.world.sites[0]
    def hold(run: CommandRun) -> None:
        guard = run.lock(LockRank.SITE, SiteGuard.objects.filter(site=site, tenant_id=run.tenant_id))[0]
        guard.freeze_id = freeze_id
        guard.save(update_fields=["freeze_id"])
    command(proof.owner, "proof.snapshot.freeze-boundary", hold)
    return freeze_id


def test_exact_actual_pause_pins_unused_numbers_without_cancelling_or_posting(online_goods: Any) -> None:
    proof = online_goods
    wire, _ = bill(proof)
    accepted = post(OnlineFinaliseView, proof, wire)
    assert accepted.status_code == 201, accepted.data
    paused = pause(proof, next_seq=2)
    before = (JournalBatch.objects.count(), TillNumberBlock.objects.count())
    evidence = snapshot(proof)
    assert str(evidence["pause_id"]) == str(paused.pk) and evidence["next_seq"] == 2
    assert len(evidence["hash"]) == 64 and len(evidence["open_number_blocks"]) == 2
    assert sum(len(row["used"]) for row in evidence["open_number_blocks"]) == 1
    assert snapshot(proof) == evidence
    assert before == (JournalBatch.objects.count(), TillNumberBlock.objects.count())
    assert not TillNumberBlock.objects.filter(closed_at__isnull=False).exists()
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 2


def test_pending_outcome_cannot_be_called_synced(online_goods: Any) -> None:
    proof = online_goods
    pause(proof)
    OnlineSaleSubmission.objects.create(tenant=proof.world.tenant, store=proof.world.sites[0],
        idempotency_uuid=uuid.uuid4(), payload_fingerprint="a" * 64)
    with pytest.raises(Refusal) as caught:
        snapshot(proof)
    assert caught.value.code == "SALE_OUTCOME_REQUIRED"
    assert not Sale.objects.exists()
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 3


@pytest.mark.parametrize("retained_allocation", [True, False])
def test_retired_counter_state_is_not_ignored(online_goods: Any, retained_allocation: bool) -> None:
    proof = online_goods
    site = proof.world.sites[0]
    if retained_allocation:
        issue_allocation(proof.till, 1, {})
    replacement, _token = register_till(site, proof.owner.user, replace=True, reason="Replace proof device")
    issue_allocation(replacement, 2, {})
    release_allocation(site, 2, proof.manager.user, "Reviewed current pause", financial_year(), 1)
    with pytest.raises(Refusal) as caught:
        snapshot(proof)
    assert caught.value.code == ("TILL_RECONCILIATION_REQUIRED" if retained_allocation else "NUMBER_RECONCILIATION_REQUIRED")
    assert not Sale.objects.exists()


@pytest.mark.parametrize("operation", ["resume", "renew", "register", "allocation", "numbers", "dataset"])
def test_frozen_boundary_refuses_counter_reactivation_and_new_allocations(online_goods: Any, operation: str) -> None:
    proof = online_goods
    site = proof.world.sites[0]
    paused = pause(proof)
    before = (TillAllocation.objects.count(), TillNumberBlock.objects.count(), JournalBatch.objects.count())
    install_boundary_fixture(proof)
    with pytest.raises((TillError, Refusal)) as caught:
        if operation == "resume":
            resume_till(site, proof.manager.user)
        elif operation == "renew":
            renew_authority(site)
        elif operation == "register":
            register_till(site, proof.owner.user, replace=True, reason="Cannot replace while counted")
        elif operation == "allocation":
            issue_allocation(proof.till, 2, {})
        elif operation == "numbers":
            till_numbering(site, proof.till, {})
        else:
            _with_till_allocation(site, {"working_set_version": 2})
    assert isinstance(caught.value, (TillError, Refusal)) and caught.value.code == "UNDER_COUNT"
    current_pause = live_pause(proof.till)
    assert current_pause is not None and current_pause.pk == paused.pk
    assert before == (TillAllocation.objects.count(), TillNumberBlock.objects.count(), JournalBatch.objects.count())


def test_frozen_boundary_keeps_exact_issued_replay_but_refuses_new_sale(online_goods: Any) -> None:
    proof = online_goods
    wire, _ = bill(proof)
    accepted = post(OnlineFinaliseView, proof, wire)
    assert accepted.status_code == 201, accepted.data
    next_wire, _ = bill(proof, seq=2)
    pause(proof, next_seq=2)
    paused_evidence = snapshot(proof)
    install_boundary_fixture(proof)
    before = (Sale.objects.count(), JournalBatch.objects.count())
    replay = post(OnlineFinaliseView, proof, wire)
    assert replay.status_code == 200 and replay.data == accepted.data
    refused = post(OnlineFinaliseView, proof, next_wire)
    assert refused.status_code == 409 and refused.data["code"] == "UNDER_COUNT", refused.data
    assert refused.data["not_issued"] is True
    assert before == (Sale.objects.count(), JournalBatch.objects.count())
    assert snapshot(proof) == paused_evidence
