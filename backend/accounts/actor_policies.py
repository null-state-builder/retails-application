"""Legacy action names evaluated by the unified assignment policy.

The old ``ActorPolicy`` rows are preserved as history and grant no access.
"""

from __future__ import annotations

from typing import Any

from accounts.role_assignments import effective_assignments
from accounts.sections import CAP_MANAGE, CAP_OPERATE, meets
from accounts.unified_policy import role_capability

LEGACY_ACTION_RULES: dict[str, tuple[str, str, frozenset[str]]] = {
    "masters.writes": ("setup", CAP_MANAGE, frozenset({"owner", "it_admin"})),
    "ptmapper.post_and_reverse_pt": ("money", CAP_MANAGE, frozenset({"owner", "accounts"})),
    "ptmapper.mapping_stewardship": ("receive_goods", CAP_OPERATE, frozenset({"warehouse"})),
    "outbound.execute_vflip": ("money", CAP_MANAGE, frozenset({"owner", "accounts"})),
    "outbound.create_return_to_brand": ("return_to_brand", CAP_OPERATE, frozenset({"warehouse"})),
}


def user_may_act(user: Any, action: str) -> bool:
    if not (user and getattr(user, "is_authenticated", False) and getattr(user, "human_id", None)):
        return False
    rule = LEGACY_ACTION_RULES.get(action)
    if rule is None:
        return False
    section, minimum, roles = rule
    return any(
        assignment.role.code in roles
        and meets(role_capability(assignment.role, section), minimum)
        for assignment in effective_assignments(user.human_id)
    )


def actions_for(user: Any) -> list[str]:
    """UI hints for old screens; requests are still checked server-side."""
    return sorted(action for action in LEGACY_ACTION_RULES if user_may_act(user, action))
