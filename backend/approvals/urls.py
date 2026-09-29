from __future__ import annotations

from django.urls import path

from approvals.views import ApprovalDecideView, ApprovalInboxView, ApprovalListView

# Legacy approval screens only. The goods-v1 inbox, list and decide answer at
# `/api/goods-v1/approvals...` (`approvals/goods_urls.py`) — GSA-T01.
urlpatterns = [
    path("approvals/inbox", ApprovalInboxView.as_view(), name="approval-inbox"),
    path("approvals", ApprovalListView.as_view(), name="approval-list"),
    path("approvals/<int:pk>/decide", ApprovalDecideView.as_view(), name="approval-decide"),
]
