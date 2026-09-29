"""Stored who-may-act policy, above the immutable people-rule floor (#131)."""

from __future__ import annotations

from typing import Any

from accounts.models import ActorPolicy


def user_may_act(user: Any, action: str) -> bool:
    """Read the live policy for every decision; a missing row fails closed."""
    if not (user and getattr(user, "is_authenticated", False)):
        return False
    if getattr(user, "is_superuser", False):
        return True
    role_code = getattr(getattr(user, "role", None), "code", "")
    if not role_code:
        return False
    roles = ActorPolicy.objects.filter(action=action).values_list("roles", flat=True).first()
    return roles is not None and role_code in roles


def actions_for(user: Any) -> list[str]:
    """Every stored action this person may take — the client's copy of the above.

    Sent with the user so a screen can stop offering a button the server is
    going to refuse. The capability ladder cannot answer these: the matrix puts
    a store and the warehouse on the same rung of Return to Brand, and it is a
    policy row that says only one of them sends stock back. Hiding the button is
    never the boundary — `user_may_act` on the request still is.
    """
    if not (user and getattr(user, "is_authenticated", False)):
        return []
    all_actions = list(ActorPolicy.objects.values_list("action", flat=True))
    if getattr(user, "is_superuser", False):
        return sorted(all_actions)
    role_code = getattr(getattr(user, "role", None), "code", "")
    if not role_code:
        return []
    return sorted(
        action
        for action, roles in ActorPolicy.objects.values_list("action", "roles")
        if role_code in roles
    )
