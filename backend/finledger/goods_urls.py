"""Goods-v1 finance routes: brand payables for outright brands (ticket 28)."""

from __future__ import annotations

from django.urls import path

from finledger.payable_views import (
    GoodsPayableInvoiceCancelView,
    GoodsPayableInvoiceCreateView,
    GoodsPayableOptionsView,
    GoodsPayablePaymentCancelView,
    GoodsPayablePaymentCreateView,
    GoodsPayablesView,
)

urlpatterns = [
    path("payables", GoodsPayablesView.as_view(), name="goods-finledger-payables"),
    path(
        "payables/options",
        GoodsPayableOptionsView.as_view(),
        name="goods-finledger-payables-options",
    ),
    path(
        "payables/invoices",
        GoodsPayableInvoiceCreateView.as_view(),
        name="goods-finledger-payables-invoices",
    ),
    path(
        "payables/invoices/<int:pk>/cancel",
        GoodsPayableInvoiceCancelView.as_view(),
        name="goods-finledger-payables-invoice-cancel",
    ),
    path(
        "payables/payments",
        GoodsPayablePaymentCreateView.as_view(),
        name="goods-finledger-payables-payments",
    ),
    path(
        "payables/payments/<int:pk>/cancel",
        GoodsPayablePaymentCancelView.as_view(),
        name="goods-finledger-payables-payment-cancel",
    ),
]
