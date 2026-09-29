"""WSGI entrypoint (kept for management/tooling parity)."""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# A served process never keeps the schema owner's role: every connection
# switches to the restricted application role (core.dbroles).
os.environ.setdefault("KDPS_DB_RUNTIME_ROLE", "1")

application = get_wsgi_application()
