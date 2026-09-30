"""Administration continuity and time-bound authority, independent of UI hints."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.access_safeguards import require_administrator_continuity
from accounts.goods_models import RoleAssignment
from accounts.sessions import issue_session, resolve_session
from accounts.unified_admin import UserAssignmentsView
from core.refusals import Refusal
from core.tenancy import tenant_context
from tests.test_so03_denials import TenantWorld, _assign, _person, _tenant_world


@pytest.fixture
def worlds(db: None) -> tuple[TenantWorld, TenantWorld]:
    return _tenant_world("a"), _tenant_world("b")


def test_self_assignment_refused_without_writing(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        actor, human = _person(world, "self-editor")
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        session = issue_session(actor).session
        before = list(RoleAssignment.objects.values_list("id", "revoked_at"))
        request = APIRequestFactory().put(
            f"/api/auth/admin/users/{actor.pk}/assignments",
            {"command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
             "expected_revision": 1, "current_password": "irrelevant", "assignments": []},
            format="json",
        )
        force_authenticate(request, actor, session)
        response = UserAssignmentsView.as_view()(request, pk=actor.pk)
        assert response.status_code == 403
        assert response.data["code"] == "SELF_ASSIGNMENT"
        assert list(RoleAssignment.objects.values_list("id", "revoked_at")) == before


def test_last_administrator_includes_scheduled_gaps(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        _, first = _person(world, "first-admin")
        assignment = _assign(world, first, "owner", all_sites=True, all_brands=True)
        now = timezone.now()
        require_administrator_continuity(world.tenant.pk, now)
        assignment.effective_to = now + timedelta(days=2)
        assignment.save(update_fields=["effective_to"])
        with pytest.raises(Refusal, match="administrator"):
            require_administrator_continuity(world.tenant.pk, now)
        _, second = _person(world, "replacement-admin")
        replacement = _assign(world, second, "it_admin", all_sites=True, all_brands=True)
        replacement.effective_from = assignment.effective_to
        replacement.save(update_fields=["effective_from"])
        require_administrator_continuity(world.tenant.pk, now)
        replacement.effective_from += timedelta(seconds=1)
        replacement.save(update_fields=["effective_from"])
        with pytest.raises(Refusal, match="administrator"):
            require_administrator_continuity(world.tenant.pk, now)


@pytest.mark.parametrize("boundary", ["effective_from", "effective_to", "revoked_at"])
def test_session_expires_at_assignment_boundary(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: Any, boundary: str,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "scheduled-session")
        row = _assign(world, human, "owner", all_sites=True, all_brands=True)
        at = timezone.now() + timedelta(minutes=10)
        setattr(row, boundary, at)
        row.save(update_fields=[boundary])
        issued = issue_session(user)
        assert issued.session.expires_at == at
        assert resolve_session(issued.token) is not None
        monkeypatch.setattr("accounts.sessions.database_now", lambda: at)
        assert resolve_session(issued.token) is None


def test_initial_role_cannot_be_deactivated(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    from accounts.goods_admin_services import update_role
    from types import SimpleNamespace

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        run: Any = SimpleNamespace(tenant_id=world.tenant.pk, advisory_lock=lambda *args: None)
        with pytest.raises(Refusal, match="initial role"):
            update_role(run, role_pk=world.roles["owner"].pk, expected_revision=1, fields={"active": False})
        world.roles["owner"].refresh_from_db()
        assert world.roles["owner"].is_active


def test_shared_policy_change_keeps_independent_review_provenance(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    from accounts.goods_admin_views import GoodsPrivilegedChangeReviewView
    from accounts.sessions import grant_step_up
    from accounts.unified_admin import RolePolicyView
    from accounts.unified_policy import initial_step_actions
    from accounts.sections import SECTION_CODES
    from accounts.rbac_matrix import section_access_for
    from core.kernel_models import AuditEvent

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        changer, change_human = _person(world, "policy-changer")
        beneficiary, benefit_human = _person(world, "existing-beneficiary")
        newly_authorised, new_human = _person(world, "new-reviewer")
        _assign(world, change_human, "owner", all_sites=True, all_brands=True)
        _assign(world, benefit_human, "owner", all_sites=True, all_brands=True)
        _assign(world, new_human, "it_admin", all_sites=True, all_brands=True)
        role = world.roles["it_admin"]
        role.section_access = {code: {"capability": "none"} for code in SECTION_CODES}
        role.permissions_map = {"step_actions": []}
        role.save(update_fields=["section_access", "permissions_map"])
        for user in (changer, beneficiary, newly_authorised):
            user.set_password("proof-password")
            user.save(update_fields=["password"])
        session = issue_session(changer).session
        request = APIRequestFactory().put(
            "/api/auth/admin/roles/it_admin/policy",
            {"command_id": str(uuid.uuid4()), "contract_version": "goods-v1", "expected_revision": 1,
             "current_password": "proof-password", "section_access": section_access_for("it_admin"),
             "field_access": [], "step_actions": initial_step_actions("it_admin")}, format="json",
        )
        force_authenticate(request, changer, session)
        response = RolePolicyView.as_view()(request, code="it_admin")
        assert response.status_code == 200, response.data
        event = AuditEvent.objects.get(tenant=world.tenant, action="access.role.policy", outcome="succeeded")
        assert str(benefit_human.pk) in event.authority["independent_reviewers"]
        assert str(new_human.pk) not in event.authority["independent_reviewers"]
        assert str(change_human.pk) not in event.authority["independent_reviewers"]

        def acknowledge(user: Any) -> Any:
            reviewer_session = issue_session(user).session
            grant_step_up(reviewer_session, "proof-password")
            request = APIRequestFactory().post(
                f"/api/auth/admin/privileged-changes/{event.pk}/review",
                {"command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
                 "note": "Reviewed the explicit before and after access."}, format="json",
            )
            force_authenticate(request, user, reviewer_session)
            return GoodsPrivilegedChangeReviewView.as_view()(request, pk=event.pk)

        self_review = acknowledge(changer)
        assert self_review.status_code == 409, self_review.data
        fabricated = acknowledge(newly_authorised)
        assert fabricated.status_code == 403, fabricated.data
        assert fabricated.data["code"] == "REVIEW_AUTHORITY_UNPROVEN"
        permitted = acknowledge(beneficiary)
        assert permitted.status_code == 200, permitted.data


def test_step_policy_controls_specialist_gate_without_role_code_grant(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    from accounts.principal import access_for_user
    from accounts.unified_policy import ACTION_LEVELS, role_actions
    from masters.goods_tax_settings_views import may_change_tax_settings

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "tax-admin")
        _assign(world, human, "it_admin", all_sites=True, all_brands=True)
        role = world.roles["it_admin"]
        assert may_change_tax_settings(user)
        role.permissions_map = {"step_actions": []}
        role.save(update_fields=["permissions_map"])
        assert not may_change_tax_settings(user)
        # A broad section or the role's familiar name cannot restore the step.
        assert access_for_user(user).can_section("setup", "manage")
        assert "tax.settings.manage" not in role_actions(role)
        role.permissions_map = {"step_actions": ["tax.settings.manage"]}
        role.save(update_fields=["permissions_map"])
        disabled = {**ACTION_LEVELS, "tax.settings.manage": ("setup", "none")}
        assert "tax.settings.manage" not in role_actions(role, action_levels=disabled)
        assert may_change_tax_settings(user)


def test_older_workflow_version_requires_explicit_action_upgrade(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from accounts.unified_policy import pending_workflow_actions, role_actions, workflow_levels
    from masters.goods_models import MasterVersion

    world, _ = worlds

    class OlderVersion:
        def order_by(self, _field: str) -> OlderVersion:
            return self

        def values_list(self, _field: str, *, flat: bool) -> OlderVersion:
            assert flat
            return self

        def first(self) -> dict[str, Any]:
            return {"action_levels": {"access.manage": {"section": "setup", "minimum": "manage"}}}

    monkeypatch.setattr(MasterVersion.objects, "filter", lambda **_filters: OlderVersion())
    levels = workflow_levels(world.tenant.pk)
    assert levels["section.home.view"] == ("home", "none")
    assert levels["section.setup.view"] == ("setup", "none")
    assert levels["tax.settings.manage"] == ("setup", "none")
    assert "section.home.view" not in role_actions(world.roles["owner"], action_levels=levels)
    pending = pending_workflow_actions(world.tenant.pk)
    assert "section.home.view" in pending
    assert "tax.settings.manage" in pending
