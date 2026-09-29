"""Fixed access-administration safeguards around the versioned policy."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from accounts.goods_models import RoleAssignment
from accounts.unified_policy import role_actions, workflow_levels
from core.refusals import Refusal


def changes_authority(action: str) -> bool:
    return action.startswith("access.") or action == "staff.retire"


def independent_reviewers(tenant_id: Any, site_id: int | None, changer: Any) -> list[str]:
    """Capture authority before the change so it cannot create its own checker."""
    from accounts.models import User
    from accounts.principal import AccessContext, effective_grants

    reviewers = []
    for user in User.objects.filter(tenant_id=tenant_id, is_active=True, human__active=True).exclude(human_id=changer).order_by("human_id"):
        if user.human_id is None:
            continue
        access = AccessContext(user=user, human_id=user.human_id, tenant_id=tenant_id,
                               session=None, grants=effective_grants(user.human_id))
        if access.can("access.review", site_id=site_id):
            reviewers.append(str(user.human_id))
    return sorted(set(reviewers))


def require_administrator_continuity(tenant_id: Any, now: datetime) -> None:
    """Retain a usable tenant administrator, including at scheduled boundaries.

    The command holds the tenant access lock before taking any person's security
    lock. All current and scheduled administrator intervals are considered;
    scheduling the last administrator's expiry is also a lockout.
    """
    levels = workflow_levels(tenant_id)
    rows = list(RoleAssignment.objects.select_related("role", "human").filter(
        tenant_id=tenant_id, all_sites=True, all_brands=True,
        human__active=True, role__is_active=True,
    ))
    from accounts.models import User
    from accounts.goods_models import Staff

    logins = set(User.objects.filter(
        tenant_id=tenant_id, is_active=True, human_id__in=[r.human_id for r in rows],
    ).values_list("human_id", flat=True))
    retirements = dict(Staff.objects.filter(
        tenant_id=tenant_id, human_id__in=logins,
    ).values_list("human_id", "retired_at"))
    intervals: list[tuple[datetime, datetime | None]] = []
    for row in rows:
        if row.human_id not in logins or "access.manage" not in role_actions(row.role, action_levels=levels):
            continue
        ends = [v for v in (row.effective_to, row.revoked_at, retirements.get(row.human_id)) if v is not None]
        end = min(ends) if ends else None
        if end is not None and (end <= now or end <= row.effective_from):
            continue
        intervals.append((row.effective_from, end))
    boundaries = {now, *(start for start, _ in intervals if start > now),
                  *(end for _, end in intervals if end is not None and end > now)}
    if any(not any(start <= at and (end is None or at < end) for start, end in intervals)
           for at in boundaries):
        raise Refusal("LAST_ACCESS_ADMIN", "Keep an active tenant-wide access administrator, including after scheduled changes.")
