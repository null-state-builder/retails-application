"""Routes for the rulebook and the price list (mounted at `/api/offers/`)."""

from __future__ import annotations

from django.urls import path

from offers.eoss_views import (
    EossConfigView,
    EossRecommendationDecisionView,
    EossRecommendationListView,
)
from offers.price_views import PriceDetailView, PriceListView, PriceRepriceView
from offers.views import OfferApprovalRequestView, OfferDetailView, OfferListCreateView

urlpatterns = [
    path("", OfferListCreateView.as_view(), name="offer-list"),
    # Before `<int:pk>`, so the fixed words are never read as an id.
    path("price-list", PriceListView.as_view(), name="price-list"),
    path("price-list/<str:barcode>", PriceDetailView.as_view(), name="price-detail"),
    path("price-list/<str:barcode>/reprice", PriceRepriceView.as_view(), name="price-reprice"),
    path("eoss/recommendations", EossRecommendationListView.as_view(), name="eoss-reco-list"),
    path(
        "eoss/recommendations/<int:pk>/decide",
        EossRecommendationDecisionView.as_view(),
        name="eoss-reco-decide",
    ),
    path("eoss/config", EossConfigView.as_view(), name="eoss-config"),
    path("<int:pk>", OfferDetailView.as_view(), name="offer-detail"),
    path(
        "<int:pk>/request-approval",
        OfferApprovalRequestView.as_view(),
        name="offer-request-approval",
    ),
]
