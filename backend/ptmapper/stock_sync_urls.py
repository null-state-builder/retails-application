from django.urls import path

from ptmapper.stock_sync_views import (
    LegacySyncOverviewView,
    StockSyncDetailView,
    StockSyncMutationView,
    StockSyncUploadView,
)

urlpatterns = [
    path("legacy-sync", LegacySyncOverviewView.as_view(), name="legacy-sync-overview"),
    path("stock-syncs/upload", StockSyncUploadView.as_view(), name="stock-sync-upload"),
    path("stock-syncs/<uuid:pk>", StockSyncDetailView.as_view(), name="stock-sync-detail"),
    path("stock-syncs/<uuid:pk>/<str:operation>", StockSyncMutationView.as_view(), name="stock-sync-mutate"),
]
