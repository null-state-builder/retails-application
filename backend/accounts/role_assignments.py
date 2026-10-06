"""Effective role assignments and the six-role protected-field baseline.

Each assignment carries one indivisible site/brand scope tuple. Consumers must
evaluate a required action and its scope against the *same* assignment; unions
of roles, sites, brands, or fields across rows would widen a person's authority.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from django.db.models import Q

from accounts.goods_models import RoleAssignment
from accounts.sections import CAP_VIEW, meets
from core.commands import database_now
from core.tenancy import require_tenant_id

INITIAL_ROLE_CODES = frozenset(
    {"owner", "store_person", "warehouse", "brand_manager", "accounts", "it_admin"}
)

# The keys are protected-field categories, not permissions copied onto an
# assignment. Resource projectors interpret the category and its row predicate.
INITIAL_FIELD_ACCESS: dict[str, list[str]] = {
    "owner": ["financial", "employee_private", "customer", "cost", "margin", "layer_value"],
    "store_person": ["customer"],
    "warehouse": ["cost_own_pt"],
    "brand_manager": ["cost", "margin"],
    "accounts": ["financial", "employee_private", "billing_identity", "cost", "margin", "layer_value"],
    "it_admin": [],
}


def effective_assignments(
    human_id: uuid.UUID, at: datetime | None = None
) -> list[RoleAssignment]:
    """Return active, tenant-bound assignments without deriving any wider union.

    Empty selected membership in either dimension grants no site/brand cell, so
    it is omitted from the runtime answer. This also makes a not-yet-resolved
    assignment harmless if it was created with an empty selected scope.
    """

    moment = at or database_now()
    rows: Any = (
        RoleAssignment.objects.select_related("role", "human")
        .filter(
            tenant_id=require_tenant_id(),
            human_id=human_id,
            human__active=True,
            role__is_active=True,
            effective_from__lte=moment,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=moment))
        .filter(Q(revoked_at__isnull=True) | Q(revoked_at__gt=moment))
        .filter(Q(human__staff__retired_at__isnull=True) | Q(human__staff__retired_at__gt=moment))
        .filter(Q(all_sites=True) | ~Q(site_ids=[]))
        .filter(Q(all_brands=True) | ~Q(brand_ids=[]))
        .order_by("effective_from", "id")
    )
    return list(rows)


def has_effective_role(
    user: Any,
    allowed_codes: Iterable[str],
    *,
    section: str | None = None,
    minimum: str | None = None,
    site_id: int | None = None,
    brand_id: int | None = None,
) -> bool:
    """Bridge a legacy role-only gate to the new, indivisible assignments.

    This does not substitute for an action decision: a caller with a named
    action should use ``AccessContext.can`` so workflow rules and protected
    fields are checked too. The helper exists for role-only gates while those
    workflows move to their target services. ``None`` is an unscoped resource
    cell: only explicit all-sites/all-brands covers it. It never means "some
    scope somewhere" or permits two assignments to contribute separate axes.
    """

    if minimum is not None and section is None:
        raise ValueError("minimum requires a section")
    human_id = getattr(user, "human_id", None)
    if (
        not getattr(user, "is_active", False)
        or human_id is None
        or getattr(user, "tenant_id", None) != require_tenant_id()
    ):
        return False
    if site_id is not None:
        from masters.models import Store

        if not Store.objects.filter(tenant_id=require_tenant_id(), pk=site_id).exists():
            return False
    if brand_id is not None:
        from masters.models import Brand

        if not Brand.objects.filter(tenant_id=require_tenant_id(), pk=brand_id).exists():
            return False
    allowed = frozenset(allowed_codes)
    for assignment in effective_assignments(human_id):
        if assignment.role.code not in allowed:
            continue
        if not (assignment.all_sites or (site_id is not None and site_id in assignment.site_ids)):
            continue
        if not (assignment.all_brands or (brand_id is not None and brand_id in assignment.brand_ids)):
            continue
        if section is not None:
            entry = (assignment.role.section_access or {}).get(section, {})
            held = entry.get("capability", "none") if isinstance(entry, dict) else "none"
            if not meets(held, minimum or CAP_VIEW):
                continue
        return True
    return False
