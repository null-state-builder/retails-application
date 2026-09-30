"""Feature administration must retain scoped live authority and independent review."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_admin_views import GoodsPrivilegedChangeReviewView
from accounts.sessions import grant_step_up, issue_session, resolve_session
from core.kernel_models import AuditEvent
from core.tenancy import tenant_context
from masters import goods_store_feature_views as views
from masters.store_feature_models import StoreFeatureSwitch
from tests.test_so03_denials import TenantWorld, _assign, _person
from tests.test_so03_denials import worlds as worlds


def _setup(world: TenantWorld, *, checker: bool = True) -> tuple[Any, Any, Any]:
    admin, human = _person(world, "feature-admin")
    assignment = _assign(world, human, "it_admin", all_sites=True, all_brands=True)
    admin.set_password("fictional-feature-proof")
    admin.save(update_fields=["password"])
    reviewer = None
    if checker:
        reviewer, person = _person(world, "feature-reviewer")
        _assign(world, person, "owner", all_sites=True, all_brands=True)
        reviewer.set_password("fictional-feature-proof")
        reviewer.save(update_fields=["password"])
    return admin, reviewer, assignment


def _switch(user: Any, site: Any, session: Any) -> Any:
    request = APIRequestFactory().post("/api/goods-v1/masters/store-features/switch", {
        "command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
        "store_id": site.pk, "feature_key": "cash-count", "enabled": True,
    }, format="json")
    force_authenticate(request, user, session)
    return views.GoodsStoreFeatureSwitchView.as_view()(request)


def test_feature_switch_requires_password_and_leaves_no_changes(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        admin, _, _ = _setup(world)
        issued = issue_session(admin)
        response = _switch(admin, world.sites[0], issued.session)
        assert response.status_code == 403, response.data
        assert response.data["code"] == "STEP_UP_REQUIRED"
        assert not StoreFeatureSwitch.objects.filter(tenant=world.tenant).exists()
        assert resolve_session(issued.token) is not None


def test_feature_switch_invalidates_sessions_and_has_different_authorised_review(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        admin, reviewer, _ = _setup(world)
        issued = issue_session(admin)
        reviewer_old = issue_session(reviewer)
        grant_step_up(issued.session, "fictional-feature-proof")
        response = _switch(admin, world.sites[0], issued.session)
        assert response.status_code == 200, response.data
        assert StoreFeatureSwitch.objects.get(tenant=world.tenant, site=world.sites[0]).enabled
        assert resolve_session(issued.token) is None and resolve_session(reviewer_old.token) is None
        event = AuditEvent.objects.get(tenant=world.tenant, action=views.SWITCH_ACTION, outcome="succeeded")
        assert event.authority["independent_reviewers"] == [str(reviewer.human_id)]

        def review(user: Any) -> Any:
            session = issue_session(user).session
            grant_step_up(session, "fictional-feature-proof")
            request = APIRequestFactory().post(f"/api/auth/admin/privileged-changes/{event.pk}/review", {
                "command_id": str(uuid.uuid4()), "contract_version": "goods-v1", "note": "Fictional review.",
            }, format="json")
            force_authenticate(request, user, session)
            return GoodsPrivilegedChangeReviewView.as_view()(request, pk=event.pk)

        assert review(admin).status_code == 409
        assert review(reviewer).status_code == 200


def test_feature_switch_refuses_without_an_independent_reviewer(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        admin, _, _ = _setup(world, checker=False)
        issued = issue_session(admin)
        grant_step_up(issued.session, "fictional-feature-proof")
        response = _switch(admin, world.sites[0], issued.session)
        assert response.status_code == 409, response.data
        assert response.data["code"] == "REVIEW_REQUIRED"
        assert not StoreFeatureSwitch.objects.filter(tenant=world.tenant).exists()
        assert resolve_session(issued.token) is not None


def test_feature_switch_rechecks_authority_before_commit(worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        admin, _, assignment = _setup(world)
        issued = issue_session(admin)
        grant_step_up(issued.session, "fictional-feature-proof")
        original = views.may_change_switches

        def withdraw(user: Any, store_id: int) -> bool:
            result = original(user, store_id)
            assignment.site_ids = [world.sites[1].pk]
            assignment.all_sites = False
            assignment.save(update_fields=["site_ids", "all_sites"])
            return result

        monkeypatch.setattr(views, "may_change_switches", withdraw)
        response = _switch(admin, world.sites[0], issued.session)
        assert response.status_code in {403, 404}, response.data
        assert not StoreFeatureSwitch.objects.filter(tenant=world.tenant).exists()
