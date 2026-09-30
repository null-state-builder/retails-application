"""Targets use Money scope without borrowing Setup or other-store authority."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_models import RoleAssignment, ServerSession
from accounts.sessions import issue_session, revoke_session
from core.commands import database_now
from core.tenancy import tenant_context
from masters import views
from masters.serializers import StoreTargetLocationSerializer, StoreTargetSerializer
from masters.models import StoreTarget
from tests.test_so03_denials import TenantWorld, _assign, _person
from tests.test_so03_denials import worlds as worlds


def request_for(user: Any, surface: str) -> tuple[Any, ServerSession]:
    session = issue_session(user).session
    request = APIRequestFactory().get(f"/api/masters/{surface}")
    force_authenticate(request, user, session)
    return request, session


def test_manager_target_locations_need_money_not_setup_and_preserve_empty_months(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    world, other = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "target-manager")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        request, _ = request_for(user, "store-targets/locations")
        response = views.StoreTargetLocationView.as_view()(request)
        assert response.status_code == 200, response.data
        assert response.data == [{"id": world.sites[0].pk, "code": world.sites[0].code, "name": world.sites[0].name}]
        assert response["Cache-Control"] == "no-store, private"
        assert other.sites[0].code not in str(response.data)
        request, _ = request_for(user, "store-targets")
        targets = views.StoreTargetView.as_view()(request)
        assert targets.status_code == 200 and targets.data == []
        # Setup remains outside this assignment even though the grid has a row.
        request, _ = request_for(user, "stores")
        setup = views.StoreListView.as_view()(request)
        assert setup.status_code == 200 and setup.data == []


def test_partial_brand_money_and_setup_at_other_site_cannot_lend_target_scope(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "target-scoped")
        _assign(world, human, "owner", sites=(world.sites[0],), brands=(world.brands[0],))
        _assign(world, human, "it_admin", sites=(world.sites[1],), all_brands=True)
        StoreTarget.objects.create(store=world.sites[0], month=date(2026, 9, 1), target_paise=99900)
        for surface, view in [("store-targets/locations", views.StoreTargetLocationView), ("store-targets", views.StoreTargetView)]:
            request, _ = request_for(user, surface)
            response = view.as_view()(request)
            assert response.status_code == 200 and response.data == [], response.data


@pytest.mark.parametrize("surface,view", [("store-targets/locations", views.StoreTargetLocationView), ("store-targets", views.StoreTargetView)])
@pytest.mark.parametrize("boundary", ["logout", "expiry", "scope", "revoked"])
def test_target_delivery_rechecks_the_deciding_session_and_resource(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
    surface: str, view: Any, boundary: str,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "target-delivery")
        assignment = _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        StoreTarget.objects.create(store=world.sites[0], month=date(2026, 9, 1), target_paise=99900)
        request, session = request_for(user, surface)
        serializer = StoreTargetLocationSerializer if "locations" in surface else StoreTargetSerializer
        original = serializer.to_representation

        def withdraw(self: Any, instance: Any) -> Any:
            body = original(self, instance)
            if boundary == "logout":
                revoke_session(session)
            elif boundary == "expiry":
                ServerSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
            elif boundary == "scope":
                RoleAssignment.objects.filter(pk=assignment.pk).update(site_ids=[world.sites[1].pk])
            else:
                RoleAssignment.objects.filter(pk=assignment.pk).update(revoked_at=database_now())
            return body

        monkeypatch.setattr(serializer, "to_representation", withdraw)
        response = view.as_view()(request)
        assert response.status_code in (401, 403, 404), response.data
        assert "target_paise" not in response.data and "name" not in response.data
