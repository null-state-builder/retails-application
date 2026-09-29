from __future__ import annotations

from django.urls import path

from vendors.views import (
    BookingCloseView,
    BookingDetailView,
    BookingDraftView,
    BookingListCreateView,
    BookingSubmitView,
    VendorDetailView,
    VendorListCreateView,
)

# Legacy vendor and booking readers only. The goods-v1 contract answers at
# `/api/goods-v1/vendors` and `/api/goods-v1/bookings/...` (GSA-T01).
urlpatterns = [
    path("vendors", VendorListCreateView.as_view(), name="vendor-list"),
    path("vendors/<int:pk>", VendorDetailView.as_view(), name="vendor-detail"),
    path("bookings", BookingListCreateView.as_view(), name="booking-list"),
    path("bookings/draft", BookingDraftView.as_view(), name="booking-draft"),
    path("bookings/<int:pk>", BookingDetailView.as_view(), name="booking-detail"),
    path("bookings/<int:pk>/close", BookingCloseView.as_view(), name="booking-close"),
    path(
        "bookings/<int:pk>/request-approval",
        BookingSubmitView.as_view(),
        name="booking-request-approval",
    ),
]
