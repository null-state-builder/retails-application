"""Goods-v1 administrative-history route."""

from django.urls import path

from core.goods_history_views import GoodsAdministrativeHistoryView

urlpatterns = [
    path(
        "history/<str:subject_kind>/<path:subject_id>",
        GoodsAdministrativeHistoryView.as_view(),
        name="goods-administrative-history",
    )
]
