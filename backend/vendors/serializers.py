from __future__ import annotations

from typing import Any

from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from accounts.role_lists import declare_role_list
from masters.models import Brand, Season
from vendors.models import Booking, BookingLine, Vendor

#: Legacy roles that never see what KDPS pays a vendor. A booking's cost per
#: piece is indicative buying data, the same kind the goods-v1 grants keep from
#: store, cashier and warehouse roles (their templates carry no ``cost`` field).
COST_BLIND_ROLES = declare_role_list(
    "vendors.booking_cost_blind_roles",
    ("store_person", "warehouse"),
    reason=(
        "A booking's cost per piece is what KDPS pays a vendor. The section ladder "
        "says which screens a role reaches, not which fields on a shared screen it "
        "may read, so the store and warehouse seats that open Bookings are named "
        "here to keep the cost column from them."
    ),
)


def shows_booking_cost(user: Any) -> bool:
    """Whether ``user`` may read a booking's cost. An unknown caller, or a login
    with no role, sees none."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    role = getattr(user, "role", None)
    return role is not None and role.code not in COST_BLIND_ROLES


class VendorSerializer(serializers.ModelSerializer[Vendor]):
    # Declared because the link is an explicit tenant-carrying model now, which DRF
    # would otherwise make read-only; the queryset reads only this tenant's brands.
    brands = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Brand.objects.all(), required=False
    )
    brand_names = serializers.SerializerMethodField()

    class Meta:
        model = Vendor
        fields = [
            "id",
            "code",
            "name",
            "city",
            "gstin",
            "state_code",
            "state_name",
            "pan",
            "payment_terms",
            "brands",
            "brand_names",
            "is_active",
        ]
        extra_kwargs = {"code": {"validators": [UniqueValidator(queryset=Vendor.objects.all())]}}

    def validate_gstin(self, value: str) -> str:
        """A nonblank GSTIN identifies at most one vendor in the tenant."""
        if value:
            clash = Vendor.objects.filter(gstin=value)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError("Another vendor already uses that GSTIN.")
        return value

    def get_brand_names(self, obj: Vendor) -> list[str]:
        return [b.name for b in obj.brands.all()]


class BookingLineSerializer(serializers.ModelSerializer[BookingLine]):
    store_name = serializers.CharField(source="store.name", read_only=True, default=None)

    class Meta:
        model = BookingLine
        fields = [
            "id",
            "style_code",
            "size",
            "description",
            "booked_qty",
            "mrp_paise",
            "cost_paise",
            "received_qty",
            "inwarded_qty",
            "store",
            "store_name",
        ]


class BookingSerializer(serializers.ModelSerializer[Booking]):
    lines = BookingLineSerializer(many=True, read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    brand_name = serializers.CharField(source="brand.name", read_only=True)
    season_name = serializers.CharField(source="season.name", read_only=True)
    season_code = serializers.CharField(source="season.code", read_only=True)
    destination_store_name = serializers.CharField(
        source="destination_store.name", read_only=True, default=None
    )
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    booked_total = serializers.SerializerMethodField()
    received_total = serializers.SerializerMethodField()
    closed_by_name = serializers.CharField(
        source="closed_by.full_name", read_only=True, default=None
    )
    open_qty = serializers.IntegerField(read_only=True)
    cost_total_paise = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id",
            "number",
            "vendor",
            "vendor_name",
            "brand",
            "brand_name",
            "season",
            "season_name",
            "season_code",
            "destination_store",
            "destination_store_name",
            "status",
            "status_label",
            "vendor_ref",
            "ownership",
            "return_terms",
            "estimated_value_paise",
            "cost_total_paise",
            "notes",
            "lines",
            "booked_total",
            "received_total",
            "open_qty",
            "close_reason",
            "closed_at",
            "closed_by_name",
            "created_at",
        ]

    def get_booked_total(self, obj: Booking) -> int:
        return sum(line.booked_qty for line in obj.lines.all())

    def get_received_total(self, obj: Booking) -> int:
        return sum(line.received_qty for line in obj.lines.all())

    def get_cost_total_paise(self, obj: Booking) -> int | None:
        """Quantity times cost over the lines that carry a cost; none carries one → null."""
        costed = [line for line in obj.lines.all() if line.cost_paise is not None]
        if not costed:
            return None
        return sum(line.booked_qty * (line.cost_paise or 0) for line in costed)

    def to_representation(self, instance: Booking) -> dict[str, Any]:
        data = super().to_representation(instance)
        request = self.context.get("request")
        # Omitted, never nulled: a missing key says "not yours to see", a null
        # says "nobody gave one". Without a request nobody is known, so no cost.
        if not shows_booking_cost(getattr(request, "user", None)):
            data.pop("cost_total_paise", None)
            for line in data.get("lines") or []:
                line.pop("cost_paise", None)
        return data


class BookingCreateSerializer(serializers.Serializer[dict[str, Any]]):
    vendor = serializers.PrimaryKeyRelatedField(queryset=Vendor.objects.all())
    brand = serializers.PrimaryKeyRelatedField(queryset=Brand.objects.all())
    season = serializers.PrimaryKeyRelatedField(queryset=Season.objects.all())
    destination_store = serializers.IntegerField(required=False, allow_null=True)
    vendor_ref = serializers.CharField(required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    source_file_id = serializers.IntegerField(required=False, allow_null=True)
    lines = serializers.ListField(child=serializers.DictField())
