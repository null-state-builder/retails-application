"""Canonical opening stock remains visible through the supported stock screens.

These disposable-proof cases use actual receipt, physical acceptance and sale
writers. Compatibility reads must neither resurrect an old balance source nor
merge protected fields from another assignment.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_models import HumanIdentity, RoleAssignment
from accounts.sessions import issue_session, revoke_session
from outbound.views import CrossLocationStockSearchView
from search.views import GlobalSearchView
from sell.views import OnlineFinaliseView
from stockledger import on_hand_projection, views
from stockledger.goods_models import JournalBatch, Position
from stockledger.models import StockOnHand
from stockledger.views import StockAvailabilityView, StockOnHandView
from tests.test_first_store_online import bill, post
from tests.test_first_store_online import online_goods as online_goods
from tests.test_so03_denials import _assign
from tests.test_so03_denials import worlds as worlds


SURFACES = {"inventory": StockOnHandView, "availability": StockAvailabilityView,
            "search": GlobalSearchView, "outbound_search": CrossLocationStockSearchView}


def _read(proof: Any, surface: str = "inventory", *, owner: bool = False,
          params: dict[str, Any] | None = None) -> Any:
    actor = proof.owner if owner else proof.manager
    request = APIRequestFactory().get("/proof/current-stock", params or {"q": proof.barcode})
    force_authenticate(request, actor.user, actor.session)
    return SURFACES[surface].as_view()(request)


def _quantity(surface: str, body: dict[str, Any]) -> int:
    if surface == "inventory":
        return int(body["summary"]["units_on_hand"])
    if surface == "availability":
        return sum(row["qty"] for group in body["results"] for size in group["sizes"] for row in size["stores"])
    if surface == "outbound_search":
        return sum(row["qty"] for row in body["rows"])
    items = next(group["results"] for group in body["groups"] if group["key"] == "items")
    return int(items[0]["meta"].split()[0])


@pytest.mark.parametrize("surface", SURFACES)
def test_supported_inventory_search_reads_actual_accepted_stock_without_legacy_copy(
    online_goods: Any, surface: str,
) -> None:
    proof = online_goods
    site = proof.world.sites[0]
    # A retained stale projection at the same store must not become a second
    # balance source after the online-alpha canonical boundary is selected.
    stale = StockOnHand.objects.create(store=site, brand_ref=proof.world.brands[0],
        sku_code=proof.barcode, brand="Untrusted old display", net_qty=99, net_value_paise=999999)
    before = (JournalBatch.objects.count(), Position.objects.count())
    response = _read(proof, surface)
    assert response.status_code == 200, response.data
    assert _quantity(surface, response.data) == 3
    assert response["Cache-Control"] == "no-store, private"
    assert "999999" not in str(response.data) and "Untrusted old display" not in str(response.data)
    if surface == "inventory":
        row = response.data["rows"][0]
        assert row["record_contract"] == "goods-v1" and row["sku_id"] == str(proof.sku_id)
        assert row["brand_id"] == proof.world.brands[0].pk and row["sku_code"] == proof.barcode
        assert "net_value_paise" not in row and "value_paise" not in response.data["summary"]
        assert response.data["summary"]["value_complete"] is False
    stale.refresh_from_db()
    assert stale.net_qty == 99 and before == (JournalBatch.objects.count(), Position.objects.count())


@pytest.mark.parametrize("group", ["sku", "brand", "store"])
def test_inventory_grouping_cost_projection_and_sale_follow_the_same_canonical_stock(
    online_goods: Any, group: str,
) -> None:
    proof = online_goods
    response = _read(proof, owner=True, params={"group_by": group})
    assert response.status_code == 200 and _quantity("inventory", response.data) == 3, response.data
    assert response.data["summary"]["value_paise"] == 150000
    assert response.data["rows"][0]["net_value_paise"] == 150000
    assert response.data["summary"]["identity_complete"] is True
    wire, _ = bill(proof)
    assert post(OnlineFinaliseView, proof, wire).status_code == 201
    after = _read(proof, owner=True, params={"group_by": group})
    assert after.status_code == 200 and _quantity("inventory", after.data) == 2, after.data
    assert after.data["summary"]["value_paise"] == 100000
    assert not StockOnHand.objects.exists()


@pytest.mark.parametrize("other_scope", ["site", "brand"])
def test_cost_at_a_different_resource_cannot_enable_this_inventory_value(
    online_goods: Any, other_scope: str,
) -> None:
    proof = online_goods
    human = HumanIdentity.objects.get(pk=proof.manager.human_id)
    _assign(proof.world, human, "warehouse",
            sites=(proof.world.sites[1 if other_scope == "site" else 0],),
            all_brands=other_scope == "site",
            brands=(proof.world.brands[1],) if other_scope == "brand" else ())
    response = _read(proof)
    assert response.status_code == 200 and _quantity("inventory", response.data) == 3, response.data
    assert "net_value_paise" not in response.data["rows"][0]
    assert "value_paise" not in response.data["summary"]
    required = _read(proof, params={"basis": "cost"})
    assert required.status_code == 403 and required.data["code"] == "FIELD_DENIED"


@pytest.mark.parametrize("filter_key", ["store", "brand"])
def test_inventory_filters_cannot_expand_resource_scope(online_goods: Any, filter_key: str) -> None:
    proof = online_goods
    value = proof.world.sites[1].code if filter_key == "store" else proof.world.brands[1].name
    response = _read(proof, params={filter_key: value})
    assert response.status_code == 200, response.data
    assert response.data["rows"] == [] and response.data["summary"]["units_on_hand"] == 0
    assert "value_paise" not in response.data["summary"]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("boundary", ["logout", "expiry", "scope"])
def test_stock_delivery_replays_live_session_and_exact_resource_demands(
    online_goods: Any, monkeypatch: pytest.MonkeyPatch, surface: str, boundary: str,
) -> None:
    proof = online_goods
    assert _read(proof, surface).status_code == 200
    session = issue_session(proof.manager.user).session
    proof.manager.session = session
    original = on_hand_projection.projected_rows

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        rows = original(*args, **kwargs)
        if boundary == "logout":
            revoke_session(session)
        elif boundary == "expiry":
            type(session).objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        else:
            RoleAssignment.objects.filter(human_id=proof.manager.human_id).update(site_ids=[proof.world.sites[1].pk])
        return rows

    monkeypatch.setattr(on_hand_projection, "projected_rows", interrupted)
    monkeypatch.setattr(views, "projected_rows", interrupted)
    response = _read(proof, surface)
    assert response.status_code in (401, 403, 404), response.data
    assert response.data["code"] in {"AUTH_REQUIRED", "SESSION_EXPIRED", "NOT_FOUND", "ACTION_DENIED"}
    assert not ({"rows", "summary", "results", "groups"} & response.data.keys())


def test_protected_value_revocation_before_inventory_delivery_refuses_the_response(
    online_goods: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = online_goods
    assert _read(proof, owner=True).data["summary"]["value_paise"] == 150000
    original = on_hand_projection.on_hand_response

    def remove_cost(*args: Any, **kwargs: Any) -> Any:
        body = original(*args, **kwargs)
        role = proof.world.roles["owner"]
        role.field_access = [field for field in role.field_access if field != "cost"]
        role.save(update_fields=["field_access"])
        return body

    monkeypatch.setattr(views, "on_hand_response", remove_cost)
    response = _read(proof, owner=True)
    assert response.status_code in (403, 404), response.data
    assert response.data["code"] in {"FIELD_DENIED", "NOT_FOUND"} and "summary" not in response.data


def test_same_display_brand_names_remain_separate_stable_identity_groups() -> None:
    base = {"store_id": 1, "store_code": "PROOF", "store_name": "Proof shop", "record_contract": "goods-v1",
            "brand": "Repeated label", "design": "Style", "color": "", "size": "M", "item": "Shirt",
            "season": "Reviewed", "sku_code": "same-alias", "net_qty": 1, "identity_complete": True}
    rows = [{**base, "brand_id": 10, "sku_id": "sku-a"}, {**base, "brand_id": 20, "sku_id": "sku-b"}]
    for group in ("brand", "sku"):
        body = on_hand_projection.on_hand_response(rows, group, 2000)
        assert len(body["rows"]) == 2 and body["summary"]["units_on_hand"] == 2
        assert {row["brand_id"] for row in body["rows"]} == {10, 20}
        assert "value_paise" not in body["summary"]
