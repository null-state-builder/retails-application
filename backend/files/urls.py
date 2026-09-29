from __future__ import annotations

from django.urls import path

from files.views import LegacyFileDownloadView

# Legacy file downloads only. Goods-v1 evidence answers at
# `/api/goods-v1/files/...` (`files/goods_urls.py`) — GSA-T01.
urlpatterns = [
    path("<int:pk>/download", LegacyFileDownloadView.as_view(), name="file-download"),
]
