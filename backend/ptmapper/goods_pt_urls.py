"""Goods-v1 PT document routes (E098-E133). UUID documents, goods contract only."""

from __future__ import annotations

from django.urls import path

from ptmapper.goods_pt_views import (
    GoodsPtDetailView,
    GoodsPtExportView,
    GoodsPtExportXlsxView,
    GoodsPtFromGrnView,
    GoodsPtFromManifestView,
    GoodsPtListView,
    GoodsPtPostView,
    GoodsPtPriceView,
    GoodsPtRecallView,
    GoodsPtReissueView,
    GoodsPtRerunView,
    GoodsPtReverseView,
    GoodsPtRowsView,
    GoodsPtSendView,
)

urlpatterns = [
    path("files", GoodsPtListView.as_view(), name="goods-pt-list"),
    path("files/from-grn/<uuid:grn_id>", GoodsPtFromGrnView.as_view(), name="goods-pt-from-grn"),
    path(
        "files/from-manifest/<uuid:manifest_id>",
        GoodsPtFromManifestView.as_view(),
        name="goods-pt-from-manifest",
    ),
    path("files/<uuid:pk>/rows", GoodsPtRowsView.as_view(), name="goods-pt-rows"),
    path("files/<uuid:pk>/price", GoodsPtPriceView.as_view(), name="goods-pt-price"),
    path("files/<uuid:pk>/rerun", GoodsPtRerunView.as_view(), name="goods-pt-rerun"),
    path("files/<uuid:pk>/send", GoodsPtSendView.as_view(), name="goods-pt-send"),
    path("files/<uuid:pk>/recall", GoodsPtRecallView.as_view(), name="goods-pt-recall"),
    path("files/<uuid:pk>/post", GoodsPtPostView.as_view(), name="goods-pt-post"),
    path("files/<uuid:pk>/reverse", GoodsPtReverseView.as_view(), name="goods-pt-reverse"),
    path("files/<uuid:pk>/reissue", GoodsPtReissueView.as_view(), name="goods-pt-reissue"),
    path("files/<uuid:pk>/export", GoodsPtExportView.as_view(), name="goods-pt-export"),
    path(
        "files/<uuid:pk>/export.xlsx", GoodsPtExportXlsxView.as_view(), name="goods-pt-export-xlsx"
    ),
    path("files/<uuid:pk>", GoodsPtDetailView.as_view(), name="goods-pt-detail"),
]
