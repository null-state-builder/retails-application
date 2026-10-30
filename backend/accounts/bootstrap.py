"""What the back office needs the moment someone signs in (caching plan, Phase 2).

One authenticated GET, ``/api/auth/bootstrap``, answers in a single round trip
what the screens would otherwise ask for one by one: the reference masters a
person can already read, the counts behind Home's "action needed" tiles, and a
``ref_version`` the client uses to tell whether its saved copy of those masters
is still current.

**No new authority and no new data.** Every block is the answer of the existing
endpoint, produced by dispatching that endpoint's own view for the caller's own
request: the same authentication, the same permission classes, the same
``resolve_access`` checks, the same tenant and unit/brand context (the
``X-KDPS-Unit`` / ``X-KDPS-Brand`` headers travel with the request), the same
queryset scoping and the same serializer. A block whose endpoint would refuse
this caller (401/403/404 or any other non-200) is simply left out, so the
bootstrap can never say more than the endpoints it stands in for. Each block is
keyed by the exact URL the frontend asks for, so the client seeds its cache
entry ``[scope, url]`` with it unchanged.

Nothing cost, margin or money is included: the blocks are masters whose
serializers carry identity and terms only, and the "action needed" figures are
bare counts — the approval inbox rows themselves carry ``value_paise``, so they
are counted here and never copied.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from collections.abc import Iterable
from typing import Any

from django.db.models import Count, Max, Model, QuerySet
from django.http import HttpRequest, QueryDict
from django.urls import Resolver404, resolve
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.goods_admin_views import REFUSAL_RESPONSE
from core.tenancy import require_tenant_id
from masters.models import Brand, Gstin, LegalEntity, Season, Store
from vendors.models import Vendor, VendorBrand

logger = logging.getLogger(__name__)

API_PREFIX = "/api"
CONTRACT_VERSION = "bootstrap-v1"

#: Reference masters, by the exact URL the frontend's `useList` asks for (no
#: `/api` prefix; an empty search box adds no query string). The client keeps
#: these on the device (they are all `isReferenceUrl` in `lib/queryCache.ts`).
REFERENCE_BLOCKS: tuple[str, ...] = (
    "/masters/stores",
    "/masters/locations",
    "/masters/brands",
    "/masters/seasons",
    "/masters/gstins",
    "/masters/entities",
    "/vendors",
)

#: Home's master counts tile. Counts only; kept in memory, never on the device.
SUMMARY_BLOCKS: tuple[str, ...] = ("/masters/summary",)

#: "Action needed" counts: the length of what each source endpoint answers this
#: caller. The rows are not copied (an approval row may carry `value_paise`).
COUNT_SOURCES: dict[str, str] = {
    "approvals": "/approvals/inbox",
    "alerts": "/alerts",
}

#: The reference tables whose change should invalidate a client's saved copy.
#: (model, has a tenant column)
_REFERENCE_TABLES: tuple[tuple[type[Model], bool], ...] = (
    (LegalEntity, True),
    (Gstin, True),
    (Store, True),
    (Brand, True),
    (Season, False),
    (Vendor, True),
)


def _subrequest(request: HttpRequest, url: str) -> HttpRequest:
    """The caller's own request, re-aimed at ``url`` as a plain GET.

    A shallow copy keeps the cookies, headers and remote address the source
    endpoint authenticates and scopes by; only the path and query change.
    """
    path, _, query = url.partition("?")
    api_path = f"{API_PREFIX}{path}"
    sub = copy.copy(request)
    sub.method = "GET"
    sub.path = api_path
    sub.path_info = api_path
    sub.META = {**request.META, "REQUEST_METHOD": "GET", "PATH_INFO": api_path, "QUERY_STRING": query}
    sub.GET = QueryDict(query)
    sub.POST = QueryDict()
    return sub


def read_as_caller(request: HttpRequest, url: str) -> tuple[bool, Any]:
    """Dispatch the endpoint at ``url`` for this caller; ``(ok, data)``.

    ``ok`` is true only for a 200: any refusal the endpoint would give this
    caller means the block is omitted, exactly as the screen would be refused.
    """
    path = f"{API_PREFIX}{url.partition('?')[0]}"
    try:
        match = resolve(path)
    except Resolver404:
        return False, None
    try:
        response = match.func(_subrequest(request, url), *match.args, **match.kwargs)
    except Exception:  # A broken block must not take the sign-in down with it.
        logger.exception("bootstrap block %s failed", url)
        return False, None
    if getattr(response, "status_code", None) != 200 or not hasattr(response, "data"):
        return False, None
    return True, response.data


def _table_marks(tenant_id: Any) -> list[list[Any]]:
    marks: list[list[Any]] = []
    for model, tenanted in _REFERENCE_TABLES:
        rows: QuerySet[Any] = model._default_manager.all()  # noqa: SLF001
        if tenanted:
            rows = rows.filter(tenant_id=tenant_id)
        agg = rows.aggregate(n=Count("pk"), at=Max("updated_at"))
        marks.append([model._meta.label, agg["n"], agg["at"].isoformat() if agg["at"] else None])
    links = VendorBrand.objects.filter(tenant_id=tenant_id).aggregate(n=Count("pk"), last=Max("pk"))
    marks.append(["vendors.VendorBrand", links["n"], links["last"]])
    return marks


def ref_version(tenant_id: Any, blocks: dict[str, Any], urls: Iterable[str]) -> str:
    """A short digest that moves whenever a reference master changes.

    The table marks (row count and latest ``updated_at`` per master) catch a
    change anywhere in the tenant, including rows behind a search or an
    ``include_inactive`` view the bootstrap does not carry; the digest of the
    caller's own reference blocks catches a change in what *this* person may
    see (a new grant, a store retired) that moves no timestamp.
    """
    seen = {url: blocks.get(url) for url in urls}
    material = json.dumps(
        {"tables": _table_marks(tenant_id), "blocks": seen},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


BOOTSTRAP_RESPONSE: dict[str, Any] = {
    "type": "object",
    "required": ["contract_version", "ref_version", "blocks", "counts"],
    "properties": {
        "contract_version": {"type": "string", "enum": [CONTRACT_VERSION]},
        "ref_version": {"type": "string"},
        "blocks": {
            "type": "object",
            "description": (
                "Each key is the exact frontend URL (without /api) of an existing endpoint; "
                "each value is that endpoint's 200 answer for this caller. A block the "
                "endpoint would refuse is absent."
            ),
            "additionalProperties": {},
        },
        "counts": {
            "type": "object",
            "description": "Action-needed counts; a source the caller may not read is absent.",
            "additionalProperties": {"type": "integer"},
        },
    },
}


class BootstrapView(APIView):
    """E003: one round trip of reference masters and action-needed counts.

    Authorises nothing itself beyond being signed in; each block is gated by its
    own endpoint (see the module docstring). A temporary-password session is
    refused by the session authentication like any business read.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: BOOTSTRAP_RESPONSE, 401: REFUSAL_RESPONSE, 403: REFUSAL_RESPONSE})
    def get(self, request: Request) -> Response:
        raw = request._request  # noqa: SLF001 - the Django request the sub-views authenticate from
        blocks: dict[str, Any] = {}
        for url in (*REFERENCE_BLOCKS, *SUMMARY_BLOCKS):
            ok, data = read_as_caller(raw, url)
            if ok:
                blocks[url] = data
        counts: dict[str, int] = {}
        for name, url in COUNT_SOURCES.items():
            ok, data = read_as_caller(raw, url)
            if ok and isinstance(data, list):
                counts[name] = len(data)
        return Response(
            {
                "contract_version": CONTRACT_VERSION,
                "ref_version": ref_version(require_tenant_id(), blocks, REFERENCE_BLOCKS),
                "blocks": blocks,
                "counts": counts,
            }
        )
