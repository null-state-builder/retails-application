"""Goods-v1 evidence routes, under ``/api/goods-v1/files/``.

The goods evidence download used to share the ``/api/files/{id}/download`` path
template with the legacy integer-keyed download, so only one of the two reached
the emitted schema (GSA-T01).
"""

from __future__ import annotations

from django.urls import path

from files.goods_views import EvidenceDownloadView, EvidenceUploadView

urlpatterns = [
    path("uploads", EvidenceUploadView.as_view(), name="goods-evidence-upload"),
    path("<uuid:pk>/download", EvidenceDownloadView.as_view(), name="goods-evidence-download"),
]
