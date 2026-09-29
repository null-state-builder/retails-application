"""Explicit section-and-resource adapters backed by the unified evaluator.

Every caller supplies its section and rung. Header choices only narrow queries;
neither a request stamp nor brand display text establishes permission.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

from accounts.principal import access_for_user
from core.tenancy import require_tenant_id
from masters.brand_identity import brand_predicate
from masters.models import Brand, Store
from masters.unit_context import active_brand_name, active_unit_code

BRAND_SCOPE = "brand"  # Historical display vocabulary only.


def is_brand_scoped(user: Any) -> bool:
    rows = access_for_user(user).grants
    return bool(rows) and all(row.all_sites and not row.all_brands for row in rows)


def _assignments(user: Any, section: str | None, minimum: str) -> list[Any]:
    return access_for_user(user).section_grants(section, minimum) if section else []


def visible_store_ids(user: Any, *, section: str | None = None, minimum: str = "view") -> list[int] | None:
    rows = [row for row in _assignments(user, section, minimum) if row.all_brands]
    if any(row.all_sites for row in rows):
        return None
    return sorted({int(site) for row in rows for site in row.site_ids})


def actionable_store_ids(user: Any, *, section: str | None = None, minimum: str = "view") -> list[int] | None:
    return visible_store_ids(user, section=section, minimum=minimum)


def actionable_stores(user: Any, *, section: str | None = None, minimum: str = "view") -> Any:
    return scoped_stores(user, section=section, minimum=minimum)


def scoped_stores(user: Any, *, section: str | None = None, minimum: str = "view") -> Any:
    rows = Store.objects.filter(tenant_id=require_tenant_id(), is_active=True).order_by("code")
    ids = visible_store_ids(user, section=section, minimum=minimum)
    return rows if ids is None else rows.filter(pk__in=ids)


def visible_brand_ids(user: Any, *, section: str | None = None, minimum: str = "view") -> list[int] | None:
    rows = _assignments(user, section, minimum)
    if any(row.all_brands for row in rows):
        return None
    return sorted({int(brand) for row in rows for brand in row.brand_ids})


def scoped_brands(user: Any, *, section: str | None = None, minimum: str = "view") -> Any:
    rows = Brand.objects.filter(tenant_id=require_tenant_id(), is_active=True)
    ids = visible_brand_ids(user, section=section, minimum=minimum)
    return rows if ids is None else rows.filter(pk__in=ids)


def _selected_site() -> int | None:
    chosen = active_unit_code()
    if not chosen:
        return None
    if not chosen.isdecimal():
        raise PermissionDenied("Choose a business unit by its stable ID.")
    site_id = Store.objects.filter(tenant_id=require_tenant_id(), pk=int(chosen), is_active=True).values_list("pk", flat=True).first()
    if site_id is None:
        raise PermissionDenied("You may not work in this business unit.")
    return int(site_id)


def _selected_brand() -> int | None:
    chosen = active_brand_name()
    if not chosen:
        return None
    if not chosen.isdecimal():
        raise PermissionDenied("Choose a brand by its stable ID.")
    brand_id = Brand.objects.filter(tenant_id=require_tenant_id(), pk=int(chosen), is_active=True).values_list("pk", flat=True).first()
    if brand_id is None:
        raise PermissionDenied("You may not work in this brand.")
    return int(brand_id)


def active_store_ids(user: Any, *, section: str | None = None, minimum: str = "view") -> list[int] | None:
    ids = visible_store_ids(user, section=section, minimum=minimum)
    selected = _selected_site()
    if selected is None:
        return ids
    if ids is not None and selected not in ids:
        raise PermissionDenied("You may not work in this business unit.")
    return [selected]


def active_brand_ids(user: Any, *, section: str | None = None, minimum: str = "view") -> list[int] | None:
    ids = visible_brand_ids(user, section=section, minimum=minimum)
    selected = _selected_brand()
    if selected is None:
        return ids
    if ids is not None and selected not in ids:
        raise PermissionDenied("You may not work in this brand.")
    return [selected]


def _store_ids(user: Any, section: str | None, minimum: str, *, context: bool) -> list[int]:
    ids = active_store_ids(user, section=section, minimum=minimum) if context else visible_store_ids(user, section=section, minimum=minimum)
    tenant_sites = Store.objects.filter(tenant_id=require_tenant_id())
    if ids is not None:
        tenant_sites = tenant_sites.filter(pk__in=ids)
    return list(tenant_sites.values_list("pk", flat=True))


def scope_by_store(qs: Any, user: Any, field: str = "store_id", *, section: str | None = None, minimum: str = "view") -> Any:
    return qs.filter(**{f"{field}__in": _store_ids(user, section, minimum, context=True)})


def scope_by_store_predicate(qs: Any, user: Any, predicate: Callable[[list[int]], Q], *, section: str | None = None, minimum: str = "view") -> Any:
    return qs.filter(predicate(_store_ids(user, section, minimum, context=True)))


def scope_by_store_many(user: Any, *targets: tuple[Any, str], section: str | None = None, minimum: str = "view") -> list[Any]:
    ids = _store_ids(user, section, minimum, context=True)
    return [qs.filter(**{f"{field}__in": ids}) for qs, field in targets]


def scope_by_entitlement(qs: Any, user: Any, field: str = "store_id", *, section: str | None = None, minimum: str = "view") -> Any:
    return qs.filter(**{f"{field}__in": _store_ids(user, section, minimum, context=False)})


def scope_by_store_and_brand(
    qs: Any, user: Any, store_field: str = "store_id", brand_field: str = "brand",
    *, section: str | None = None, minimum: str = "view", context: bool = True, brandless: Q | None = None,
) -> Any:
    rows = _assignments(user, section, minimum)
    if not rows:
        return qs.none()
    tenant_id = require_tenant_id()
    qs, brand_id_field = brand_predicate(qs, brand_field, tenant_id, brandless=brandless)
    qs = qs.filter(**{f"{store_field.removesuffix('_id')}__tenant_id": tenant_id})
    allowed = Q(pk__in=[])
    for row in rows:
        if row.all_sites and row.all_brands:
            allowed = Q()
            break
        cell = Q()
        if not row.all_sites:
            cell &= Q(**{f"{store_field}__in": row.site_ids})
        if not row.all_brands:
            cell &= Q(**{f"{brand_id_field}__in": row.brand_ids})
        allowed |= cell
    qs = qs.filter(allowed)
    site = _selected_site() if context else None
    brand = _selected_brand() if context else None
    if site is not None:
        qs = qs.filter(**{store_field: site})
    if brand is not None:
        qs = qs.filter(**{brand_id_field: brand})
    return qs


def scope_by_store_or_brand(qs: Any, user: Any, store_field: str = "store_id", brand_field: str = "brand", *, section: str | None = None, minimum: str = "view", brandless: Q | None = None) -> Any:
    return scope_by_store_and_brand(qs, user, store_field, brand_field, section=section, minimum=minimum, brandless=brandless)


def scope_by_entitlement_or_brand(qs: Any, user: Any, store_field: str = "store_id", brand_field: str = "brand", *, section: str | None = None, minimum: str = "view") -> Any:
    return scope_by_store_and_brand(qs, user, store_field, brand_field, section=section, minimum=minimum, context=False)


def scope_by_entitled_brands(qs: Any, user: Any, brand_field: str = "brand", *, section: str | None = None, minimum: str = "view") -> Any:
    # Availability respects both assignment axes. A site-limited role cannot
    # turn an availability lookup into a chain-wide stock query.
    return scope_by_store_and_brand(qs, user, brand_field=brand_field, section=section, minimum=minimum, context=False)


def scope_by_brand(qs: Any, user: Any, field: str = "brand", *, section: str | None = None, minimum: str = "view") -> Any:
    return scope_by_store_and_brand(qs, user, brand_field=field, section=section, minimum=minimum)
