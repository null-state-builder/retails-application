"""C07 exact route, scope, field and downstream boundary proof on actual goods."""
from __future__ import annotations

from typing import Any

import pytest

from approvals.goods_models import ApprovalRequest
from masters.goods_config import withdraw
from masters.goods_models import ConfigVersion
from outbound.goods_models import GoodsTransfer
from outbound.goods_transfer_views import TransferApproveView, TransferDispatchView, TransferListCreateView, TransferSubmitView
from outbound.transfer_authority import request_for
from stockledger.goods_models import ActiveReservation
from tests.first_store_goods import _publish_tenant_config, command, live_access
from tests.test_first_store_goods_operations import (
    approval_wire, dispatch_wire, operational_goods as operational_goods,
    post, receiving_goods as receiving_goods, submitted_transfer, transfer_wire, truth, wire,
)
from tests.test_so03_denials import _assign, _person
from tests.test_so03_denials import worlds as worlds


def replace_policy(proof: Any, **changes: Any) -> ConfigVersion:
    for old in ConfigVersion.objects.filter(kind="approval", payload__action="pt.approve.transfer"):
        def change(run: Any, old: ConfigVersion = old) -> Any:
            return withdraw(run, old, reason_code="PROOF_REVIEWED_REPLACEMENT")
        command(proof.owner, "proof.policy.withdraw", change)
    payload = {"action": "pt.approve.transfer", "roles": ["owner"], "site_ids": [], "brand_ids": [],
        "require_distinct": True, "qty_max": 100, "value_max": "10000000", "step_up": True, "unknown_value": "refuse", **changes}
    return _publish_tenant_config(proof.world.tenant, proof.owner.human_id, kind="approval", payload=payload, label="Reviewed proof transfer route")


def test_two_step_route_reserves_only_after_separate_final_checker(operational_goods: Any) -> None:
    proof = operational_goods
    policy = replace_policy(proof, steps=[{"label": "Stock review", "roles": ["owner"]}, {"label": "Final authorisation", "roles": ["owner"]}])
    transfer = submitted_transfer(proof)
    request = request_for(transfer)
    assert request.policy_version_id == policy.pk
    assert len(request.policy_basis["cells"]) == 2 and request.policy_basis["fields"] == ["cost"]
    before = truth()
    body = approval_wire(transfer)
    first = post(TransferApproveView, proof.owner, body, pk=transfer.pk)
    assert first.status_code == 200, first.data
    request.refresh_from_db()
    transfer.refresh_from_db()
    assert request.state == "pending" and transfer.state == "submitted" and truth() == before
    assert request.decisions.get().outcome == "step_approved"
    # Lost response replay returns the step already committed; no second step.
    assert post(TransferApproveView, proof.owner, body, pk=transfer.pk).status_code == 200
    assert request.decisions.count() == 1
    second = post(TransferApproveView, proof.owner, approval_wire(transfer), pk=transfer.pk)
    assert second.status_code == 403 and second.data["code"] == "SELF_APPROVAL", second.data
    other, human = _person(proof.world, "independent-final-transfer")
    _assign(proof.world, human, "owner", all_sites=True, all_brands=True)
    result = post(TransferApproveView, live_access(other), approval_wire(transfer), pk=transfer.pk)
    assert result.status_code == 200, result.data
    request.refresh_from_db()
    transfer.refresh_from_db()
    assert request.state == "approved" and transfer.state == "approved"
    assert request.decisions.filter(outcome="approved").count() == 1
    assert ActiveReservation.objects.filter(transfer_version__document__goods_pt__transfer=transfer).exists()


@pytest.mark.parametrize("case", ["quantity", "value", "step_limit"])
def test_limits_refuse_submission_without_new_pt_or_journal(operational_goods: Any, case: str) -> None:
    proof = operational_goods
    changes: dict[str, Any] = {"qty_max": 1} if case == "quantity" else {"value_max": "99999"} if case == "value" else {
        "steps": [{"label": "Limited review", "roles": ["owner"], "qty_max": 1}]}
    replace_policy(proof, **changes)
    created = post(TransferListCreateView, proof.manager, transfer_wire(proof),)
    assert created.status_code == 201
    transfer = GoodsTransfer.objects.get(pk=created.data["id"])
    before = truth()
    response = post(TransferSubmitView, proof.manager, wire(), pk=transfer.pk)
    assert response.status_code == 422 and response.data["code"] == "APPROVAL_POLICY_BLOCKED", response.data
    transfer.refresh_from_db()
    assert transfer.state == "draft" and truth() == before
    assert not ApprovalRequest.objects.filter(subject_key=str(transfer.pk)).exists()


@pytest.mark.parametrize("case", ["destination", "field", "mixed_assignments"])
def test_review_needs_whole_cells_and_fields_on_qualifying_assignments(operational_goods: Any, case: str) -> None:
    proof = operational_goods
    transfer = submitted_transfer(proof)
    actor, human = _person(proof.world, "restricted-transfer-checker")
    if case == "destination":
        _assign(proof.world, human, "owner", sites=(proof.world.sites[0],), all_brands=True)
    else:
        role = proof.world.roles["owner"]
        role.field_access = [field for field in role.field_access if field != "cost"]
        role.save(update_fields=["field_access"])
        _assign(proof.world, human, "owner", all_sites=True, all_brands=True)
        if case == "mixed_assignments":
            _assign(proof.world, human, "accounts", all_sites=True, all_brands=True)
    before = truth()
    result = post(TransferApproveView, live_access(actor), approval_wire(transfer), pk=transfer.pk)
    assert result.status_code == 403 and result.data["code"] == "ACTION_DENIED", result.data
    assert truth() == before


def test_policy_replacement_stales_decision_and_explicit_resubmission_preserves_evidence(operational_goods: Any) -> None:
    proof = operational_goods
    transfer = submitted_transfer(proof)
    old = request_for(transfer)
    replace_policy(proof, qty_max=2)
    before = truth()
    stale = post(TransferApproveView, proof.owner, approval_wire(transfer), pk=transfer.pk)
    assert stale.status_code == 409 and stale.data["code"] == "APPROVAL_STALE", stale.data
    assert truth() == before
    assert post(TransferSubmitView, proof.manager, wire(), pk=transfer.pk).status_code == 200
    old.refresh_from_db()
    current = request_for(transfer)
    assert old.state == "superseded" and old.pk != current.pk and old.reviewed_hash == current.reviewed_hash
    assert post(TransferApproveView, proof.owner, approval_wire(transfer), pk=transfer.pk).status_code == 200


def test_withdrawn_policy_denies_dispatch_without_consuming_reservations(operational_goods: Any) -> None:
    proof = operational_goods
    transfer = submitted_transfer(proof)
    body, _ = dispatch_wire(proof, transfer)
    request = request_for(transfer)
    assert request.policy_version is not None
    policy = request.policy_version
    command(proof.owner, "proof.withdraw", lambda run: withdraw(run, policy, reason_code="PROOF_WITHDRAWN"))
    before = truth()
    response = post(TransferDispatchView, proof.manager, body, pk=transfer.pk)
    assert response.status_code == 409 and response.data["code"] == "APPROVAL_STALE", response.data
    assert truth() == before


def test_unpinned_historical_request_cannot_be_approved(operational_goods: Any) -> None:
    proof = operational_goods
    transfer = submitted_transfer(proof)
    request = request_for(transfer)
    body = approval_wire(transfer)
    request.policy_basis = {}
    request.save(update_fields=["policy_basis"])
    result = post(TransferApproveView, proof.owner, body, pk=transfer.pk)
    assert result.status_code == 409 and result.data["code"] == "APPROVAL_STALE", result.data
