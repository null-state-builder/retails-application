"""Approvals API — one inbox for the whole system (#70).

Three endpoints, no per-document-type variants:

    GET  /api/approvals/inbox        everything waiting for *me*
    GET  /api/approvals              the audit view (mine + my stores'), filterable
    POST /api/approvals/<pk>/decide  approve / reject (reason required on reject)

Deciding writes the approval and nothing else. The document's own approver
column is stamped by the module that owns it, at post time (ADR-0002: a
module's database is private) — so a *rejected* document is never stamped with
the name of the person who refused it.
"""

from __future__ import annotations

from typing import Any

from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from approvals.models import Approval, ApprovalStatus
from approvals.serializers import ApprovalDecisionSerializer, ApprovalReadSerializer
from approvals.services import (
    ApprovalError,
    ApprovalRightsError,
    decide,
    inbox_for,
)
from core.dates import bad_since, parse_day
from core.textsearch import search_term, text_filter
from masters.scoping import scope_by_entitlement_or_brand, scope_by_store_or_brand

#: Approvals (#106) — this model has no `doc_number` column of its own; `title`
#: is the requesting module's snapshot one-liner and the nearest stand-in.
#: "Type" is `kind_label` (the human label; `kind` is the machine code the
#: client routes on). "Requester" is `requested_by`.
APPROVAL_SEARCH_FIELDS = (
    "title",
    "kind_label",
    "requested_by__full_name",
    "requested_by__username",
)


class ApprovalInboxView(generics.ListAPIView[Approval]):
    """The one "waiting for you" screen. Never lists the caller's own requests —
    a self-approval can't succeed, so it is never offered."""

    permission_classes = [IsAuthenticated]
    serializer_class = ApprovalReadSerializer
    pagination_class = None

    def get_queryset(self) -> Any:
        qs = inbox_for(self.request.user)
        # The screen's own search box (#102), applied last so it can only
        # narrow what the inbox's own scoping already allows.
        return text_filter(qs, search_term(self.request), APPROVAL_SEARCH_FIELDS)


class ApprovalListView(generics.ListAPIView[Approval]):
    """History across every document type, store-scoped (ADR-0003). Includes the
    caller's own requests — the maker must be able to watch their own decision.

    The bell's Approvals History reads this with ``?decided=1&since=`` (#226) -
    two narrowings on the same list rather than an endpoint of its own, because
    "decisions, lately" is a question this view was already shaped to answer.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ApprovalReadSerializer
    pagination_class = None

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        # Checked before the queryset is built: `get_queryset` has no way to
        # answer a refusal, and a bad date must be a sentence with a code rather
        # than an empty list that reads like "no approvals ever". The same
        # `bad_since` the alerts history uses, so the popup meets one behaviour.
        refusal = bad_since((request.query_params.get("since") or "").strip())
        if refusal:
            return Response(refusal, status=status.HTTP_400_BAD_REQUEST)
        return super().list(request, *args, **kwargs)

    def get_queryset(self) -> Any:
        # The route and its cleared steps come along for the ride: the serializer
        # renders a step trail per row and this list is unpaginated, so without
        # them one history page is two queries per approval (#172).
        qs = Approval.objects.select_related(
            "store", "requested_by", "decided_by", "route"
        ).prefetch_related("step_decisions__decided_by")
        qs = scope_by_store_or_brand(qs, self.request.user)
        st = self.request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        kind = self.request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        # Only what a *person* decided (#226). `not_required` is a real row -
        # "auto-adjust, logged" - but nobody decided it, so it belongs on the
        # full screen (reachable through `status`) rather than in a history of
        # decisions.
        if self.request.query_params.get("decided") == "1":
            qs = qs.filter(status__in=[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED])
        # `since` is about the decision, not the request: an undecided row has no
        # `decided_at`, so it drops out of a dated window by construction.
        asked = (self.request.query_params.get("since") or "").strip()
        if asked:
            since = parse_day(asked)
            # `list` already refused an unreadable one; this is belt and braces
            # so a future caller of `get_queryset` cannot widen the window to
            # "everything" by passing nonsense.
            if since is not None:
                qs = qs.filter(decided_at__date__gte=since)
        # The screen's own search box (#102), applied last.
        return text_filter(qs, search_term(self.request), APPROVAL_SEARCH_FIELDS)


class ApprovalDecideView(APIView):
    """POST: approve or reject one approval as the signed-in user."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request, pk: int) -> Response:
        # Scope first: an out-of-scope approval must look like it doesn't exist.
        # By entitlement, not by the switcher — deciding is an act, and the unit
        # on screen must not narrow what the caller may act on. Or-brand, because
        # the brand manager the return policy names is bounded by brands and
        # would otherwise be scoped out of every row (#75).
        visible = scope_by_entitlement_or_brand(Approval.objects.all(), request.user)
        try:
            approval = visible.get(pk=pk)
        except Approval.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        ser = ApprovalDecisionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            approval = decide(
                approval,
                actor=request.user,
                action=ser.validated_data["action"],
                reason=ser.validated_data.get("reason", ""),
            )
        except ApprovalRightsError as e:
            return Response({"error": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ApprovalError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(ApprovalReadSerializer(approval).data)
