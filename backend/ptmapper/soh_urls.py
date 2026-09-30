from django.urls import path

from ptmapper.soh_views import SohImportDetailView, SohImportListView, SohImportMutationView, SohImportRowsView, SohImportUploadView

urlpatterns = [
    path("soh-imports", SohImportListView.as_view(), name="soh-import-list"),
    path("soh-imports/upload", SohImportUploadView.as_view(), name="soh-import-upload"),
    path("soh-imports/<uuid:pk>", SohImportDetailView.as_view(), name="soh-import-detail"),
    path("soh-imports/<uuid:pk>/rows", SohImportRowsView.as_view(), name="soh-import-rows"),
    path("soh-imports/<uuid:pk>/<str:operation>", SohImportMutationView.as_view(), name="soh-import-mutate"),
]
