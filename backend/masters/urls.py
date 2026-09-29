from __future__ import annotations

from django.urls import path

from masters.views import (
    BrandDetailView,
    BrandListView,
    GstinDetailView,
    GstinListView,
    LegalEntityListView,
    LocationListView,
    SeasonDetailView,
    SeasonListView,
    SkuLookupView,
    StoreDetailView,
    StoreListView,
    StoreTargetView,
    SummaryView,
)

# Legacy masters readers only. The goods-v1 masters contract answers at
# `/api/goods-v1/masters/...` (`masters/goods_urls.py`); no path here inspects a
# request body or query marker to choose a contract (GSA-T01).
urlpatterns = [
    path("skus/lookup", SkuLookupView.as_view(), name="sku-lookup"),
    path("stores", StoreListView.as_view(), name="store-list"),
    path("stores/<int:pk>", StoreDetailView.as_view(), name="store-detail"),
    path("locations", LocationListView.as_view(), name="location-list"),
    # Before `stores/<int:pk>` would ever be consulted, and its own path anyway -
    # the monthly target grid is keyed by store *code*, not by a store row id.
    path("store-targets", StoreTargetView.as_view(), name="store-target-grid"),
    path("brands", BrandListView.as_view(), name="brand-list"),
    path("brands/<int:pk>", BrandDetailView.as_view(), name="brand-detail"),
    path("seasons", SeasonListView.as_view(), name="season-list"),
    path("seasons/<int:pk>", SeasonDetailView.as_view(), name="season-detail"),
    path("gstins", GstinListView.as_view(), name="gstin-list"),
    path("gstins/<int:pk>", GstinDetailView.as_view(), name="gstin-detail"),
    path("entities", LegalEntityListView.as_view(), name="entity-list"),
    path("summary", SummaryView.as_view(), name="masters-summary"),
]
