"""Goods-v1 movement routes, under ``/api/goods-v1/outbound/``.

Movements, mark-damaged and stock search share their tails with the legacy
outbound readers, which keep ``/api/outbound/...`` unchanged. A goods client
chooses the goods contract by URL and nothing else (#303).
"""

from __future__ import annotations

from django.urls import path

from outbound.goods_soh_views import SohReconciliationDetailView, SohReconciliationMutationView, SohReconciliationStartView

from outbound.count_schedule_views import (
    CountScheduleChangeView,
    CountScheduleListView,
    CountScheduleStopView,
)
from outbound.goods_count_views import (
    CountLookupView,
    CountPassDetailView,
    CountPassResumeView,
    CountPassScanView,
    CountPassSubmitView,
    StocktakeCancelView,
    StocktakeCloseView,
    StocktakeDetailView,
    StocktakeListCreateView,
    StocktakePassOpenView,
    StocktakeRecountView,
    StocktakeVarianceView,
)
from outbound.goods_size_balancing_views import (
    GoodsSizeBalanceApproveView,
    GoodsSizeBalanceRejectView,
    GoodsSizeBalancingView,
)
from outbound.goods_transfer_views import (
    CorrectiveConfirmationView,
    DispatchAcceptView,
    DispatchArrivalView,
    DispatchCountView,
    DispatchDocumentView,
    DispatchEwayView,
    DispatchReturnAcceptView,
    DispatchReturnView,
    DispatchSessionDetailView,
    DispatchSessionScanView,
    DispatchShortageView,
    ExcessCorrectiveView,
    ShortageDecisionView,
    TransferApproveView,
    TransferCancelOutstandingView,
    TransferDestinationsView,
    TransferDetailView,
    TransferDispatchView,
    TransferListCreateView,
    TransferPreparationOpenView,
    TransferPrePtCustodyView,
    TransferQuarantineStockView,
    TransferRequestListCreateView,
    TransferSubmitView,
)
from outbound.goods_views import (
    AdjustmentReferencesView,
    DamageReportDecideView,
    DamageReportListView,
    MarkDamagedView,
    MovementDetailView,
    MovementListCreateView,
    MovementSubmitView,
    RtvAcknowledgementView,
    RtvEwayView,
    RtvPickupView,
    RtvPutawayView,
    RtvShipmentView,
    RtvShortfallClosureView,
    RtvSourceReturnView,
    RtvWithdrawalView,
    StockSearchView,
)

urlpatterns = [
    path("soh-reconciliations", SohReconciliationStartView.as_view(), name="goods-soh-reconciliation-start"),
    path("soh-reconciliations/<uuid:pk>", SohReconciliationDetailView.as_view(), name="goods-soh-reconciliation-detail"),
    path("soh-reconciliations/<uuid:pk>/<str:operation>", SohReconciliationMutationView.as_view(), name="goods-soh-reconciliation-mutate"),
    # Store operations ticket 34 (ST-TRF-1): transfers suggested to fill broken sizes.
    path("size-balancing", GoodsSizeBalancingView.as_view(), name="goods-size-balancing"),
    path(
        "size-balancing/approve",
        GoodsSizeBalanceApproveView.as_view(),
        name="goods-size-balancing-approve",
    ),
    path(
        "size-balancing/reject",
        GoodsSizeBalanceRejectView.as_view(),
        name="goods-size-balancing-reject",
    ),
    # OPS-06: the transfer lifecycle. The word routes come before the detail
    # route so a step's name is never read as a transfer's id.
    path("transfers", TransferListCreateView.as_view(), name="goods-transfers"),
    path(
        "transfers/destinations",
        TransferDestinationsView.as_view(),
        name="goods-transfer-destinations",
    ),
    path(
        "transfers/quarantine-stock",
        TransferQuarantineStockView.as_view(),
        name="goods-transfer-quarantine-stock",
    ),
    path(
        "transfers/pre-pt-custody",
        TransferPrePtCustodyView.as_view(),
        name="goods-transfer-pre-pt-custody",
    ),
    path(
        "transfer-requests",
        TransferRequestListCreateView.as_view(),
        name="goods-transfer-requests",
    ),
    path("transfers/<uuid:pk>/submit", TransferSubmitView.as_view(), name="goods-transfer-submit"),
    path(
        "transfers/<uuid:pk>/approve", TransferApproveView.as_view(), name="goods-transfer-approve"
    ),
    path(
        "transfers/<uuid:pk>/cancel-outstanding",
        TransferCancelOutstandingView.as_view(),
        name="goods-transfer-cancel-outstanding",
    ),
    path(
        "transfers/<uuid:pk>/dispatches",
        TransferDispatchView.as_view(),
        name="goods-transfer-dispatches",
    ),
    # Goods ticket 13B: E242 opens or resumes the shipment being scanned; E243
    # reads it and E244 scans into it; E147 records a shipment's arrival and
    # E150 its e-way evidence.
    path(
        "transfers/<uuid:pk>/dispatch-sessions",
        TransferPreparationOpenView.as_view(),
        name="goods-transfer-dispatch-sessions",
    ),
    path(
        "dispatch-sessions/<uuid:pk>/scan",
        DispatchSessionScanView.as_view(),
        name="goods-dispatch-session-scan",
    ),
    path(
        "dispatch-sessions/<uuid:pk>",
        DispatchSessionDetailView.as_view(),
        name="goods-dispatch-session",
    ),
    path(
        "transfers/<uuid:pk>/dispatches/<uuid:dispatch_id>/arrival",
        DispatchArrivalView.as_view(),
        name="goods-transfer-dispatch-arrival",
    ),
    # Store operations ticket 36: the challan or tax invoice a shipment left with.
    path(
        "transfers/<uuid:pk>/dispatches/<uuid:dispatch_id>/document",
        DispatchDocumentView.as_view(),
        name="goods-transfer-dispatch-document",
    ),
    path(
        "transfers/<uuid:pk>/dispatches/<uuid:dispatch_id>/eway",
        DispatchEwayView.as_view(),
        name="goods-transfer-dispatch-eway",
    ),
    path(
        "transfers/<uuid:pk>/dispatches/<uuid:dispatch_id>/count",
        DispatchCountView.as_view(),
        name="goods-transfer-dispatch-count",
    ),
    path(
        "transfers/<uuid:pk>/dispatches/<uuid:dispatch_id>/accept",
        DispatchAcceptView.as_view(),
        name="goods-transfer-dispatch-accept",
    ),
    path(
        "transfers/<uuid:pk>/dispatches/<uuid:dispatch_id>/return-to-source",
        DispatchReturnView.as_view(),
        name="goods-transfer-dispatch-return",
    ),
    path(
        "transfers/<uuid:pk>/dispatches/<uuid:dispatch_id>/accept-returned",
        DispatchReturnAcceptView.as_view(),
        name="goods-transfer-dispatch-accept-returned",
    ),
    # Goods ticket 14: a counted shipment's shortage, proposed and decided.
    path(
        "transfers/<uuid:pk>/dispatches/<uuid:dispatch_id>/shortage",
        DispatchShortageView.as_view(),
        name="goods-transfer-dispatch-shortage",
    ),
    path(
        "transfers/<uuid:pk>/shortages/<uuid:gap_id>/decide",
        ShortageDecisionView.as_view(),
        name="goods-transfer-shortage-decide",
    ),
    # Goods ticket 16: correcting observed excess with a corrective transfer.
    path(
        "transfers/<uuid:pk>/excess/<uuid:lot_id>/corrective",
        ExcessCorrectiveView.as_view(),
        name="goods-transfer-excess-corrective",
    ),
    path(
        "transfers/<uuid:pk>/corrective-confirmation",
        CorrectiveConfirmationView.as_view(),
        name="goods-transfer-corrective-confirmation",
    ),
    path("transfers/<uuid:pk>", TransferDetailView.as_view(), name="goods-transfer"),
    # E104 list / E151 create.
    path("movements", MovementListCreateView.as_view(), name="goods-movements"),
    # Goods ticket 15A: the GRN/PT an adjust-down may reference. A word route, so
    # it comes before the detail route.
    path(
        "movements/adjustment-references",
        AdjustmentReferencesView.as_view(),
        name="goods-movement-adjustment-references",
    ),
    # E155 submit, before the detail route so the word is never read as an id.
    path("movements/<uuid:pk>/submit", MovementSubmitView.as_view(), name="goods-movement-submit"),
    # Goods ticket 15B: an approved RTV's vendor pickups and balance withdrawals.
    path(
        "movements/<uuid:pk>/rtv-pickups",
        RtvPickupView.as_view(),
        name="goods-movement-rtv-pickups",
    ),
    path(
        "movements/<uuid:pk>/rtv-withdrawals",
        RtvWithdrawalView.as_view(),
        name="goods-movement-rtv-withdrawals",
    ),
    # Goods ticket 15F: shipped RTV and vendor receipt.
    path(
        "movements/<uuid:pk>/rtv-shipments",
        RtvShipmentView.as_view(),
        name="goods-movement-rtv-shipments",
    ),
    path(
        "movements/<uuid:pk>/rtv-shipments/<uuid:shipment_id>/acknowledgements",
        RtvAcknowledgementView.as_view(),
        name="goods-movement-rtv-shipment-acknowledgements",
    ),
    path(
        "movements/<uuid:pk>/rtv-shipments/<uuid:shipment_id>/returns",
        RtvSourceReturnView.as_view(),
        name="goods-movement-rtv-shipment-returns",
    ),
    path(
        "movements/<uuid:pk>/rtv-shipments/<uuid:shipment_id>/putaways",
        RtvPutawayView.as_view(),
        name="goods-movement-rtv-shipment-putaways",
    ),
    path(
        "movements/<uuid:pk>/rtv-shipments/<uuid:shipment_id>/eway",
        RtvEwayView.as_view(),
        name="goods-movement-rtv-shipment-eway",
    ),
    path(
        "movements/<uuid:pk>/rtv-shipments/<uuid:shipment_id>/shortfall-closures",
        RtvShortfallClosureView.as_view(),
        name="goods-movement-rtv-shipment-shortfall-closures",
    ),
    # E105 detail.
    path("movements/<uuid:pk>", MovementDetailView.as_view(), name="goods-movement"),
    # E209 mark damaged.
    path("mark-damaged", MarkDamagedView.as_view(), name="goods-mark-damaged"),
    # OPS-05: the damage reports mark-damaged opens, and the second person's decision.
    path("damage-reports", DamageReportListView.as_view(), name="goods-damage-reports"),
    path(
        "damage-reports/<uuid:pk>/decide",
        DamageReportDecideView.as_view(),
        name="goods-damage-report-decide",
    ),
    # E210 stock search.
    path("stock-search", StockSearchView.as_view(), name="goods-stock-search"),
    # Goods ticket 17: non-trading counts - start (E159), passes (E160-E162 and
    # resume), the reviewer's variance and recount (E163/E164), the zero-variance
    # close and cancellation (P18, E166), and the blind lookup (E211). Word routes
    # before the detail route, as everywhere in this file.
    path("stocktakes", StocktakeListCreateView.as_view(), name="goods-stocktakes"),
    path(
        "stocktakes/<uuid:pk>/sessions",
        StocktakePassOpenView.as_view(),
        name="goods-stocktake-sessions",
    ),
    path(
        "stocktakes/<uuid:pk>/variance",
        StocktakeVarianceView.as_view(),
        name="goods-stocktake-variance",
    ),
    path(
        "stocktakes/<uuid:pk>/recount",
        StocktakeRecountView.as_view(),
        name="goods-stocktake-recount",
    ),
    path("stocktakes/<uuid:pk>/close", StocktakeCloseView.as_view(), name="goods-stocktake-close"),
    path(
        "stocktakes/<uuid:pk>/cancel", StocktakeCancelView.as_view(), name="goods-stocktake-cancel"
    ),
    path("stocktakes/<uuid:pk>", StocktakeDetailView.as_view(), name="goods-stocktake"),
    path(
        "count-sessions/<uuid:pk>/scan",
        CountPassScanView.as_view(),
        name="goods-count-session-scan",
    ),
    path(
        "count-sessions/<uuid:pk>/submit",
        CountPassSubmitView.as_view(),
        name="goods-count-session-submit",
    ),
    path(
        "count-sessions/<uuid:pk>/resume",
        CountPassResumeView.as_view(),
        name="goods-count-session-resume",
    ),
    path("count-sessions/<uuid:pk>", CountPassDetailView.as_view(), name="goods-count-session"),
    path("count-lookup", CountLookupView.as_view(), name="goods-count-lookup"),
    # Store operations ticket 35 (ST-INV-3): when each store's blind count is due.
    path("count-schedules", CountScheduleListView.as_view(), name="goods-count-schedules"),
    path(
        "count-schedules/<uuid:pk>",
        CountScheduleChangeView.as_view(),
        name="goods-count-schedule-change",
    ),
    path(
        "count-schedules/<uuid:pk>/stop",
        CountScheduleStopView.as_view(),
        name="goods-count-schedule-stop",
    ),
]
