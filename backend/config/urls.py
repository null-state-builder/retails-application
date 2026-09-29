"""Project URL configuration.

The API seam lives under `/api/...` (the Kubernetes ingress routes `/api` to the
backend). Auth, masters and dashboard are mounted here; the OpenAPI schema +
Swagger UI back the generated typed TS client (ADR-0001).
"""

from __future__ import annotations

import os

from django.conf import settings
from django.contrib import admin
from django.http import HttpRequest, JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from core.identity import migration_identity
from sell.running_offers_views import RunningOffersView

# The migration *names* spell out every schema change ever made, so publish them
# only where the reader is a developer or CI and the endpoint is not open to the
# internet. The digest and count go out everywhere - they are enough to detect a
# mismatch, which is all a caller needs (issue #93).
_PUBLISH_MIGRATION_NAMES = settings.DEBUG or bool(os.environ.get("CI"))


def health(_request: HttpRequest) -> JsonResponse:
    """Liveness, plus *which code* is alive.

    Unauthenticated and database-free by design: it must answer when Postgres is
    down, and callers use it before they hold a token. `migrations` is the
    server's identity - `app/backend/tests/conftest.py` compares it against the
    working tree so a stale server can never masquerade as the one under test.
    """
    return JsonResponse(
        {
            "status": "ok",
            "service": "kdps-backend",
            "migrations": migration_identity(include_names=_PUBLISH_MIGRATION_NAMES),
        }
    )


# Django admin bypasses the tenant role-assignment authority. Keep it available
# only for an explicitly enabled local development process; a production process
# cannot mount it even if an old ENABLE_DJANGO_ADMIN flag remains configured.
_ENABLE_ADMIN = settings.DEBUG and os.environ.get("ENABLE_DJANGO_ADMIN") == "1"

urlpatterns = [
    path("api/health", health),
    # The goods-v1 contract has one explicit namespace and is chosen by URL
    # alone (GSA-T01, issue #303). Mounted first so it is read as one surface,
    # never as an overlay on the legacy paths below.
    path("api/goods-v1/", include("config.goods_v1_urls")),
    path("api/auth/", include("accounts.urls")),
    path("api/masters/", include("masters.urls")),
    path("api/files/", include("files.urls")),
    path("api/", include("vendors.urls")),
    path("api/stockledger/", include("stockledger.urls")),
    # The counter's stock question, not the back office's ledger read — see
    # `stockledger/urls_stock.py` for why the two are mounted apart (#175).
    path("api/stock/", include("stockledger.urls_stock")),
    path("api/finledger/", include("finledger.urls")),
    path("api/outbound/", include("outbound.urls")),
    path("api/sell/", include("sell.urls")),
    # Running Offers (OPS-10, PRD §11) is an Offers & Price address whose answer
    # is built from the counter's working set, and the rulebook may not import
    # the counter (the import contract's one-way street). So the view lives in
    # `sell` and this is the one line that puts it where the screen asks for it.
    # Before the include, so the fixed word is never read as an offer id.
    path("api/offers/running", RunningOffersView.as_view(), name="offers-running"),
    path("api/offers/", include("offers.urls")),
    path("api/", include("approvals.urls")),
    path("api/", include("alerts.urls")),
    path("api/mail/", include("mail.urls")),
    path("api/", include("search.urls")),
    path("api/store/", include("storefront.urls")),
    path("api/reports/", include("reporting.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]

if _ENABLE_ADMIN:
    urlpatterns.insert(0, path("admin/", admin.site.urls))
