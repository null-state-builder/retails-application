"""Actual online exchange preserves the original and returns custody, not ATS."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from django.db.models import Sum

from core.gl import GLEntry
from sell.models import Sale, SaleLine, SaleTender
from sell.services.goods_stock import read_shelf
from sell.views import OnlineFinaliseView
from stockledger.goods_models import JournalBatch
from stockledger.models import StockOnHand
from tests.test_first_store_online import bill, post
from tests.test_first_store_online import online_goods as online_goods
from tests.test_so03_denials import worlds as worlds


def exchange_wire(proof: Any, original: dict[str, Any], condition: str) -> dict[str, Any]:
    wire, _ = bill(proof, seq=2)
    returned = copy.deepcopy(original["lines"][0])
    returned.update(line_no=2, original_line=1, condition=condition, reason="size")
    wire["exchange"] = {"original": {"store": original["store"], "fy": original["fy"], "till_seq": 1}, "lines": [returned]}
    wire["totals"].update(net_paise=0, gst_paise=0)
    wire["tenders"] = []
    return wire


@pytest.mark.parametrize("condition", ["good", "damaged"])
def test_online_exchange_preserves_original_cost_and_never_resells_unaccepted_return(
    online_goods: Any, condition: str,
) -> None:
    proof = online_goods
    site = proof.world.sites[0]
    original, _ = bill(proof)
    assert post(OnlineFinaliseView, proof, original).status_code == 201
    saved = Sale.objects.get(till_seq=1)
    first_line = SaleLine.objects.get(sale=saved)
    immutable = (saved.doc_number, saved.net_paise, copy.deepcopy(first_line.goods_allocations))
    wire = exchange_wire(proof, original, condition)
    response = post(OnlineFinaliseView, proof, wire)
    assert response.status_code == 201, response.data
    assert sum(read_shelf(site).quantities.values()) == 1
    assert Sale.objects.count() == 2 and SaleTender.objects.count() == 1
    assert not StockOnHand.objects.exists()
    assert GLEntry.objects.aggregate(total=Sum("amount"))["total"] == 0
    saved.refresh_from_db()
    first_line.refresh_from_db()
    assert (saved.doc_number, saved.net_paise, first_line.goods_allocations) == immutable
    before = JournalBatch.objects.count()
    replay = post(OnlineFinaliseView, proof, wire)
    assert replay.status_code == 200 and replay.data == response.data
    assert JournalBatch.objects.count() == before and Sale.objects.count() == 2


def test_unknown_original_exchange_is_refused_without_stock_bill_or_tender_changes(
    online_goods: Any,
) -> None:
    proof = online_goods
    original, _ = bill(proof)
    assert post(OnlineFinaliseView, proof, original).status_code == 201
    wire = exchange_wire(proof, original, "good")
    wire["exchange"]["original"]["till_seq"] = 999
    before = JournalBatch.objects.count()
    denied = post(OnlineFinaliseView, proof, wire)
    assert denied.status_code in (404, 422), denied.data
    assert denied.data["code"] == "ORIGINAL_REQUIRED"
    assert JournalBatch.objects.count() == before and Sale.objects.count() == 1
    assert SaleTender.objects.count() == 1 and sum(read_shelf(proof.world.sites[0]).quantities.values()) == 2
