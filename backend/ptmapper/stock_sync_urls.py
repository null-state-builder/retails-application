from django.urls import path

from ptmapper.stock_sync_views import (
    LegacySyncOverviewView,
    StockSyncAcknowledgeView,
    StockSyncDetailView,
    StockSyncMutationView,
    StockSyncRowsView,
    StockSyncUploadView,
)

urlpatterns = [
    path("legacy-sync", LegacySyncOverviewView.as_view(), name="legacy-sync-overview"),
    path("stock-syncs/upload", StockSyncUploadView.as_view(), name="stock-sync-upload"),
    path("stock-syncs/<uuid:pk>", StockSyncDetailView.as_view(), name="stock-sync-detail"),
    # Before the generic operation route, which would otherwise take "rows".
    path("stock-syncs/<uuid:pk>/rows", StockSyncRowsView.as_view(), name="stock-sync-rows"),
    path("stock-syncs/<uuid:pk>/rows/acknowledge", StockSyncAcknowledgeView.as_view(),
         name="stock-sync-rows-acknowledge"),
    path("stock-syncs/<uuid:pk>/<str:operation>", StockSyncMutationView.as_view(), name="stock-sync-mutate"),
]
