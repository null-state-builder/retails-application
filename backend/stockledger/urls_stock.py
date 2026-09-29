"""The `/api/stock/...` surface — stock questions asked from the shop floor.

Separate from `stockledger/urls.py` (`/api/stockledger/...`) because the two
answer different people. Those are ledger reads: entries, summaries, the on-hand
projection, in-transit and quarantine buckets — the back office's view of the
books. This one is the counter's: "who has this shirt in L?", asked with a
customer waiting. The D10 api-contract names the path, and it is the path a
store person's screen calls.

The goods-v1 availability read (E176) answers at `/api/goods-v1/stock/availability`.
"""

from __future__ import annotations

from django.urls import path

from stockledger.views import StockAvailabilityView

urlpatterns = [
    path("availability", StockAvailabilityView.as_view(), name="stock-availability"),
]
