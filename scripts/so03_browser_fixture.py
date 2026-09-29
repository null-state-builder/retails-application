"""Prepare explicit policy upgrade and future assignment in disposable proof data."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from accounts.actions import SECTION_ACTIONS
from accounts.goods_models import RoleAssignment
from accounts.models import Role, User
from accounts.sessions import issue_session
from accounts.unified_admin import RolePolicyView, WorkflowPolicyView
from accounts.unified_policy import ACTION_LEVELS, serialise_workflow_levels
from core.tenancy import tenant_context
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate


def main() -> None:
    if os.environ.get("KDPS_PROOF_MODE") != "1":
        raise RuntimeError("SO-03 browser fixture only runs against proof data")
    owner = User.objects.select_related("tenant").get(email="owner@kdps.demo")
    target = User.objects.get(email="deo.manager@kdps.demo", tenant=owner.tenant)
    if not owner.tenant.synthetic or target.human_id is None:
        raise RuntimeError("The proof tenant or target identity was not verified")
    with tenant_context(owner.tenant_id):
        factory = APIRequestFactory()
        session = issue_session(owner).session
        read = factory.get("/api/auth/admin/workflow-policy")
        force_authenticate(read, owner, session)
        policy = WorkflowPolicyView.as_view()(read)
        if policy.status_code != 200:
            raise RuntimeError(f"Proof policy read failed: {policy.status_code}")
        section_defaults = serialise_workflow_levels({
            action: ACTION_LEVELS[action] for action in SECTION_ACTIONS.values()
        })
        section_upgrade = {
            action: value for action, value in section_defaults.items()
            if policy.data["action_levels"].get(action) != value
        }
        if section_upgrade:
            # Test setup explicitly approves the new vocabulary in this synthetic
            # tenant. The fixture deliberately restores its section baseline;
            # specialist steps remain unchanged and no real tenant is changed.
            write = factory.put("/api/auth/admin/workflow-policy", {
                "command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
                "expected_revision": policy.data["revision"],
                "action_levels": {**policy.data["action_levels"], **section_upgrade},
                "current_password": "Owner@123",
            }, format="json")
            force_authenticate(write, owner, session)
            applied = WorkflowPolicyView.as_view()(write)
            if applied.status_code != 200:
                raise RuntimeError(f"Proof policy upgrade failed: {applied.status_code} {applied.data}")
            print(f"Proof section policy upgrade: revision {applied.data['revision']}")
        session = issue_session(owner).session
        read_role = factory.get("/api/auth/admin/roles/owner/policy")
        force_authenticate(read_role, owner, session)
        role_policy = RolePolicyView.as_view()(read_role, code="owner")
        if role_policy.status_code != 200:
            raise RuntimeError(f"Proof role policy read failed: {role_policy.status_code}")
        if "staff.manage" not in role_policy.data["step_actions"]:
            write_role = factory.put("/api/auth/admin/roles/owner/policy", {
                "command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
                "expected_revision": role_policy.data["revision"],
                "section_access": role_policy.data["section_access"],
                "field_access": role_policy.data["field_access"],
                "step_actions": sorted({*role_policy.data["step_actions"], "staff.manage"}),
                "current_password": "Owner@123",
            }, format="json")
            force_authenticate(write_role, owner, session)
            applied_role = RolePolicyView.as_view()(write_role, code="owner")
            if applied_role.status_code != 200:
                raise RuntimeError(f"Proof staff policy upgrade failed: {applied_role.status_code} {applied_role.data}")
            print(f"Proof Owner staff policy upgrade: revision {applied_role.data['revision']}")
        role = Role.objects.get(tenant=owner.tenant, code="warehouse")
        marker = uuid.uuid5(uuid.NAMESPACE_URL, f"so03-browser-scheduled:{owner.tenant_id}:{target.human_id}")
        row, _ = RoleAssignment.objects.update_or_create(
            pk=marker,
            defaults={
                "tenant": owner.tenant, "human_id": target.human_id, "role": role,
                "all_sites": True, "site_ids": [], "all_brands": True, "brand_ids": [],
                "effective_from": timezone.now() + timedelta(days=7),
                "effective_to": None, "revoked_at": None,
            },
        )
        print(f"Proof scheduled assignment: {row.pk}")


if __name__ == "__main__":
    main()
