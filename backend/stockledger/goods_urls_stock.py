"""The goods-v1 ``/api/goods-v1/stock/...`` surface — the counter's stock question.

Separate from ``stockledger/goods_urls.py`` for the same reason the legacy pair
is split (#175): those are ledger reads for the back office, this one is
"who has this shirt in L?", asked with a customer waiting (E176).
"""

from __future__ import annotations

from django.urls import path

from stockledger.broken_size_views import (
    GoodsBrokenSizeActView,
    GoodsBrokenSizesView,
    GoodsSizeRuleView,
)
from stockledger.goods_ageing_views import GoodsSeasonEndView, GoodsStockAgeingView
from stockledger.goods_views import GoodsStockAvailabilityView
from stockledger.sor_ageing_views import (
    GoodsSorAgeingView,
    GoodsSorBrandInvoiceView,
    GoodsSorDispatchDateView,
)

urlpatterns = [
    path("availability", GoodsStockAvailabilityView.as_view(), name="goods-stock-availability"),
    # Store operations ticket 33 (ST-INV-2): stock aged against its season.
    path("ageing", GoodsStockAgeingView.as_view(), name="goods-stock-ageing"),
    path("ageing/season-end", GoodsSeasonEndView.as_view(), name="goods-stock-season-end"),
    # Store operations ticket 32 (ST-INV-1): broken-size alerts and their rules.
    path("broken-sizes", GoodsBrokenSizesView.as_view(), name="goods-stock-broken-sizes"),
    path("broken-sizes/act", GoodsBrokenSizeActView.as_view(), name="goods-stock-broken-size-act"),
    path("broken-sizes/rules", GoodsSizeRuleView.as_view(), name="goods-stock-size-rules"),
    # Store operations ticket 24 (ST-BRD-5): SOR stock aged from the brand's dispatch date.
    path("sor-ageing", GoodsSorAgeingView.as_view(), name="goods-stock-sor-ageing"),
    path(
        "sor-ageing/dispatch-date",
        GoodsSorDispatchDateView.as_view(),
        name="goods-stock-sor-dispatch-date",
    ),
    path(
        "sor-ageing/brand-invoice",
        GoodsSorBrandInvoiceView.as_view(),
        name="goods-stock-sor-brand-invoice",
    ),
]
