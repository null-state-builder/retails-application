"""Tenant-defined roles the store test run seeds (not fixed role codes).

Each role is created and versioned through the same records the role-policy
editor writes (``Role`` with ``is_system=False`` plus a ``MasterVersion``
``role_policy`` payload, see ``accounts.unified_admin.RolePolicyView``), so an
administrator can later review or narrow it in People & Access like any other
custom role. ``accounts.warehouse_manager`` and ``accounts.operations_head``
describe their policies; this module writes them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from accounts.goods_models import RoleAssignment
from accounts.models import Role
from accounts.rbac_matrix import section_access_for
from accounts.sections import CAP_OPERATE, CAP_VIEW, meets
from accounts.unified_policy import initial_step_actions
from core.commands import CommandResult, CommandRun, CommandSpec, LockRank, execute_command
from core.refusals import Refusal
from core.tenancy import tenant_context

SERVICE = "seed"
LABELS = {CAP_VIEW: "View", CAP_OPERATE: "Operate"}


@dataclass(frozen=True)
class CustomRole:
    code: str
    name: str
    description: str
    #: The matrix row the sections start from.
    base: str
    #: Step actions on top of ``base``'s initial ones.
    added_steps: tuple[str, ...]
    fields: tuple[str, ...]
    #: Sections raised to at least this rung; a higher base rung is kept.
    section_floor: Mapping[str, str] = field(default_factory=dict)
    #: Sections set to exactly this (capability, label), up or down.
    section_set: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    #: Steps the base row would bring that this role must not hold.
    removed_steps: tuple[str, ...] = ()

    def policy(self) -> dict[str, Any]:
        """The reviewed policy payload, in the shape the role-policy editor stores."""
        sections = section_access_for(self.base)
        for section, rung in self.section_floor.items():
            cell = dict(sections.get(section) or {})
            if not meets(str(cell.get("capability", "none")), rung):
                cell["capability"] = rung
                cell["label"] = LABELS.get(rung, rung.title())
            sections[section] = cell
        for section, (capability, label) in self.section_set.items():
            sections[section] = {"capability": capability, "label": label}
        steps = (set(initial_step_actions(self.base)) | set(self.added_steps)) - set(self.removed_steps)
        return {"section_access": sections, "field_access": sorted(self.fields), "step_actions": sorted(steps)}

    def ensure_policy(self, tenant: Any) -> Role:
        """Create or refresh the tenant's role through the kernel.

        Idempotent: when the stored policy already matches, nothing is written. A
        role with this code that the tenant marked as system-owned is refused
        rather than edited.
        """
        from accounts.floors import floor_violations
        from accounts.goods_admin_services import audit_values, record_version
        from accounts.sessions import bump_security_epoch
        from accounts.unified_admin import _revision

        wanted = self.policy()
        if floor_violations(self.code, wanted["section_access"]):  # pragma: no cover - code invariant
            raise Refusal("ACTION_DENIED", f"The {self.name.lower()} policy crosses a fixed business floor.")

        with tenant_context(tenant.pk):
            role = Role.objects.filter(tenant_id=tenant.pk, code=self.code).first()
            if role is not None and role.is_system:
                raise Refusal("STATE_CONFLICT", f"Role {self.code} exists as a system role.")
            if role is not None and role.is_active and _current(role) == wanted and self._has_version(tenant.pk):
                return role

            def handler(run: CommandRun) -> CommandResult:
                run.advisory_lock(LockRank.SECURITY, [f"role:{self.code}"])
                row = Role.objects.filter(tenant_id=run.tenant_id, code=self.code).first()
                before = _current(row) if row is not None else {}
                if row is None:
                    row = Role.objects.create(
                        tenant_id=run.tenant_id, code=self.code, name=self.name, description=self.description,
                        is_active=True, is_system=False, section_access={},
                    )
                from masters.goods_models import MasterVersion

                versioned = MasterVersion.objects.filter(
                    tenant_id=run.tenant_id, kind="role", target_key=self.code).exists()
                revision = _revision(run.tenant_id, "role", self.code) if versioned else 0
                row.is_active = True
                row.section_access = wanted["section_access"]
                row.field_access = wanted["field_access"]
                row.permissions_map = {**(row.permissions_map or {}), "step_actions": wanted["step_actions"]}
                row.save()
                record_version(run, kind="role", target_key=self.code, revision=revision + 1,
                               payload={"role_policy": wanted})
                for human_id in RoleAssignment.objects.filter(
                    tenant_id=run.tenant_id, role=row
                ).values_list("human_id", flat=True).distinct():
                    bump_security_epoch(human_id, run.tenant_id)
                run.audit_before = audit_values(before)
                run.audit_after = audit_values(wanted)
                run.audit_subject_key = f"role:{self.code}"
                return CommandResult(resource_type="role_policy", resource_id=str(row.pk), revision=revision + 1)

            execute_command(
                _service_principal(tenant.pk),
                CommandSpec(action="access.role.policy", command_id=uuid.uuid4(),
                            business_input={"code": self.code, "policy": wanted}),
                handler,
            )
            return Role.objects.get(tenant_id=tenant.pk, code=self.code)

    def assign(
        self,
        tenant: Any,
        human: Any,
        *,
        site_ids: Iterable[int] = (),
        all_sites: bool = False,
        brand_ids: Iterable[int] | None = None,
        effective_from: datetime | None = None,
    ) -> RoleAssignment:
        """Assign the policy to one person for exactly ``site_ids``, or every site.

        ``brand_ids=None`` means all brands. An open assignment of this role for
        the person is replaced only when its scope differs; the old row is closed,
        not edited.
        """
        sites = [] if all_sites else sorted({int(site) for site in site_ids})
        if not all_sites and not sites:
            raise Refusal("INVALID_REQUEST", f"A {self.name.lower()} needs at least one site.")
        from masters.models import Store

        with tenant_context(tenant.pk):
            if Store.objects.filter(tenant_id=tenant.pk, pk__in=sites).count() != len(sites):
                raise Refusal("SCOPE_DENIED", "Every site must belong to this tenant.")
            brands = None if brand_ids is None else sorted({int(brand) for brand in brand_ids})
            if brands is not None and not brands:
                raise Refusal("INVALID_REQUEST", "Pass brand_ids=None for all brands, or name at least one.")
            role = self.ensure_policy(tenant)
            holder: dict[str, RoleAssignment] = {}

            def handler(run: CommandRun) -> CommandResult:
                from accounts.goods_admin_services import lock_person
                from accounts.sessions import bump_security_epoch

                lock_person(run, human.pk)
                start = effective_from or run.now
                open_rows = list(RoleAssignment.objects.filter(
                    tenant_id=run.tenant_id, human_id=human.pk, role=role, revoked_at__isnull=True,
                    effective_to__isnull=True,
                ))
                for row in open_rows:
                    if (sorted(row.site_ids) == sites and row.all_sites == all_sites
                            and row.all_brands == (brands is None) and sorted(row.brand_ids) == (brands or [])):
                        holder["row"] = row
                        return CommandResult(resource_type="role_assignment", resource_id=str(row.pk))
                for row in open_rows:
                    row.effective_to = max(start, row.effective_from)
                    row.save(update_fields=["effective_to"])
                holder["row"] = RoleAssignment.objects.create(
                    tenant_id=run.tenant_id, human_id=human.pk, role=role, effective_from=start,
                    all_sites=all_sites, site_ids=sites,
                    all_brands=brands is None, brand_ids=brands or [],
                )
                bump_security_epoch(human.pk, run.tenant_id)
                run.audit_subject_key = f"person:{human.pk}"
                return CommandResult(resource_type="role_assignment", resource_id=str(holder["row"].pk))

            execute_command(
                _service_principal(tenant.pk),
                CommandSpec(action="access.assignment.replace", command_id=uuid.uuid4(),
                            business_input={"target": str(human.pk), "role": self.code, "site_ids": sites,
                                            "brand_ids": brands, **({"all_sites": True} if all_sites else {})}),
                handler,
            )
            return holder["row"]

    def _has_version(self, tenant_id: Any) -> bool:
        from masters.goods_models import MasterVersion

        return MasterVersion.objects.filter(
            tenant_id=tenant_id, kind="role", target_key=self.code, payload__has_key="role_policy"
        ).exists()


def _service_principal(tenant_id: uuid.UUID) -> Any:
    from accounts.goods_setup import service_principal

    return service_principal(tenant_id, SERVICE)


def _current(role: Role | None) -> dict[str, Any]:
    if role is None:
        return {}
    return {
        "section_access": role.section_access,
        "field_access": sorted(role.field_access or []),
        "step_actions": sorted((role.permissions_map or {}).get("step_actions", [])),
    }


__all__ = ["CustomRole"]
