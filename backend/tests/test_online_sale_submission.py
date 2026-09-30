"""Online pre-issue boundaries; real tenant-owned intent persistence on proof DB."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from core.tenancy import tenant_context
from sell.models import OnlineSaleSubmission, Sale
from sell.services.accept import AcceptError
from sell.services.online import check_lines_before_issue, commercial_revision, finalise_submission
from tests.test_so03_denials import _assign, _person
from tests.test_so03_denials import worlds as worlds


def _data(store: Any) -> dict[str, Any]:
    return {"idempotency_uuid": uuid.uuid4(), "store": store.code, "lines": [],
            "fy": "26-27", "till_seq": 1, "commercial_revision": "a" * 64}


def test_definitive_refusal_persists_and_same_uuid_cannot_later_issue(worlds: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        cashier, human = _person(world, "online")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        data = _data(world.sites[0])
        calls: list[Any] = []
        def refuse(*args: Any, **kwargs: Any) -> None:
            calls.append(args)
            raise AcceptError("PRICING_STALE", "Refresh prices before issue.", 409)
        monkeypatch.setattr("sell.services.accept.accept_sale", refuse)
        result, refusal = finalise_submission(data, cashier, None, "")
        assert result is None and refusal is not None and refusal["not_issued"] is True
        intent = OnlineSaleSubmission.objects.get(idempotency_uuid=data["idempotency_uuid"])
        assert intent.tenant_id == world.tenant.pk and intent.status == "rejected"
        result, replay = finalise_submission(data, cashier, None, "")
        assert replay == refusal and len(calls) == 1
        assert not Sale.objects.exists()
        with pytest.raises(AcceptError, match="different bill"):
            finalise_submission({**data, "till_seq": 2}, cashier, None, "")
        assert OnlineSaleSubmission.objects.count() == 1


def test_auth_failure_never_claims_unknown_bill_was_not_issued(worlds: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        cashier, human = _person(world, "online-revoked")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        data = _data(world.sites[0])
        def refuse(*args: Any, **kwargs: Any) -> None:
            raise AcceptError("AUTH_REQUIRED", "Sign in again.", 403)
        monkeypatch.setattr("sell.services.accept.accept_sale", refuse)
        with pytest.raises(AcceptError):
            finalise_submission(data, cashier, None, "")
        assert not OnlineSaleSubmission.objects.exists()
        assert not Sale.objects.exists()


def test_wrong_store_intent_never_is_written(worlds: Any) -> None:
    world, other = worlds
    with tenant_context(world.tenant.pk):
        cashier, human = _person(world, "online-scope")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        with pytest.raises(AcceptError, match="own store"):
            finalise_submission(_data(other.sites[0]), cashier, None, "")
        assert not OnlineSaleSubmission.objects.exists()


def test_commercial_revision_ignores_stock_changes_but_pins_protected_rule_inputs() -> None:
    first = {"items": [{"sku_id": "stable", "mrp_paise": 1000, "hsn": "6101", "no_discount": False}],
             "offers": [], "policy": {"version": "v1"}, "stock": [{"qty": 1}]}
    assert commercial_revision(first) == commercial_revision({**first, "stock": [{"qty": 10}]})
    for key, changed in (("items", [{"sku_id": "stable", "mrp_paise": 1001}]),
                         ("offers", [{"id": 1}]), ("policy", {"version": "v2"})):
        assert commercial_revision(first) != commercial_revision({**first, key: changed})


@pytest.mark.parametrize("change,code", [
    ({"mrp_paise": 0}, "PRICE_STALE"), ({"mrp_paise": 1001}, "PRICE_STALE"),
    ({"hsn": ""}, "HSN_REQUIRED"), ({"brand_id": None}, "IDENTITY_REQUIRED"),
    ({"no_discount": True}, "NO_DISCOUNT"),
])
def test_online_line_requires_trusted_complete_inputs(change: dict[str, Any], code: str, monkeypatch: pytest.MonkeyPatch) -> None:
    tax = SimpleNamespace(version=1, line_tax=lambda *args: SimpleNamespace(
        rule_missing=False, split=SimpleNamespace(rate=Decimal("5"), gst_paise=43)))
    monkeypatch.setattr("sell.services.tax_rulebook.StoreTaxBooks", lambda store: SimpleNamespace(at=lambda at: tax))
    monkeypatch.setattr("sell.services.after_discount_check.after_discount_on", lambda store: False)
    piece = SimpleNamespace(brand_id=1, hsn="6101", mrp_paise=1000, no_discount=False)
    for key, value in change.items():
        setattr(piece, key, value)
    line = SimpleNamespace(is_return=False, is_alteration=False, goods_piece=piece,
        cost=SimpleNamespace(postable=True),
        value_paise=900, qty=1, payload={"line_no": 1, "mrp_paise": 1000, "disc_paise": 100,
                                     "gst_rate": Decimal("5"), "gst_paise": 43, "offer_evidence": {}})
    access = SimpleNamespace(require_all_actions=lambda *args, **kwargs: None)
    rulebook = SimpleNamespace(resolution=SimpleNamespace(by_line=lambda: {}))
    with pytest.raises(AcceptError) as caught:
        check_lines_before_issue({"billed_at": None}, SimpleNamespace(pk=1), [line], rulebook, access)
    assert caught.value.code == code


def test_goods_ownership_uses_stable_id_with_duplicate_labels(worlds: Any) -> None:
    from masters.models import Brand
    from sell.models import SaleLine
    from sell.services.postings import resolve_goods_cost_plan
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        own, held = world.brands
        held.name = own.name
        held.ownership = Brand.Ownership.BRAND_OWNED
        held.save(update_fields=["name", "ownership"])
        assert resolve_goods_cost_plan(brand_id=own.pk, unit_cost_paise=50000).book == SaleLine.CostBook.OWN
        assert not resolve_goods_cost_plan(brand_id=held.pk, unit_cost_paise=50000).postable


def test_bulk_cost_lookup_never_adopts_a_foreign_or_missing_stable_brand(worlds: Any) -> None:
    from sell.services.postings import resolve_goods_cost_plan
    world, other = worlds
    with tenant_context(world.tenant.pk):
        own = world.brands[0]
        assert resolve_goods_cost_plan(brand_id=own.pk, unit_cost_paise=50000, brands={own.pk: own}).postable
        assert not resolve_goods_cost_plan(brand_id=own.pk, unit_cost_paise=50000, brands={}).postable
        assert not resolve_goods_cost_plan(brand_id=own.pk, unit_cost_paise=50000,
                                           brands={own.pk: other.brands[0]}).postable
