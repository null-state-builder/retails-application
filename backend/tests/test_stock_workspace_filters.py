"""Store and barcode filters narrow retained availability without new authority."""
from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from core.tenancy import tenant_context
from stockledger.models import StockOnHand
from stockledger.views import StockAvailabilityView
from tests.first_store_goods import live_access
from tests.test_so03_denials import TenantWorld, _assign, _person, worlds as worlds


@pytest.mark.parametrize("filters,expected", [
    ({"store": "a-s1"}, 3),
    ({"store": "a-s2"}, 5),
    ({"store": "unknown"}, 0),
    ({"store": "a-s1", "sku": "no-such-barcode"}, 0),
])
def test_retained_availability_honours_exact_stock_workspace_filters(
    worlds: tuple[TenantWorld, TenantWorld], filters: dict[str, str], expected: int,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "stock-workspace-reader")
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        access = live_access(user)
        for store, quantity in zip(world.sites, (3, 5), strict=True):
            StockOnHand.objects.create(store=store, brand_ref=world.brands[0], brand=world.brands[0].name,
                                       sku_code="WORKSPACE", item="Reviewed shirt", design="Same design",
                                       net_qty=quantity, net_value_paise=quantity * 12345)
        request = APIRequestFactory().get("/api/stock/availability", {"q": "WORKSPACE", **filters})
        force_authenticate(request, user, access.session)
        response: Any = StockAvailabilityView.as_view()(request)
        assert response.status_code == 200, response.data
        quantity = sum(row["qty"] for design in response.data["results"] for size in design["sizes"] for row in size["stores"])
        assert quantity == expected
        assert not any(word in str(response.data) for word in ("value_paise", "margin", "cost", "mrp"))
