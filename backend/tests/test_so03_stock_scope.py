"""SO-03: stock reads preserve each role assignment's selected-brand scope."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from accounts.principal import AccessContext, GrantView
from stockledger import goods_reads


def test_selected_brands_produce_separate_stock_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    first_sku, second_sku, other_sku = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    skus = {7: frozenset({first_sku}), 8: frozenset({second_sku})}
    monkeypatch.setattr(goods_reads, "_skus_of_brand", lambda _tenant, brand: skus[brand])
    grant = GrantView(
        id=uuid.uuid4(), role_code="brand_manager", scope_kind="cells",
        entity_id=None, site_id=11, sbu_id=None, sbu_site_id=None,
        sbu_brand_id=None, brand_id=None, actions=frozenset({"stock.view"}),
        fields=frozenset({"cost"}), all_sites=False, site_ids=frozenset({11}),
        all_brands=False, brand_ids=frozenset({7, 8}),
    )
    access = AccessContext(
        user=SimpleNamespace(), human_id=uuid.uuid4(), tenant_id=uuid.uuid4(),
        session=None, grants=[grant], _tenant_sites={11, 12}, _tenant_brands={7, 8},
    )

    terms = goods_reads._grant_terms(access, frozenset({11, 12}))

    assert {term.brand_id for term in terms} == {7, 8}
    assert {term.brand_id for term in terms if term.covers(11, first_sku)} == {7}
    assert {term.brand_id for term in terms if term.covers(11, second_sku)} == {8}
    assert not any(term.covers(11, other_sku) for term in terms)
    assert not any(term.covers(11, None) for term in terms)
    assert not any(term.covers(12, first_sku) for term in terms)
