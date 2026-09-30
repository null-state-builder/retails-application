"""Routes for the counter (mounted at `/api/sell/`).

`doc_number` is matched with `<path:...>` because the Tally join key carries
slashes - `26-27/DEO/SAL/74` is one identifier, not four segments.
"""

from __future__ import annotations

from django.urls import path

from sell.alteration_views import (
    AlterationBillView,
    AlterationCancelView,
    AlterationDetailView,
    AlterationListView,
    AlterationStepView,
)
from sell.cash_views import CashCountsView, CashMovementsView, CashPositionView
from sell.discount_views import DiscountReportView
from sell.gift_voucher_views import GiftVoucherListView, GiftVoucherLookupView
from sell.petty_cash_views import (
    PettyCashBillView,
    PettyCashFloatView,
    PettyCashSpendsView,
    PettyCashSpendView,
    PettyCashTopUpsView,
    PettyCashView,
)
from sell.reservation_views import (
    ReservationCancelView,
    ReservationDetailView,
    ReservationListView,
    ReservationRefundView,
    ReservationTermsView,
)
from sell.special_order_views import (
    SpecialOrderArriveView,
    SpecialOrderCancelView,
    SpecialOrderDetailView,
    SpecialOrderListView,
    SpecialOrderOrderView,
    SpecialOrderRefundView,
    SpecialOrderTellView,
)
from sell.views import (
    ConsentView,
    CustomerDisplayView,
    DatasetView,
    HeldBillsView,
    IrnQueueItemView,
    IrnQueueView,
    RegisterHandoverView,
    RegisterView,
    ReturnWhereView,
    SaleDetailView,
    SaleListCreateView,
    OnlineFinaliseView,
    SavedSizeView,
    SellPolicyView,
    StoreFlagsView,
    StoreFlagView,
    TillAllocationReleaseView,
    TillNumberBlocksView,
    TillRegisterView,
    TillRenewView,
    TillPairView,
    TillResumeView,
    TillView,
)

urlpatterns = [
    path("sales/finalise-online", OnlineFinaliseView.as_view(), name="sell-finalise-online"),
    path("till/pair", TillPairView.as_view(), name="sell-till-pair"),
    path("policy", SellPolicyView.as_view(), name="sell-policy"),
    path("dataset", DatasetView.as_view(), name="sell-dataset"),
    path("customer-display", CustomerDisplayView.as_view(), name="sell-customer-display"),
    # Ticket 15: customer consent - read what stands for a number, record an answer.
    path("consents", ConsentView.as_view(), name="sell-consents"),
    # Ticket 18: the customer's saved size per brand - read, and correct.
    path("saved-sizes", SavedSizeView.as_view(), name="sell-saved-sizes"),
    # Ticket 41: the day-close cash count, and cash leaving the drawer.
    path("cash-count", CashPositionView.as_view(), name="sell-cash-position"),
    path("cash-counts", CashCountsView.as_view(), name="sell-cash-counts"),
    path("cash-movements", CashMovementsView.as_view(), name="sell-cash-movements"),
    # Ticket 42: the store's petty cash box, its top-ups and its spends.
    path("petty-cash", PettyCashView.as_view(), name="sell-petty-cash"),
    path("petty-cash/float", PettyCashFloatView.as_view(), name="sell-petty-cash-float"),
    path("petty-cash/top-ups", PettyCashTopUpsView.as_view(), name="sell-petty-cash-top-ups"),
    path("petty-cash/spends", PettyCashSpendsView.as_view(), name="sell-petty-cash-spends"),
    path("petty-cash/spends/<int:pk>", PettyCashSpendView.as_view(), name="sell-petty-cash-spend"),
    path(
        "petty-cash/spends/<int:pk>/bill",
        PettyCashBillView.as_view(),
        name="sell-petty-cash-spend-bill",
    ),
    # Ticket 20: customer reservations - online only, the switch for new ones.
    path("reservations", ReservationListView.as_view(), name="sell-reservations"),
    path("reservations/terms", ReservationTermsView.as_view(), name="sell-reservation-terms"),
    path("reservations/<uuid:pk>", ReservationDetailView.as_view(), name="sell-reservation"),
    path(
        "reservations/<uuid:pk>/cancel",
        ReservationCancelView.as_view(),
        name="sell-reservation-cancel",
    ),
    path(
        "reservations/<uuid:pk>/refund",
        ReservationRefundView.as_view(),
        name="sell-reservation-refund",
    ),
    # Ticket 19: gift vouchers - sold and looked up online only, the switch for both.
    path("gift-vouchers", GiftVoucherListView.as_view(), name="sell-gift-vouchers"),
    path("gift-vouchers/lookup", GiftVoucherLookupView.as_view(), name="sell-gift-voucher-lookup"),
    # Ticket 21: special orders - online only, the switch for new ones.
    path("special-orders", SpecialOrderListView.as_view(), name="sell-special-orders"),
    path("special-orders/<uuid:pk>", SpecialOrderDetailView.as_view(), name="sell-special-order"),
    path(
        "special-orders/<uuid:pk>/order",
        SpecialOrderOrderView.as_view(),
        name="sell-special-order-order",
    ),
    path(
        "special-orders/<uuid:pk>/arrive",
        SpecialOrderArriveView.as_view(),
        name="sell-special-order-arrive",
    ),
    path(
        "special-orders/<uuid:pk>/tell",
        SpecialOrderTellView.as_view(),
        name="sell-special-order-tell",
    ),
    path(
        "special-orders/<uuid:pk>/cancel",
        SpecialOrderCancelView.as_view(),
        name="sell-special-order-cancel",
    ),
    path(
        "special-orders/<uuid:pk>/refund",
        SpecialOrderRefundView.as_view(),
        name="sell-special-order-refund",
    ),
    # Ticket 22: alteration job cards - online only, the switch for new ones.
    path("alterations", AlterationListView.as_view(), name="sell-alterations"),
    path("alterations/bill", AlterationBillView.as_view(), name="sell-alteration-bill"),
    path("alterations/<uuid:pk>", AlterationDetailView.as_view(), name="sell-alteration"),
    *[
        path(
            f"alterations/<uuid:pk>/{step}",
            AlterationStepView.as_view(step=step),
            name=f"sell-alteration-{step}",
        )
        for step in AlterationStepView.STEPS
    ],
    path(
        "alterations/<uuid:pk>/cancel",
        AlterationCancelView.as_view(),
        name="sell-alteration-cancel",
    ),
    # Ticket 13: where a bill of another store can go back, never what is on it.
    path("return-where", ReturnWhereView.as_view(), name="sell-return-where"),
    path("register/handover", RegisterHandoverView.as_view(), name="sell-register-handover"),
    path("register", RegisterView.as_view(), name="sell-register"),
    # The device that owns this store's series, and the window it bills in
    # (OPS-09, PRD §10.1-10.2). Ordered longest-first: `till/register` and
    # `till/renew` must be matched before the bare `till` read.
    path("till/register", TillRegisterView.as_view(), name="sell-till-register"),
    path("till/renew", TillRenewView.as_view(), name="sell-till-renew"),
    path("till/number-blocks", TillNumberBlocksView.as_view(), name="sell-till-number-blocks"),
    path(
        "till/allocations/<int:version>/release",
        TillAllocationReleaseView.as_view(),
        name="sell-till-allocation-release",
    ),
    path("till/resume", TillResumeView.as_view(), name="sell-till-resume"),
    path("till", TillView.as_view(), name="sell-till"),
    path("held-bills", HeldBillsView.as_view(), name="sell-held-bills"),
    path("flags", StoreFlagsView.as_view(), name="sell-flags"),
    path("flags/<int:pk>", StoreFlagView.as_view(), name="sell-flag"),
    path("irn-queue", IrnQueueView.as_view(), name="sell-irn-queue"),
    path("irn-queue/<int:pk>", IrnQueueItemView.as_view(), name="sell-irn-queue-item"),
    # What the chain gave away, and what it forgot to give (D11 §5, §8). Read
    # off bills, so it lives here; gated on `offers_price`, because that is the
    # screen it answers.
    path("discounts", DiscountReportView.as_view(), name="discount-report"),
    path("sales", SaleListCreateView.as_view(), name="sale-list"),
    path("sales/<path:doc_number>", SaleDetailView.as_view(), name="sale-detail"),
]
