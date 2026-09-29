"""The canonical editor retains scheduled assignment identity and dates."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_models import HumanIdentity, RoleAssignment, ServerSession
from accounts.models import Role, User
from accounts.rbac_matrix import section_access_for
from accounts.sessions import issue_session
from accounts.unified_admin import UserAssignmentsView
from core.tenancy import tenant_context
from masters.goods_models import Tenant


@pytest.mark.django_db
def test_future_assignment_edit_keeps_id_and_start_then_removal_revokes() -> None:
    tenant = Tenant.objects.create(
        code=f"scheduled-{uuid.uuid4().hex[:8]}", name="Scheduled proof",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        owner = Role.objects.create(
            tenant=tenant, code="owner", name="Owner", section_access=section_access_for("owner"),
        )
        warehouse = Role.objects.create(
            tenant=tenant, code="warehouse", name="Warehouse", section_access=section_access_for("warehouse"),
        )
        actor_human = HumanIdentity.objects.create(tenant=tenant, staff_code="EDITOR", display_name="Editor")
        target_human = HumanIdentity.objects.create(tenant=tenant, staff_code="TARGET", display_name="Target")
        actor = User.objects.create(
            tenant=tenant, human=actor_human, username=f"editor-{uuid.uuid4().hex[:8]}",
            email=f"editor-{uuid.uuid4().hex[:8]}@example.test",
        )
        actor.set_password("proof-password")
        actor.save(update_fields=["password"])
        target = User.objects.create(
            tenant=tenant, human=target_human, username=f"target-{uuid.uuid4().hex[:8]}",
        )
        RoleAssignment.objects.create(
            tenant=tenant, human=actor_human, role=owner, all_sites=True,
            all_brands=True, effective_from=timezone.now() - timedelta(days=1),
        )
        start = timezone.now() + timedelta(days=4)
        row = RoleAssignment.objects.create(
            tenant=tenant, human=target_human, role=warehouse, all_sites=True,
            all_brands=True, effective_from=start,
        )
        session = cast(ServerSession, issue_session(actor).session)

        def call(revision: int, assignments: list[dict[str, Any]]) -> Any:
            request = APIRequestFactory().put(
                f"/api/auth/admin/users/{target.pk}/assignments",
                {"command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
                 "expected_revision": revision, "current_password": "proof-password",
                 "assignments": assignments},
                format="json",
            )
            force_authenticate(request, user=actor, token=cast(Any, session))
            return UserAssignmentsView.as_view()(request, pk=target.pk)

        payload = {
            "id": str(row.pk), "effective_from": start.isoformat(),
            "role_code": "warehouse", "all_sites": True, "site_ids": [],
            "all_brands": True, "brand_ids": [], "effective_to": None,
        }
        unchanged = call(1, [payload])
        assert unchanged.status_code == 200, unchanged.data
        row.refresh_from_db()
        assert row.effective_from == start and row.revoked_at is None
        edited = call(2, [{**payload, "effective_to": (start + timedelta(days=2)).isoformat()}])
        assert edited.status_code == 200, edited.data
        row.refresh_from_db()
        assert row.effective_from == start
        assert row.effective_to == start + timedelta(days=2)
        assert row.revoked_at is None
        assert RoleAssignment.objects.filter(tenant=tenant, human=target_human).count() == 1
        removed = call(3, [])
        assert removed.status_code == 200, removed.data
        row.refresh_from_db()
        assert row.revoked_at is not None
        assert removed.data["items"] == []
