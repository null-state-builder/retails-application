"""The goods-v1 API namespace, mounted once at ``/api/goods-v1/`` (GSA-T01).

A goods client chooses the goods contract **by URL**. Nothing here reads a
request body, query marker or header to decide which contract answers: the path
is the decision, and the legacy paths this namespace was carved out of now serve
only their own legacy contract (issue #303).

The tails mirror the legacy prefixes one-for-one — ``/api/masters/stores``
becomes ``/api/goods-v1/masters/stores`` — so a migration is a prefix change and
nothing else. Every route name here starts with ``goods-`` and every path/method
pair is unique, which is what makes the emitted OpenAPI operation the goods one
rather than a path-colliding legacy operation.
"""

from __future__ import annotations

from django.urls import include, path

from config.goods_ops_views import CommandStatusView, OperationsHealthView

urlpatterns = [
    path("", include("core.goods_history_urls")),
    path("auth/", include("accounts.goods_urls")),
    path("masters/", include("masters.goods_urls")),
    path("inbound/", include("inbound.goods_urls")),
    path("ptmapper/", include("ptmapper.goods_urls")),
    path("stockledger/", include("stockledger.goods_urls")),
    path("outbound/", include("outbound.goods_urls")),
    # The counter's stock question keeps its own prefix here too, for the same
    # reason `stockledger/urls_stock.py` gives on the legacy side (#175).
    path("stock/", include("stockledger.goods_urls_stock")),
    path("files/", include("files.goods_urls")),
    # Ticket 07: the staff list per store and the old salesperson rows. There is
    # no legacy `/api/sell/` twin for these; they were born goods-v1.
    path("sell/", include("sell.goods_urls")),
    # Store operations ticket 28: brand payables for outright brands, born goods-v1
    # beside the legacy vendor ledger it does not replace.
    path("finledger/", include("finledger.goods_urls")),
    # Vendors, bookings, alerts, exceptions, events and approvals sit directly
    # under the namespace, exactly as they sit directly under `/api/`.
    path("", include("vendors.goods_urls")),
    path("", include("alerts.goods_urls")),
    path("", include("approvals.goods_urls")),
    path("", include("files.goods_export_urls")),
    path("commands/<uuid:command_id>", CommandStatusView.as_view(), name="goods-command-status"),
    path("operations/health", OperationsHealthView.as_view(), name="goods-operations-health"),
]
