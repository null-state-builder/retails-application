from __future__ import annotations
from typing import Any

from rest_framework import serializers

from core.money import paise_to_rupees_str
from stockledger.models import StockLedgerEntry
from stockledger.access import project_entry


class StockLedgerEntrySerializer(serializers.ModelSerializer[StockLedgerEntry]):
    def to_representation(self, instance: StockLedgerEntry) -> dict[str, Any]:
        return project_entry(self.context.get("request"), instance, super().to_representation(instance))

    store_code = serializers.CharField(source="store.code", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    value_rupees = serializers.SerializerMethodField()
    booking_number = serializers.CharField(source="booking.number", read_only=True, default="")

    class Meta:
        model = StockLedgerEntry
        fields = [
            "id",
            "created_at",
            "doc_number",
            "kind",
            "kind_label",
            "store_code",
            "store_name",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "qty",
            "amount",
            "value_rupees",
            "line_no",
            "booking",
            "booking_number",
        ]

    def get_value_rupees(self, obj: StockLedgerEntry) -> str:
        return paise_to_rupees_str(obj.amount)
