"""Server-side section enforcement for the SIDEBAR RBAC contract (issue #85).

The sidebar is not a security boundary — hiding a menu item does nothing if the
API behind it still answers. So the same section→capability data that shapes a
user's sidebar also gates the API: ``require_section(section, minimum)`` is a
DRF permission any view can carry, and it resolves the acting user's capability
from ``Role.section_access`` (the DB authority), fail-closed.

Resolution order (all fail-closed):
  · anonymous / unauthenticated → nothing;
  · superuser → ``manage`` on every section (the break-glass account);
  · a user with no role → nothing;
  · otherwise the capability stored on the role for that section, or ``none``.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request

from accounts.sections import (
    CAP_MANAGE,
    CAP_NONE,
    CAP_VIEW,
    SECTIONS,
    is_valid_section,
    meets,
)


def _resolve_section(user: Any, section: str) -> tuple[str, str]:
    """``(capability, sheet-label)`` for ``user`` on ``section`` — one read of
    the role row, fail-closed to ``(none, "")`` on any doubt."""
    if not (user and getattr(user, "is_authenticated", False)):
        return CAP_NONE, ""
    if getattr(user, "is_superuser", False):
        return CAP_MANAGE, "All"
    role = getattr(user, "role", None)
    if role is None:
        return CAP_NONE, ""
    entry = (role.section_access or {}).get(section)
    if not isinstance(entry, dict):
        return CAP_NONE, ""
    capability = entry.get("capability", CAP_NONE)
    label = entry.get("label", "")
    return (
        capability if isinstance(capability, str) else CAP_NONE,
        label if isinstance(label, str) else "",
    )


def user_section_capability(user: Any, section: str) -> str:
    """The capability ``user`` holds on ``section`` — ``none`` if any doubt."""
    return _resolve_section(user, section)[0]


def user_can(user: Any, section: str, minimum: str = CAP_VIEW) -> bool:
    """Does ``user`` reach at least ``minimum`` capability on ``section``?"""
    return meets(user_section_capability(user, section), minimum)


def visible_sections(user: Any) -> list[dict[str, str | int]]:
    """The sections ``user`` may see, in sidebar order, with their capability.

    Only sections the user genuinely reaches (``view`` and up) are returned — a
    role with no grants, an off-ladder capability, or no role at all yields an
    empty list, so the shell fails closed to nothing.
    """
    out: list[dict[str, str | int]] = []
    for order, (code, label) in enumerate(SECTIONS):
        capability, scope_label = _resolve_section(user, code)
        if not meets(capability, CAP_VIEW):
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
