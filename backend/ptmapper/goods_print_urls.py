"""Goods-v1 print-job routes (E182-E184). UUID jobs, goods contract only."""

from __future__ import annotations

from django.urls import path

from ptmapper.goods_print_views import (
    GoodsPrintJobCreateView,
    GoodsPrintJobDetailView,
    GoodsPrintJobOutcomeView,
)

urlpatterns = [
    path("print-jobs", GoodsPrintJobCreateView.as_view(), name="goods-print-job-create"),
    path(
        "print-jobs/<uuid:pk>/outcome",
        GoodsPrintJobOutcomeView.as_view(),
        name="goods-print-job-outcome",
    ),
    path("print-jobs/<uuid:pk>", GoodsPrintJobDetailView.as_view(), name="goods-print-job-detail"),
]
