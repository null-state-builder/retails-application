"""Goods-v1 approval routes, under ``/api/goods-v1/``.

The inbox, the list and the decide command are the three operations Ticket 08A
left partial: they shared ``/api/approvals/inbox``, ``/api/approvals`` and the
``/api/approvals/{id}/decide`` path template with the legacy approval screens,
so only the legacy contract reached the emitted schema. Here each is the single
operation at its own path (GSA-T01).
"""

from __future__ import annotations

from django.urls import path

from approvals.goods_views import (
    GoodsApprovalDecideView,
    GoodsApprovalInboxView,
    GoodsApprovalListView,
)

urlpatterns = [
    path("approvals/inbox", GoodsApprovalInboxView.as_view(), name="goods-approval-inbox"),
    path("approvals", GoodsApprovalListView.as_view(), name="goods-approval-list"),
    path(
        "approvals/<uuid:pk>/decide",
        GoodsApprovalDecideView.as_view(),
        name="goods-approval-decide",
    ),
]
