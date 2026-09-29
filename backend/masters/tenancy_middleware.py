"""Bind the deployment's tenant to every request (design §4.2).

The tenant comes from the deployment binding (``KDPS_DEPLOYMENT_KEY``), never
from a header, cookie or body. It is mirrored onto the database connection so
row-level security answers only this tenant's goods rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from core.tenancy import tenant_context


def deployment_tenant_id() -> uuid.UUID | None:
    """The tenant bound to ``KDPS_DEPLOYMENT_KEY``, read fresh every time.

    Never cached: a reseed or reset gives the tenant a new id, and a remembered
    old id makes every write (a failed login, for one) hit a missing tenant.
    """
    key = str(getattr(settings, "KDPS_DEPLOYMENT_KEY", "") or "")
    if not key:
        return None
    from masters.goods_models import Tenant

    return Tenant.objects.filter(deployment_key=key).values_list("id", flat=True).first()


class TenantMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # The admin reads the same tenant-walled masters as the API does.
        bound = request.path.startswith(("/api/", "/admin/"))
        if not bound or request.path == "/api/health":
            return self.get_response(request)
        tenant_id = deployment_tenant_id()
        with tenant_context(tenant_id):
            return self.get_response(request)
