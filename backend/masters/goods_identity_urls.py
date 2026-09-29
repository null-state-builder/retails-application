"""Goods-v1 product master and identity routes (E041-E060, E090, E091).

Included first from ``masters.goods_urls`` so the literal ``skus/lookup`` route
is declared before the UUID-keyed SKU routes. The legacy registry lookup keeps
its own path at ``/api/masters/skus/lookup``; the goods contract answers here.
"""

from __future__ import annotations

from django.urls import path

from masters.goods_identity_views import (
    AliasDetailView,
    AliasListCreateView,
    AliasRetireView,
    CrosswalkDetailView,
    CrosswalkListCreateView,
    CrosswalkRetireView,
    GoodsSkuLookupView,
    IdentityPickView,
    ItemProposalListView,
    SkuDetailView,
    SkuListCreateView,
    SkuRetireView,
    StyleDetailView,
    StyleListCreateView,
    StyleRetireView,
)

urlpatterns = [
    path("skus/lookup", GoodsSkuLookupView.as_view(), name="goods-sku-lookup"),
    path("styles", StyleListCreateView.as_view(), name="goods-style-list"),
    path("styles/<uuid:pk>", StyleDetailView.as_view(), name="goods-style-detail"),
    path("styles/<uuid:pk>/retire", StyleRetireView.as_view(), name="goods-style-retire"),
    path("skus", SkuListCreateView.as_view(), name="goods-sku-list"),
    path("skus/<uuid:pk>", SkuDetailView.as_view(), name="goods-sku-detail"),
    path("skus/<uuid:pk>/retire", SkuRetireView.as_view(), name="goods-sku-retire"),
    path("aliases", AliasListCreateView.as_view(), name="goods-alias-list"),
    path("aliases/<uuid:pk>", AliasDetailView.as_view(), name="goods-alias-detail"),
    path("aliases/<uuid:pk>/retire", AliasRetireView.as_view(), name="goods-alias-retire"),
    path("crosswalks", CrosswalkListCreateView.as_view(), name="goods-crosswalk-list"),
    path("crosswalks/<uuid:pk>", CrosswalkDetailView.as_view(), name="goods-crosswalk-detail"),
    path(
        "crosswalks/<uuid:pk>/retire",
        CrosswalkRetireView.as_view(),
        name="goods-crosswalk-retire",
    ),
    path("identity-picks", IdentityPickView.as_view(), name="goods-identity-pick"),
    # New items a PT proposed, waiting for the product-master owner (PRD §5.4, OPS-17A).
    path("item-proposals", ItemProposalListView.as_view(), name="goods-item-proposals"),
]
