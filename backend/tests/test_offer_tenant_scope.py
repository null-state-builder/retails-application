"""SO-03: legacy offers never cross tenants through a shared store code."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

import pytest
from django.utils import timezone

from accounts.goods_models import HumanIdentity, RoleAssignment
from accounts.models import Role, User
from accounts.rbac_matrix import section_access_for
from core.tenancy import tenant_context
from masters.goods_models import Tenant
from masters.models import Brand, Gstin, LegalEntity, Store
from offers.models import Offer
from offers.views import visible_offers
from sell.services.dataset import Sync, _offers
from sell.services.recompute import credit_from_cited_rule, rulebook_for
from sell.services.running_offers import running_offers
from storefront.dashboard import live_offers


@dataclass(frozen=True)
class World:
    tenant: Tenant
    store: Store
    brand: Brand
    user: User


def _world(label: str) -> World:
    tenant = Tenant.objects.create(
        code=f"offer-{label}-{uuid.uuid4().hex[:6]}", name=f"Offer tenant {label}",
        deployment_key=uuid.uuid4(), timezone="Asia/Kolkata", currency="INR",
        locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        entity = LegalEntity.objects.create(tenant=tenant, code="entity", name=f"Entity {label}")
        gstin = Gstin.objects.create(
            tenant=tenant, legal_entity=entity,
            gstin=f"10{uuid.uuid4().int % 10**13:013d}", state_code="10", state_name="Bihar",
        )
        # A store code is unique only within its tenant. Both worlds use it.
        store = Store.objects.create(tenant=tenant, gstin=gstin, code="shared", name="Shared code")
        brand = Brand.objects.create(tenant=tenant, code="brand", name=f"Brand {label}")
        role = Role.objects.create(
            tenant=tenant, code="owner", name="Owner", section_access=section_access_for("owner"),
        )
        human = HumanIdentity.objects.create(
            tenant=tenant, staff_code=f"offer-{label}", display_name=f"Owner {label}",
        )
        user = User.objects.create(
            username=f"offer-{label}-{uuid.uuid4().hex[:6]}", tenant=tenant,
            human=human, role=role, full_name=f"Owner {label}",
        )
        RoleAssignment.objects.create(
            tenant=tenant, human=human, role=role, all_sites=True, all_brands=True,
            effective_from=timezone.now() - timedelta(days=1),
        )
    return World(tenant, store, brand, user)


def _offer(name: str, day: date, *, brand: Brand | None, author: User | None,
           approver: User | None, status: str = Offer.Status.LIVE) -> Offer:
    return Offer.objects.create(
        name=name, brand=brand, created_by=author, approved_by=approver,
        funder=Offer.Funder.KDPS, layer=Offer.Layer.BRAND if brand else Offer.Layer.STOREWIDE,
        trigger_type=Offer.Trigger.NONE, reward_type=Offer.Reward.PCT_OFF,
        reward_config={"percent": "10.00"}, store_scope={"kind": "specific", "stores": ["SHARED"]},
        starts_on=day, status=status,
    )


@pytest.mark.django_db
def test_offer_readers_and_pricing_use_stable_tenant_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = _world("a"), _world("b")
    day = timezone.localdate()
    first_brand = _offer("A brand", day, brand=first.brand, author=first.user, approver=first.user)
    first_storewide = _offer("A storewide", day, brand=None, author=first.user, approver=first.user)
    second_brand = _offer("B brand", day, brand=second.brand, author=second.user, approver=second.user)
    second_storewide = _offer("B storewide", day, brand=None, author=second.user, approver=second.user)
    conflicted = _offer("Conflicted actors", day, brand=None, author=first.user, approver=second.user)
    orphan = _offer(
        "Orphan history", day, brand=None, author=None, approver=None, status=Offer.Status.ENDED,
    )
    expected = {
        first.tenant.pk: {first_brand.pk, first_storewide.pk},
        second.tenant.pk: {second_brand.pk, second_storewide.pk},
    }
    monkeypatch.setattr("sell.services.running_offers.working_set_items", lambda *_args: [])

    for world in (first, second):
        ids = expected[world.tenant.pk]
        with tenant_context(world.tenant.pk):
            assert set(Offer.objects.for_tenant(world.tenant.pk).values_list("pk", flat=True)) == ids
            assert set(visible_offers(world.user, include_ended=True).values_list("pk", flat=True)) == ids
            assert {row["id"] for row in _offers(Sync(world.store, None, day))[0]} == ids
            assert {row["id"] for row in running_offers(world.store, day)} == ids
            assert {row["id"] for row in live_offers(world.store, day)} == ids
            assert {rule.id for rule in rulebook_for("SHARED", day, tenant_id=world.tenant.pk)} == ids
            foreign_id = second_brand.pk if world is first else first_brand.pk
            assert credit_from_cited_rule(
                foreign_id, "SHARED", day, [], 1, tenant_id=world.tenant.pk,
            ) == 0

    assert conflicted.pk not in expected[first.tenant.pk] | expected[second.tenant.pk]
    assert orphan.pk not in expected[first.tenant.pk] | expected[second.tenant.pk]
    assert not Offer.objects.for_tenant(None).exists()
