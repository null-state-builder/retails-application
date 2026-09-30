"""Actual-session delivery regressions for the first-store administration slice.

Evidence fixtures use the kernel recorder; authority changes deliberately bypass
session invalidation only inside the disposable proof to test demand replay too.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts import goods_admin_services as svc, unified_admin
from accounts.goods_admin_views import GoodsPrivilegedChangeListView, GoodsStaffListCreateView
from accounts.goods_models import RoleAssignment, ServerSession, Staff
from accounts.models import User
from accounts.sessions import issue_session, revoke_session
from accounts.unified_admin import UserAssignmentsView
from core.commands import database_now
from core.kernel_models import AuditEvent
from core.tenancy import tenant_context
from tests.first_store_goods import command, live_access
from tests.test_so03_denials import TenantWorld, _assign, _person
from tests.test_so03_denials import worlds as worlds


def _fixture(world: TenantWorld) -> tuple[User, RoleAssignment, User, Staff, AuditEvent]:
    actor, human = _person(world, "delivery-owner")
    assignment = _assign(world, human, "owner", all_sites=True, all_brands=True)
    target, target_human = _person(world, "delivery-person")
    _assign(world, target_human, "store_person", sites=(world.sites[0],), all_brands=True)
    staff = Staff.objects.create(
        tenant=world.tenant, human=target_human, mobile="+910000000000",
        # No active action hints: scope and field demands must still be replayed.
        retired_at=timezone.now() - timedelta(minutes=1),
    )
    access = live_access(actor)
    command(access, "proof.admin.delivery", lambda run: svc.open_assignment(
        run, staff, world.sites[0].pk, run.now - timedelta(days=1),
    ))
    event: AuditEvent = command(access, "proof.admin.delivery", lambda run: run.record(AuditEvent(
        action="access.user.create", site=world.sites[0], outcome="succeeded",
        subject_key=str(target.pk), before=[], after=[],
        authority={"independent_reviewers": []},
    )))
    return actor, assignment, target, staff, event


def _request(actor: User) -> tuple[Any, ServerSession]:
    session = issue_session(actor).session
    request = APIRequestFactory().get("/proof/administration")
    force_authenticate(request, actor, session)
    return request, session


def _view(surface: str, request: Any, target: User) -> Any:
    if surface == "assignments":
        return UserAssignmentsView.as_view()(request, pk=target.pk)
    if surface == "staff":
        return GoodsStaffListCreateView.as_view()(request)
    return GoodsPrivilegedChangeListView.as_view()(request)


def _hook(surface: str) -> tuple[Any, str]:
    if surface == "assignments":
        return unified_admin, "_choices"
    return svc, "staff_dto" if surface == "staff" else "privileged_dtos"


def _during_projection(
    monkeypatch: pytest.MonkeyPatch, surface: str, boundary: Callable[[], None],
) -> None:
    module, name = _hook(surface)
    original = getattr(module, name)

    def project(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        boundary()
        return result

    monkeypatch.setattr(module, name, project)


@pytest.mark.parametrize("surface", ["assignments", "staff", "privileged"])
@pytest.mark.parametrize("boundary", ["logout", "expiry", "revoked", "scope"])
def test_admin_delivery_replays_session_and_exact_resource_authority(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
    surface: str, boundary: str,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        actor, assignment, target, staff, event = _fixture(world)
        request, _ = _request(actor)
        allowed = _view(surface, request, target)
        assert allowed.status_code == 200, allowed.data
        assert allowed["Cache-Control"] == "no-store, private"
        assert allowed["Pragma"] == "no-cache"
        if surface == "assignments":
            assert allowed.data["items"][0]["role_code"] == "store_person"
        elif surface == "staff":
            item = next(row for row in allowed.data["items"] if row["id"] == str(staff.pk))
            assert item["data"]["mobile"] == "+910000000000"
            assert item["allowed_actions"] == []
        else:
            assert any(row["id"] == str(event.pk) for row in allowed.data["items"])

        request, session = _request(actor)

        def withdraw() -> None:
            if boundary == "logout":
                revoke_session(session)
            elif boundary == "expiry":
                ServerSession.objects.filter(pk=session.pk).update(
                    expires_at=timezone.now() - timedelta(seconds=1),
                )
            elif boundary == "revoked":
                RoleAssignment.objects.filter(pk=assignment.pk).update(revoked_at=database_now())
            else:
                RoleAssignment.objects.filter(pk=assignment.pk).update(
                    all_sites=False, site_ids=[world.sites[1].pk],
                )

        _during_projection(monkeypatch, surface, withdraw)
        denied = _view(surface, request, target)
        expected = {"logout": (401, "AUTH_REQUIRED"), "expiry": (401, "SESSION_EXPIRED"),
                    "revoked": (403, "ACTION_DENIED"), "scope": (404, "NOT_FOUND")}
        assert (denied.status_code, denied.data["code"]) == expected[boundary]
        assert "items" not in denied.data and "choices" not in denied.data
        assert "data" not in denied.data


def test_staff_delivery_replays_protected_fields_after_projection(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        actor, _, target, _, _ = _fixture(world)
        request, _ = _request(actor)

        def withdraw_field() -> None:
            role = world.roles["owner"]
            role.field_access = [field for field in role.field_access if field != "employee_private"]
            role.save(update_fields=["field_access"])

        _during_projection(monkeypatch, "staff", withdraw_field)
        denied = _view("staff", request, target)
        assert denied.status_code == 404 and denied.data["code"] == "NOT_FOUND"
        assert "items" not in denied.data


def test_staff_administration_without_personal_field_omits_mobile(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        _, _, target, staff, _ = _fixture(world)
        admin, human = _person(world, "delivery-admin")
        _assign(world, human, "it_admin", all_sites=True, all_brands=True)
        request, _ = _request(admin)
        response = _view("staff", request, target)
        assert response.status_code == 200, response.data
        item = next(row for row in response.data["items"] if row["id"] == str(staff.pk))
        assert "mobile" not in item["data"]


def test_assignment_administration_refuses_foreign_target_and_store_scoped_actor(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    world, other = worlds
    with tenant_context(world.tenant.pk):
        actor, assignment, target, _, _ = _fixture(world)
    with tenant_context(other.tenant.pk):
        foreign, _ = _person(other, "foreign-assignment-target")
    with tenant_context(world.tenant.pk):
        request, _ = _request(actor)
        foreign_response = _view("assignments", request, foreign)
        assert foreign_response.status_code == 404 and "items" not in foreign_response.data
        assignment.all_sites, assignment.site_ids = False, [world.sites[0].pk]
        assignment.save(update_fields=["all_sites", "site_ids"])
        request, _ = _request(actor)
        scoped_response = _view("assignments", request, target)
        assert scoped_response.status_code == 404 and "items" not in scoped_response.data
