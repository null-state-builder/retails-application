"""Booking visibility from the same indivisible role assignments as commands.

A booking can span destinations. Reading any one of its lines admits the
booking header, while protected totals still require every line's scope cell.
The brand and vendor tenant links are required even for an all-scope role.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Exists, Model, OuterRef, Q, QuerySet
from rest_framework.exceptions import PermissionDenied

from accounts.principal import access_for_user
from core.tenancy import require_tenant_id
from masters.models import Brand, Store
from masters.unit_context import active_brand_name, active_unit_code
from vendors.models import BookingLine


def booking_at_stores(store_ids: list[int]) -> Q:
    """A line's store, or the booking destination when the line has none."""
    return Q(lines__store_id__in=store_ids) | Q(
        lines__store__isnull=True, destination_store_id__in=store_ids
    )


def scope_bookings[M: Model](qs: QuerySet[M], user: Any, *, minimum: str | None = None) -> QuerySet[M]:
    """Return only tenant bookings reached by a qualifying assignment tuple."""
    tenant_id = require_tenant_id()
    qs = qs.filter(brand__tenant_id=tenant_id, vendor__tenant_id=tenant_id)
    if (
        not getattr(user, "is_authenticated", False)
        or getattr(user, "tenant_id", None) != tenant_id
        or not getattr(user, "human_id", None)
    ):
        return qs.none()
    if minimum is None:
        return qs.none()
    assignments = access_for_user(user).section_grants("booking", minimum)
    condition = Q(pk__in=[])
    tenant_brand_ids = Brand.objects.filter(tenant_id=tenant_id).values_list("pk", flat=True)
    for brand_id in tenant_brand_ids:
        covering = [
            row for row in assignments
            if row.all_brands or brand_id in row.brand_ids
        ]
        if not covering:
            continue
        if any(row.all_sites for row in covering):
            condition |= Q(brand_id=brand_id)
            continue
        site_ids = sorted({site_id for row in covering for site_id in row.site_ids})
        if not site_ids:
            continue
        lines = BookingLine.objects.filter(booking_id=OuterRef("pk"))
        outside = lines.exclude(
            Q(store_id__in=site_ids)
            | Q(store__isnull=True, booking__destination_store_id__in=site_ids)
        )
        covered = (
            qs.filter(brand_id=brand_id)
            .annotate(_has_line=Exists(lines), _outside_line=Exists(outside))
            .filter(_outside_line=False)
            .filter(Q(_has_line=True) | Q(destination_store_id__in=site_ids))
            .values("pk")
        )
        condition |= Q(pk__in=covered)
    qs = qs.filter(condition)

    selected_site = active_unit_code()
    if selected_site:
        stores = Store.objects.filter(tenant_id=tenant_id)
        site = stores.filter(pk=int(selected_site)).first() if selected_site.isdecimal() else None
        if site is None:
            raise PermissionDenied("You may not work in this business unit.")
        qs = qs.filter(booking_at_stores([site.pk]))
    selected_brand = active_brand_name()
    if selected_brand:
        brands = Brand.objects.filter(tenant_id=tenant_id)
        brand = brands.filter(pk=int(selected_brand)).first() if selected_brand.isdecimal() else None
        if brand is None:
            raise PermissionDenied("You may not work in this brand.")
        qs = qs.filter(brand_id=brand.pk)
    return qs.distinct()
