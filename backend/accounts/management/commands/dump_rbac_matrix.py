"""Publish the ratified access table to the client, as one generated file (#130).

The PWA reads a user's sections from ``/me`` at runtime, so the live menu is
already the server's answer. Its *tests* were the drift risk: every persona was
hand-copied into ``routeAccess.test.ts``, which meant a cell could be corrected
here and the client suite would keep passing against the old wording. That is a
second source of truth wearing a test's clothes.

So the table is dumped once, checked in, and imported by the client tests. A
contract test regenerates it and fails if the checked-in file is stale, naming
this command - so the copy cannot silently rot, and there is still exactly one
place a role's access is decided.

Only the granted cells are written, keyed by role code, because that is the
shape ``/me`` sends (``capabilities``): a section a role does not hold is absent
rather than ``none``. Labels are left out - they are display context the guard
never reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand

from accounts.rbac_matrix import KNOWN_ROLE_CODES, section_access_for
from accounts.sections import CAP_NONE

#: app/backend/accounts/management/commands → app/frontend/src/auth
TARGET = (
    Path(__file__).resolve().parents[4] / "frontend" / "src" / "auth" / "rbacMatrix.generated.json"
)


def matrix_payload() -> dict[str, dict[str, str]]:
    """``{role_code: {section: capability}}``, granted cells only."""
    return {
        code: {
            section: entry["capability"]
            for section, entry in section_access_for(code).items()
            if entry["capability"] != CAP_NONE
        }
        for code in KNOWN_ROLE_CODES
    }


def rendered() -> str:
    return json.dumps(matrix_payload(), indent=2, sort_keys=True) + "\n"


class Command(BaseCommand):
    help = "Write the ratified access table to the PWA as rbacMatrix.generated.json."

    def handle(self, *args: Any, **options: Any) -> None:
        TARGET.write_text(rendered())
        self.stdout.write(self.style.SUCCESS(f"wrote {TARGET}"))
