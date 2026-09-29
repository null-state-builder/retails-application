from __future__ import annotations

from typing import Any

from rest_framework import serializers

from accounts.actions import TENANT_MASTER_READ_ACTIONS
from accounts.principal import AccessContext, effective_grants
from accounts.role_assignments import effective_assignments
from accounts.sections import meets
from accounts.unified_policy import role_capability
from core.tenancy import current_tenant_id, require_tenant_id
from masters.models import Brand, Season
from vendors.models import Booking, BookingLine, Vendor

def shows_booking_cost(
    user: Any, cells: list[tuple[int | None, int]] | None = None, *, minimum: str = "view"
) -> bool:
    """Cost needs one qualifying assignment for every site/brand cell.

    Before a draft identifies its brand and destinations, only an explicit
    all-sites/all-brands assignment may see the extracted cost.
    """
    if (
        user is None or not getattr(user, "is_authenticated", False)
        or getattr(user, "tenant_id", None) != require_tenant_id()
        or not getattr(user, "human_id", None)
    ):
        return False
    assignments = effective_assignments(user.human_id)
    roles = {row.pk: row.role for row in assignments}
    access = AccessContext(
        user=user, human_id=user.human_id, tenant_id=user.tenant_id,
        session=None, grants=effective_grants(user.human_id),
    )
    qualifying = [
        grant for grant in access.grants
        if "cost" in grant.fields
        and (role := roles.get(grant.id)) is not None
        and meets(role_capability(role, "booking"), minimum)
    ]
    if cells is None:
        return any(grant.all_sites and grant.all_brands for grant in qualifying)
    return bool(cells) and all(
        any(access.grant_covers(grant, site_id, brand_id) for grant in qualifying)
        for site_id, brand_id in cells
    )


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
    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        # `many=True` wraps the PK field. Narrow its lookup before DRF resolves
        # an input ID, so foreign and nonexistent brands get the same refusal.
        request = self.context.get("request")
        actor_tenant = getattr(getattr(request, "user", None), "tenant_id", None)
        tenant_id = current_tenant_id()
        if tenant_id != actor_tenant:
            tenant_id = None
        brand_field = fields["brands"]
        assert isinstance(brand_field, serializers.ManyRelatedField)
        child = brand_field.child_relation
        assert isinstance(child, serializers.PrimaryKeyRelatedField)
        child.queryset = Brand.objects.filter(tenant_id=tenant_id) if tenant_id else Brand.objects.none()
        return fields

    def validate_code(self, value: str) -> str:
        clash = Vendor.objects.filter(tenant_id=require_tenant_id(), code=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("Another vendor already uses that code.")
        return value

    def validate_gstin(self, value: str) -> str:
        """A nonblank GSTIN identifies at most one vendor in the tenant."""
        if value:
            clash = Vendor.objects.filter(tenant_id=require_tenant_id(), gstin=value)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError("Another vendor already uses that GSTIN.")
        return value

    def get_brand_names(self, obj: Vendor) -> list[str]:
        return [b.name for b in self._readable_brands(obj)]

    def _readable_brands(self, obj: Vendor) -> list[Brand]:
        access = self.context.get("vendor_access")
        if not isinstance(access, AccessContext):
            return []
        return [
            brand for brand in obj.brands.all()
            if brand.tenant_id == access.tenant_id
            and access.can_reach_brand(TENANT_MASTER_READ_ACTIONS, brand.pk)
        ]

    def to_representation(self, instance: Vendor) -> dict[str, Any]:
        data = super().to_representation(instance)
        data["brands"] = [brand.pk for brand in self._readable_brands(instance)]
        return data


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
        cells = [
            (line.store_id or instance.destination_store_id, instance.brand_id)
            for line in instance.lines.all()
        ]
        if not shows_booking_cost(getattr(request, "user", None), cells):
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
