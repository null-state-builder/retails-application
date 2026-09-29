"""Goods-v1 vendor and booking routes, under ``/api/goods-v1/``.

Booking documents use UUIDs; vendors keep their integer IDs. The legacy vendor
and booking readers keep their own routes under ``/api/``, so neither family has
to inspect a request to know which contract it is serving (GSA-T01).
"""

from __future__ import annotations

from django.urls import path

from vendors.goods_size_curve_views import GoodsSizeCurveFillView, GoodsSizeCurveView
from vendors.goods_views import (
    GoodsBookingCloseView,
    GoodsBookingConfirmView,
    GoodsBookingCorrectionsView,
    GoodsBookingDetailView,
    GoodsBookingListCreateView,
    GoodsBookingReceiptLinksView,
    GoodsVendorDetailView,
    GoodsVendorListCreateView,
    GoodsVendorRetireView,
)
from vendors.open_to_buy_views import (
    GoodsBookingOpenToBuyAskView,
    GoodsBookingOpenToBuyView,
    GoodsOpenToBuyAskDetailView,
    GoodsOpenToBuyListView,
    GoodsOpenToBuySetView,
)

urlpatterns = [
    path("vendors", GoodsVendorListCreateView.as_view(), name="goods-vendor-list"),
    path("vendors/<int:pk>/retire", GoodsVendorRetireView.as_view(), name="goods-vendor-retire"),
    path("vendors/<int:pk>", GoodsVendorDetailView.as_view(), name="goods-vendor-detail"),
    path("bookings", GoodsBookingListCreateView.as_view(), name="goods-booking-list"),
    # Ticket 40: the size curve a style total is split by (before any booking exists).
    path("bookings/size-curve", GoodsSizeCurveView.as_view(), name="goods-booking-size-curve"),
    path(
        "bookings/size-curve/fills",
        GoodsSizeCurveFillView.as_view(),
        name="goods-booking-size-curve-fill",
    ),
    path(
        "bookings/<uuid:pk>/request-approval",
        GoodsBookingConfirmView.as_view(),
        name="goods-booking-confirm",
    ),
    path("bookings/<uuid:pk>/close", GoodsBookingCloseView.as_view(), name="goods-booking-close"),
    path(
        "bookings/<uuid:pk>/receipt-links",
        GoodsBookingReceiptLinksView.as_view(),
        name="goods-booking-receipt-links",
    ),
    path(
        "bookings/<uuid:pk>/corrections",
        GoodsBookingCorrectionsView.as_view(),
        name="goods-booking-corrections",
    ),
    path("bookings/<uuid:pk>", GoodsBookingDetailView.as_view(), name="goods-booking-detail"),
    # Store operations ticket 39: open-to-buy budgets and bookings over them.
    path(
        "bookings/<uuid:pk>/open-to-buy/request-approval",
        GoodsBookingOpenToBuyAskView.as_view(),
        name="goods-booking-open-to-buy-ask",
    ),
    path(
        "bookings/<uuid:pk>/open-to-buy",
        GoodsBookingOpenToBuyView.as_view(),
        name="goods-booking-open-to-buy",
    ),
    path("open-to-buy", GoodsOpenToBuyListView.as_view(), name="goods-open-to-buy"),
    path("open-to-buy/budgets", GoodsOpenToBuySetView.as_view(), name="goods-open-to-buy-set"),
    path(
        "open-to-buy/asks/<int:pk>",
        GoodsOpenToBuyAskDetailView.as_view(),
        name="goods-open-to-buy-ask",
    ),
]
