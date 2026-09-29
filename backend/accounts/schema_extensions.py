"""OpenAPI description of the application-owned server session cookie."""

from __future__ import annotations

from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension

from accounts.sessions import CSRF_HEADER, SESSION_COOKIE


# drf-spectacular ships an untyped extension metaclass; this single inheritance
# boundary cannot be checked by mypy, while the override below remains typed.
class ServerSessionOpenApiExtension(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    target_class = "accounts.authentication.ServerSessionAuthentication"
    name = "KDPS server session"

    def get_security_definition(self, auto_schema: Any) -> dict[str, Any]:
        return {
            "type": "apiKey",
            "in": "cookie",
            "name": SESSION_COOKIE,
            "description": (
                "Application-owned server session. Unsafe methods also require the "
                f"session-bound {CSRF_HEADER} header and allowed Origin."
            ),
        }
