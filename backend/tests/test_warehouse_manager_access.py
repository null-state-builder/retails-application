"""Store test run step 4: the Warehouse manager policy, allow and deny.

Site 0 plays the warehouse (WH-A) and site 1 its linked store (store-A); a
third site plays store-B, outside the manager's assignment.
"""
from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts import warehouse_manager
from accounts.unified_policy import role_actions
from masters.goods_models import MasterVersion, SiteGuard
from masters.goods_views import GoodsSiteReadinessView
from masters.models import Store
from outbound.goods_models import GoodsTransfer
from outbound.goods_transfer_views import TransferApproveView, TransferListCreateView, TransferSubmitView
from reporting.base import viewer_stores
from reporting.views import SalesReportView
from stockledger.goods_views import GoodsStockOnHandView
from tests.first_store_goods import live_access
from tests.test_first_store_goods_operations import (
    approval_wire, operational_goods as operational_goods, post,
    receiving_goods as receiving_goods, submitted_transfer, transfer_wire, truth, wire,
)
from tests.first_store_goods import _publish_tenant_config, command
from tests.test_so03_denials import _assign, _person
from tests.test_so03_denials import worlds as worlds

pytestmark = pytest.mark.django_db


def route(proof: Any, roles: list[str]) -> None:
    """Replace the in-force transfer approval route with one naming ``roles``."""
    from masters.goods_config import withdraw
    from masters.goods_models import ConfigVersion, EffectiveVersionPeriod

    withdrawn = EffectiveVersionPeriod.objects.filter(withdrawn_at__isnull=False).values_list("target_id", flat=True)
    for old in ConfigVersion.objects.filter(kind="approval", payload__action="pt.approve.transfer").exclude(pk__in=withdrawn):
        def change(run: Any, old: ConfigVersion = old) -> Any:
            return withdraw(run, old, reason_code="PROOF_REVIEWED_REPLACEMENT")
        command(proof.owner, "proof.policy.withdraw", change)
    _publish_tenant_config(proof.world.tenant, proof.owner.human_id, kind="approval", payload={
        "action": "pt.approve.transfer", "roles": roles, "site_ids": [], "brand_ids": [],
        "require_distinct": True, "qty_max": 100, "value_max": "10000000", "step_up": True,
        "unknown_value": "refuse"}, label=f"warehouse-manager-route-{len(roles)}")


def get(view: Any, actor: Any, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    request = APIRequestFactory().get("/proof/warehouse-manager", params or {})
    force_authenticate(request, actor.user, actor.session)
    return view.as_view()(request, **kwargs)


def manager(proof: Any, label: str, *sites: Store) -> Any:
    user, human = _person(proof.world, label)
    warehouse_manager.assign(proof.world.tenant, human, site_ids=[site.pk for site in sites])
    return live_access(user)


@pytest.fixture
def wh(operational_goods: Any) -> Any:
    proof = operational_goods
    world = proof.world
    proof.warehouse_site, proof.store_a = world.sites
    proof.store_b = Store.objects.create(tenant=world.tenant, gstin=world.sites[0].gstin,
                                         code="store-b", name="Store B")
    SiteGuard.objects.create(tenant=world.tenant, site=proof.store_b, stock_contract="goods_v1",
                             goods_ready=True, lifecycle="active", selling_mode="online_alpha")
    warehouse_manager.ensure_policy(world.tenant)
    route(proof, ["owner", warehouse_manager.ROLE_CODE])
    from django.utils import timezone

    from masters.store_feature_models import StoreFeatureSwitch
    from reporting.sales_report import FEATURE_KEY

    for site in (proof.store_a, proof.store_b):
        StoreFeatureSwitch.objects.create(tenant=world.tenant, site=site, feature_key=FEATURE_KEY,
                                          enabled=True, updated_at=timezone.now())
    proof.wh_manager = manager(proof, "wh-a-manager", proof.warehouse_site, proof.store_a)
    return proof


def test_policy_is_a_versioned_custom_role_and_idempotent(wh: Any) -> None:
    tenant = wh.world.tenant
    role = warehouse_manager.ensure_policy(tenant)
    versions = MasterVersion.objects.filter(tenant_id=tenant.pk, kind="role",
                                            target_key=warehouse_manager.ROLE_CODE)
    assert not role.is_system and versions.count() == 1
    warehouse_manager.ensure_policy(tenant)
    assert versions.count() == 1
    from accounts.unified_admin import _assignable_role

    assert _assignable_role(role, tenant.pk)
    actions = role_actions(role)
    assert {"pt.approve.transfer", "org.site.lifecycle.run", "pt.prepare", "transfer.move",
            "stock.view", "section.sell.view", "section.reports.view", "section.setup.operate"} <= actions
    assert "section.sell.operate" not in actions
    assert role.field_access == ["cost"]


def test_manager_approves_transfer_between_own_warehouse_and_store(wh: Any) -> None:
    transfer = submitted_transfer(wh)
    result = post(TransferApproveView, wh.wh_manager, approval_wire(transfer, reason="Warehouse review"), pk=transfer.pk)
    assert result.status_code == 200, result.data
    transfer.refresh_from_db()
    assert transfer.state == "approved"


def test_manager_who_drafted_cannot_approve_own_transfer(wh: Any) -> None:
    created = post(TransferListCreateView, wh.wh_manager, transfer_wire(wh))
    assert created.status_code == 201, created.data
    transfer = GoodsTransfer.objects.get(pk=created.data["id"])
    assert post(TransferSubmitView, wh.wh_manager, wire(), pk=transfer.pk).status_code == 200
    before = truth()
    result = post(TransferApproveView, wh.wh_manager, approval_wire(transfer), pk=transfer.pk)
    assert result.status_code == 403 and result.data["code"] == "SELF_APPROVAL", result.data
    assert truth() == before
    # A second manager of the same sites is the independent checker.
    other = manager(wh, "wh-a-second-manager", wh.warehouse_site, wh.store_a)
    assert post(TransferApproveView, other, approval_wire(transfer), pk=transfer.pk).status_code == 200


def test_manager_views_store_stock_sales_and_readiness(wh: Any) -> None:
    store = wh.store_a
    stock = get(GoodsStockOnHandView, wh.wh_manager, {"site_id": store.pk})
    assert stock.status_code == 200, stock.data
    assert store.pk in {site.pk for site in viewer_stores(wh.wh_manager.user)}
    sales = get(SalesReportView, wh.wh_manager, {"store": store.code})
    assert sales.status_code == 200, sales.data
    readiness = get(GoodsSiteReadinessView, wh.wh_manager, pk=store.pk)
    assert readiness.status_code == 200, readiness.data


def test_manager_of_wh_a_cannot_act_on_store_b(wh: Any) -> None:
    outsider = manager(wh, "wh-a-store-b-manager", wh.warehouse_site, wh.store_b)
    transfer = submitted_transfer(wh)  # WH-A -> store-A
    before = truth()
    approve = post(TransferApproveView, outsider, approval_wire(transfer), pk=transfer.pk)
    assert approve.status_code == 403 and approve.data["code"] == "ACTION_DENIED", approve.data
    assert truth() == before
    # The store-A manager cannot reach store-B either.
    actor, store = wh.wh_manager, wh.store_b
    assert get(GoodsStockOnHandView, actor, {"site_id": store.pk}).status_code == 404
    assert get(GoodsSiteReadinessView, actor, pk=store.pk).status_code == 404
    sales = get(SalesReportView, actor, {"store": store.code})
    assert sales.status_code == 403 and sales.data["code"] == "SCOPE_DENIED", sales.data
    assert store.pk not in {site.pk for site in viewer_stores(actor.user)}


def test_manager_cannot_bill_without_store_person_assignment(wh: Any) -> None:
    store = wh.store_a

    def bills(actor: Any) -> bool:
        return bool(actor.covers_all_actions(
            {"section.sell.operate"}, [(store.pk, None)], {"customer"}, roles={"store_person"}))

    assert not bills(wh.wh_manager)
    # Even a sell rung on the manager role would not satisfy the store-seat gate.
    assert not wh.wh_manager.covers_all_actions({"section.sell.operate"}, [(store.pk, None)], set())
    _assign(wh.world, wh.wh_manager.user.human, "store_person", sites=(store,), all_brands=True)
    assert bills(live_access(wh.wh_manager.user))


@pytest.mark.parametrize("named_in_policy", [False, True])
def test_store_person_cannot_approve_transfer(wh: Any, named_in_policy: bool) -> None:
    if named_in_policy:
        route(wh, ["owner", warehouse_manager.ROLE_CODE, "store_person"])
    user, human = _person(wh.world, "store-checker")
    _assign(wh.world, human, "store_person", sites=(wh.warehouse_site, wh.store_a), all_brands=True)
    transfer = submitted_transfer(wh)
    before = truth()
    result = post(TransferApproveView, live_access(user), approval_wire(transfer), pk=transfer.pk)
    assert result.status_code == 403 and result.data["code"] == "ACTION_DENIED", result.data
    assert truth() == before
    assert GoodsTransfer.objects.get(pk=transfer.pk).state == "submitted"


def test_assign_rejects_foreign_site_and_replaces_changed_scope(wh: Any) -> None:
    from accounts.goods_models import RoleAssignment
    from core.refusals import Refusal

    foreign = wh.foreign_world.sites[0]
    user, human = _person(wh.world, "scope-change")
    with pytest.raises(Refusal):
        warehouse_manager.assign(wh.world.tenant, human, site_ids=[foreign.pk])
    first = warehouse_manager.assign(wh.world.tenant, human, site_ids=[wh.warehouse_site.pk, wh.store_a.pk])
    again = warehouse_manager.assign(wh.world.tenant, human, site_ids=[wh.store_a.pk, wh.warehouse_site.pk])
    assert again.pk == first.pk
    moved = warehouse_manager.assign(wh.world.tenant, human, site_ids=[wh.warehouse_site.pk, wh.store_b.pk])
    first.refresh_from_db()
    assert moved.pk != first.pk and first.effective_to is not None
    assert RoleAssignment.objects.filter(human=human, effective_to__isnull=True).count() == 1
