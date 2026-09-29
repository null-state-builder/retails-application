"""Serializers for the EOSS planning screen (recommendations + ladder config)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from masters.models import Brand
from offers.models import EossLadderStep, EossRecommendation, SellThroughTarget


class EossRecommendationSerializer(serializers.ModelSerializer[EossRecommendation]):
    brand_name = serializers.CharField(source="brand.name", read_only=True, default="")
    brand_code = serializers.CharField(source="brand.code", read_only=True, default="")
    season_code = serializers.CharField(source="season.code", read_only=True)
    decided_by_name = serializers.CharField(
        source="decided_by.get_full_name", read_only=True, default=""
    )

    class Meta:
        model = EossRecommendation
        fields = [
            "id",
            "season_code",
            "brand_name",
            "brand_code",
            "design",
            "color",
            "item",
            "weeks_in_stock",
            "on_hand_qty",
            "sold_qty",
            "sell_through_pct",
            "target_pct",
            "gap_pct",
            "broken_size_run",
            "margin_floor_pct",
            "recommended_discount_pct",
            "reason",
            "status",
            "decided_discount_pct",
            "decided_by_name",
            "decided_at",
            "offer",
            "updated_at",
        ]
        read_only_fields = fields


class EossLadderStepSerializer(serializers.ModelSerializer[EossLadderStep]):
    brand = serializers.SlugRelatedField(
        slug_field="code", queryset=Brand.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = EossLadderStep
        fields = ["id", "brand", "step_no", "trigger_type", "trigger_value", "discount_pct"]

    def validate_discount_pct(self, value: Any) -> Any:
        if not (0 < value <= 90):
            raise serializers.ValidationError("A markdown step must be above 0% and at most 90%.")
        return value


class SellThroughTargetSerializer(serializers.ModelSerializer[SellThroughTarget]):
    brand = serializers.SlugRelatedField(
        slug_field="code", queryset=Brand.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = SellThroughTarget
        fields = ["id", "brand", "week_number", "target_pct"]

    def validate_target_pct(self, value: Any) -> Any:
        if not (0 <= value <= 100):
            raise serializers.ValidationError("A sell-through target is a percent, 0-100.")
        return value
