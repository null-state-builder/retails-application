"""SO-03: global search cannot discover another tenant or exchange scope cells."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_models import HumanIdentity, RoleAssignment, ServerSession
from accounts.models import Role, User
from accounts.rbac_matrix import section_access_for
from accounts.sessions import issue_session
from core.tenancy import tenant_context
from masters.goods_models import Tenant
from masters.models import Brand, Cohort, Gstin, LegalEntity, Season, Sku, Store
from outbound.models import StoreTransfer, StoreTransferLine
from search.views import GlobalSearchView
from stockledger.models import StockOnHand
from vendors.models import Booking, Vendor


@dataclass(frozen=True)
class SearchWorld:
    tenant: Tenant
    user: User
    session: ServerSession
    role: Role
    human: HumanIdentity
    stores: tuple[Store, Store]
    brands: tuple[Brand, Brand]
    vendor: Vendor
    season: Season


def _world(label: str) -> SearchWorld:
    tenant = Tenant.objects.create(
        code=f"search-{label}-{uuid.uuid4().hex[:6]}",
        name=f"Search {label}", deployment_key=uuid.uuid4(),
        timezone="Asia/Kolkata", currency="INR", locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        entity = LegalEntity.objects.create(tenant=tenant, code=f"entity-{label}", name=label)
        gstin = Gstin.objects.create(
            tenant=tenant, legal_entity=entity,
            gstin=f"10{uuid.uuid4().int % 10**13:013d}", state_code="10", state_name="Bihar",
        )
        stores = tuple(
            Store.objects.create(
                tenant=tenant, gstin=gstin, code=f"{label}-s{index}", name=f"{label} Store {index}",
            )
            for index in (1, 2)
        )
        brands = tuple(
            Brand.objects.create(
                tenant=tenant, code=f"find-{label}-{index}", name=f"Find {label} Brand {index}",
            )
            for index in (1, 2)
        )
        vendor = Vendor.objects.create(tenant=tenant, code=f"vendor-{label}", name=label)
        season = Season.objects.create(code=f"{label}-{uuid.uuid4().hex[:6]}", name=label)
        role = Role.objects.create(
            tenant=tenant, code="owner", name="Owner", section_access=section_access_for("owner"),
        )
        human = HumanIdentity.objects.create(
            tenant=tenant, staff_code=f"search-{label}", display_name=f"Search {label}",
        )
        user = User.objects.create(
            username=f"search-{label}-{uuid.uuid4().hex[:6]}", tenant=tenant, human=human,
        )
        RoleAssignment.objects.create(
            tenant=tenant, human=human, role=role, all_sites=True, all_brands=True,
            effective_from=timezone.now() - timedelta(days=1),
        )
        session = cast(ServerSession, issue_session(user).session)
    return SearchWorld(tenant, user, session, role, human, cast(tuple[Store, Store], stores),
                       cast(tuple[Brand, Brand], brands), vendor, season)


def _response(world: SearchWorld, query: str) -> Any:
    request = APIRequestFactory().get("/api/search", {"q": query})
    force_authenticate(request, user=world.user, token=cast(Any, world.session))
    with tenant_context(world.tenant.pk):
        response = GlobalSearchView.as_view()(request)
    assert response.status_code == 200
    return response.data


def _results(payload: Any, key: str) -> list[dict[str, Any]]:
    return next((group["results"] for group in payload["groups"] if group["key"] == key), [])


@pytest.mark.django_db
def test_search_all_scope_remains_tenant_bound_for_documents_brands_and_items() -> None:
    first = _world("a")
    second = _world("b")
    with tenant_context(first.tenant.pk):
        Booking.objects.create(
            number="FIND-BOOK-A", vendor=first.vendor, brand=first.brands[0],
            season=first.season, destination_store=first.stores[0],
        )
        StoreTransfer.objects.create(
            doc_number="FIND-TRANSFER-A", source_store=first.stores[0],
            destination_store=first.stores[1],
        )
        StockOnHand.objects.create(
            store=first.stores[0], sku_code="FIND-SKU-A", brand=first.brands[0].name, brand_ref=first.brands[0],
            design="Own design", season="Own season", net_qty=3,
        )
        # The legacy SKU and cohort rows are global. Their dimensions, season
        # and price must not be projected even when the barcode is present in
        # this tenant's stock.
        global_sku = Sku.objects.create(
            barcode="FIND-SKU-A", brand="Foreign secret brand", design="Foreign secret design",
            mrp_paise=9000,
        )
        Cohort.objects.create(
            sku=global_sku, barcode=global_sku.barcode, season="Foreign secret season",
            unit_cost_paise=1000, mrp_paise=9000,
        )
    with tenant_context(second.tenant.pk):
        Booking.objects.create(
            number="FIND-BOOK-B", vendor=second.vendor, brand=second.brands[0],
            season=second.season, destination_store=second.stores[0],
        )
        StoreTransfer.objects.create(
            doc_number="FIND-TRANSFER-B", source_store=second.stores[0],
            destination_store=second.stores[1],
        )
        StockOnHand.objects.create(
            store=second.stores[0], sku_code="FIND-SKU-B", brand=second.brands[0].name, brand_ref=second.brands[0],
            design="Other design", season="Other season", net_qty=7,
        )
        Sku.objects.create(barcode="FIND-SKU-B", brand=second.brands[0].name)

    payload = _response(first, "FIND")
    assert {row["title"] for row in _results(payload, "documents")} == {
        "FIND-BOOK-A", "FIND-TRANSFER-A",
    }
    assert {row["title"] for row in _results(payload, "brands")} == {
        first.brands[0].name, first.brands[1].name,
    }
    items = _results(payload, "items")
    assert {row["title"] for row in items} == {"FIND-SKU-A"}
    assert items[0]["subtitle"] == f"{first.brands[0].name} · Own design · Own season"
    assert items[0]["meta"] == f"3 pcs at {first.stores[0].code}"
    assert items[0]["mrp_paise"] is None
    assert "Foreign secret" not in str(payload)


@pytest.mark.django_db
def test_search_keeps_each_assignment_site_and_brand_together() -> None:
    world = _world("cells")
    with tenant_context(world.tenant.pk):
        world.human.active = True
        world.human.save(update_fields=["active"])
        RoleAssignment.objects.filter(human=world.human).delete()
        for store, brand in zip(world.stores, world.brands, strict=True):
            RoleAssignment.objects.create(
                tenant=world.tenant, human=world.human, role=world.role,
                site_ids=[store.pk], brand_ids=[brand.pk],
                effective_from=timezone.now() - timedelta(days=1),
            )
        StockOnHand.objects.create(
            store=world.stores[0], sku_code="FIND-ALLOWED-1", brand=world.brands[0].name, brand_ref=world.brands[0],
            net_qty=2,
        )
        StockOnHand.objects.create(
            store=world.stores[0], sku_code="FIND-CROSSED-1", brand=world.brands[1].name, brand_ref=world.brands[1],
            net_qty=4,
        )
        StockOnHand.objects.create(
            store=world.stores[1], sku_code="FIND-ALLOWED-2", brand=world.brands[1].name, brand_ref=world.brands[1],
            net_qty=5,
        )
        StockOnHand.objects.create(
            store=world.stores[1], sku_code="FIND-CROSSED-2", brand=world.brands[0].name, brand_ref=world.brands[0],
            net_qty=6,
        )
        Booking.objects.create(
            number="FIND-CROSSED-BOOK", vendor=world.vendor, brand=world.brands[0],
            season=world.season, destination_store=world.stores[1],
        )
        Booking.objects.create(
            number="FIND-ALLOWED-BOOK", vendor=world.vendor, brand=world.brands[0],
            season=world.season, destination_store=world.stores[0],
        )
        allowed_transfer = StoreTransfer.objects.create(
            doc_number="FIND-ALLOWED-TRANSFER", source_store=world.stores[0],
            destination_store=world.stores[1],
        )
        StoreTransferLine.objects.create(
            transfer=allowed_transfer, sku_code="ALLOWED-T", brand=world.brands[0].name, brand_ref=world.brands[0],
        )
        crossed_transfer = StoreTransfer.objects.create(
            doc_number="FIND-CROSSED-TRANSFER", source_store=world.stores[1],
            destination_store=world.stores[1],
        )
        StoreTransferLine.objects.create(
            transfer=crossed_transfer, sku_code="CROSSED-T", brand=world.brands[0].name, brand_ref=world.brands[0],
        )

    payload = _response(world, "FIND")
    assert {row["title"] for row in _results(payload, "items")} == {
        "FIND-ALLOWED-1", "FIND-ALLOWED-2",
    }
    assert {row["title"] for row in _results(payload, "documents")} == {
        "FIND-ALLOWED-BOOK", "FIND-ALLOWED-TRANSFER",
    }
