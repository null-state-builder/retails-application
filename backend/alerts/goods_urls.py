"""Goods-v1 alert, exception and event routes, under ``/api/goods-v1/``.

The legacy bell keeps ``/api/alerts``, ``/api/alerts/history`` and
``/api/alerts/seen``; the goods contract answers only here (GSA-T01).
"""

from __future__ import annotations

from django.urls import path

from alerts.goods_views import (
    ConfirmOriginUnavailableView,
    EventStreamView,
    ExceptionEventView,
    ExceptionListView,
    GoodsAlertHistoryView,
    GoodsAlertInboxView,
    GoodsAlertSeenView,
)

urlpatterns = [
    path("alerts", GoodsAlertInboxView.as_view(), name="goods-alert-inbox"),
    path("alerts/history", GoodsAlertHistoryView.as_view(), name="goods-alert-history"),
    path("alerts/seen", GoodsAlertSeenView.as_view(), name="goods-alert-seen"),
    path("exceptions", ExceptionListView.as_view(), name="goods-exceptions"),
    path(
        "exceptions/<uuid:pk>/events", ExceptionEventView.as_view(), name="goods-exception-events"
    ),
    path(
        "exceptions/<uuid:pk>/confirm-origin-unavailable",
        ConfirmOriginUnavailableView.as_view(),
        name="goods-exception-confirm-origin-unavailable",
    ),
    path("events", EventStreamView.as_view(), name="goods-events"),
]
