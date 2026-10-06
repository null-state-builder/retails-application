"""Canonical shortage projection after direct and checked E119 decisions.

The original invoice/count comparison is evidence. Its undecided quantity must
come from the same accepted dispositions that the decision writer checks, even
when a direct decision has no approval draft in the history projection.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from approvals.goods_models import ApprovalRequest
from approvals.goods_views import GoodsApprovalDecideView
from core.commands import database_now
from inbound.goods_models import Disposition
from inbound.goods_views import (
    GoodsArrivalInvoiceView,
    GoodsArrivalListCreateView,
    GoodsArrivalSessionView,
    GoodsDispositionView,
    GoodsGrnDetailView,
    GoodsGrnListCreateView,
    GoodsObservationView,
)
from stockledger.goods_models import JournalBatch, Origin, Position, ValueLeg
from tests.first_store_goods import _publish_tenant_config, live_access
from tests.test_first_store_goods_operations import (
    operational_goods as operational_goods,
    post,
    receiving_goods as receiving_goods,
    shelf,
    wire,
)
from tests.test_so03_denials import _assign, _person
from tests.test_so03_denials import worlds as worlds
from vendors.models import Vendor


def detail(proof: Any, grn_id: uuid.UUID) -> dict[str, Any]:
    request = APIRequestFactory().get("/proof/grn-shortage")
    force_authenticate(request, proof.owner.user, proof.owner.session)
    response = GoodsGrnDetailView.as_view()(request, pk=grn_id)
    assert response.status_code == 200, response.data
    return dict(response.data)


def counted_shortage(proof: Any) -> tuple[uuid.UUID, str]:
    vendor = Vendor.objects.create(tenant=proof.world.tenant, code="shortage-proof", name="Fictional incoming vendor")
    arrival = post(GoodsArrivalListCreateView, proof.manager, wire(
        site_id=proof.world.sites[0].pk, brand_id=proof.world.brands[0].pk, vendor_id=vendor.pk,
        actual_arrival_at=database_now().isoformat(), transporter_ref="Fictional delivery"))
    assert arrival.status_code == 201, arrival.data
    arrival_id = uuid.UUID(arrival.data["id"])
    claim_key = str(uuid.uuid4())
    claim = post(GoodsArrivalInvoiceView, proof.manager, wire(
        expected_revision=arrival.data["revision"], invoice_number="FICTIONAL-4",
        invoice_date=database_now().date().isoformat(),
        lines=[{"line_key": claim_key, "sku_id": str(proof.sku_id),
                "description": "Fictional shirt", "claimed_qty": 4}]), pk=arrival_id)
    assert claim.status_code == 200, claim.data
    session = post(GoodsArrivalSessionView, proof.manager, wire(
        counter_id=str(proof.manager.human_id), entry_user_id=str(proof.manager.human_id)), pk=arrival_id)
    assert session.status_code == 201, session.data
    session_id = uuid.UUID(session.data["id"])
    observations = post(GoodsObservationView, proof.manager, wire(
        expected_revision=session.data["revision"], observations=[{
            "scan_key": str(uuid.uuid4()), "sku_id": str(proof.sku_id),
            "description": "Counted shirt", "alias_value": proof.barcode,
            "condition": "good", "qty": 3}]), pk=session_id)
    assert observations.status_code == 200, observations.data
    grn = post(GoodsGrnListCreateView, proof.manager, wire(
        expected_revision=observations.data["revision"], count_session_id=str(session_id),
        reviewed_hash=observations.data["content_hash"],
        remarks=[{"claim_line_key": claim_key, "remark": "One claimed unit was not delivered"}]))
    assert grn.status_code == 201, grn.data
    return uuid.UUID(grn.data["id"]), claim_key


def stock_truth(proof: Any) -> tuple[Any, ...]:
    return (
        shelf(proof),
        tuple(JournalBatch.objects.order_by("pk").values_list("pk", flat=True)),
        tuple(Origin.objects.order_by("pk").values_list("pk", flat=True)),
        tuple(Position.objects.order_by("pk").values_list("pk", "portion", "boundary")),
        tuple(ValueLeg.objects.order_by("pk").values_list("pk", flat=True)),
    )


def decision_body(proof: Any, grn_id: uuid.UUID, claim_key: str) -> dict[str, Any]:
    reviewed = detail(proof, grn_id)
    return wire(expected_revision=reviewed["revision"], kind="accept_shortage",
        source_document_id=str(grn_id), source_line_key=claim_key, qty=1, reason_code="SHORT",
        reviewed_grn_hash=reviewed["content_hash"])


def assert_comparison(proof: Any, grn_id: uuid.UUID, claim_key: str, remaining: int) -> dict[str, Any]:
    reloaded = detail(proof, grn_id)
    comparison, = reloaded["data"]["invoice_comparison"]
    assert comparison == {
        "claim_line_key": claim_key, "claimed_qty": 4, "counted_qty": 3, "difference": -1,
        "remaining_shortage_qty": remaining, "line_keys": comparison["line_keys"],
    }
    return reloaded


def test_direct_accepted_shortage_survives_reload_without_an_approval_draft(operational_goods: Any) -> None:
    proof = operational_goods
    grn_id, claim_key = counted_shortage(proof)
    assert_comparison(proof, grn_id, claim_key, 1)
    before = stock_truth(proof)
    body = decision_body(proof, grn_id, claim_key)
    denied = post(GoodsDispositionView, proof.manager, body, pk=grn_id)
    assert denied.status_code == 403 and denied.data["code"] == "ACTION_DENIED", denied.data
    assert stock_truth(proof) == before
    accepted = post(GoodsDispositionView, proof.owner, body, pk=grn_id)
    assert accepted.status_code == 200 and accepted.data["state"] == "recorded", accepted.data
    reloaded = assert_comparison(proof, grn_id, claim_key, 0)
    assert reloaded["data"]["dispositions"] == []  # Direct decisions have no ActionDraft.
    assert Disposition.objects.filter(document_id=grn_id, kind="accept_shortage", source_line_key=claim_key).count() == 1
    assert stock_truth(proof) == before
    assert post(GoodsDispositionView, proof.owner, body, pk=grn_id).status_code == 200
    assert_comparison(proof, grn_id, claim_key, 0)
    stale = post(GoodsDispositionView, proof.owner, decision_body(proof, grn_id, claim_key), pk=grn_id)
    assert stale.status_code == 409 and stale.data["code"] == "DISPOSITION_STALE", stale.data
    assert stock_truth(proof) == before
    assert Disposition.objects.filter(document_id=grn_id, kind="accept_shortage", source_line_key=claim_key).count() == 1


@pytest.mark.parametrize("decision,remaining,state", [("approve", 0, "approved"), ("reject", 1, "rejected")])
def test_checked_shortage_projects_real_pending_and_decided_states(
    operational_goods: Any, decision: str, remaining: int, state: str,
) -> None:
    proof = operational_goods
    _publish_tenant_config(proof.world.tenant, proof.owner.human_id, kind="approval", payload={
        "action": "receipt.disposition.decide", "roles": ["owner"], "site_ids": [], "brand_ids": [],
        "require_distinct": True, "qty_max": 100, "step_up": True, "unknown_value": "quantity_only",
    }, label="fictional-shortage-review")
    checker_user, checker_human = _person(proof.world, "shortage-checker")
    _assign(proof.world, checker_human, "owner", all_sites=True, all_brands=True)
    checker = live_access(checker_user)
    grn_id, claim_key = counted_shortage(proof)
    before = stock_truth(proof)
    body = decision_body(proof, grn_id, claim_key)
    requested = post(GoodsDispositionView, proof.owner, body, pk=grn_id)
    assert requested.status_code == 200 and requested.data["state"] == "approval_pending", requested.data
    pending = assert_comparison(proof, grn_id, claim_key, 1)
    row, = pending["data"]["dispositions"]
    assert row["kind"] == "accept_shortage" and row["state"] == "pending" and row["qty"] == 1
    approval = ApprovalRequest.objects.get(pk=row["approval_request_id"])
    review = wire(expected_revision=approval.revision, decision=decision,
        reviewed_hash=approval.reviewed_hash, reason_code="PHYSICAL_REVIEW")
    own = post(GoodsApprovalDecideView, proof.owner, review, pk=approval.pk)
    assert own.status_code == 403 and own.data["code"] == "SELF_APPROVAL", own.data
    assert stock_truth(proof) == before
    result = post(GoodsApprovalDecideView, checker, review, pk=approval.pk)
    assert result.status_code == 200, result.data
    reloaded = assert_comparison(proof, grn_id, claim_key, remaining)
    history, = reloaded["data"]["dispositions"]
    assert history["state"] == state
    assert Disposition.objects.filter(document_id=grn_id, kind="accept_shortage").count() == (decision == "approve")
    assert stock_truth(proof) == before
    assert post(GoodsApprovalDecideView, checker, review, pk=approval.pk).status_code == 200
    assert_comparison(proof, grn_id, claim_key, remaining)
    assert stock_truth(proof) == before
