"""Goods-v1 durable export routes, directly under ``/api/goods-v1/`` (E189, E190).

``exports`` sits at the namespace root, like ``commands`` and ``operations``: an
export is not a file in the evidence store's sense - it is a job that produces
one.
"""

from __future__ import annotations

from django.urls import path

from files.goods_export_views import GoodsExportCreateView, GoodsExportDetailView

urlpatterns = [
    path("exports", GoodsExportCreateView.as_view(), name="goods-export-create"),
    path("exports/<uuid:pk>", GoodsExportDetailView.as_view(), name="goods-export-detail"),
]
