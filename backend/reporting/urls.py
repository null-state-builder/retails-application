from __future__ import annotations

from django.urls import path

from reporting.brand_report_views import (
    BrandLayoutsView,
    BrandReportFileView,
    BrandReportMakeView,
    BrandReportsView,
)
from reporting.views import (
    BrandPerformanceExportView,
    BrandPerformanceView,
    DiscountFundingExportView,
    DiscountFundingView,
    ExceptionsReportExportView,
    ExceptionsReportView,
    GiftReportExportView,
    GiftReportView,
    GstReportExportView,
    GstReportView,
    InventoryReportExportView,
    InventoryReportView,
    MarginShareExportView,
    MarginShareView,
    OfferReturnExportView,
    OfferReturnView,
    OfferSimulationExportView,
    OfferSimulationView,
    SalesReportExportView,
    SalesReportView,
    ShrinkageReportExportView,
    ShrinkageReportView,
    StaffReportExportView,
    StaffReportView,
    StaffTargetsView,
)

urlpatterns = [
    path("sales", SalesReportView.as_view(), name="reports-sales"),
    path("sales/export.xlsx", SalesReportExportView.as_view(), name="reports-sales-export"),
    path("offer-simulation", OfferSimulationView.as_view(), name="reports-offer-simulation"),
    path(
        "offer-simulation/export.xlsx",
        OfferSimulationExportView.as_view(),
        name="reports-offer-simulation-export",
    ),
    path("offer-return", OfferReturnView.as_view(), name="reports-offer-return"),
    path(
        "offer-return/export.xlsx",
        OfferReturnExportView.as_view(),
        name="reports-offer-return-export",
    ),
    path("gst", GstReportView.as_view(), name="reports-gst"),
    path("gst/export.xlsx", GstReportExportView.as_view(), name="reports-gst-export"),
    path("gift-stock", GiftReportView.as_view(), name="reports-gift-stock"),
    path(
        "gift-stock/export.xlsx",
        GiftReportExportView.as_view(),
        name="reports-gift-stock-export",
    ),
    path("discount-funding", DiscountFundingView.as_view(), name="reports-discount-funding"),
    path(
        "discount-funding/export.xlsx",
        DiscountFundingExportView.as_view(),
        name="reports-discount-funding-export",
    ),
    path("shrinkage", ShrinkageReportView.as_view(), name="reports-shrinkage"),
    path(
        "shrinkage/export.xlsx",
        ShrinkageReportExportView.as_view(),
        name="reports-shrinkage-export",
    ),
    path("inventory", InventoryReportView.as_view(), name="reports-inventory"),
    path(
        "inventory/export.xlsx",
        InventoryReportExportView.as_view(),
        name="reports-inventory-export",
    ),
    path("margin-share", MarginShareView.as_view(), name="reports-margin-share"),
    path(
        "margin-share/export.xlsx",
        MarginShareExportView.as_view(),
        name="reports-margin-share-export",
    ),
    path("exceptions", ExceptionsReportView.as_view(), name="reports-exceptions"),
    path(
        "exceptions/export.xlsx",
        ExceptionsReportExportView.as_view(),
        name="reports-exceptions-export",
    ),
    path("brand-performance", BrandPerformanceView.as_view(), name="reports-brand-performance"),
    path(
        "brand-performance/export.xlsx",
        BrandPerformanceExportView.as_view(),
        name="reports-brand-performance-export",
    ),
    path("staff", StaffReportView.as_view(), name="reports-staff"),
    path("staff/export.xlsx", StaffReportExportView.as_view(), name="reports-staff-export"),
    path("staff/targets", StaffTargetsView.as_view(), name="reports-staff-targets"),
    path("brand-reports", BrandReportsView.as_view(), name="reports-brand-reports"),
    path("brand-reports/make", BrandReportMakeView.as_view(), name="reports-brand-reports-make"),
    path(
        "brand-reports/files/<int:pk>",
        BrandReportFileView.as_view(),
        name="reports-brand-reports-file",
    ),
    path("brand-layouts", BrandLayoutsView.as_view(), name="reports-brand-layouts"),
]
