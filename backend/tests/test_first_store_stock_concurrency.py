"""Competing mounted stock-review commands serialize on their actual site guards."""
from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import close_old_connections

from accounts.goods_models import ServerSession
from accounts.models import User
from approvals.goods_views import GoodsApprovalDecideView
from core.tenancy import tenant_context
from outbound.goods_models import CountDecision
from outbound.goods_transfer_views import TransferApproveView
from stockledger.goods_models import ActiveReservation, JournalBatch
from tests.test_first_store_goods_operations import (
    approval_wire, operational_goods as operational_goods, post, receiving_goods as receiving_goods, submitted_transfer,
)
from tests.test_first_store_online import online_goods as online_goods
from tests.test_first_store_online_concurrency import allow_named_disposable_test_flush as allow_named_disposable_test_flush
from tests.test_first_store_trading_counts import captured, decision, pending, setup, submit
from tests.test_so03_denials import worlds as worlds

pytestmark = pytest.mark.django_db(transaction=True)


def race(proof: Any, view: Any, actor: Any, pk: Any, bodies: list[dict[str, Any]]) -> list[tuple[int, Any]]:
    barrier = threading.Barrier(2)
    def run(body: dict[str, Any]) -> tuple[int, Any]:
        close_old_connections()
        try:
            with tenant_context(proof.world.tenant.pk):
                local = SimpleNamespace(user=User.objects.get(pk=actor.user.pk), session=ServerSession.objects.get(pk=actor.session.pk))
                barrier.wait(timeout=20)
                response = post(view, local, body, pk=pk)
                return response.status_code, response.data
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(run, bodies))


def test_concurrent_count_decisions_close_and_post_once(online_goods: Any) -> None:
    proof = setup(online_goods)
    row, report = captured(proof, 2)
    assert submit(proof, row, report).status_code == 200
    request = pending(row)
    before = JournalBatch.objects.count()
    responses = race(proof, GoodsApprovalDecideView, proof.checker, request.pk, [decision(request), decision(request)])
    assert sorted(status for status, _body in responses) == [200, 409], responses
    assert CountDecision.objects.filter(stocktake=row).count() == 1
    assert JournalBatch.objects.count() == before + 1
    assert request.decisions.filter(outcome="approved").count() == 1


def test_concurrent_transfer_retry_has_one_reservation_effect(operational_goods: Any) -> None:
    proof = operational_goods
    transfer = submitted_transfer(proof)
    body = approval_wire(transfer)
    responses = race(proof, TransferApproveView, proof.owner, transfer.pk, [body, copy.deepcopy(body)])
    assert [status for status, _body in responses] == [200, 200], responses
    assert responses[0][1] == responses[1][1]
    assert sum(row.portion.upper - row.portion.lower for row in ActiveReservation.objects.filter(transfer_version__document__goods_pt__transfer=transfer)) == 2
