from django.urls import path

from ptmapper.legacy_sales_views import (
    LegacySalesDetailView,
    LegacySalesListView,
    LegacySalesMutationView,
    LegacySalesUploadView,
)

urlpatterns = [
    path("legacy-sales", LegacySalesListView.as_view(), name="legacy-sales-list"),
    path("legacy-sales/upload", LegacySalesUploadView.as_view(), name="legacy-sales-upload"),
    path("legacy-sales/<uuid:pk>", LegacySalesDetailView.as_view(), name="legacy-sales-detail"),
    path("legacy-sales/<uuid:pk>/<str:operation>", LegacySalesMutationView.as_view(), name="legacy-sales-mutate"),
]
