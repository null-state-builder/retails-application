"""Cheap refreshes for the back office's hottest reads (caching plan, Phase 3).

The back office refreshes its reference masters, the sign-in bootstrap, the
approvals inbox, the alerts list and the stock-on-hand screens every time a
screen is reopened or the window regains focus. Most of those refreshes find
nothing changed. For the GETs listed in :data:`CONDITIONAL_PATHS` this
middleware lets the client say "I already have this" (``If-None-Match``) and
answers ``304 Not Modified`` with no body when that is still true.

**The view always runs in full.** The ETag is computed *after* the view has
authenticated, authorised, scoped and serialised the caller's answer, as a
digest of that exact body. A caller the endpoint refuses gets the refusal (the
middleware only touches a 200), and a 304 can only ever confirm a body the
same caller would receive now. The digest also folds in who is asking (tenant,
user) and the unit/brand context headers, so one person's ETag never matches
another person's or another store's answer even when the bytes coincide.

**Nothing is stored on the device by the browser.** The responses stay
``Cache-Control: no-store, private`` (several of these bodies carry vendor PAN,
payment terms or cost-bearing values, which the device must not keep, and the
browser's HTTP cache outlives a logout). The client keeps the last body and its
ETag in memory only and sends ``If-None-Match`` itself (``frontend/src/lib/
conditionalGet.ts``); with ``no-store`` the browser passes the 304 through to
it. ``Vary`` still names the cookie and the context headers, so no shared cache
can ever hand one caller's answer to another.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.utils.cache import get_conditional_response, patch_vary_headers

#: Exact request paths (no query string; any filters are part of the body the
#: digest covers). Add a path here only for a plain JSON read whose answer is a
#: pure function of the caller and the request.
CONDITIONAL_PATHS: frozenset[str] = frozenset(
    {
        # Reference masters and vendors (the sign-in bootstrap's blocks).
        "/api/masters/stores",
        "/api/masters/locations",
        "/api/masters/brands",
        "/api/masters/seasons",
        "/api/masters/gstins",
        "/api/masters/entities",
        "/api/vendors",
        # The sign-in bootstrap itself.
        "/api/auth/bootstrap",
        # Action-needed lists.
        "/api/approvals/inbox",
        "/api/alerts",
        # Stock on hand and its summary (the back office's stock screens). The
        # goods-v1 readers are not listed: their body carries the moment of the
        # read (`as_of`), so it never repeats and a 304 could never happen.
        "/api/stockledger/on-hand",
        "/api/stockledger/summary",
    }
)

#: The request headers that change what these endpoints answer.
VARY_ON: tuple[str, ...] = ("Cookie", "X-KDPS-Unit", "X-KDPS-Brand")

CACHE_CONTROL = "no-store, private"

#: A compressing proxy in front of the API (Caddy's `encode`, `deploy/Caddyfile`)
#: may mark the ETag of a body it compressed with the encoding ("abc-gzip"). The
#: body is the same, only the transport differs, so the mark is ignored here.
_ENCODING_MARK = re.compile(r'-(?:gzip|zstd|br|deflate)"')


def _unmark_encodings(request: HttpRequest) -> None:
    value = request.META.get("HTTP_IF_NONE_MATCH")
    if value:
        request.META["HTTP_IF_NONE_MATCH"] = _ENCODING_MARK.sub('"', value)


def _caller_marks(request: HttpRequest) -> bytes:
    user = getattr(request, "user", None)
    parts = [
        str(getattr(user, "tenant_id", "") or ""),
        str(getattr(user, "pk", "") or "") if getattr(user, "is_authenticated", False) else "anon",
        request.headers.get("X-KDPS-Unit", ""),
        request.headers.get("X-KDPS-Brand", ""),
    ]
    return "\x1f".join(parts).encode()


def body_etag(request: HttpRequest, content: bytes) -> str:
    """A strong ETag over the caller's marks and the exact body they get."""
    digest = hashlib.sha256()
    digest.update(_caller_marks(request))
    digest.update(b"\x1e")
    digest.update(content)
    return f'"{digest.hexdigest()[:40]}"'


def _eligible(request: HttpRequest, response: HttpResponseBase) -> bool:
    return (
        request.method in ("GET", "HEAD")
        and request.path in CONDITIONAL_PATHS
        and response.status_code == 200
        and isinstance(response, HttpResponse)  # never a streamed body
        and str(response.get("Content-Type", "")).startswith("application/json")
    )


class ConditionalApiGetMiddleware:
    """ETag + 304 for :data:`CONDITIONAL_PATHS`, computed after the view."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        if not _eligible(request, response):
            return response
        assert isinstance(response, HttpResponse)
        _unmark_encodings(request)
        response["ETag"] = body_etag(request, response.content)
        response["Cache-Control"] = CACHE_CONTROL
        patch_vary_headers(response, VARY_ON)
        # Copies ETag, Cache-Control, Vary and any cookie the view set onto the 304.
        answer = get_conditional_response(request, etag=response["ETag"], response=response)
        return answer if answer is not None else response
