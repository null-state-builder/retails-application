"""RBK-01: selective access compensation after real synthetic sale postings.

Every change and review uses the canonical mounted handler and command kernel.
The distinct humans are fictional test actors, not operational product approval.
No proof history is reset and no legacy permission writer is restored.
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.db.models import Sum
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_admin_views import GoodsPrivilegedChangeReviewView
from accounts.goods_models import RoleAssignment, RoleGrant, SecurityGuard
from accounts.models import User
from accounts.principal import resolve_access
from accounts.sessions import grant_step_up, issue_session, resolve_session
from accounts.unified_admin import RolePolicyView, UserAssignmentsView
from core.commands import database_now
from core.documents import VoucherSeries
from core.gl import GLEntry
from core.kernel_models import AuditEvent, DocumentIdentity, OfficialVersion, PrivilegedReview
from finledger.models import CashLedgerEntry
from masters.brand_identity import fingerprint
from masters.brand_reconciliation_views import BrandReconciliationView
from masters.goods_models import BrandIdentityBinding, MasterVersion
from ptmapper.goods_models import OpeningManifest, OpeningManifestRow, OpeningManifestVersion
from ptmapper.soh_models import SohImport, SohImportBatch, SohImportReview, SohImportRow
from ptmapper.soh_views import SohImportMutationView
from sell.models import OnlineSaleSubmission, Sale, SaleLine, SaleLineShare, SaleTender
from sell.services.goods_stock import read_shelf
from sell.views import OnlineFinaliseView
from stockledger.goods_models import CustodyLot, JournalBatch, Origin, Position, QuantityLeg, ValueLeg
from tests.test_first_store_online import bill, post
from tests.test_first_store_online import online_goods as online_goods
from tests.test_so03_denials import worlds as worlds


PASSWORD = "fictional-rollback-password"


def business_snapshot() -> dict[str, list[dict[str, Any]]]:
    """Compare full rows and links, not just matching quantities or money totals."""
    tables: tuple[Any, ...] = (
        Sale, SaleLine, SaleLineShare, SaleTender, OnlineSaleSubmission,
        DocumentIdentity, OfficialVersion, Origin, CustodyLot, Position,
        JournalBatch, QuantityLeg, ValueLeg, CashLedgerEntry, GLEntry, VoucherSeries,
        SohImport, SohImportRow, SohImportReview, SohImportBatch,
        OpeningManifest, OpeningManifestVersion, OpeningManifestRow, BrandIdentityBinding,
    )
    return {model._meta.label: list(model.objects.order_by("pk").values()) for model in tables}


def assignment_snapshot() -> list[dict[str, Any]]:
    return [dict(row) for row in RoleAssignment.objects.order_by("pk").values()]


def assignments(actor: Any, target: User) -> dict[str, Any]:
    request = APIRequestFactory().get(f"/api/auth/admin/users/{target.pk}/assignments")
    force_authenticate(request, actor.user, actor.session)
    response = UserAssignmentsView.as_view()(request, pk=target.pk)
    assert response.status_code == 200, response.data
    return dict(response.data)


def replace(actor: Any, target: User, revision: int, items: list[dict[str, Any]], *,
            password: str = PASSWORD, command_id: str | None = None) -> Any:
    request = APIRequestFactory().put(f"/api/auth/admin/users/{target.pk}/assignments", {
        "command_id": command_id or str(uuid.uuid4()), "contract_version": "goods-v1",
        "expected_revision": revision, "current_password": password, "assignments": items,
    }, format="json")
    force_authenticate(request, actor.user, actor.session)
    return UserAssignmentsView.as_view()(request, pk=target.pk)


def new_assignment(role: str, site_id: int, *, brand_id: int | None = None,
                   ends: str | None = None) -> dict[str, Any]:
    return {"role_code": role, "all_sites": False, "site_ids": [site_id],
            "all_brands": brand_id is None, "brand_ids": [] if brand_id is None else [brand_id],
            "effective_to": ends}


def fresh(user: User) -> tuple[Any, Any]:
    issued = issue_session(user)
    return resolve_access(SimpleNamespace(user=user, auth=issued.session)), issued


def review(user: User, event: AuditEvent) -> Any:
    issued = issue_session(user)
    grant_step_up(issued.session, PASSWORD)
    request = APIRequestFactory().post(f"/api/auth/admin/privileged-changes/{event.pk}/review", {
        "command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
        "note": "Fictional reviewer checked exact selective compensation and preserved history.",
    }, format="json")
    force_authenticate(request, user, issued.session)
    return GoodsPrivilegedChangeReviewView.as_view()(request, pk=event.pk)


def recorded_change(command_id: str) -> AuditEvent:
    return AuditEvent.objects.get(action="access.assignment.replace", outcome="succeeded",
                                  command_key__command_id=uuid.UUID(command_id))


def policy(actor: Any, code: str) -> dict[str, Any]:
    request = APIRequestFactory().get(f"/api/auth/admin/roles/{code}/policy")
    force_authenticate(request, actor.user, actor.session)
    response = RolePolicyView.as_view()(request, code=code)
    assert response.status_code == 200, response.data
    return dict(response.data)


def change_policy(actor: Any, body: dict[str, Any], command_id: str, *, password: str = PASSWORD) -> Any:
    request = APIRequestFactory().put(f"/api/auth/admin/roles/{body['code']}/policy", {
        "command_id": command_id, "contract_version": "goods-v1", "expected_revision": body["revision"],
        "current_password": password, "section_access": body["section_access"],
        "field_access": body["field_access"], "step_actions": body["step_actions"],
    }, format="json")
    force_authenticate(request, actor.user, actor.session)
    return RolePolicyView.as_view()(request, code=body["code"])


def policy_event(command_id: str) -> AuditEvent:
    return AuditEvent.objects.get(action="access.role.policy", outcome="succeeded",
                                  command_key__command_id=uuid.UUID(command_id))


def setup(proof: Any) -> Any:
    for user in (proof.owner.user, proof.warehouse.user):
        user.set_password(PASSWORD)
        user.save(update_fields=["password"])
    proof.owner, _ = fresh(proof.owner.user)
    proof.warehouse, _ = fresh(proof.warehouse.user)
    proof.manager, proof.manager_issued = fresh(proof.manager.user)
    return proof


@pytest.mark.parametrize("password,status,code", [
    ("", 400, "INVALID_REQUEST"), ("wrong-fictional-password", 401, "INVALID_CREDENTIALS"),
])
def test_compensation_requires_password_without_protected_mutation(
    online_goods: Any, password: str, status: int, code: str,
) -> None:
    proof = setup(online_goods)
    wire, _ = bill(proof)
    assert post(OnlineFinaliseView, proof, wire).status_code == 201
    state = assignments(proof.owner, proof.manager.user)
    history, authority = business_snapshot(), assignment_snapshot()
    response = replace(proof.owner, proof.manager.user, state["revision"], [], password=password)
    assert (response.status_code, response.data["code"]) == (status, code), response.data
    assert "items" not in response.data
    assert business_snapshot() == history and assignment_snapshot() == authority
    assert not AuditEvent.objects.filter(action="access.assignment.replace", outcome="succeeded").exists()
    assert resolve_session(proof.manager_issued.token) is not None


def test_reviewed_compensation_preserves_new_sales_revocations_and_later_edits(online_goods: Any) -> None:
    proof = setup(online_goods)
    world, site = proof.world, proof.world.sites[0]
    manager = proof.manager.user
    first_wire, _ = bill(proof)
    first = post(OnlineFinaliseView, proof, first_wire)
    assert first.status_code == 201, first.data
    initial = assignments(proof.owner, manager)
    original_assignment = initial["items"][0]
    bridge = list(RoleGrant.objects.order_by("pk").values())

    # A mistaken extra scope is an additive reviewed command, never a restored
    # pre-cutover authority snapshot. A second extra row will be revoked later.
    expansion_id = str(uuid.uuid4())
    expanded = replace(proof.owner, manager, initial["revision"], [
        *initial["items"], new_assignment("warehouse", world.sites[1].pk),
        new_assignment("brand_manager", world.sites[1].pk, brand_id=world.brands[1].pk),
    ], command_id=expansion_id)
    assert expanded.status_code == 200, expanded.data
    expansion = recorded_change(expansion_id)
    assert str(proof.warehouse.human_id) in expansion.authority["independent_reviewers"]
    assert str(proof.owner.human_id) not in expansion.authority["independent_reviewers"]
    assert resolve_session(proof.manager_issued.token) is None
    assert review(proof.warehouse.user, expansion).status_code == 200
    proof.manager, after_expansion = fresh(manager)
    second_wire, _ = bill(proof, seq=2)
    second = post(OnlineFinaliseView, proof, second_wire)
    assert second.status_code == 201, second.data
    assert first.data["id"] != second.data["id"]

    # This later authorised edit is unrelated to the mistaken scope. A selective
    # compensation must preserve its exact new identity, period and scope.
    later_id = str(uuid.uuid4())
    later = replace(proof.owner, manager, expanded.data["revision"], [
        *expanded.data["items"], new_assignment("warehouse", site.pk,
            brand_id=world.brands[1].pk, ends=(database_now() + timedelta(days=7)).isoformat()),
    ], command_id=later_id)
    assert later.status_code == 200, later.data
    assert review(proof.warehouse.user, recorded_change(later_id)).status_code == 200
    revoked_item = next(item for item in later.data["items"] if item["role_code"] == "brand_manager")
    kept = [item for item in later.data["items"] if item["id"] != revoked_item["id"]]
    revoke_id = str(uuid.uuid4())
    revoked = replace(proof.owner, manager, later.data["revision"], kept, command_id=revoke_id)
    assert revoked.status_code == 200, revoked.data
    assert review(proof.warehouse.user, recorded_change(revoke_id)).status_code == 200
    revoked_row = RoleAssignment.objects.get(pk=revoked_item["id"])
    assert revoked_row.revoked_at is not None
    revoked_at = revoked_row.revoked_at
    current = assignments(proof.owner, manager)
    later_item = next(item for item in current["items"] if item["role_code"] == "warehouse"
                      and item["site_ids"] == [site.pk])
    temporary = next(item for item in current["items"] if item["role_code"] == "warehouse"
                     and item["site_ids"] == [world.sites[1].pk])
    later_before = RoleAssignment.objects.values().get(pk=later_item["id"])
    original_before = RoleAssignment.objects.values().get(pk=original_assignment["id"])
    proof.manager, before_compensation = fresh(manager)
    epoch = SecurityGuard.objects.get(human_id=manager.human_id).epoch
    history, authority = business_snapshot(), assignment_snapshot()

    stale = replace(proof.owner, manager, expanded.data["revision"], initial["items"])
    assert (stale.status_code, stale.data["code"]) == (409, "STALE_REVISION"), stale.data
    assert business_snapshot() == history and assignment_snapshot() == authority
    # Even a current outer revision cannot reactivate a historical revoked ID.
    resurrect = replace(proof.owner, manager, current["revision"], [*current["items"], revoked_item])
    assert (resurrect.status_code, resurrect.data["code"]) == (409, "STALE_REVISION"), resurrect.data
    assert business_snapshot() == history and assignment_snapshot() == authority
    assert resolve_session(before_compensation.token) is not None

    compensation_id = str(uuid.uuid4())
    wanted = [item for item in current["items"] if item["id"] != temporary["id"]]
    compensated = replace(proof.owner, manager, current["revision"], wanted, command_id=compensation_id)
    assert compensated.status_code == 200, compensated.data
    event = recorded_change(compensation_id)
    event_before = AuditEvent.objects.values().get(pk=event.pk)
    assert event.actor_id == proof.owner.human_id
    assert str(proof.warehouse.human_id) in event.authority["independent_reviewers"]
    assert SecurityGuard.objects.get(human_id=manager.human_id).epoch == epoch + 1
    assert all(resolve_session(session.token) is None for session in
               (proof.manager_issued, after_expansion, before_compensation))
    assert RoleAssignment.objects.get(pk=temporary["id"]).revoked_at is not None
    revoked_row.refresh_from_db()
    assert revoked_row.revoked_at == revoked_at
    assert RoleAssignment.objects.values().get(pk=later_item["id"]) == later_before
    assert RoleAssignment.objects.values().get(pk=original_assignment["id"]) == original_before
    assert list(RoleGrant.objects.order_by("pk").values()) == bridge
    assert business_snapshot() == history

    # The maker cannot review their change, even from another fresh session.
    denied = review(proof.owner.user, event)
    assert (denied.status_code, denied.data["code"]) == (409, "REVIEW_INVALID"), denied.data
    assert not PrivilegedReview.objects.filter(audit_event=event).exists()
    assert AuditEvent.objects.values().get(pk=event.pk) == event_before
    assert business_snapshot() == history
    # The database enforces one login per human, preventing a second login from
    # being fabricated to bypass the review's human-identity check.
    with pytest.raises(IntegrityError, match="accounts_user_human_id_key"), transaction.atomic():
        User.objects.create(tenant=world.tenant, human=proof.owner.user.human,
            username=f"rollback-same-human-{uuid.uuid4().hex[:8]}", role=world.roles["it_admin"])
    assert User.objects.filter(human_id=proof.owner.human_id).count() == 1
    assert not PrivilegedReview.objects.filter(audit_event=event).exists()
    assert business_snapshot() == history
    approved = review(proof.warehouse.user, event)
    assert approved.status_code == 200, approved.data
    evidence = PrivilegedReview.objects.get(audit_event=event)
    assert evidence.reviewer_id == proof.warehouse.human_id != event.actor_id
    assert AuditEvent.objects.values().get(pk=event.pk) == event_before
    assert business_snapshot() == history

    # An invalidated session may not learn an already issued bill on replay.
    refused = post(OnlineFinaliseView, proof, first_wire)
    assert (refused.status_code, refused.data["code"]) == (401, "AUTH_REQUIRED"), refused.data
    assert "id" not in refused.data and "doc_number" not in refused.data
    assert business_snapshot() == history
    proof.manager, _ = fresh(manager)
    assert proof.manager.can("section.sell.operate", site_id=site.pk)
    assert not proof.manager.can("stock.read", site_id=world.sites[1].pk, brand_id=world.brands[0].pk)
    replay = post(OnlineFinaliseView, proof, first_wire)
    assert replay.status_code == 200 and replay.data == first.data
    repeated = replace(proof.owner, manager, current["revision"], wanted, command_id=compensation_id)
    assert repeated.status_code == 200 and repeated.data == compensated.data
    assert SecurityGuard.objects.get(human_id=manager.human_id).epoch == epoch + 1
    assert PrivilegedReview.objects.filter(audit_event=event).count() == 1
    assert business_snapshot() == history
    assert sum(read_shelf(site).quantities.values()) == 1
    assert Sale.objects.count() == OnlineSaleSubmission.objects.filter(status="accepted").count() == 2
    assert SaleTender.objects.aggregate(total=Sum("amount_paise"))["total"] == 200000
    assert CashLedgerEntry.objects.aggregate(total=Sum("amount"))["total"] == 200000
    assert GLEntry.objects.aggregate(total=Sum("amount"))["total"] == 0


def test_reviewed_policy_compensation_preserves_later_policy_and_business_versions(online_goods: Any) -> None:
    proof = setup(online_goods)
    manager, world = proof.manager.user, proof.world
    site = world.sites[0]
    first_wire, _ = bill(proof)
    first = post(OnlineFinaliseView, proof, first_wire)
    assert first.status_code == 201, first.data
    initial = policy(proof.owner, "store_person")
    assert {"label.print", "transfer.move"} <= set(initial["step_actions"])
    authority = assignment_snapshot()
    history = business_snapshot()
    for password, status, code in (("", 400, "INVALID_REQUEST"), ("incorrect", 401, "INVALID_CREDENTIALS")):
        denied = change_policy(proof.owner, initial, str(uuid.uuid4()), password=password)
        assert (denied.status_code, denied.data["code"]) == (status, code), denied.data
        assert policy(proof.owner, "store_person") == initial
        assert assignment_snapshot() == authority and business_snapshot() == history

    # Compensate a mistaken removal of one supported action. A later, deliberate
    # removal of a different action must survive; do not restore a whole policy.
    mistaken = deepcopy(initial)
    mistaken["step_actions"].remove("label.print")
    mistaken_id = str(uuid.uuid4())
    changed = change_policy(proof.owner, mistaken, mistaken_id)
    assert changed.status_code == 200, changed.data
    assert resolve_session(proof.manager_issued.token) is None
    assert review(proof.warehouse.user, policy_event(mistaken_id)).status_code == 200
    proof.manager, after_change = fresh(manager)
    assert not proof.manager.can("label.print", site_id=site.pk, brand_id=world.brands[0].pk)
    second_wire, _ = bill(proof, seq=2)
    second = post(OnlineFinaliseView, proof, second_wire)
    assert second.status_code == 201, second.data

    later = deepcopy(dict(changed.data))
    later["step_actions"].remove("transfer.move")
    later_id = str(uuid.uuid4())
    latest = change_policy(proof.owner, later, later_id)
    assert latest.status_code == 200, latest.data
    assert review(proof.warehouse.user, policy_event(later_id)).status_code == 200
    versions = list(MasterVersion.objects.filter(kind="role", target_key="store_person")
                    .order_by("revision").values())
    assert len(versions) == 2
    history = business_snapshot()
    stale = change_policy(proof.owner, dict(changed.data), str(uuid.uuid4()))
    assert (stale.status_code, stale.data["code"]) == (409, "STALE_REVISION"), stale.data
    current = policy(proof.owner, "store_person")
    assert {key: current[key] for key in latest.data} == dict(latest.data)
    assert current["initial_step_defaults"] == initial["initial_step_defaults"]
    assert business_snapshot() == history and assignment_snapshot() == authority
    assert list(MasterVersion.objects.filter(kind="role", target_key="store_person")
                .order_by("revision").values()) == versions

    proof.manager, before_compensation = fresh(manager)
    epoch = SecurityGuard.objects.get(human_id=manager.human_id).epoch
    wanted = deepcopy(dict(latest.data))
    wanted["step_actions"] = sorted([*wanted["step_actions"], "label.print"])
    compensation_id = str(uuid.uuid4())
    compensated = change_policy(proof.owner, wanted, compensation_id)
    assert compensated.status_code == 200, compensated.data
    assert "transfer.move" not in compensated.data["step_actions"]
    event = policy_event(compensation_id)
    assert str(proof.warehouse.human_id) in event.authority["independent_reviewers"]
    denied = review(proof.owner.user, event)
    assert (denied.status_code, denied.data["code"]) == (409, "REVIEW_INVALID"), denied.data
    assert not PrivilegedReview.objects.filter(audit_event=event).exists()
    assert review(proof.warehouse.user, event).status_code == 200
    assert PrivilegedReview.objects.get(audit_event=event).reviewer_id == proof.warehouse.human_id
    assert SecurityGuard.objects.get(human_id=manager.human_id).epoch == epoch + 1
    assert all(resolve_session(session.token) is None for session in
               (proof.manager_issued, after_change, before_compensation))
    final_versions = list(MasterVersion.objects.filter(kind="role", target_key="store_person")
                          .order_by("revision").values())
    assert len(final_versions) == 3 and final_versions[:2] == versions
    assert final_versions[-1]["payload"]["role_policy"]["step_actions"] == wanted["step_actions"]
    assert assignment_snapshot() == authority and business_snapshot() == history
    proof.manager, fresh_manager = fresh(manager)
    assert proof.manager.can("label.print", site_id=site.pk, brand_id=world.brands[0].pk)
    assert not proof.manager.can("transfer.move", site_id=site.pk, brand_id=world.brands[0].pk)
    replay = post(OnlineFinaliseView, proof, first_wire)
    assert replay.status_code == 200 and replay.data == first.data
    repeated = change_policy(proof.owner, wanted, compensation_id)
    assert repeated.status_code == 200 and repeated.data == compensated.data
    assert resolve_session(fresh_manager.token) is not None
    assert SecurityGuard.objects.get(human_id=manager.human_id).epoch == epoch + 1
    assert business_snapshot() == history
    assert list(MasterVersion.objects.filter(kind="role", target_key="store_person")
                .order_by("revision").values()) == final_versions


@pytest.mark.parametrize("operation", ["prepare", "verify", "withdraw"])
def test_accepted_opening_mapping_cannot_be_rewritten_or_withdrawn_after_sales(
    online_goods: Any, operation: str,
) -> None:
    proof = setup(online_goods)
    wire, _ = bill(proof)
    assert post(OnlineFinaliseView, proof, wire).status_code == 201
    source = proof.source
    source.refresh_from_db()
    assert source.state == "applied" and source.batches.exists()
    actor = proof.owner if operation == "withdraw" else proof.warehouse
    grant_step_up(actor.session, PASSWORD)
    bodies = {"prepare": {"configuration": deepcopy(source.configuration)},
              "verify": {"observations": [{"barcode": proof.barcode, "observed_qty": 3,
                         "observed_condition": "good", "reason": "Fictional replacement observation"}]},
              "withdraw": {"reason": "Fictional mapping compensation attempt after stock was sold"}}
    history, authority = business_snapshot(), assignment_snapshot()
    request = APIRequestFactory().post(f"/api/goods-v1/soh-imports/{source.pk}/{operation}", {
        "command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
        "expected_revision": source.revision, **bodies[operation],
    }, format="json")
    force_authenticate(request, actor.user, actor.session)
    refused = SohImportMutationView.as_view()(request, pk=source.pk, operation=operation)
    assert (refused.status_code, refused.data["code"]) == (409, "SOH_ALREADY_APPLIED"), refused.data
    assert "configuration" not in refused.data and "rows" not in refused.data
    assert business_snapshot() == history and assignment_snapshot() == authority


def test_established_sale_brand_mapping_cannot_be_rebound_after_sale(online_goods: Any) -> None:
    proof = setup(online_goods)
    wire, _ = bill(proof)
    saved = post(OnlineFinaliseView, proof, wire)
    assert saved.status_code == 201
    line = SaleLine.objects.get(sale_id=saved.data["id"])
    assert line.brand_ref_id == proof.world.brands[0].pk
    history, authority = business_snapshot(), assignment_snapshot()
    request = APIRequestFactory().post("/api/goods-v1/masters/brand-reconciliation", {
        "command_id": str(uuid.uuid4()), "contract_version": "goods-v1", "current_password": PASSWORD,
        "resource": "sell.saleline", "rows": [{"id": line.pk,
            "brand_id": proof.world.brands[1].pk, "fingerprint": fingerprint(line)}],
    }, format="json")
    force_authenticate(request, proof.owner.user, proof.owner.session)
    refused = BrandReconciliationView.as_view()(request)
    assert (refused.status_code, refused.data["code"]) == (409, "IDENTITY_CONFLICT"), refused.data
    assert "outcomes" not in refused.data and "applied" not in refused.data
    assert business_snapshot() == history and assignment_snapshot() == authority
