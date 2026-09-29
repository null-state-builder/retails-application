"""SO-03 continuation: real resource, policy, session and integrity boundaries."""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.principal import resolve_access
from accounts.sessions import issue_session, revoke_session
from core.refusals import Refusal
from core.tenancy import tenant_context
from masters.models import Sku
from offers.eoss_views import EossConfigView, EossRecommendationDecisionView
from offers.models import EossLadderStep, Offer
from offers.price_views import PriceDetailView, PriceListView, PriceRepriceView
from reporting.base import ReportScope, record_export
from stockledger.models import StockLedgerEntry, StockOnHand
from stockledger.projections import post_on_hand_movement
from tests.test_so03_denials import TenantWorld, _assign, _person
from tests.test_so03_denials import worlds as worlds


def request_for(user: Any, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = getattr(APIRequestFactory(), method)(path, body or {}, format="json")
    force_authenticate(request, user=user, token=issue_session(user).session)
    return request


def test_price_scope_fields_and_shared_writer_denial(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, other = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "prices")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        for _owner, site, brand, barcode in (
            (world, world.sites[0], world.brands[0], "own"),
            (other, other.sites[0], other.brands[0], "foreign"),
        ):
            Sku.objects.create(barcode=barcode, brand="Same label", mrp_paise=1000)
            StockOnHand.objects.create(store=site, gstin=site.gstin, sku_code=barcode,
                                       brand="Same label", brand_ref=brand, net_qty=2, net_value_paise=400)
        response = PriceListView.as_view()(request_for(user, "get", "/api/offers/price-list"))
        assert response.status_code == 200
        assert [row["barcode"] for row in response.data["rows"]] == ["own"]
        assert response.data["rows"][0]["cost_paise"] is None
        assert response.data["rows"][0]["margin_pct"] is None
        response = PriceDetailView.as_view()(request_for(user, "get", "/price/foreign"), barcode="foreign")
        assert response.status_code == 404
        response = PriceRepriceView.as_view()(request_for(user, "post", "/price/own", {"mrp_paise": 2000, "reason": "Test"}), barcode="own")
        assert response.status_code in {403, 409}
        assert Sku.objects.get(barcode="own").mrp_paise == 1000


def test_eoss_invalid_replacement_preserves_configuration_and_no_self_approval(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "eoss")
        role = world.roles["owner"]
        role.section_access = {**role.section_access, "offers_price": {"capability": "manage"}}
        role.save(update_fields=["section_access"])
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        step = EossLadderStep.objects.create(brand=world.brands[0], step_no=1, trigger_type="weeks", trigger_value=2, discount_pct=10)
        response = EossConfigView.as_view()(request_for(user, "put", "/eoss/config", {
            "brand": world.brands[0].code, "ladder": [{"discount_pct": 101}], "targets": [],
        }))
        assert response.status_code == 400
        assert EossLadderStep.objects.filter(pk=step.pk).exists()
        response = EossRecommendationDecisionView.as_view()(request_for(user, "post", "/eoss/1", {"action": "approve"}), pk=1)
        assert response.status_code == 409
        assert not Offer.objects.exists()


def test_export_refuses_logout_after_render(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "export")
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        issued = issue_session(user)
        access = resolve_access(SimpleNamespace(user=user, auth=issued.session))
        scope = ReportScope(user=user, options=list(world.sites), stores=list(world.sites),
                            date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
        revoke_session(issued.session)
        with pytest.raises(Refusal):
            record_export(user, report="sales", scope=scope, detail={}, access=access)


def test_new_stock_legs_preserve_id_and_rebinding_rolls_back(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, _ = _person(world, "stock")
        source = SimpleNamespace(brand="Historical label", brand_ref_id=world.brands[0].pk)
        barcode = uuid.uuid4().hex
        entry = post_on_hand_movement(store=world.sites[0], gstin=world.sites[0].gstin,
            sku_code=barcode, source=source, qty=2, unit_cost_paise=100,
            kind=StockLedgerEntry.Kind.PT_INWARD, doc_number="SO03-proof", line_no=1, posted_by=user)
        assert entry.brand_ref_id == world.brands[0].pk
        assert StockOnHand.objects.get(sku_code=barcode).brand_ref_id == world.brands[0].pk
        source.brand_ref_id = world.brands[1].pk
        from django.db import transaction

        with pytest.raises(Refusal), transaction.atomic():
            post_on_hand_movement(store=world.sites[0], gstin=world.sites[0].gstin,
                sku_code=barcode, source=source, qty=1, unit_cost_paise=100,
                kind=StockLedgerEntry.Kind.PT_INWARD, doc_number="SO03-conflict", line_no=1)
        assert StockOnHand.objects.get(sku_code=barcode).net_qty == 2
        assert not StockLedgerEntry.objects.filter(doc_number="SO03-conflict").exists()
