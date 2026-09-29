"""Petty cash read and write shapes (store operations ticket 42, ST-MNY-3)."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from approvals.names import display_name


class PettyCashFloatWriteSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField(help_text="The screen's own id for this change; a retry reuses it.")
    site_id = serializers.IntegerField()
    float_paise = serializers.IntegerField(min_value=1)
    custodian = serializers.IntegerField(help_text="The user who holds the box.")
    expected_revision = serializers.IntegerField(
        min_value=0, help_text="The float's revision the screen showed; 0 when none is set."
    )


class PettyCashTopUpWriteSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField(help_text="The screen's own id for this top-up; a retry reuses it.")
    site_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    origin = serializers.CharField(help_text="Where the cash came from: till or head_office.")
    amount_paise = serializers.IntegerField()
    given_by_name = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=120,
        help_text="Head office only: who brought the cash.",
    )
    reference = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=64,
        help_text="Head office only: the voucher or receipt number.",
    )


class PettyCashSpendWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """Sent as a multipart form, with the bill photo as ``bill`` when there is one."""

    id = serializers.UUIDField(help_text="The screen's own id for this spend; a retry reuses it.")
    site_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    head = serializers.CharField(max_length=60)
    amount_paise = serializers.IntegerField()
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)
    bill = serializers.FileField(
        required=False, allow_null=True, default=None, help_text="The bill photo: JPEG, PNG or PDF."
    )


class PettyCashBillWriteSerializer(serializers.Serializer[dict[str, Any]]):
    bill = serializers.FileField(help_text="The bill photo: JPEG, PNG or PDF.")


class PettyCashPersonSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()

    def get_name(self, user: Any) -> str:
        return display_name(user)


class PettyCashFloatReadSerializer(serializers.Serializer[Any]):
    float_paise = serializers.IntegerField()
    custodian = serializers.IntegerField(source="custodian_id")
    custodian_name = serializers.SerializerMethodField()
    revision = serializers.IntegerField()
    set_at = serializers.DateTimeField()

    def get_custodian_name(self, row: Any) -> str:
        return display_name(row.custodian)


class PettyCashTopUpReadSerializer(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    origin = serializers.CharField(source="source", help_text="till or head_office.")
    amount_paise = serializers.IntegerField()
    custodian_name = serializers.SerializerMethodField()
    recorded_by_name = serializers.SerializerMethodField()
    given_by_name = serializers.CharField()
    reference = serializers.CharField()
    recorded_at = serializers.DateTimeField()

    def get_custodian_name(self, row: Any) -> str:
        return display_name(row.custodian)

    def get_recorded_by_name(self, row: Any) -> str:
        return display_name(row.recorded_by)


class PettyCashSpendReadSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    client_id = serializers.UUIDField()
    store = serializers.CharField(source="store.code")
    site_id = serializers.IntegerField(source="store_id")
    head = serializers.CharField()
    amount_paise = serializers.IntegerField()
    note = serializers.CharField()
    status = serializers.CharField(help_text="spent, waiting, approved or rejected.")
    counts_from = serializers.DateTimeField(allow_null=True)
    custodian_name = serializers.SerializerMethodField()
    recorded_by_name = serializers.SerializerMethodField()
    recorded_at = serializers.DateTimeField()
    decided_by_name = serializers.SerializerMethodField()
    decided_at = serializers.DateTimeField(allow_null=True)
    reject_reason = serializers.CharField()
    has_bill = serializers.BooleanField()
    bill_media_type = serializers.CharField()
    bill_url = serializers.SerializerMethodField()

    def get_custodian_name(self, row: Any) -> str:
        return display_name(row.custodian)

    def get_recorded_by_name(self, row: Any) -> str:
        return display_name(row.recorded_by)

    def get_decided_by_name(self, row: Any) -> str:
        return display_name(row.decided_by) if row.decided_by_id else ""

    def get_bill_url(self, row: Any) -> str | None:
        return f"/api/sell/petty-cash/spends/{row.pk}/bill" if row.has_bill else None


class PettyCashPositionSerializer(serializers.Serializer[Any]):
    """The store's box: float, custodian, balance, and the latest spends and top-ups."""

    site_id = serializers.IntegerField(source="store.pk")
    store = serializers.CharField(source="store.code")
    store_name = serializers.CharField(source="store.name")
    switched_on = serializers.BooleanField(
        help_text="Off: what is saved can be read, nothing new can be recorded."
    )
    float = PettyCashFloatReadSerializer(source="float_row", allow_null=True)
    balance_paise = serializers.IntegerField(help_text="What the box holds now.")
    waiting_paise = serializers.IntegerField(help_text="Spends waiting for the Owner.")
    approval_above_paise = serializers.IntegerField()
    heads = serializers.ListField(child=serializers.CharField())
    missing_bills = serializers.IntegerField()
    may_set_float = serializers.BooleanField()
    custodians = PettyCashPersonSerializer(many=True)
    spends = PettyCashSpendReadSerializer(many=True)
    top_ups = PettyCashTopUpReadSerializer(many=True)
