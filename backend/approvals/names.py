"""How a person is named on screen — one spelling, for the whole spine.

Its own module because both ``approvals.models`` and ``approvals.services`` need
it and services already imports models: a name shared by two layers of the same
app belongs below both of them, not duplicated across them.
"""

from __future__ import annotations

from typing import Any


def display_name(user: Any) -> str:
    """Full name, else username. The one spelling, shared by the inbox, the step
    trail and every document's maker/checker line."""
    if user is None:
        return ""
    return getattr(user, "full_name", "") or getattr(user, "username", "") or ""
