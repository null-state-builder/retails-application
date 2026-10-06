"""Historical Owner request labels use current assignments and stable evidence."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from approvals.goods_models import ApprovalRequest
from approvals.goods_policy import eligible_checker
from approvals.goods_services import subject_cells
from approvals.goods_views import GoodsApprovalDecideView, GoodsApprovalInboxView
from core.tenancy import tenant_context
from ptmapper import goods_manifest_services as manifests, soh_services
from stockledger.goods_models import JournalBatch, Origin, Position
from tests.first_store_goods import actors, approve, command, live_access, reviewed_source
from tests.test_first_store_goods_operations import wire
from tests.test_so03_denials import _assign
from tests.test_so03_denials import worlds as worlds


def inbox(actor: Any) -> Any:
    request = APIRequestFactory().get("/api/goods-v1/approvals/inbox")
    force_authenticate(request, actor.user, actor.session)
    return GoodsApprovalInboxView.as_view()(request)


def decide(actor: Any, target: ApprovalRequest) -> Any:
    request = APIRequestFactory().post("/api/goods-v1/approvals/decide", wire(
        decision="approve", reviewed_hash=target.reviewed_hash, expected_revision=target.revision), format="json")
    force_authenticate(request, actor.user, actor.session)
    return GoodsApprovalDecideView.as_view()(request, pk=target.pk)


def truth(manifest: Any, request: ApprovalRequest) -> tuple[Any, ...]:
    manifest.refresh_from_db()
    request.refresh_from_db()
    return (manifest.current_version_id, manifest.approved_version_id, manifest.revision,
            request.state, request.revision, request.reviewed_hash, tuple(request.required_roles),
            JournalBatch.objects.count(), Origin.objects.count(), Position.objects.count())


@pytest.mark.parametrize("stored_role", ["C-OWN", "owner"])
def test_pending_manifest_owner_label_uses_current_canonical_owner_and_distinct_maker(worlds: Any, stored_role: str) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = reviewed_source(world, *actors(world))
        assert proof.source.approval_request.required_roles == ["owner"]
        approve(proof.owner, proof.source.approval_request)
        proof.source.refresh_from_db()
        batch = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(
            run, proof.warehouse, proof.source, 1, proof.source.revision))
        request = ApprovalRequest.objects.get(subject_kind="manifest", subject_key=str(batch.manifest_id), state="pending")
        assert request.required_roles == ["owner"]
        if stored_role == "C-OWN":
            # An existing historical request keeps its original stored evidence.
            request.required_roles = [stored_role]
            request.save(update_fields=["required_roles"])
        assert {grant.role_code for grant in proof.owner.grants} == {"owner"}
        before = truth(batch.manifest, request)
        response = inbox(proof.owner)
        assert response.status_code == 200
        assert str(request.pk) in {row["id"] for row in response.data["items"]}
        # A maker cannot decide even after receiving another canonical role.
        _assign(world, proof.warehouse.user.human, "owner", all_sites=True, all_brands=True)
        maker = live_access(proof.warehouse.user)
        maker_inbox = inbox(maker)
        assert maker_inbox.status_code == 200
        assert str(request.pk) not in {row["id"] for row in maker_inbox.data["items"]}
        refused = decide(maker, request)
        assert refused.status_code == 403 and refused.data["code"] == "SELF_APPROVAL", refused.data
        assert truth(batch.manifest, request) == before
        accepted = decide(proof.owner, request)
        assert accepted.status_code == 200, accepted.data
        batch.manifest.refresh_from_db()
        request.refresh_from_db()
        assert batch.manifest.approved_version_id == batch.manifest.current_version_id
        assert request.state == "approved" and request.required_roles == [stored_role]
        assert request.decisions.count() == 1
        assert JournalBatch.objects.count() == 0 and Origin.objects.count() == 0 and Position.objects.count() == 0
        assert manifests.manifest_cells(batch.manifest)


def test_historical_owner_selector_never_qualifies_legacy_grant_or_unknown_role(worlds: Any) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = reviewed_source(world, *actors(world))
        request = proof.source.approval_request
        request.required_roles = ["C-OWN"]
        cells = subject_cells(request)
        assert eligible_checker(proof.owner, request, cells)
        current_grants = proof.owner.grants
        proof.owner.grants = [replace(grant, role_code="C-OWN") for grant in current_grants]
        assert not eligible_checker(proof.owner, request, cells)
        proof.owner.grants = [replace(grant, all_sites=False, site_ids=frozenset({world.sites[1].pk})) for grant in current_grants]
        assert not eligible_checker(proof.owner, request, cells)
        proof.owner.grants = [replace(grant, actions=frozenset()) for grant in current_grants]
        assert not eligible_checker(proof.owner, request, cells)
        proof.owner.grants = current_grants
        request.required_roles = ["C-INV"]
        assert not eligible_checker(proof.owner, request, cells)
        request.required_roles = ["unknown-legacy-code"]
        assert not eligible_checker(proof.owner, request, cells)
        proof.source.refresh_from_db()
        assert proof.source.state == "submitted" and not proof.source.batches.exists()
        assert not JournalBatch.objects.exists() and not Origin.objects.exists()
