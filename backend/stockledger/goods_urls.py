"""Goods-v1 stock routes, under ``/api/goods-v1/stockledger/``.

The five ledger reads (E171-E175) used to share their paths with the legacy
back-office readers, which keep ``/api/stockledger/...`` unchanged. Acceptance,
putaway, the origin journey and pending-acceptance work answer only here.
"""

from __future__ import annotations

from django.urls import path

from stockledger.goods_views import (
    AcceptanceCompleteView,
    AcceptanceExtraReportView,
    AcceptanceScanView,
    AcceptanceSessionCreateView,
    AcceptanceSessionDetailView,
    GoodsStockEntriesView,
    GoodsStockInTransitView,
    GoodsStockOnHandView,
    GoodsStockQuarantineView,
    GoodsStockSummaryView,
    OriginJourneyView,
    PendingAcceptanceView,
    ReturnedPiecesView,
)

urlpatterns = [
    path("entries", GoodsStockEntriesView.as_view(), name="goods-stock-entries"),
    path("summary", GoodsStockSummaryView.as_view(), name="goods-stock-summary"),
    path("on-hand", GoodsStockOnHandView.as_view(), name="goods-stock-on-hand"),
    path("in-transit", GoodsStockInTransitView.as_view(), name="goods-stock-in-transit"),
    path("quarantine", GoodsStockQuarantineView.as_view(), name="goods-stock-quarantine"),
    # Goods-v1 origin journey (E181).
    path("origins/<uuid:pk>/journey", OriginJourneyView.as_view(), name="goods-origin-journey"),
    # Goods-v1 pending acceptance work (E248): discovery for stock.accept alone.
    path("pending-acceptance", PendingAcceptanceView.as_view(), name="goods-pending-acceptance"),
    # Pieces a customer brought back, and putting them away (OPS-09, PRD §10.4).
    path(
        "returned-pieces",
        ReturnedPiecesView.as_view(),
        name="goods-returned-pieces",
    ),
    path(
        "returned-pieces/accept",
        ReturnedPiecesView.as_view(),
        name="goods-returned-pieces-accept",
    ),
    # Goods-v1 acceptance and putaway (E139-E142).
    path(
        "acceptance-sessions",
        AcceptanceSessionCreateView.as_view(),
        name="goods-acceptance-sessions",
    ),
    path(
        "acceptance-sessions/<uuid:pk>/scan",
        AcceptanceScanView.as_view(),
        name="goods-acceptance-scan",
    ),
    path(
        "acceptance-sessions/<uuid:pk>/complete",
        AcceptanceCompleteView.as_view(),
        name="goods-acceptance-complete",
    ),
    # Ticket 07B (E251): hand extra pieces over to the receipt's correction owner.
    path(
        "acceptance-sessions/<uuid:pk>/extra",
        AcceptanceExtraReportView.as_view(),
        name="goods-acceptance-extra",
    ),
    path(
        "acceptance-sessions/<uuid:pk>",
        AcceptanceSessionDetailView.as_view(),
        name="goods-acceptance-session",
    ),
]
