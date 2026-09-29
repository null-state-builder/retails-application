"""``/api/goods-v1/sell/``: the staff list and old salesperson rows (ticket 07), the
items with no HSN (ticket 12), the Customers menu (tickets 16 and 17) and
Brands > Claims (ticket 26)."""

from __future__ import annotations

from django.urls import path

from sell.brand_claim_views import (
    GoodsBrandClaimAcceptView,
    GoodsBrandClaimDetailView,
    GoodsBrandClaimListView,
    GoodsBrandClaimRaiseView,
    GoodsBrandClaimSettleView,
)
from sell.goods_customer_views import (
    GoodsCustomerCorrectView,
    GoodsCustomerDetailView,
    GoodsCustomerEraseView,
    GoodsCustomerHeldView,
    GoodsCustomerListView,
    GoodsCustomerMergeView,
    GoodsCustomerMoveView,
    GoodsCustomerWithdrawView,
)
from sell.goods_missing_hsn_views import GoodsMissingHsnView
from sell.goods_salesperson_views import (
    GoodsSalespersonMatchesView,
    GoodsSalespersonMatchResolveView,
    GoodsStaffListView,
)

urlpatterns = [
    # Ticket 26: Brands > Claims - brand-funded discounts claimed at month end.
    path("brand-claims", GoodsBrandClaimListView.as_view(), name="goods-sell-brand-claims"),
    path(
        "brand-claims/raise",
        GoodsBrandClaimRaiseView.as_view(),
        name="goods-sell-brand-claims-raise",
    ),
    path(
        "brand-claims/<int:pk>",
        GoodsBrandClaimDetailView.as_view(),
        name="goods-sell-brand-claim",
    ),
    path(
        "brand-claims/<int:pk>/accept",
        GoodsBrandClaimAcceptView.as_view(),
        name="goods-sell-brand-claim-accept",
    ),
    path(
        "brand-claims/<int:pk>/settle",
        GoodsBrandClaimSettleView.as_view(),
        name="goods-sell-brand-claim-settle",
    ),
    # Ticket 16: the Customers menu - list, customer page and the rights screen.
    path("customers", GoodsCustomerListView.as_view(), name="goods-sell-customers"),
    path("customers/<int:pk>", GoodsCustomerDetailView.as_view(), name="goods-sell-customer"),
    path(
        "customers/<int:pk>/held",
        GoodsCustomerHeldView.as_view(),
        name="goods-sell-customer-held",
    ),
    path(
        "customers/<int:pk>/correct",
        GoodsCustomerCorrectView.as_view(),
        name="goods-sell-customer-correct",
    ),
    path(
        "customers/<int:pk>/withdraw",
        GoodsCustomerWithdrawView.as_view(),
        name="goods-sell-customer-withdraw",
    ),
    path(
        "customers/<int:pk>/erase",
        GoodsCustomerEraseView.as_view(),
        name="goods-sell-customer-erase",
    ),
    # Ticket 17: merge two records for one person, and move to a new number.
    path(
        "customers/<int:pk>/merge",
        GoodsCustomerMergeView.as_view(),
        name="goods-sell-customer-merge",
    ),
    path(
        "customers/<int:pk>/move",
        GoodsCustomerMoveView.as_view(),
        name="goods-sell-customer-move",
    ),
    path("missing-hsn", GoodsMissingHsnView.as_view(), name="goods-sell-missing-hsn"),
    path("staff-list", GoodsStaffListView.as_view(), name="goods-sell-staff-list"),
    path(
        "salesperson-matches",
        GoodsSalespersonMatchesView.as_view(),
        name="goods-sell-salesperson-matches",
    ),
    path(
        "salesperson-matches/<int:pk>/resolve",
        GoodsSalespersonMatchResolveView.as_view(),
        name="goods-sell-salesperson-match-resolve",
    ),
]
