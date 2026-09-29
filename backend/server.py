"""Uvicorn entrypoint for the platform process manager.

Supervisor runs `/root/.venv/bin/uvicorn server:app` from `/app/backend` (a
symlink to `app/backend`). It re-exports the Django ASGI application so the real
Django/Postgres stack runs unchanged under the platform's expected command.
"""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from config.asgi import application as app  # noqa: E402

__all__ = ["app"]
