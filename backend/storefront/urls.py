from __future__ import annotations

from django.urls import path

from storefront.checklist_views import (
    ChecklistTemplateChangeView,
    ChecklistTemplateStopView,
    ChecklistTemplatesView,
    ChecklistTickPhotoView,
    ChecklistTickView,
    ChecklistTodayView,
)
from storefront.views import CashSummaryView, DashboardView

urlpatterns = [
    path("dashboard", DashboardView.as_view(), name="store-dashboard"),
    path("cash-summary", CashSummaryView.as_view(), name="store-cash-summary"),
    # Store operations ticket 49 (ST-OPS-4): task checklists.
    path("checklists", ChecklistTodayView.as_view(), name="store-checklists"),
    path("checklists/ticks", ChecklistTickView.as_view(), name="store-checklist-ticks"),
    path(
        "checklists/ticks/<uuid:pk>/photo",
        ChecklistTickPhotoView.as_view(),
        name="store-checklist-tick-photo",
    ),
    path("checklist-templates", ChecklistTemplatesView.as_view(), name="store-checklist-templates"),
    path(
        "checklist-templates/<uuid:pk>",
        ChecklistTemplateChangeView.as_view(),
        name="store-checklist-template",
    ),
    path(
        "checklist-templates/<uuid:pk>/stop",
        ChecklistTemplateStopView.as_view(),
        name="store-checklist-template-stop",
    ),
]
