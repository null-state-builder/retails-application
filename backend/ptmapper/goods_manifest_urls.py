"""Opening manifest and variance routes (E106, E107, E137, E138, E235)."""

from __future__ import annotations

from django.urls import path

from ptmapper.goods_manifest_views import (
    GoodsOpeningManifestDetailView,
    GoodsOpeningManifestListView,
    GoodsOpeningSeasonCorrectionView,
    GoodsOpeningVarianceView,
)

urlpatterns = [
    path(
        "opening-manifests",
        GoodsOpeningManifestListView.as_view(),
        name="goods-opening-manifest-list",
    ),
    path(
        "opening-manifests/<uuid:pk>",
        GoodsOpeningManifestDetailView.as_view(),
        name="goods-opening-manifest-detail",
    ),
    path(
        "opening-manifests/<uuid:pk>/variances",
        GoodsOpeningVarianceView.as_view(),
        name="goods-opening-manifest-variances",
    ),
    # The governed season correction on one opening row.
    path(
        "opening-manifests/<uuid:pk>/rows/<uuid:row_id>/season-correction",
        GoodsOpeningSeasonCorrectionView.as_view(),
        name="goods-opening-season-correction",
    ),
]
