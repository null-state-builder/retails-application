"""Session-cookie authentication for the whole API (design §4.2).

Replaces the JWT cookie/header flow. A request is authenticated only by a live
server session; unsafe methods must also pass the Origin check and echo the
session-bound CSRF token. There is no bearer token.
"""

from __future__ import annotations

import re
from typing import Any, cast
from urllib.parse import urlsplit

from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.request import Request

from accounts.sessions import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    csrf_matches,
    resolve_session,
)
from core.refusals import Refusal

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: GSA-T03/ticket 03A: what a temporary-password session may still reach.
#: `enforce_password_change_restriction` runs only inside
#: `ServerSessionAuthentication.authenticate()` below, so it only ever gates a
#: route that actually uses that authentication class — in practice `/api/auth/me`,
#: `/api/auth/change-password`, `/api/auth/step-up` and every goods-v1/business
#: route (the ones this restriction exists to block). `/api/auth/csrf`,
#: `/api/auth/logout` and `/api/auth/refresh` carry `authentication_classes = []`
#: (`accounts.views`) and resolve their own session by hand, so this function
#: never runs for them at all — they are open to a temporary-password session
#: regardless of whether they are listed here, because own-session lifecycle
#: work is exactly what a temporary-password session is meant to keep. Their
#: entries below are kept for clarity/future-proofing (in case one of them ever
#: gains the default authentication class), not because they are enforced today.
#: Step-up is deliberately not allowed: it only unlocks privileged business
#: actions, which are already unreachable.
PASSWORD_CHANGE_ALLOWED_PATHS = frozenset(
    {
        "/api/auth/csrf",
        "/api/auth/me",
        "/api/auth/refresh",
        "/api/auth/logout",
        "/api/auth/change-password",
    }
)


def origin_allowed(request: Any) -> bool:
    origin = request.META.get("HTTP_ORIGIN")
    if not origin:
        return True
    parts = urlsplit(origin)
    host = request.get_host()
    if parts.netloc == host:
        return True
    allowed = set(getattr(settings, "CORS_ALLOWED_ORIGINS", []) or [])
    if origin in allowed:
        return True
    for pattern in getattr(settings, "CORS_ALLOWED_ORIGIN_REGEXES", []) or []:
        if re.match(pattern, origin):
            return True
    for trusted in getattr(settings, "CSRF_TRUSTED_ORIGINS", []) or []:
        if trusted == origin:
            return True
        if "*" in trusted:
            regex = "^" + re.escape(trusted).replace(r"\*", "[^/]+") + "$"
            if re.match(regex, origin):
                return True
    return False


def enforce_write_protection(request: Any, session: Any = None) -> None:
    if request.method in SAFE_METHODS:
        return
    if not origin_allowed(request):
        raise Refusal("CSRF_FAILED", "This request did not come from an allowed page.")
    header = request.META.get(CSRF_HEADER)
    cookie = request.COOKIES.get(CSRF_COOKIE)
    if not csrf_matches(header, cookie, session):
        raise Refusal(
            "CSRF_FAILED", "The page's security token is missing or stale. Reload and try again."
        )


def enforce_password_change_restriction(request: Any, user: Any) -> None:
    if not getattr(user, "must_change_password", False):
        return
    if request.path in PASSWORD_CHANGE_ALLOWED_PATHS:
        return
    raise Refusal(
        "PASSWORD_CHANGE_REQUIRED",
        "Replace your temporary password before doing anything else.",
    )


class ServerSessionAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> tuple[Any, Any] | None:
        token = request.COOKIES.get(SESSION_COOKIE)
        if not token:
            return None
        session = resolve_session(token)
        if session is None:
            return None
        enforce_write_protection(request._request, session)
        enforce_password_change_restriction(request._request, session.user)
        from accounts.principal import AccessContext, effective_grants

        session.user._access_context = AccessContext(
            user=session.user, human_id=session.user.human_id, tenant_id=session.tenant_id,
            session=session, grants=effective_grants(session.user.human_id),
        )
        cast(Any, request)._goods_access = session.user._access_context
        return session.user, session

    def authenticate_header(self, request: Request) -> str:
        return 'Session realm="kdps"'
