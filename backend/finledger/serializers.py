from __future__ import annotations

from rest_framework import serializers

from core.money import paise_to_rupees_str
from finledger.models import CashLedgerEntry, VendorLedgerEntry


class VendorLedgerEntrySerializer(serializers.ModelSerializer[VendorLedgerEntry]):
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    vendor_code = serializers.CharField(source="vendor.code", read_only=True)
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    amount_rupees = serializers.SerializerMethodField()

    class Meta:
        model = VendorLedgerEntry
        fields = [
            "id",
            "created_at",
            "doc_number",
            "kind",
            "kind_label",
            "vendor",
            "vendor_name",
            "vendor_code",
            "amount",
            "amount_rupees",
            "description",
            "reference",
            "mode",
            "booking",
        ]

    def get_amount_rupees(self, obj: VendorLedgerEntry) -> str:
        return paise_to_rupees_str(obj.amount)


class CashLedgerEntrySerializer(serializers.ModelSerializer[CashLedgerEntry]):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True, default="")
    amount_rupees = serializers.SerializerMethodField()

    class Meta:
        model = CashLedgerEntry
        fields = [
            "id",
            "created_at",
            "doc_number",
            "kind",
            "kind_label",
            "account",
            "amount",
            "amount_rupees",
            "description",
            "mode",
            "vendor",
            "vendor_name",
        ]

    def get_amount_rupees(self, obj: CashLedgerEntry) -> str:
        return paise_to_rupees_str(obj.amount)
