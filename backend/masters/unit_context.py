"""The business unit a person is *acting in* for this request (issue #88).

Scope and context are different questions. **Scope** (ADR-0003) is what a person
may ever see — data, set by an admin, enforced fail-closed in `masters.scoping`.
**Context** is which one of those units they are working in right now — the
top-bar switcher. The PWA sends the chosen unit on every call as `X-KDPS-Unit`
(and a brand manager's chosen brand as `X-KDPS-Brand`), and the server narrows
the answer to it.

The header only ever *narrows*: it is intersected with the caller's scope, so no
amount of header-forging widens what anyone sees — an out-of-scope unit is a 403,
not a peek. That is why the switcher can be a plain header and still be safe.

Held in a `ContextVar` set and reset by the middleware, so the scoping helpers
keep their `(queryset, user)` signature and every existing call site inherits
the unit context without being rewritten.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

from django.http import HttpRequest, HttpResponse

UNIT_HEADER = "HTTP_X_KDPS_UNIT"
BRAND_HEADER = "HTTP_X_KDPS_BRAND"

# Empty string = "no unit chosen" = the caller's whole scope (the network view).
_active_unit: ContextVar[str] = ContextVar("kdps_active_unit", default="")
_active_brand: ContextVar[str] = ContextVar("kdps_active_brand", default="")


def active_unit_code() -> str:
    """Store code the caller is acting in, or `""` for their whole scope."""
    return _active_unit.get()


def active_brand_name() -> str:
    """Brand the caller is filtered to, or `""` for all brands in scope."""
    return _active_brand.get()


class ActiveContextMiddleware:
    """Carry the switcher's choice from the request headers into the request."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        unit = _active_unit.set((request.META.get(UNIT_HEADER) or "").strip())
        brand = _active_brand.set((request.META.get(BRAND_HEADER) or "").strip())
        try:
            return self.get_response(request)
        finally:
            # Reset even on an exception: a worker thread is reused, and a leaked
            # unit would silently narrow the *next* person's request.
            _active_unit.reset(unit)
            _active_brand.reset(brand)
