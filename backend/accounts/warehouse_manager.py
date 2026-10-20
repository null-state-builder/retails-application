"""The reusable "Warehouse manager" role policy (store test run, plan step 4).

A tenant-defined role, not a seventh fixed role code: it is created and
versioned through the same records the role-policy editor writes
(``Role`` with ``is_system=False`` plus a ``MasterVersion`` ``role_policy``
payload, see ``accounts.unified_admin.RolePolicyView``), so an administrator
can later review or narrow it in People & Access like any other custom role.

The policy starts from the ``warehouse`` row and adds only:

* ``pt.approve.transfer`` - the manager approves the warehouse's transfers.
  The drafter≠approver rule is unchanged (``approvals.goods_services.decide``
  and the transfer route's ``require_distinct``); the tenant's transfer
  approval policy must also name :data:`ROLE_CODE` among its roles.
* Home, Sell and Reports at **view** - store stock and sales are read, never
  billed. Billing keeps its ``store_person`` gate (``sell/services/online.py``),
  so a manager who bills needs a separate ``store_person`` assignment.
* Setup at operate plus ``org.site.lifecycle.run`` so store readiness opens.

Field access is ``cost`` only: transfer approval pins ``fields=["cost"]``
(``outbound.transfer_authority.submit``), and the warehouse-only
``cost_own_pt`` exception is never available to a custom role code.

Scope is the assignment's: :func:`assign` takes the warehouse and the store
it serves, and nothing outside those sites is reachable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
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

ROLE_CODE = "warehouse_manager"
ROLE_NAME = "Warehouse manager"
BASE_ROLE = "warehouse"
SERVICE = "seed"

ADDED_STEPS = ("pt.approve.transfer", "org.site.lifecycle.run")
FIELDS = ("cost",)
#: Sections raised to at least this rung; a higher base rung is kept.
SECTION_FLOOR = {"home": CAP_VIEW, "sell": CAP_VIEW, "reports": CAP_VIEW, "setup": CAP_OPERATE}


def policy() -> dict[str, Any]:
    """The reviewed policy payload, in the shape the role-policy editor stores."""
    sections = section_access_for(BASE_ROLE)
    for section, rung in SECTION_FLOOR.items():
        cell = dict(sections.get(section) or {})
        if not meets(str(cell.get("capability", "none")), rung):
            cell["capability"] = rung
            cell["label"] = "View" if rung == CAP_VIEW else "Operate"
        sections[section] = cell
    return {
        "section_access": sections,
        "field_access": sorted(FIELDS),
        "step_actions": sorted(set(initial_step_actions(BASE_ROLE)) | set(ADDED_STEPS)),
    }


def _service_principal(tenant_id: uuid.UUID) -> Any:
    from accounts.goods_setup import service_principal

    return service_principal(tenant_id, SERVICE)


def ensure_policy(tenant: Any) -> Role:
    """Create or refresh the tenant's Warehouse manager role through the kernel.

    Idempotent: when the stored policy already matches, nothing is written. A
    role with this code that the tenant marked as system-owned is refused
    rather than edited.
    """
    from accounts.floors import floor_violations
    from accounts.goods_admin_services import audit_values, record_version
    from accounts.sessions import bump_security_epoch
    from accounts.unified_admin import _revision

    wanted = policy()
    if floor_violations(ROLE_CODE, wanted["section_access"]):  # pragma: no cover - code invariant
        raise Refusal("ACTION_DENIED", "The warehouse manager policy crosses a fixed business floor.")

    with tenant_context(tenant.pk):
        role = Role.objects.filter(tenant_id=tenant.pk, code=ROLE_CODE).first()
        if role is not None and role.is_system:
            raise Refusal("STATE_CONFLICT", f"Role {ROLE_CODE} exists as a system role.")
        if role is not None and role.is_active and _current(role) == wanted and _has_version(tenant.pk):
            return role

        def handler(run: CommandRun) -> CommandResult:
            run.advisory_lock(LockRank.SECURITY, [f"role:{ROLE_CODE}"])
            row = Role.objects.filter(tenant_id=run.tenant_id, code=ROLE_CODE).first()
            before = _current(row) if row is not None else {}
            if row is None:
                row = Role.objects.create(
                    tenant_id=run.tenant_id, code=ROLE_CODE, name=ROLE_NAME,
                    description="Warehouse staff who approve transfers and oversee their linked store.",
                    is_active=True, is_system=False, section_access={},
                )
            from masters.goods_models import MasterVersion

            versioned = MasterVersion.objects.filter(
                tenant_id=run.tenant_id, kind="role", target_key=ROLE_CODE).exists()
            revision = _revision(run.tenant_id, "role", ROLE_CODE) if versioned else 0
            row.is_active = True
            row.section_access = wanted["section_access"]
            row.field_access = wanted["field_access"]
            row.permissions_map = {**(row.permissions_map or {}), "step_actions": wanted["step_actions"]}
            row.save()
            record_version(run, kind="role", target_key=ROLE_CODE, revision=revision + 1,
                           payload={"role_policy": wanted})
            for human_id in RoleAssignment.objects.filter(
                tenant_id=run.tenant_id, role=row
            ).values_list("human_id", flat=True).distinct():
                bump_security_epoch(human_id, run.tenant_id)
            run.audit_before = audit_values(before)
            run.audit_after = audit_values(wanted)
            run.audit_subject_key = f"role:{ROLE_CODE}"
            return CommandResult(resource_type="role_policy", resource_id=str(row.pk), revision=revision + 1)

        execute_command(
            _service_principal(tenant.pk),
            CommandSpec(action="access.role.policy", command_id=uuid.uuid4(),
                        business_input={"code": ROLE_CODE, "policy": wanted}),
            handler,
        )
        return Role.objects.get(tenant_id=tenant.pk, code=ROLE_CODE)


def assign(
    tenant: Any,
    human: Any,
    *,
    site_ids: Iterable[int],
    brand_ids: Iterable[int] | None = None,
    effective_from: datetime | None = None,
) -> RoleAssignment:
    """Assign the policy to one person for exactly ``site_ids`` (warehouse + store).

    ``brand_ids=None`` means all brands, which transfer approval needs for every
    piece it reviews. An open assignment of this role for the person is
    replaced only when its scope differs; the old row is closed, not edited.
    """
    sites = sorted({int(site) for site in site_ids})
    if not sites:
        raise Refusal("INVALID_REQUEST", "A warehouse manager needs at least one site.")
    from masters.models import Store

    with tenant_context(tenant.pk):
        if Store.objects.filter(tenant_id=tenant.pk, pk__in=sites).count() != len(sites):
            raise Refusal("SCOPE_DENIED", "Every site must belong to this tenant.")
        brands = None if brand_ids is None else sorted({int(brand) for brand in brand_ids})
        if brands is not None and not brands:
            raise Refusal("INVALID_REQUEST", "Pass brand_ids=None for all brands, or name at least one.")
        role = ensure_policy(tenant)
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
                if (sorted(row.site_ids) == sites and not row.all_sites
                        and row.all_brands == (brands is None) and sorted(row.brand_ids) == (brands or [])):
                    holder["row"] = row
                    return CommandResult(resource_type="role_assignment", resource_id=str(row.pk))
            for row in open_rows:
                row.effective_to = max(start, row.effective_from)
                row.save(update_fields=["effective_to"])
            holder["row"] = RoleAssignment.objects.create(
                tenant_id=run.tenant_id, human_id=human.pk, role=role, effective_from=start,
                all_sites=False, site_ids=sites,
                all_brands=brands is None, brand_ids=brands or [],
            )
            bump_security_epoch(human.pk, run.tenant_id)
            run.audit_subject_key = f"person:{human.pk}"
            return CommandResult(resource_type="role_assignment", resource_id=str(holder["row"].pk))

        execute_command(
            _service_principal(tenant.pk),
            CommandSpec(action="access.assignment.replace", command_id=uuid.uuid4(),
                        business_input={"target": str(human.pk), "role": ROLE_CODE,
                                        "site_ids": sites, "brand_ids": brands}),
            handler,
        )
        return holder["row"]


def _current(role: Role | None) -> dict[str, Any]:
    if role is None:
        return {}
    return {
        "section_access": role.section_access,
        "field_access": sorted(role.field_access or []),
        "step_actions": sorted((role.permissions_map or {}).get("step_actions", [])),
    }


def _has_version(tenant_id: Any) -> bool:
    from masters.goods_models import MasterVersion

    return MasterVersion.objects.filter(
        tenant_id=tenant_id, kind="role", target_key=ROLE_CODE, payload__has_key="role_policy"
    ).exists()


__all__ = ["ROLE_CODE", "ROLE_NAME", "assign", "ensure_policy", "policy"]