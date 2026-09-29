from __future__ import annotations

from django.urls import path

from alerts.views import AlertHistoryView, AlertInboxView, AlertSeenView

# Legacy bell only. The goods-v1 alert, exception and event surface answers at
# `/api/goods-v1/...` (`alerts/goods_urls.py`) — GSA-T01.
urlpatterns = [
    path("alerts", AlertInboxView.as_view(), name="alert-inbox"),
    path("alerts/history", AlertHistoryView.as_view(), name="alert-history"),
    path("alerts/seen", AlertSeenView.as_view(), name="alert-seen"),
]
