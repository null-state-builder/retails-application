"""A personal credential never widens scoped counter approval authority."""

from __future__ import annotations

from typing import Any
from datetime import timedelta

import pytest
from rest_framework.test import APIClient

from accounts.goods_models import AuthenticationFailure, RoleAssignment
from accounts.session_payload import session_payload
from accounts.sessions import issue_session
from accounts.till_pin import (
    SET_ACTION,
    may_hold_till_pin,
    may_set_personal_till_pin,
    set_own_pin,
    verify_till_pin,
)
from core.kernel_models import AuditEvent
from core.commands import database_now
from core.refusals import Refusal
from core.tenancy import tenant_context
from sell.services.dataset import _managers
from tests.test_so03_denials import _assign, _person, worlds as worlds


def session_for(user: Any, settings: Any) -> tuple[APIClient, Any, str]:
    password = "Generated-Proof-Only-Personal-Credential-29!"
    user.set_password(password)
    user.save(update_fields=["password"])
    settings.KDPS_DEPLOYMENT_KEY = str(user.tenant.deployment_key)
    issued = issue_session(user)
    client = APIClient()
    client.cookies["kdps_session"] = issued.token
    client.cookies["kdps_csrf"] = issued.csrf_token
    client.credentials(HTTP_X_CSRF_TOKEN=issued.csrf_token)
    return client, issued.session, password


def test_operating_manager_sets_own_pin_without_exception_authority(worlds: Any, settings: Any) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "personal-counter")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        client, session, password = session_for(user, settings)
        assert may_set_personal_till_pin(user, site_id=world.sites[0].pk)
        assert not may_set_personal_till_pin(user, site_id=world.sites[1].pk)
        assert not may_hold_till_pin(user, site_id=world.sites[0].pk)
        before = dict(world.roles["store_person"].section_access)
        result = client.put("/api/auth/me/till-pin", {"pin": "493827", "current_password": password}, format="json")
        assert result.status_code == 200
        user.refresh_from_db()
        assert verify_till_pin("493827", user.till_pin_hash)
        assert not may_hold_till_pin(user, site_id=world.sites[0].pk)
        assert _managers(world.sites[0]) == []
        world.roles["store_person"].refresh_from_db()
        assert world.roles["store_person"].section_access == before
        assert RoleAssignment.objects.filter(human=human).count() == 1
        payload = session_payload(user, session)
        assert payload["user"]["may_set_till_pin"] is True
        assert payload["user"]["may_hold_till_pin"] is False
        event = AuditEvent.objects.get(action=SET_ACTION)
        assert "493827" not in str(event.after)


def test_own_pin_refuses_wrong_password_and_other_person_target(worlds: Any, settings: Any) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "own-pin")
        other, _ = _person(world, "other-pin")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        client, _, password = session_for(user, settings)
        result = client.put("/api/auth/me/till-pin", {"pin": "493827", "current_password": "Wrong proof password"}, format="json")
        assert result.status_code == 403
        assert AuthenticationFailure.objects.get(tenant=world.tenant).failure_count == 1
        result = client.put("/api/auth/me/till-pin", {"pin": "493827", "current_password": password, "user_id": other.pk}, format="json")
        assert result.status_code == 400 and result.data["code"] == "INVALID_REQUEST"
        user.refresh_from_db()
        other.refresh_from_db()
        assert not user.till_pin_hash and not other.till_pin_hash


@pytest.mark.parametrize("kind", ["network", "brand-limited", "admin"])
def test_personal_pin_requires_one_selected_store_operating_assignment(worlds: Any, settings: Any, kind: str) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, f"pin-{kind}")
        if kind == "network":
            _assign(world, human, "store_person", all_sites=True, all_brands=True)
        elif kind == "brand-limited":
            _assign(world, human, "store_person", sites=(world.sites[0],), brands=(world.brands[0],))
        else:
            _assign(world, human, "it_admin", sites=(world.sites[0],), all_brands=True)
        _, session, _ = session_for(user, settings)
        assert not may_set_personal_till_pin(user)
        with pytest.raises(Refusal) as denied:
            set_own_pin(user, "493827", session)
        assert denied.value.code == "ACTION_DENIED"
        user.refresh_from_db()
        assert not user.till_pin_hash


def test_scheduled_store_eligibility_end_is_rechecked_before_pin_commit(worlds: Any, settings: Any, monkeypatch: Any) -> None:
    import accounts.till_pin as pins
    import accounts.principal as principals

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "pin-boundary")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True, ends=timedelta(minutes=1))
        _, session, _ = session_for(user, settings)
        from accounts.sessions import grant_step_up

        grant_step_up(session, "Generated-Proof-Only-Personal-Credential-29!")
        original = pins.write_pin
        initial_clock = database_now()

        def changed(run: Any, user_pk: int, new_hash: str, *, by: str) -> None:
            original(run, user_pk, new_hash, by=by)
            monkeypatch.setattr(principals, "database_now", lambda: initial_clock + timedelta(minutes=2))

        monkeypatch.setattr(pins, "write_pin", changed)
        with pytest.raises(Refusal) as denied:
            set_own_pin(user, "493827", session)
        # Session issuance itself pins the next assignment boundary. The
        # deciding command must roll back when that boundary passes mid-write.
        assert denied.value.code == "SESSION_EXPIRED"
        user.refresh_from_db()
        assert not user.till_pin_hash
