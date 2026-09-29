"""Server-side section enforcement for the SIDEBAR RBAC contract (issue #85).

The sidebar is not a security boundary — hiding a menu item does nothing if the
API behind it still answers. So the same section→capability data that shapes a
user's sidebar also gates the API: ``require_section(section, minimum)`` is a
DRF permission any view can carry, and it resolves the acting user's capability
from ``Role.section_access`` (the DB authority), fail-closed.

Resolution order (all fail-closed): anonymous or unassigned people have no
section authority; otherwise each effective role assignment contributes only
its configured section level inside that assignment's scope.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request

from accounts.role_assignments import effective_assignments
from accounts.sections import (
    CAP_NONE,
    CAP_VIEW,
    CAPABILITY_RANK,
    CAPABILITY_WORDS,
    SECTIONS,
    is_valid_section,
    meets,
)


def _resolve_section(user: Any, section: str) -> tuple[str, str]:
    """Best display rung among effective assignments, never business authority.

    Resource access still evaluates the requested section and scope inside the
    *same* assignment.  This projection only decides which navigation groups
    may be offered to a signed-in person.
    """
    if not (user and getattr(user, "is_authenticated", False)):
        return CAP_NONE, ""
    human_id = getattr(user, "human_id", None)
    if human_id is None:
        return CAP_NONE, ""
    from accounts.principal import access_for_user

    access = access_for_user(user)
    best = next((level for level in reversed(CAPABILITY_RANK)
                 if level != CAP_NONE and access.section_grants(section, level)), CAP_NONE)
    return best, CAPABILITY_WORDS[best] if best != CAP_NONE else ""


def user_can_at(
    user: Any, section: str, minimum: str = CAP_VIEW, *,
    site_id: int | None = None, brand_id: int | None = None,
) -> bool:
    """Check one section rung and one resource against the same assignment."""
    from accounts.principal import access_for_user

    return access_for_user(user).can_section(section, minimum, site_id=site_id, brand_id=brand_id)


def user_section_capability(user: Any, section: str) -> str:
    """The capability ``user`` holds on ``section`` — ``none`` if any doubt."""
    return _resolve_section(user, section)[0]


def user_can(user: Any, section: str, minimum: str = CAP_VIEW) -> bool:
    """Does ``user`` reach at least ``minimum`` capability on ``section``?"""
    return meets(user_section_capability(user, section), minimum)


def visible_sections(user: Any) -> list[dict[str, str | int]]:
    """The sections ``user`` may see, in sidebar order, with their capability.

    Additive workflow steps may make a section navigable even with no section
    rung.  They are returned with capability ``none`` so legacy section gates
    cannot mistake the navigation hint for broader read permission.
    """
    out: list[dict[str, str | int]] = []
    from accounts.unified_policy import role_actions, workflow_levels

    human_id = getattr(user, "human_id", None)
    assignment_roles = [row.role for row in effective_assignments(human_id)] if human_id else []
    levels = workflow_levels(getattr(user, "tenant_id", None)) if assignment_roles else {}
    step_sections = {
        levels[action][0]
        for role in assignment_roles
        for action in role_actions(role, action_levels=levels)
        if action in levels
    }
    for order, (code, label) in enumerate(SECTIONS):
        capability, scope_label = _resolve_section(user, code)
        if not meets(capability, CAP_VIEW) and code not in step_sections:
            continue
        out.append(
            {
                "code": code,
                "label": label,
                "order": order,
                "capability": capability,
                "scope_label": scope_label,
            }
        )
    return out


def require_section(
    section: str, minimum: str = CAP_VIEW, *, write_minimum: str | None = None
) -> type[BasePermission]:
    """Build a DRF permission gating a view behind a section capability.

    ``write_minimum`` is for the one view that both lists and creates: reads
    answer at ``minimum``, writes at the higher rung. Without it a
    list-and-create endpoint has to pick one rung for both, and picking the
    lower one is how a ``view`` cell quietly becomes a create.
    """
    if not is_valid_section(section):  # pragma: no cover - programmer error
        raise ValueError(f"Unknown section {section!r}")

    read_only = f"You do not have access to the {section} section."
    write_denied = f"You may read the {section} section, but not write in it."

    class _HasSectionAccess(BasePermission):
        message = read_only

        def has_permission(self, request: Request, view: Any) -> bool:
            writing = write_minimum is not None and request.method not in SAFE_METHODS
            needed = minimum
            if writing and write_minimum is not None:
                needed = write_minimum
            allowed = user_can(request.user, section, needed)
            # A caller who holds the read rung and was refused the write one is
            # told which of the two they failed, rather than "no access to
            # Booking" on a screen they are looking at.
            if not allowed and writing and user_can(request.user, section, minimum):
                self.message = write_denied
            return allowed

    # The rungs are both in the name, so two permissions on the same section
    # cannot be one identity in a traceback.
    rungs = minimum if write_minimum is None else f"{minimum}_write_{write_minimum}"
    _HasSectionAccess.__name__ = f"HasSection_{section}_{rungs}"
    return _HasSectionAccess
