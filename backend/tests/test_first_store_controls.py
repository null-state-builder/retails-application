"""Proof-only first-store readiness, counter and protected source boundaries."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.principal import resolve_access
from accounts.sessions import issue_session
from core.refusals import Refusal
from core.tenancy import tenant_context
from files.goods_models import EvidenceObject
from files.goods_services import readable_by
from files.goods_views import _scope
from masters.first_store_views import FirstStoreCounterView
from masters.goods_models import SiteGuard
from masters.goods_services import require_sell_ready, validate_config_payload
from sell.models import RegisteredTill
from tests.test_so03_denials import TenantWorld, _assign, _person
from tests.test_so03_denials import worlds as worlds


def _request(user: Any, method: str, body: dict[str, Any] | None = None, *, confirmed: bool = True) -> Any:
    session = issue_session(user).session
    if confirmed:
        session.step_up_at = timezone.now()
        session.save(update_fields=["step_up_at"])
    request = getattr(APIRequestFactory(), method)("/counter", body or {}, format="json")
    force_authenticate(request, user, session)
    return request


def test_counter_registration_requires_password_and_exact_store_assignment(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "setup-owner")
        _assign(world, human, "owner", sites=(world.sites[0],), all_brands=True)
        for site in world.sites:
            SiteGuard.objects.create(tenant=world.tenant, site=site, selling_mode="online_alpha")
        body = {"command_id": str(uuid.uuid4()), "contract_version": "goods-v1", "expected_revision": 1}
        response = FirstStoreCounterView.as_view()(_request(user, "post", body, confirmed=False), pk=world.sites[0].pk)
        assert response.status_code == 403 and response.data["code"] == "STEP_UP_REQUIRED"
        response = FirstStoreCounterView.as_view()(_request(user, "post", body), pk=world.sites[1].pk)
        assert response.status_code in {403, 404}
        assert not RegisteredTill.objects.exists()


def test_pairing_code_is_one_time_and_replay_preserves_counter(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "counter-owner")
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        site = world.sites[0]
        guard = SiteGuard.objects.create(tenant=world.tenant, site=site, selling_mode="online_alpha")
        body = {"command_id": str(uuid.uuid4()), "contract_version": "goods-v1", "expected_revision": guard.revision}
        response = FirstStoreCounterView.as_view()(_request(user, "post", body), pk=site.pk)
        assert response.status_code == 201 and response.data["token_issued"]
        assert response.data.get("device_token")
        first_id = response.data["device_id"]
        response = FirstStoreCounterView.as_view()(_request(user, "post", body), pk=site.pk)
        assert response.status_code == 201 and not response.data["token_issued"]
        assert "device_token" not in response.data and response.data["device_id"] == first_id
        response = FirstStoreCounterView.as_view()(_request(user, "get"), pk=site.pk)
        assert response.status_code == 200 and "device_token" not in response.data
        assert RegisteredTill.objects.count() == 1
        guard.refresh_from_db()
        assert not guard.sell_ready and guard.revision == 2


def test_old_sell_ready_flag_cannot_bypass_current_setup(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        site = world.sites[0]
        SiteGuard.objects.create(tenant=world.tenant, site=site, selling_mode="online_alpha", sell_ready=True,
                                 goods_ready=True, lifecycle="active", stock_contract="goods_v1")
        with pytest.raises(Refusal) as caught:
            require_sell_ready(site)
        assert caught.value.code == "SELL_NOT_READY"
        keys = {row["field"] for row in caught.value.issues}
        assert {"current_setup", "opening_reconciled", "selling_policy", "tax_configuration", "registered_counter"} <= keys


def test_opaque_original_requires_financial_and_cost_in_one_assignment(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "opaque-source")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        access = resolve_access(SimpleNamespace(user=user, auth=issue_session(user).session))
        scope = _scope({"site_ids": [world.sites[0].pk], "brand_ids": [world.brands[0].pk],
                        "sensitive_fields": ["cost", "financial"]})
        source = EvidenceObject(scope=scope, contains_fields=["cost", "financial"])
        assert not readable_by(access, source)
        _assign(world, human, "it_admin", all_sites=True, all_brands=True)
        access = resolve_access(SimpleNamespace(user=user, auth=issue_session(user).session))
        assert not readable_by(access, source)
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        access = resolve_access(SimpleNamespace(user=user, auth=issue_session(user).session))
        assert readable_by(access, source)


def test_selling_policy_closed_schema_and_discount_edges() -> None:
    payload = {"manual_discount_cap_percent": "100.00", "manual_discount_on_offer_lines": False,
               "return_window_days": 365}
    validate_config_payload("sell_policy", payload)
    for changed in ({"manual_discount_cap_percent": "100.01"}, {"manual_discount_cap_percent": True},
                    {"manual_discount_on_offer_lines": "false"}, {"return_window_days": 366}, {"unknown": True}):
        with pytest.raises(Refusal):
            validate_config_payload("sell_policy", {**payload, **changed})


@pytest.mark.parametrize("reason", ["x" * 61, 17])
def test_readiness_reference_refuses_invalid_value_before_capability_write(
    worlds: tuple[TenantWorld, TenantWorld], reason: Any,
) -> None:
    from masters.goods_models import SiteCapabilityEvent
    from masters.goods_views import GoodsSiteReadinessView

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "opening-reviewer")
        _assign(world, human, "owner", sites=(world.sites[0],), all_brands=True)
        guard = SiteGuard.objects.create(
            tenant=world.tenant, site=world.sites[0], selling_mode="online_alpha"
        )
        body = {
            "command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
            "expected_revision": guard.revision, "action": "approve_opening_setup",
            "reason_code": reason,
        }
        response = GoodsSiteReadinessView.as_view()(
            _request(user, "post", body), pk=world.sites[0].pk
        )
        assert response.status_code == 400 and response.data["code"] == "INVALID_REQUEST"
        guard.refresh_from_db()
        assert not guard.opening_setup_ready and guard.revision == 1
        assert SiteCapabilityEvent.objects.count() == 0
