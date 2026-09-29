"""SO-03 assignment scope and conversion safety checks."""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.assignment_migration import (
    Scope, _grant_scope, apply_migration_plan, build_migration_plan,
)
from accounts.goods_models import HumanIdentity, RoleAssignment, RoleGrant, SecurityGuard, ServerSession
from accounts.goods_demo import PersonaSpec, seed_goods_personas
from accounts.goods_setup import GrantRequest, add_grant, bootstrap_deployment, ensure_goods_roles, service_principal
from accounts.management.commands.seed_foundation import Command as SeedFoundationCommand, deployment_tenant
from accounts.models import Role, User
from accounts import role_assignments
from accounts.role_assignments import effective_assignments, has_effective_role
from accounts.unified_policy import role_actions
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.refusals import Refusal
from core.tenancy import tenant_context
from masters.goods_models import MasterVersion, Tenant
from accounts.rbac_matrix import section_access_for


def test_scope_intersection_preserves_one_site_brand_tuple() -> None:
    by_brand = Scope(True, (), False, (7,))
    by_site = Scope(False, (3,), True, ())
    assert by_brand.intersect(by_site) == Scope(False, (3,), False, (7,))
    assert by_brand.intersect(Scope(True, (), False, (8,))).empty()
    assert Scope(True, (), True, ()).intersect(by_site) == by_site


def test_malformed_legacy_grant_scope_is_not_guessed() -> None:
    grant = RoleGrant(scope_kind="site", site_id=3, brand_id=7)
    assert _grant_scope(grant, uuid.uuid4()) is None


@pytest.mark.django_db
def test_retired_identity_is_excluded_from_migration_replay() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-retired-{uuid.uuid4().hex[:6]}", name="Retired migration proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        role = Role.objects.create(
            tenant=tenant, code="owner", name="Owner", section_access=section_access_for("owner"),
        )
        human = HumanIdentity.objects.create(
            tenant=tenant, staff_code="MIG-RETIRED", display_name="Retired", active=False,
        )
        user = User.objects.create(
            tenant=tenant, human=human, role=role, scope_type="all",
            username=f"retired-{uuid.uuid4().hex[:8]}", is_active=False,
        )
        plan = build_migration_plan(tenant.pk)
        assert any(row["user_id"] == user.pk and row["code"] == "INACTIVE_IDENTITY"
                   for row in plan.blocked)
        assert all(candidate.user_id != user.pk for candidate in plan.candidates)


def test_role_bridge_cannot_mix_scope_tuples(monkeypatch: pytest.MonkeyPatch) -> None:
    from masters.models import Brand, Store

    tenant_id = uuid.uuid4()
    human_id = uuid.uuid4()
    owner = SimpleNamespace(
        role=SimpleNamespace(code="owner", section_access={"stock": {"capability": "manage"}}),
        all_sites=False, site_ids=[1], all_brands=False, brand_ids=[2],
    )
    store_person = SimpleNamespace(
        role=SimpleNamespace(code="store_person", section_access={"stock": {"capability": "view"}}),
        all_sites=False, site_ids=[3], all_brands=False, brand_ids=[4],
    )
    monkeypatch.setattr(role_assignments, "require_tenant_id", lambda: tenant_id)
    monkeypatch.setattr(role_assignments, "effective_assignments", lambda human: [owner, store_person])
    monkeypatch.setattr(Store.objects, "filter", lambda **kwargs: SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr(Brand.objects, "filter", lambda **kwargs: SimpleNamespace(exists=lambda: True))
    user = SimpleNamespace(is_active=True, tenant_id=tenant_id, human_id=human_id)
    assert has_effective_role(user, {"owner"}, section="stock", minimum="manage", site_id=1, brand_id=2)
    assert not has_effective_role(user, {"owner"}, section="stock", minimum="manage", site_id=3, brand_id=2)
    assert not has_effective_role(user, {"owner"}, section="stock", minimum="manage", site_id=1, brand_id=4)
    assert not has_effective_role(user, {"store_person"}, section="stock", minimum="manage", site_id=3, brand_id=4)
    assert not has_effective_role(user, {"owner"}, section="stock", minimum="manage")


@pytest.mark.django_db
def test_effective_assignments_exclude_expired_and_empty_scopes() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-{uuid.uuid4().hex[:8]}",
        name="SO-03 proof tenant",
        deployment_key=uuid.uuid4(),
        timezone="Asia/Kolkata",
        currency="INR",
        locale="en-IN",
        synthetic=True,
    )
    with tenant_context(tenant.pk):
        human = HumanIdentity.objects.create(tenant=tenant, staff_code="SO03", display_name="SO-03")
        role = Role.objects.create(tenant=tenant, code="owner", name="Owner")
        user = User.objects.create(
            username=f"so03-{uuid.uuid4().hex[:8]}", tenant=tenant, human=human,
            role=role, is_superuser=True,
        )
        assert not user.may_post_pt_or_vflip_floor
        moment = timezone.now()
        full = RoleAssignment.objects.create(
            tenant=tenant, human=human, role=role,
            all_sites=True, all_brands=True,
            effective_from=moment - timedelta(days=1),
        )
        RoleAssignment.objects.create(
            tenant=tenant, human=human, role=role,
            all_sites=False, site_ids=[], all_brands=True,
            effective_from=moment - timedelta(days=1),
        )
        RoleAssignment.objects.create(
            tenant=tenant, human=human, role=role,
            all_sites=True, all_brands=True,
            effective_from=moment - timedelta(days=2), effective_to=moment - timedelta(days=1),
        )
        assert [row.pk for row in effective_assignments(human.pk, moment)] == [full.pk]
        # An assignment has no posting power until its versioned role policy
        # holds the registered Money action; the legacy role name is insufficient.
        assert not user.may_post_pt_or_vflip_floor
        role.section_access = section_access_for("owner")
        role.save(update_fields=["section_access"])
        assert user.may_post_pt_or_vflip_floor

        invalid = RoleAssignment(
            tenant=tenant, human=human, role=role,
            all_sites=True, site_ids=[999999], all_brands=True,
            effective_from=moment,
        )
        with pytest.raises(ValidationError):
            invalid.save()


@pytest.mark.django_db
def test_first_cutover_invalidates_blocked_sessions_once() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-cutover-{uuid.uuid4().hex[:6]}",
        name="SO-03 cutover proof", deployment_key=uuid.uuid4(),
        timezone="Asia/Kolkata", currency="INR", locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        humans = [
            HumanIdentity.objects.create(
                tenant=tenant, staff_code=f"P{index}", display_name=f"Person {index}"
            )
            for index in (1, 2)
        ]
        sessions = []
        now = timezone.now()
        for human in humans:
            user = User.objects.create(
                username=f"cutover-{uuid.uuid4().hex[:8]}", tenant=tenant, human=human
            )
            SecurityGuard.objects.create(tenant=tenant, human=human, epoch=1)
            sessions.append(ServerSession.objects.create(
                tenant=tenant, user=user,
                token_hash=uuid.uuid4().hex * 2,
                csrf_hash=uuid.uuid4().hex * 2,
                issued_at=now, last_seen_at=now,
                expires_at=now + timedelta(hours=1), security_epoch=1,
            ))
        plan = build_migration_plan(tenant.pk)
        assert plan.blocked
        first = apply_migration_plan(plan, apply_policy_defaults=False)
        assert first["cutover"] == {
            "applied": True, "invalidated_humans": 2, "revoked_sessions": 2,
        }
        assert list(SecurityGuard.objects.filter(tenant=tenant).values_list("epoch", flat=True)) == [2, 2]
        assert ServerSession.objects.filter(pk__in=[row.pk for row in sessions], revoked_at__isnull=True).count() == 0
        assert MasterVersion.objects.filter(
            tenant=tenant, kind="tenant", target_key="access-cutover:so03"
        ).count() == 1
        second = apply_migration_plan(build_migration_plan(tenant.pk), apply_policy_defaults=False)
        assert second["cutover"]["applied"] is False
        assert sorted(SecurityGuard.objects.filter(tenant=tenant).values_list("epoch", flat=True)) == [2, 2]


@pytest.mark.django_db
def test_fresh_bootstrap_uses_six_roles_and_explicit_owner_admin_assignments() -> None:
    deployment_key = uuid.uuid4()
    kwargs: dict[str, Any] = {
        "deployment_key": deployment_key,
        "code": f"SO03-BOOT-{uuid.uuid4().hex[:6]}",
        "name": "SO-03 bootstrap proof",
        "timezone_name": "Asia/Kolkata",
        "currency": "INR",
        "locale": "en-IN",
        "synthetic": True,
        "admin_email": f"bootstrap-{uuid.uuid4().hex[:8]}@example.test",
        "admin_name": "Tenant Bootstrap Owner",
        "admin_staff_code": "bootstrap-owner",
        "admin_password": "Bootstrap@123",
    }
    tenant = bootstrap_deployment(**kwargs)
    with tenant_context(tenant.pk):
        assert set(Role.objects.filter(tenant=tenant).values_list("code", flat=True)) == {
            "owner", "store_person", "warehouse", "brand_manager", "accounts", "it_admin"
        }
        assert Role.objects.get(tenant=tenant, code="it_admin").name == "Admin"
        user = User.objects.get(email=kwargs["admin_email"])
        assignments = list(RoleAssignment.objects.filter(tenant=tenant, human=user.human))
        assert {row.role.code for row in assignments} == {"owner", "it_admin"}
        assert all(row.all_sites and row.all_brands for row in assignments)
        assert RoleGrant.objects.filter(tenant=tenant).count() == 0
        assert "pt.approve.receipt" in Role.objects.get(
            tenant=tenant, code="owner"
        ).permissions_map["step_actions"]
    assert bootstrap_deployment(**kwargs).pk == tenant.pk
    with tenant_context(tenant.pk):
        assert RoleAssignment.objects.filter(tenant=tenant).count() == 2
        assert RoleGrant.objects.filter(tenant=tenant).count() == 0


@pytest.mark.django_db
def test_role_seed_retires_old_system_role_without_touching_grant_history() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-retire-{uuid.uuid4().hex[:6]}", name="SO-03 retirement proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        old_role = Role.objects.create(tenant=tenant, code="C-OWN", name="Old Owner", is_system=True)
        human = HumanIdentity.objects.create(tenant=tenant, staff_code="OLD", display_name="Old Person")

        def grant_handler(run: CommandRun) -> CommandResult:
            grant = add_grant(run, human, GrantRequest(role_code="C-OWN"))
            return CommandResult(resource_type="grant", resource_id=str(grant.pk))

        execute_command(
            service_principal(tenant.pk, "seed"),
            CommandSpec(action="seed.historical_role_proof", command_id=uuid.uuid4(), business_input={}),
            grant_handler,
        )
        grant = RoleGrant.objects.get(tenant=tenant, human=human)
        ensure_goods_roles()
        old_role.refresh_from_db()
        assert not old_role.is_active
        assert RoleGrant.objects.get(pk=grant.pk).role.pk == old_role.pk
        assert RoleGrant.objects.filter(tenant=tenant).count() == 1
        ensure_goods_roles()
        assert Role.objects.filter(tenant=tenant).count() == 7
        assert RoleGrant.objects.filter(tenant=tenant).count() == 1


@pytest.mark.django_db
def test_cutover_versions_owner_approval_defaults_as_editable_role_policy() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-owner-{uuid.uuid4().hex[:6]}", name="SO-03 Owner policy proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        owner = Role.objects.create(
            tenant=tenant, code="owner", name="Owner",
            section_access=section_access_for("owner"),
        )
        assert "pt.approve.receipt" not in role_actions(owner)
        plan = build_migration_plan(tenant.pk)
        change = next(item for item in plan.policy_changes if item["role"] == "owner")
        assert "pt.approve.receipt" in change["after"]["step_actions"]
        apply_migration_plan(plan)
        owner.refresh_from_db()
        assert "pt.approve.receipt" in role_actions(owner)
        assert MasterVersion.objects.filter(
            tenant=tenant, kind="role", target_key="owner",
            reason_code="so03_access_cutover",
        ).exists()
        owner.permissions_map = {"step_actions": []}
        owner.save(update_fields=["permissions_map"])
        assert "pt.approve.receipt" not in role_actions(owner)
        assert not [
            item for item in build_migration_plan(tenant.pk).policy_changes
            if item["role"] == "owner"
        ]


@pytest.mark.django_db
def test_demo_reseed_creates_no_legacy_grants_or_platform_assignment() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-demo-{uuid.uuid4().hex[:6]}", name="SO-03 demo seed proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    personas = (
        PersonaSpec("so03.owner", "SO-03 Owner", "C-OWN"),
        PersonaSpec("so03.platform", "SO-03 Platform", "X-PLT"),
    )
    with tenant_context(tenant.pk):
        ensure_goods_roles()
        seed_goods_personas(tenant, {}, personas)
        owner = User.objects.get(email=personas[0].email)
        platform = User.objects.get(email=personas[1].email)
        assignment = RoleAssignment.objects.get(tenant=tenant, human=owner.human)
        assert assignment.role.code == "owner" and assignment.all_sites and assignment.all_brands
        assert not RoleAssignment.objects.filter(tenant=tenant, human=platform.human).exists()
        assert RoleGrant.objects.filter(tenant=tenant).count() == 0
        revoked_at = timezone.now()
        RoleAssignment.objects.filter(pk=assignment.pk).update(effective_to=revoked_at)
        seed_goods_personas(tenant, {}, personas)
        assert RoleAssignment.objects.filter(tenant=tenant).count() == 1
        assert RoleAssignment.objects.get(pk=assignment.pk).effective_to == revoked_at
        assert RoleGrant.objects.filter(tenant=tenant).count() == 0


@pytest.mark.django_db
def test_foundation_seed_assigns_new_owner_admin_but_not_existing_users() -> None:
    tenant = deployment_tenant()
    with tenant_context(tenant.pk):
        ensure_goods_roles()
        for username in ("owner", "admin", "superadmin"):
            User.objects.create(username=username, tenant=tenant)
        seed = SeedFoundationCommand()
        seed._seed_goods_people({}, {"owner", "admin", "superadmin"})
        assert set(Role.objects.filter(tenant=tenant, is_active=True).values_list("code", flat=True)) == {
            "owner", "store_person", "warehouse", "brand_manager", "accounts", "it_admin"
        }
        assert {
            (row.human.staff_code, row.role.code)
            for row in RoleAssignment.objects.filter(tenant=tenant).select_related("human", "role")
        } == {("owner", "owner"), ("admin", "it_admin")}
        assert RoleGrant.objects.filter(tenant=tenant).count() == 0
        RoleAssignment.objects.filter(tenant=tenant, role__code="it_admin").delete()
        seed._seed_goods_people({}, set())
        assert RoleAssignment.objects.filter(tenant=tenant).count() == 1
        assert RoleGrant.objects.filter(tenant=tenant).count() == 0


@pytest.mark.django_db
def test_older_cutover_owner_policy_gets_one_time_step_default_backfill() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-step-{uuid.uuid4().hex[:6]}", name="SO-03 older policy proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        owner = Role.objects.create(
            tenant=tenant, code="owner", name="Owner",
            section_access=section_access_for("owner"),
        )

        def old_cutover(run: CommandRun) -> CommandResult:
            run.record(MasterVersion(
                kind="role", target_key="owner", revision=1,
                payload={"code": "owner", "section_access": owner.section_access, "field_access": []},
                reason_code="so03_access_cutover", effective_from=run.now,
            ))
            return CommandResult(resource_type="role", resource_id=str(owner.pk))

        execute_command(
            service_principal(tenant.pk, "seed"),
            CommandSpec(action="seed.old_policy_proof", command_id=uuid.uuid4(), business_input={}),
            old_cutover,
        )
        plan = build_migration_plan(tenant.pk)
        owner_changes = [item for item in plan.policy_changes if item["role"] == "owner"]
        assert len(owner_changes) == 1
        assert owner_changes[0]["reason_code"] == "so03_owner_step_defaults"
        apply_migration_plan(plan)
        owner.refresh_from_db()
        assert "pt.approve.receipt" in role_actions(owner)
        assert MasterVersion.objects.filter(
            tenant=tenant, kind="role", target_key="owner",
            reason_code="so03_owner_step_defaults",
        ).count() == 1
        assert not [
            item for item in build_migration_plan(tenant.pk).policy_changes
            if item["role"] == "owner"
        ]


@pytest.mark.django_db
def test_revoked_legacy_grant_cannot_become_permanent_user_role_fallback() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-revoked-{uuid.uuid4().hex[:6]}", name="SO-03 revoked grant proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        roles = ensure_goods_roles()
        Role.objects.create(tenant=tenant, code="C-OWN", name="Historical Owner", is_system=True)
        human = HumanIdentity.objects.create(tenant=tenant, staff_code="REVOKED", display_name="Revoked")
        start = timezone.now() - timedelta(days=2)
        end = timezone.now() - timedelta(days=1)
        User.objects.create(
            username=f"revoked-{uuid.uuid4().hex[:8]}", tenant=tenant, human=human,
            role=roles["owner"], scope_type="all", date_joined=start - timedelta(days=1),
        )
        original_id: uuid.UUID | None = None

        def historical_grants(run: CommandRun) -> CommandResult:
            nonlocal original_id
            original = add_grant(
                run, human, GrantRequest(role_code="C-OWN", effective_from=start)
            )
            original_id = original.pk
            run.record(RoleGrant(
                human=human, role=original.role, scope_kind="tenant",
                effective_from=end, action_set={"actions": [], "scope_kind": "tenant"},
                field_set=[], revokes=original,
            ))
            return CommandResult(resource_type="grant", resource_id=str(original.pk))

        execute_command(
            service_principal(tenant.pk, "seed"),
            CommandSpec(action="seed.revoked_grant_proof", command_id=uuid.uuid4(), business_input={}),
            historical_grants,
        )
        plan = build_migration_plan(tenant.pk)
        assert not plan.blocked
        assert len(plan.candidates) == 1
        assert plan.candidates[0].legacy_grant_id == original_id
        assert plan.candidates[0].effective_to == end
        apply_migration_plan(plan)
        assert RoleAssignment.objects.filter(tenant=tenant, human=human).count() == 1
        assert effective_assignments(human.pk) == []


@pytest.mark.django_db
def test_migration_rejects_changed_source_and_never_restores_revoked_target() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-stale-{uuid.uuid4().hex[:6]}", name="SO-03 stale preview proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        owner = Role.objects.create(tenant=tenant, code="owner", name="Owner")
        human = HumanIdentity.objects.create(tenant=tenant, staff_code="STALE", display_name="Stale")
        user = User.objects.create(
            username=f"stale-{uuid.uuid4().hex[:8]}", tenant=tenant, human=human,
            role=owner, scope_type="all",
        )
        preview = build_migration_plan(tenant.pk)
        assert len(preview.candidates) == 1
        user.scope_type = "brand"
        user.save(update_fields=["scope_type"])
        with pytest.raises(Refusal) as stale:
            apply_migration_plan(preview, apply_policy_defaults=False)
        assert stale.value.code == "STALE_MIGRATION_PLAN"
        assert not RoleAssignment.objects.filter(tenant=tenant).exists()

        user.scope_type = "all"
        user.save(update_fields=["scope_type"])
        first = apply_migration_plan(build_migration_plan(tenant.pk), apply_policy_defaults=False)
        assert first["created"] == 1
        assignment = RoleAssignment.objects.get(tenant=tenant, legacy_user=user)
        assignment.revoked_at = timezone.now()
        assignment.save(update_fields=["revoked_at"])
        second = apply_migration_plan(build_migration_plan(tenant.pk), apply_policy_defaults=False)
        assignment.refresh_from_db()
        assert second["created"] == 0
        assert second["unchanged"] == 1
        assert assignment.revoked_at is not None
        assert effective_assignments(human.pk) == []


@pytest.mark.django_db
def test_migration_does_not_add_old_all_scope_beside_an_unlinked_target() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-target-{uuid.uuid4().hex[:6]}", name="SO-03 target proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        role = Role.objects.create(tenant=tenant, code="owner", name="Owner")
        human = HumanIdentity.objects.create(tenant=tenant, staff_code="TARGET", display_name="Target")
        User.objects.create(
            username=f"target-{uuid.uuid4().hex[:8]}", tenant=tenant, human=human,
            role=role, scope_type="all",
        )
        target = RoleAssignment.objects.create(
            tenant=tenant, human=human, role=role, all_sites=True,
            all_brands=True, effective_from=timezone.now() - timedelta(days=1),
            revoked_at=timezone.now(),
        )
        plan = build_migration_plan(tenant.pk)
        assert not plan.candidates
        assert any(row["code"] == "EXISTING_TARGET_AUTHORITY" for row in plan.blocked)
        result = apply_migration_plan(plan, apply_policy_defaults=False)
        assert result["created"] == 0
        assert list(RoleAssignment.objects.filter(tenant=tenant)) == [target]


@pytest.mark.django_db
def test_foreign_tenant_legacy_role_is_rejected_before_migration() -> None:
    local = Tenant.objects.create(
        code=f"so03-local-{uuid.uuid4().hex[:6]}", name="SO-03 local tenant",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    foreign = Tenant.objects.create(
        code=f"so03-foreign-{uuid.uuid4().hex[:6]}", name="SO-03 foreign tenant",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(foreign.pk):
        foreign_owner = Role.objects.create(tenant=foreign, code="owner", name="Foreign Owner")
    with tenant_context(local.pk):
        ensure_goods_roles()
        human = HumanIdentity.objects.create(tenant=local, staff_code="LOCAL", display_name="Local")
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create(
                username=f"foreign-role-{uuid.uuid4().hex[:8]}", tenant=local,
                human=human, role=foreign_owner, scope_type="all",
            )
        assert not build_migration_plan(local.pk).candidates
