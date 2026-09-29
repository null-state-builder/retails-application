"""Project legacy stock reads through the unified action and field authority."""

from __future__ import annotations

from typing import Any

from accounts.principal import resolve_access
from core.refusals import Refusal
from masters.brand_identity import identity_id

VALUE_FIELDS = frozenset({"amount", "value_paise", "value_rupees", "net_value_paise", "net_value_rupees"})


def can_read_value(request: Any, query: Any) -> bool:
    if request is None:
        return False
    access = resolve_access(request)
    site_fields = ("source_store_id", "destination_store_id") if query.model._meta.model_name == "intransitstock" else ("store_id",)
    cells = query.values_list(*site_fields, "_access_brand_id").distinct()
    return all(brand is not None and all(
        access.can_section("stock", "view", site_id=site, brand_id=brand, fields={"cost"})
        for site in sites
    ) for *sites, brand in cells)


def strip_values(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: strip_values(value) for key, value in data.items() if key not in VALUE_FIELDS}
    if isinstance(data, list):
        return [strip_values(value) for value in data]
    return data


def project_stock(request: Any, query: Any, data: dict[str, Any]) -> dict[str, Any]:
    if can_read_value(request, query):
        return data
    if request is not None and request.query_params.get("basis") in {"cost", "value", "valuation", "margin"}:
        raise Refusal("FIELD_DENIED", "This stock request requires protected financial fields.", status=403)
    return strip_values(data)  # type: ignore[no-any-return]


def project_entry(request: Any, row: Any, data: dict[str, Any]) -> dict[str, Any]:
    if request is not None:
        access = resolve_access(request)
        brand_id = identity_id(row, access.tenant_id)
        if brand_id is not None and access.can_section(
            "stock", "view", site_id=row.store_id, brand_id=brand_id, fields={"cost"},
        ):
            return data
    return strip_values(data)  # type: ignore[no-any-return]
