"""Canonical admin reads use active assignments, never legacy grant rows."""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.utils import timezone

from accounts import goods_admin_services as admin
from accounts.goods_models import HumanIdentity, RoleAssignment
from accounts.models import Role, User
from accounts.principal import AccessContext, effective_grants
from accounts.rbac_matrix import section_access_for
from accounts.unified_admin import _choices, _parse_assignment
from core.refusals import Refusal
from core.tenancy import tenant_context
from masters.goods_models import Tenant
from masters.models import Brand, Gstin, LegalEntity, Store


def test_login_projection_has_no_legacy_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin, "current_revision", lambda *_: 1)
    monkeypatch.setattr(admin, "may_hold_till_pin", lambda *_: False)
    monkeypatch.setattr(admin, "may_reset_till_pin", lambda *_: False)
    monkeypatch.setattr(admin, "live_grants", lambda *_args, **_kwargs: pytest.fail("legacy grants read"))
    user = cast(Any, SimpleNamespace(
        pk=7, human_id=uuid.uuid4(), human=SimpleNamespace(display_name="Colleague", active=True),
        email="colleague@example.test", full_name="Colleague", is_active=True,
        must_change_password=False, till_pin_hash="",
    ))
    access = cast(Any, SimpleNamespace(
        holds=lambda action: action == "access.manage",
        can=lambda action: action == "access.manage",
        user=user,
    ))

    answer = admin.user_dto(access, user, with_pin_state=True)

    assert "grants" not in answer["data"]
    assert answer["allowed_actions"] == ["update"]
    assert answer["data"]["has_till_pin"] is False


@pytest.mark.django_db
def test_role_holders_follow_assignment_rows() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-admin-{uuid.uuid4().hex[:8]}", name="SO-03 admin proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        human = HumanIdentity.objects.create(tenant=tenant, staff_code="SO03-ADMIN", display_name="Colleague")
        role = Role.objects.create(tenant=tenant, code="owner", name="Owner")
        RoleAssignment.objects.create(
            tenant=tenant, human=human, role=role, all_sites=True, all_brands=True,
            effective_from=timezone.now() - timedelta(days=1),
        )

        assert admin._holders(tenant.pk, role.pk) == {human.pk}


@pytest.mark.django_db
def test_login_scope_reads_assignment_cells() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-scope-{uuid.uuid4().hex[:8]}", name="SO-03 scope proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        human = HumanIdentity.objects.create(tenant=tenant, staff_code="SO03-SCOPE", display_name="Colleague")
        role = Role.objects.create(tenant=tenant, code="store_person", name="Store Person")
        login = User.objects.create(
            tenant=tenant, human=human, username=f"so03-{uuid.uuid4().hex[:12]}",
            email=f"so03-{uuid.uuid4().hex[:12]}@example.test",
        )
        entity = LegalEntity.objects.create(tenant=tenant, code="so03-entity", name="Entity")
        gstin = Gstin.objects.create(
            tenant=tenant, legal_entity=entity, gstin=f"10{uuid.uuid4().int % 10**13:013d}",
            state_code="10", state_name="Bihar",
        )
        site = Store.objects.create(tenant=tenant, gstin=gstin, code="so03-site", name="Site")
        brand = Brand.objects.create(tenant=tenant, code="so03-brand", name="Brand")
        RoleAssignment.objects.create(
            tenant=tenant, human=human, role=role, site_ids=[site.pk], brand_ids=[brand.pk],
            effective_from=timezone.now() - timedelta(days=1),
        )
        actor = cast(Any, SimpleNamespace(
            tenant_id=tenant.pk,
            can=lambda action, *, site_id=None, brand_id=None: (
                action == "access.manage" and site_id == site.pk and brand_id == brand.pk
            ),
        ))
        admin.require_login_in_scope(actor, login.pk)
        actor.can = lambda action, *, site_id=None, brand_id=None: False
        with pytest.raises(Refusal) as denied:
            admin.require_login_in_scope(actor, login.pk)
        assert denied.value.code == "NOT_FOUND"

        scoped_human = HumanIdentity.objects.create(
            tenant=tenant, staff_code="SO03-SCOPED-OWNER", display_name="Scoped Owner",
        )
        scoped_role = Role.objects.create(
            tenant=tenant, code="owner", name="Owner", section_access=section_access_for("owner"),
        )
        scoped_user = User.objects.create(
            tenant=tenant, human=scoped_human, username=f"so03-{uuid.uuid4().hex[:12]}",
            email=f"so03-{uuid.uuid4().hex[:12]}@example.test",
        )
        RoleAssignment.objects.create(
            tenant=tenant, human=scoped_human, role=scoped_role,
            site_ids=[site.pk], brand_ids=[brand.pk],
            effective_from=timezone.now() - timedelta(days=1),
        )
        scoped_access = AccessContext(
            user=scoped_user, human_id=scoped_human.pk, tenant_id=tenant.pk,
            session=None, grants=effective_grants(scoped_human.pk),
        )
        assert scoped_access.holds("access.manage")
        with pytest.raises(Refusal) as denied_list:
            admin.list_users(scoped_access, {})
        assert denied_list.value.code == "NOT_FOUND"


@pytest.mark.django_db
def test_admin_meta_has_no_legacy_permission_catalogue() -> None:
    tenant = Tenant.objects.create(
        code=f"so03-meta-{uuid.uuid4().hex[:8]}", name="SO-03 meta proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        actor = cast(Any, SimpleNamespace(
            tenant_id=tenant.pk,
            require_action=lambda _: None,
            site_ids=lambda _: set(),
        ))
        assert admin.admin_meta(actor) == {"sites": []}


@pytest.mark.django_db
def test_assignment_editor_never_offers_or_accepts_another_tenants_ids() -> None:
    tenants = [Tenant.objects.create(
        code=f"so03-choice-{uuid.uuid4().hex[:8]}", name="SO-03 choice proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    ) for _ in range(2)]
    choices: list[tuple[Role, Store, Brand]] = []
    for index, tenant in enumerate(tenants):
        with tenant_context(tenant.pk):
            role = Role.objects.create(tenant=tenant, code="owner", name="Owner")
            Role.objects.create(tenant=tenant, code="ho_ops", name="Old HO role", is_system=True)
            entity = LegalEntity.objects.create(tenant=tenant, code=f"choice-{index}", name="Entity")
            gstin = Gstin.objects.create(
                tenant=tenant, legal_entity=entity,
                gstin=f"10{uuid.uuid4().int % 10**13:013d}",
                state_code="10", state_name="Bihar",
            )
            site = Store.objects.create(tenant=tenant, gstin=gstin, code=f"choice-site-{index}", name="Site")
            brand = Brand.objects.create(tenant=tenant, code=f"choice-brand-{index}", name="Brand")
            choices.append((role, site, brand))

    with tenant_context(tenants[0].pk):
        offered = _choices(tenants[0].pk)
        assert [row["code"] for row in offered["roles"]] == ["owner"]
        assert [row["id"] for row in offered["sites"]] == [choices[0][1].pk]
        assert [row["id"] for row in offered["brands"]] == [choices[0][2].pk]
        with pytest.raises(Refusal) as denied:
            _parse_assignment({
                "role_code": "owner", "all_sites": False,
                "site_ids": [choices[1][1].pk], "all_brands": False,
                "brand_ids": [choices[0][2].pk],
            }, tenant_id=tenants[0].pk)
        assert denied.value.code == "NOT_FOUND"
