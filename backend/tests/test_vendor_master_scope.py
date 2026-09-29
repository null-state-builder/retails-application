"""SO-03: the legacy vendor master obeys tenant and assignment brand scope."""

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
from accounts.unified_policy import initial_step_actions
from core.tenancy import tenant_context
from masters.goods_models import Tenant
from masters.models import Brand
from vendors.models import Vendor, VendorBrand
from vendors.views import VendorDetailView, VendorListCreateView


@dataclass(frozen=True)
class World:
    tenant: Tenant
    brands: tuple[Brand, Brand]
    user: User
    session: ServerSession


def _world(label: str) -> World:
    tenant = Tenant.objects.create(
        code=f"vendor-{label}-{uuid.uuid4().hex[:6]}", name=f"Vendor {label}",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        brands = (
            Brand.objects.create(tenant=tenant, code=f"{label}-brand-1", name=f"Brand {label} 1"),
            Brand.objects.create(tenant=tenant, code=f"{label}-brand-2", name=f"Brand {label} 2"),
        )
        role = Role.objects.create(
            tenant=tenant, code="brand_manager", name="Brand Manager",
            section_access=section_access_for("brand_manager"),
            permissions_map={"step_actions": initial_step_actions("brand_manager")},
        )
        human = HumanIdentity.objects.create(
            tenant=tenant, staff_code=f"vendor-{label}", display_name=f"Manager {label}",
        )
        user = User.objects.create(
            username=f"vendor-{label}-{uuid.uuid4().hex[:6]}",
            tenant=tenant, human=human, role=role, full_name=f"Manager {label}",
        )
        RoleAssignment.objects.create(
            tenant=tenant, human=human, role=role, all_sites=True,
            brand_ids=[brands[0].pk], effective_from=timezone.now() - timedelta(days=1),
        )
        session = issue_session(user).session
    return World(tenant, brands, user, session)


def _vendor(world: World, code: str, *brands: Brand) -> Vendor:
    with tenant_context(world.tenant.pk):
        vendor = Vendor.objects.create(tenant=world.tenant, code=code, name=code)
        for brand in brands:
            VendorBrand.objects.create(tenant=world.tenant, vendor=vendor, brand=brand)
        return vendor


def _request(world: World, method: str, path: str, data: dict[str, object] | None = None) -> Any:
    factory = APIRequestFactory()
    request = getattr(factory, method)(path, data=data, format="json")
    force_authenticate(request, user=world.user, token=cast(Any, world.session))
    return request


@pytest.mark.django_db
def test_vendor_master_hides_foreign_and_unassigned_brands_and_refuses_writes() -> None:
    first, second = _world("a"), _world("b")
    own = _vendor(first, "own", first.brands[0])
    other_brand = _vendor(first, "other-brand", first.brands[1])
    shared = _vendor(first, "shared", *first.brands)
    unbranded = _vendor(first, "unbranded")
    foreign = _vendor(second, "foreign", second.brands[0])
    first_only = _vendor(first, "first-only", first.brands[0])
    first_only.gstin = "123456789012345"
    first_only.save(update_fields=["gstin"])

    with tenant_context(first.tenant.pk):
        listed = VendorListCreateView.as_view()(_request(first, "get", "/api/vendors"))
        assert listed.status_code == 200
        assert {row["id"] for row in listed.data} == {own.pk, shared.pk, first_only.pk}
        shared_detail = VendorDetailView.as_view()(
            _request(first, "get", f"/api/vendors/{shared.pk}"), pk=shared.pk,
        )
        assert shared_detail.status_code == 200
        assert shared_detail.data["brands"] == [first.brands[0].pk]
        assert shared_detail.data["brand_names"] == [first.brands[0].name]
        for hidden in (other_brand, unbranded, foreign):
            response = VendorDetailView.as_view()(
                _request(first, "get", f"/api/vendors/{hidden.pk}"), pk=hidden.pk,
            )
            assert response.status_code == 404

        refused = VendorDetailView.as_view()(
            _request(first, "patch", f"/api/vendors/{shared.pk}", {"name": "Changed"}),
            pk=shared.pk,
        )
        assert refused.status_code == 404
        shared.refresh_from_db()
        assert shared.name == "shared"
        allowed = VendorListCreateView.as_view()(_request(first, "post", "/api/vendors", {
            "code": "new-a", "name": "New A", "brands": [first.brands[0].pk],
        }))
        assert allowed.status_code == 201
        assert Vendor.objects.get(pk=allowed.data["id"]).tenant_id == first.tenant.pk
        wrong_brand = VendorListCreateView.as_view()(_request(first, "post", "/api/vendors", {
            "code": "new-b", "name": "New B", "brands": [first.brands[1].pk],
        }))
        assert wrong_brand.status_code == 404
        foreign_brand = VendorListCreateView.as_view()(_request(first, "post", "/api/vendors", {
            "code": "new-foreign", "name": "Foreign", "brands": [second.brands[0].pk],
        }))
        assert foreign_brand.status_code == 400
        assert not Vendor.objects.filter(tenant_id=first.tenant.pk, code__in=["new-b", "new-foreign"]).exists()

    # A code and GSTIN in another tenant are no longer treated as a clash.
    with tenant_context(second.tenant.pk):
        duplicate_across_tenants = VendorListCreateView.as_view()(
            _request(second, "post", "/api/vendors", {
                "code": "first-only", "name": "Allowed in B", "gstin": "123456789012345",
                "brands": [second.brands[0].pk],
            }),
        )
        assert duplicate_across_tenants.status_code == 201
