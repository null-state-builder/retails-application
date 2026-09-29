from __future__ import annotations

from django.contrib import admin

from masters.models import Brand, CategoryMargin, Gstin, LegalEntity, Season, Store

# `admin.ModelAdmin` is generic to django-stubs but, unlike `core.money.MoneyField`,
# is not made subscriptable at runtime here (that trick is deliberately local to
# money columns - see the comment on `_MoneyBase` in `core/money.py`), so
# `ModelAdmin[Model]` would raise `TypeError: type 'ModelAdmin' is not
# subscriptable` the moment Django's admin autodiscovery imports this module.
# `# type: ignore[type-arg]` is the narrowest fix that doesn't crash the app.


@admin.register(LegalEntity)
class LegalEntityAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ["name", "code", "pan", "is_active"]
    search_fields = ["name", "code", "pan"]


@admin.register(Gstin)
class GstinAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ["gstin", "state_name", "state_code", "legal_entity", "is_active"]
    list_filter = ["state_name", "is_active"]
    search_fields = ["gstin", "state_name"]


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ["code", "name", "store_type", "gstin", "city", "is_active"]
    list_filter = ["store_type", "gstin__state_name", "is_active"]
    search_fields = ["code", "name", "city"]


@admin.register(Season)
class SeasonAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ["code", "name", "status", "sort_order", "ended_on"]
    list_filter = ["status"]
    #: Written only by the audited season-end command (store operations ticket 33).
    readonly_fields = ["ended_on"]


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ["name", "code", "ownership", "return_terms", "commercial_label", "is_active"]
    list_filter = ["ownership", "return_terms", "is_active"]
    search_fields = ["name", "code"]


@admin.register(CategoryMargin)
class CategoryMarginAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ["item", "margin_pct", "band_min_pct", "band_max_pct", "is_active"]
    search_fields = ["item"]
