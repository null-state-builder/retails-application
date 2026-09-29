"""The EOSS planning API: recommendations to review, and the ladder that
proposes them (mounted under `/api/offers/eoss/`).

Reads answer at `view`; regenerating the plan and deciding a recommendation
both need `approve` (the human-in-the-loop gate the research calls the
industry norm); the ladder/target curve itself needs `manage`, the same rung
`offers.views` asks before anybody may author a rule.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework import serializers
from rest_framework.serializers import Serializer
from rest_framework.views import APIView

from accounts.principal import resolve_access
from accounts.permissions import require_section
from accounts.sections import CAP_APPROVE, CAP_MANAGE, CAP_VIEW
from core.refusals import Refusal, first_message, refusal_body
from masters.models import Brand
from offers.eoss_serializers import (
    EossLadderStepSerializer,
    EossRecommendationSerializer,
    SellThroughTargetSerializer,
)
from offers.models import EossLadderStep, EossRecommendation, SellThroughTarget

CanReadOrApprove = require_section("offers_price", CAP_VIEW, write_minimum=CAP_APPROVE)
CanManageConfig = require_section("offers_price", CAP_VIEW, write_minimum=CAP_MANAGE)


class EossPlanRequestSerializer(serializers.Serializer[Any]):
    season = serializers.CharField()


class EossPlanResponseSerializer(serializers.Serializer[Any]):
    generated = serializers.IntegerField(read_only=True)
    recommendations = EossRecommendationSerializer(many=True, read_only=True)


class EossDecisionRequestSerializer(serializers.Serializer[Any]):
    action = serializers.ChoiceField(choices=("approve", "reject"))
    discount_pct = serializers.FloatField(required=False, min_value=0, max_value=90)


class EossConfigRequestSerializer(serializers.Serializer[Any]):
    brand = serializers.CharField(required=False)
    ladder = EossLadderStepSerializer(many=True)
    targets = SellThroughTargetSerializer(many=True)


class EossConfigResponseSerializer(serializers.Serializer[Any]):
    ladder = EossLadderStepSerializer(many=True, read_only=True)
    targets = SellThroughTargetSerializer(many=True, read_only=True)


REFUSAL_RESPONSE = {
    "type": "object", "required": ["error", "code"],
    "properties": {"error": {"type": "string"}, "code": {"type": "string"}},
}


def _bad_request(message: str) -> Response:
    return Response(refusal_body("VALIDATION", message), status=400)


class EossRecommendationListView(APIView):
    """`GET` the current plan. `POST` recomputes it for a season."""

    permission_classes = [IsAuthenticated, CanReadOrApprove]

    @extend_schema(
        parameters=[
            OpenApiParameter("season", str), OpenApiParameter("brand", str),
            OpenApiParameter("status", str),
        ],
        responses={200: EossRecommendationSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        access = resolve_access(request)
        rows = EossRecommendation.objects.filter(brand__tenant_id=access.tenant_id).select_related("season", "brand")
        season = (request.query_params.get("season") or "").strip()
        if season:
            rows = rows.filter(season__code__iexact=season)
        brand = (request.query_params.get("brand") or "").strip()
        if brand:
            rows = rows.filter(brand__code__iexact=brand)
        status = (request.query_params.get("status") or "").strip()
        if status:
            rows = rows.filter(status=status)
        access = resolve_access(request)
        visible = [row for row in rows if access.can_section(
            "offers_price", "view", brand_id=row.brand_id, fields={"cost", "margin"},
        )]
        return Response(EossRecommendationSerializer(visible, many=True).data)

    @extend_schema(
        request=EossPlanRequestSerializer,
        responses={200: EossPlanResponseSerializer, 400: REFUSAL_RESPONSE,
                   404: REFUSAL_RESPONSE},
    )
    def post(self, request: Request) -> Response:
        raise Refusal("OWNERSHIP_UNRESOLVED", "EOSS generation awaits SO-06 tenant-owned inputs and preparer provenance.", status=409)


class EossRecommendationDecisionView(APIView):
    """Approve (spins up the live `Offer`) or reject one recommendation."""

    permission_classes = [IsAuthenticated, CanReadOrApprove]

    @extend_schema(
        request=EossDecisionRequestSerializer,
        responses={200: EossRecommendationSerializer, 400: REFUSAL_RESPONSE,
                   404: REFUSAL_RESPONSE},
    )
    def post(self, request: Request, pk: int) -> Response:
        raise Refusal("APPROVAL_POLICY_REQUIRED", "This legacy recommendation lacks pinned inputs, policy and maker identity; SO-06 must resubmit it through the governed approval path.", status=409)


class EossConfigView(APIView):
    """`GET`/`PUT` the markdown ladder and the sell-through target curve.

    `PUT` replaces a brand's whole ladder (or the whole default curve) in one
    call - a step numbered out of order left behind by a partial edit is worse
    than asking the screen to send the complete list every time.
    """

    permission_classes = [IsAuthenticated, CanManageConfig]

    @extend_schema(
        parameters=[OpenApiParameter("brand", str)],
        responses={200: EossConfigResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        brand_code = (request.query_params.get("brand") or "").strip()
        brand = _config_brand(request, brand_code, "view")
        ladder = EossLadderStep.objects.filter(brand=brand).select_related("brand")
        targets = SellThroughTarget.objects.filter(brand=brand).select_related("brand")
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

    @extend_schema(
        request=EossConfigRequestSerializer,
        responses={200: EossConfigResponseSerializer, 400: REFUSAL_RESPONSE,
                   404: REFUSAL_RESPONSE},
    )
    @transaction.atomic
    def put(self, request: Request) -> Response:
        brand_code = (request.data.get("brand") or "").strip() or None
        ladder_in = request.data.get("ladder")
        targets_in = request.data.get("targets")
        if not isinstance(ladder_in, list) or not isinstance(targets_in, list):
            return _bad_request("Send whole 'ladder' and 'targets' lists.")

        brand = _config_brand(request, brand_code or "", "manage")

        ladder_rows = []
        for i, raw in enumerate(ladder_in, start=1):
            data = {**raw, "step_no": raw.get("step_no", i)}
            data.pop("brand", None)
            serializer: Serializer[Any] = EossLadderStepSerializer(data=data)
            if not serializer.is_valid():
                return _bad_request(f"ladder[{i - 1}]: {first_message(serializer.errors)}")
            row = dict(serializer.validated_data)
            row.pop("brand", None)
            ladder_rows.append(row)

        target_rows = []
        for i, raw in enumerate(targets_in):
            data = dict(raw)
            data.pop("brand", None)
            serializer = SellThroughTargetSerializer(data=data)
            if not serializer.is_valid():
                return _bad_request(f"targets[{i}]: {first_message(serializer.errors)}")
            row = dict(serializer.validated_data)
            row.pop("brand", None)
            target_rows.append(row)

        access = resolve_access(request)
        with access.guard_legacy_write(lambda current: current.can_section(
            "offers_price", "manage", brand_id=brand.pk,
        )):
            EossLadderStep.objects.filter(brand=brand).delete()
            SellThroughTarget.objects.filter(brand=brand).delete()
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


def _config_brand(request: Request, code: str, level: str) -> Brand:
    if not code:
        raise Refusal("OWNERSHIP_UNRESOLVED", "Shared EOSS defaults await SO-06 tenant configuration.", status=409)
    access = resolve_access(request)
    brand = Brand.objects.filter(tenant_id=access.tenant_id, code=code).first()
    if brand is None or not access.can_section("offers_price", level, brand_id=brand.pk):
        raise Refusal("NOT_FOUND", "That brand configuration was not found.", status=404)
    return brand
