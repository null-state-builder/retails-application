"""The "Operations head" role (store test run, The Wardrobe, 28 Oct 2026).

A tenant-defined role written through ``accounts.custom_roles``, so an
administrator can review or narrow it in People & Access. It starts from the
retired ``ho_ops`` matrix row (the network operator) and is assigned over every
site:

* Home, Sell, Stock, Receive goods and Reports at **view** - stock and sales are
  read, never billed or received.
* ``pt.approve.transfer`` - approves transfers that still need a second person
  (store → warehouse returns; warehouse → store needs none at The Wardrobe). The
  tenant's transfer approval policy must name :data:`ROLE_CODE`.
* ``count.review`` - approves count differences.
* ``opening.import.stage`` - uploads a store's PT files and legacy SOH exports
  (29 Oct 2026), like the store manager and the warehouse.
* No Money, no Setup, and no opening approval: those stay with the owner. Booking
  and offers are lowered to view.

Field access is ``cost``: transfer approval pins ``fields=["cost"]``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from accounts.custom_roles import CustomRole
from accounts.goods_models import RoleAssignment
from accounts.models import Role
from accounts.sections import CAP_NONE, CAP_VIEW

ROLE_CODE = "operations_head"
ROLE = CustomRole(
    code=ROLE_CODE,
    name="Operations head",
    description="Oversees every store and warehouse: reads stock and sales, approves transfers and count differences.",
    base="ho_ops",
    added_steps=("pt.approve.transfer", "count.review", "opening.import.stage"),
    fields=("cost",),
    section_floor={"home": CAP_VIEW, "sell": CAP_VIEW, "reports": CAP_VIEW, "stock": CAP_VIEW},
    section_set={"booking": (CAP_VIEW, "View"), "offers_price": (CAP_VIEW, "View"),
                 "money": (CAP_NONE, "No"), "setup": (CAP_NONE, "No")},
)


def ensure_policy(tenant: Any) -> Role:
    return ROLE.ensure_policy(tenant)


def assign(tenant: Any, human: Any, *, site_ids: Iterable[int] = (), all_sites: bool = True,
           effective_from: datetime | None = None) -> RoleAssignment:
    """Every site by default; all brands."""
    return ROLE.assign(tenant, human, site_ids=site_ids, all_sites=all_sites, effective_from=effective_from)


__all__ = ["ROLE_CODE", "ROLE", "assign", "ensure_policy"]
