"""The reusable "Warehouse manager" role policy (store test run, plan step 4).

A tenant-defined role, not a seventh fixed role code, written through
``accounts.custom_roles`` so an administrator can later review or narrow it in
People & Access like any other custom role.

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

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from accounts.custom_roles import CustomRole
from accounts.goods_models import RoleAssignment
from accounts.models import Role
from accounts.sections import CAP_OPERATE, CAP_VIEW

ROLE_CODE = "warehouse_manager"
ROLE_NAME = "Warehouse manager"
BASE_ROLE = "warehouse"

ADDED_STEPS = ("pt.approve.transfer", "org.site.lifecycle.run")
FIELDS = ("cost",)
#: Sections raised to at least this rung; a higher base rung is kept.
SECTION_FLOOR = {"home": CAP_VIEW, "sell": CAP_VIEW, "reports": CAP_VIEW, "setup": CAP_OPERATE}

ROLE = CustomRole(code=ROLE_CODE, name=ROLE_NAME, base=BASE_ROLE, added_steps=ADDED_STEPS, fields=FIELDS,
                  description="Warehouse staff who approve transfers and oversee their linked store.",
                  section_floor=SECTION_FLOOR)


def policy() -> dict[str, Any]:
    """The reviewed policy payload, in the shape the role-policy editor stores."""
    return ROLE.policy()


def ensure_policy(tenant: Any) -> Role:
    """Create or refresh the tenant's Warehouse manager role through the kernel (idempotent)."""
    return ROLE.ensure_policy(tenant)


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
    return ROLE.assign(tenant, human, site_ids=site_ids, brand_ids=brand_ids, effective_from=effective_from)


__all__ = ["ROLE_CODE", "ROLE_NAME", "assign", "ensure_policy", "policy"]
