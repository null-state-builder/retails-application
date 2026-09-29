"""Goods-v1 receiving routes, under ``/api/goods-v1/inbound/``.

Goods-v1 resources use UUIDs (GSA-T01). The legacy pending, queue and GRN
readers were deleted with legacy receiving (OPS-18).
"""

from __future__ import annotations

from django.urls import path

from inbound.debit_note_views import (
    GoodsDebitNoteCancelView,
    GoodsDebitNoteDetailView,
    GoodsDebitNoteIssueView,
    GoodsDebitNoteListView,
    GoodsDebitNoteRequestApprovalView,
    GoodsDebitNoteReviewView,
)
from inbound.goods_views import (
    GoodsArrivalDetailView,
    GoodsArrivalDuplicateWarningView,
    GoodsArrivalInvoiceView,
    GoodsArrivalListCreateView,
    GoodsArrivalNoBookingView,
    GoodsArrivalSessionView,
    GoodsArrivalThreeWayView,
    GoodsCounterGrnView,
    GoodsCountHandoverView,
    GoodsDispositionView,
    GoodsGrnDamageReportView,
    GoodsGrnDetailView,
    GoodsGrnListCreateView,
    GoodsInboxView,
    GoodsObservationView,
    GoodsPendingView,
    GoodsQueueView,
)

urlpatterns = [
    path("pending", GoodsPendingView.as_view(), name="goods-inbound-pending"),
    # The receiving inbox (OPS-04): one list per site of what is arriving and
    # what each delivery waits on, Pending and History.
    path("inbox", GoodsInboxView.as_view(), name="goods-inbound-inbox"),
    path("queue", GoodsQueueView.as_view(), name="goods-inbound-queue"),
    path("arrivals", GoodsArrivalListCreateView.as_view(), name="goods-arrival-list"),
    # Before the ``<uuid:pk>`` detail route only for readability; a UUID
    # converter never matches this literal.
    path(
        "arrivals/duplicate-warning",
        GoodsArrivalDuplicateWarningView.as_view(),
        name="goods-arrival-duplicate-warning",
    ),
    path(
        "arrivals/<uuid:pk>/invoice",
        GoodsArrivalInvoiceView.as_view(),
        name="goods-arrival-invoice",
    ),
    path(
        "arrivals/<uuid:pk>/sessions",
        GoodsArrivalSessionView.as_view(),
        name="goods-arrival-sessions",
    ),
    path(
        "arrivals/<uuid:pk>/three-way-match",
        GoodsArrivalThreeWayView.as_view(),
        name="goods-arrival-three-way-match",
    ),
    path(
        "arrivals/<uuid:pk>/no-booking",
        GoodsArrivalNoBookingView.as_view(),
        name="goods-arrival-no-booking",
    ),
    path("arrivals/<uuid:pk>", GoodsArrivalDetailView.as_view(), name="goods-arrival-detail"),
    path(
        "count-sessions/<uuid:pk>/handover",
        GoodsCountHandoverView.as_view(),
        name="goods-count-session-handover",
    ),
    path(
        "count-sessions/<uuid:pk>/observations",
        GoodsObservationView.as_view(),
        name="goods-count-session-observations",
    ),
    path("grns", GoodsGrnListCreateView.as_view(), name="goods-grn-list"),
    path("grns/<uuid:pk>/counter", GoodsCounterGrnView.as_view(), name="goods-grn-counter"),
    path(
        "grns/<uuid:pk>/dispositions",
        GoodsDispositionView.as_view(),
        name="goods-grn-dispositions",
    ),
    path(
        "grns/<uuid:pk>/damage-reports",
        GoodsGrnDamageReportView.as_view(),
        name="goods-grn-damage-reports",
    ),
    path("grns/<uuid:pk>", GoodsGrnDetailView.as_view(), name="goods-grn-detail"),
    # Store operations ticket 38: debit notes drafted for accepted shortages.
    path("debit-notes", GoodsDebitNoteListView.as_view(), name="goods-debit-notes"),
    path("debit-notes/<int:pk>", GoodsDebitNoteDetailView.as_view(), name="goods-debit-note"),
    path(
        "debit-notes/<int:pk>/review",
        GoodsDebitNoteReviewView.as_view(),
        name="goods-debit-note-review",
    ),
    path(
        "debit-notes/<int:pk>/request-approval",
        GoodsDebitNoteRequestApprovalView.as_view(),
        name="goods-debit-note-request-approval",
    ),
    path(
        "debit-notes/<int:pk>/issue",
        GoodsDebitNoteIssueView.as_view(),
        name="goods-debit-note-issue",
    ),
    path(
        "debit-notes/<int:pk>/cancel",
        GoodsDebitNoteCancelView.as_view(),
        name="goods-debit-note-cancel",
    ),
]
