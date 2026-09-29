"""ASGI entrypoint. The platform's supervisor serves `server:app` with uvicorn;
`server.py` re-exports this `application`."""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# A served process never keeps the schema owner's role: every connection
# switches to the restricted application role (core.dbroles).
os.environ.setdefault("KDPS_DB_RUNTIME_ROLE", "1")

application = get_asgi_application()
