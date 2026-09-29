"""Keep backend verification away from a developer's working database."""

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("KDPS_PROOF_MODE") != "1":
        raise pytest.UsageError(
            "Backend tests require the isolated proof database. "
            "Run `python3 scripts/proof.py run --cwd backend -- .venv/bin/pytest`."
        )
