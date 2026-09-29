"""Goods-v1 PT vocabulary routes (E226-E231).

The four read paths and the two decision routes answer the goods contract only
(GSA-T01); the legacy vocabulary readers were deleted (OPS-18).
"""

from __future__ import annotations

from django.urls import path

from ptmapper.goods_vocab_views import (
    GoodsControlledValuesView,
    GoodsProposalDecideView,
    GoodsProposalListView,
    GoodsReviewListView,
    GoodsReviewResolveView,
    GoodsSuggestView,
)

urlpatterns = [
    path("controlled", GoodsControlledValuesView.as_view(), name="goods-pt-controlled"),
    path("review", GoodsReviewListView.as_view(), name="goods-pt-review-list"),
    path("proposals", GoodsProposalListView.as_view(), name="goods-pt-proposal-list"),
    path("suggest", GoodsSuggestView.as_view(), name="goods-pt-suggest"),
    path(
        "review/<uuid:pk>/resolve",
        GoodsReviewResolveView.as_view(),
        name="goods-pt-review-resolve",
    ),
    path(
        "proposals/<uuid:pk>/decide",
        GoodsProposalDecideView.as_view(),
        name="goods-pt-proposal-decide",
    ),
]
