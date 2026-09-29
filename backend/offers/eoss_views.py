"""The EOSS planning API: recommendations to review, and the ladder that
proposes them (mounted under `/api/offers/eoss/`).

Reads answer at `view`; regenerating the plan and deciding a recommendation
both need `approve` (the human-in-the-loop gate the research calls the
industry norm); the ladder/target curve itself needs `manage`, the same rung
`offers.views` asks before anybody may author a rule.
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import Serializer
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import require_section
from accounts.sections import CAP_APPROVE, CAP_VIEW
from core.refusals import first_message, refusal_body
from masters.models import Brand, Season, Store
from offers.eoss_engine import generate_recommendations
from offers.eoss_serializers import (
    EossLadderStepSerializer,
    EossRecommendationSerializer,
    SellThroughTargetSerializer,
)
from offers.models import EossLadderStep, EossRecommendation, Offer, SellThroughTarget

CanReadOrApprove = require_section("offers_price", CAP_VIEW, write_minimum=CAP_APPROVE)
CanManageConfig = require_section("offers_price", CAP_VIEW, write_minimum=CAP_APPROVE)


def _bad_request(message: str) -> Response:
    return Response(refusal_body("VALIDATION", message), status=400)


class EossRecommendationListView(APIView):
    """`GET` the current plan. `POST` recomputes it for a season."""

    permission_classes = [IsAuthenticated, CanReadOrApprove]

    def get(self, request: Request) -> Response:
        rows = EossRecommendation.objects.select_related("season", "brand")
        season = (request.query_params.get("season") or "").strip()
        if season:
            rows = rows.filter(season__code__iexact=season)
        brand = (request.query_params.get("brand") or "").strip()
        if brand:
            rows = rows.filter(brand__code__iexact=brand)
        status = (request.query_params.get("status") or "").strip()
        if status:
            rows = rows.filter(status=status)
        return Response(EossRecommendationSerializer(rows, many=True).data)

    def post(self, request: Request) -> Response:
        season_code = str(request.data.get("season") or "").strip()
        if not season_code:
            return _bad_request("Choose a season to plan for.")
        if not Season.objects.filter(code=season_code).exists():
            return Response(refusal_body("NOT_FOUND", f"No season {season_code}."), status=404)
        written = generate_recommendations(season_code)
        rows = EossRecommendation.objects.filter(season__code=season_code).select_related(
            "season", "brand"
        )
        return Response(
            {
                "generated": written,
                "recommendations": EossRecommendationSerializer(rows, many=True).data,
            }
        )


class EossRecommendationDecisionView(APIView):
    """Approve (spins up the live `Offer`) or reject one recommendation."""

    permission_classes = [IsAuthenticated, CanReadOrApprove]

    @transaction.atomic
    def post(self, request: Request, pk: int) -> Response:
        rec = (
            EossRecommendation.objects.select_for_update(of=("self",))
            .select_related("season", "brand")
            .filter(pk=pk)
            .first()
        )
        if rec is None:
            return Response(refusal_body("NOT_FOUND", f"No recommendation {pk}."), status=404)
        if rec.status != EossRecommendation.Status.PENDING:
            return _bad_request(f"This recommendation was already {rec.status}.")

        # IsAuthenticated above guarantees a real user, never AnonymousUser.
        user = cast(User, request.user)

        action = str(request.data.get("action") or "")
        if action == "reject":
            rec.status = EossRecommendation.Status.REJECTED
            rec.decided_by = user
            rec.decided_at = timezone.now()
            rec.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
            return Response(EossRecommendationSerializer(rec).data)

        if action != "approve":
            return _bad_request("action must be 'approve' or 'reject'.")

        pct = request.data.get("discount_pct", rec.recommended_discount_pct)
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            return _bad_request("discount_pct must be a number.")
        if not (0 < pct <= 90):
            return _bad_request("discount_pct must be above 0% and at most 90%.")

        stores = sorted(Store.objects.filter(is_active=True).values_list("code", flat=True))
        offer = Offer.objects.create(
            name=f"EOSS · {rec.design} {rec.color}".strip(" ·"),
            mode=Offer.Mode.EOSS,
            brand=rec.brand,
            funder=Offer.Funder.KDPS,
            layer=Offer.Layer.BRAND if rec.brand else Offer.Layer.STOREWIDE,
            trigger_type=Offer.Trigger.NONE,
            reward_type=Offer.Reward.PCT_OFF,
            reward_config={"percent": f"{pct:.2f}"},
            item_scope={"styles": [rec.design], "colors": [rec.color]} if rec.design else {},
            store_scope={"kind": "all", "stores": stores},
            starts_on=date.today(),
            status=Offer.Status.APPROVED,
            approved_by=user,
            created_by=user,
        )
        rec.status = EossRecommendation.Status.APPROVED
        rec.decided_discount_pct = pct
        rec.decided_by = user
        rec.decided_at = timezone.now()
        rec.offer = offer
        rec.save(
            update_fields=[
                "status",
                "decided_discount_pct",
                "decided_by",
                "decided_at",
                "offer",
                "updated_at",
            ]
        )
        return Response(EossRecommendationSerializer(rec).data)


class EossConfigView(APIView):
    """`GET`/`PUT` the markdown ladder and the sell-through target curve.

    `PUT` replaces a brand's whole ladder (or the whole default curve) in one
    call - a step numbered out of order left behind by a partial edit is worse
    than asking the screen to send the complete list every time.
    """

    permission_classes = [IsAuthenticated, CanManageConfig]

    def get(self, request: Request) -> Response:
        brand_code = (request.query_params.get("brand") or "").strip()
        ladder = EossLadderStep.objects.select_related("brand")
        targets = SellThroughTarget.objects.select_related("brand")
        if brand_code:
            ladder = ladder.filter(brand__code__iexact=brand_code)
            targets = targets.filter(brand__code__iexact=brand_code)
        else:
            ladder = ladder.filter(brand__isnull=True)
            targets = targets.filter(brand__isnull=True)
        return Response(
            {
                "ladder": EossLadderStepSerializer(ladder, many=True).data,
                "targets": SellThroughTargetSerializer(targets, many=True).data,
            }
        )

    @transaction.atomic
    def put(self, request: Request) -> Response:
        brand_code = (request.data.get("brand") or "").strip() or None
        ladder_in = request.data.get("ladder")
        targets_in = request.data.get("targets")
        if not isinstance(ladder_in, list) or not isinstance(targets_in, list):
            return _bad_request("Send whole 'ladder' and 'targets' lists.")

        brand = None
        if brand_code:
            brand = Brand.objects.filter(code=brand_code).first()
            if brand is None:
                return Response(refusal_body("NOT_FOUND", f"No brand {brand_code}."), status=404)

        # Clear this brand's existing rows *before* validating the new ones -
        # otherwise every row here collides with the very rows it is about to
        # replace, and PUT can never be called twice for the same brand.
        EossLadderStep.objects.filter(brand=brand).delete()
        SellThroughTarget.objects.filter(brand=brand).delete()

        ladder_rows = []
        for i, raw in enumerate(ladder_in, start=1):
            data = {**raw, "brand": brand_code, "step_no": raw.get("step_no", i)}
            serializer: Serializer[Any] = EossLadderStepSerializer(data=data)
            if not serializer.is_valid():
                return _bad_request(f"ladder[{i - 1}]: {first_message(serializer.errors)}")
            row = dict(serializer.validated_data)
            row.pop("brand", None)
            ladder_rows.append(row)

        target_rows = []
        for i, raw in enumerate(targets_in):
            data = {**raw, "brand": brand_code}
            serializer = SellThroughTargetSerializer(data=data)
            if not serializer.is_valid():
                return _bad_request(f"targets[{i}]: {first_message(serializer.errors)}")
            row = dict(serializer.validated_data)
            row.pop("brand", None)
            target_rows.append(row)

        EossLadderStep.objects.bulk_create(
            [EossLadderStep(brand=brand, **row) for row in ladder_rows]
        )
        SellThroughTarget.objects.bulk_create(
            [SellThroughTarget(brand=brand, **row) for row in target_rows]
        )
        ladder = EossLadderStep.objects.filter(brand=brand).select_related("brand")
        targets = SellThroughTarget.objects.filter(brand=brand).select_related("brand")
        return Response(
            {
                "ladder": EossLadderStepSerializer(ladder, many=True).data,
                "targets": SellThroughTargetSerializer(targets, many=True).data,
            }
        )
