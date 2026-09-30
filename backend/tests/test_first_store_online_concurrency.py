"""Concurrent real-writer online intents on the identified disposable database."""
from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import close_old_connections, connection, transaction

from accounts.goods_models import ServerSession
from accounts.models import User
from core.tenancy import tenant_context
from core.commands import CommandRun
from core.refusals import Refusal
from core.goods_fields import bounds
from finledger.models import CashLedgerEntry
from sell.models import Sale, SaleTender
from sell.services.goods_stock import read_shelf
from sell.views import OnlineFinaliseView
from masters.goods_models import SiteGuard
from sell.services.till_authority import TillError, resume_till
from tests.first_store_goods import command
from stockledger.goods_models import Position
from tests.test_first_store_online import bill, online_goods as online_goods, post
from tests.test_so03_denials import worlds as worlds

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def allow_named_disposable_test_flush(django_db_blocker: Any) -> None:
    # This existing harness hatch is solely TransactionTestCase teardown on the
    # exact disposable test DB. It never disables immutable write guards.
    with django_db_blocker.unblock():
        assert os.environ.get("KDPS_PROOF_MODE") == "1"
        assert connection.settings_dict["NAME"] == "kdps_proof_test"
        with connection.cursor() as cursor:
            cursor.execute("SET kdps.allow_truncate = 'on'")


@pytest.mark.parametrize("same_intent", [True, False])
def test_concurrent_intents_one_registered_frontier_one_posting(online_goods: Any, same_intent: bool) -> None:
    proof = online_goods
    wire, _ = bill(proof)
    second = copy.deepcopy(wire)
    if not same_intent:
        second["idempotency_uuid"] = str(uuid.uuid4())
    barrier = threading.Barrier(2)
    def submit(payload: dict[str, Any]) -> tuple[int, Any]:
        close_old_connections()
        try:
            with tenant_context(proof.world.tenant.pk):
                # Independent request objects and access contexts, as in actual
                # devices/tabs, while both present the same registered counter.
                local = copy.copy(proof)
                local.manager = SimpleNamespace(user=User.objects.get(pk=proof.manager.user.pk),
                                                session=ServerSession.objects.get(pk=proof.manager.session.pk))
                barrier.wait(timeout=20)
                response = post(OnlineFinaliseView, local, payload)
                return response.status_code, response.data
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (wire, second)))
    assert sorted(status for status, _body in results) == ([200, 201] if same_intent else [201, 409]), results
    assert Sale.objects.count() == 1 and SaleTender.objects.count() == 1 and CashLedgerEntry.objects.count() == 1
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 2
    if same_intent:
        assert results[0][1] == results[1][1]
    else:
        assert next(body for status, body in results if status == 409)["code"] == "BILL_NO_STALE"


def backend_pid() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return int(cursor.fetchone()[0])


def assert_blocked_by(blocked: int, blocker: int) -> None:
    """Observe the real PostgreSQL wait, rather than guess from thread timing."""
    until = time.monotonic() + 5
    while time.monotonic() < until:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_blocking_pids(%s)", [blocked])
            if blocker in cursor.fetchone()[0]:
                return
        threading.Event().wait(0.01)
    pytest.fail("The competing operation never waited on the held site boundary.")


@pytest.mark.parametrize("operation", ["sale", "resume"])
def test_actual_snapshot_freeze_wins_before_counter_can_issue_or_resume(online_goods: Any, operation: str) -> None:
    from outbound import goods_soh_reconciliation as counts
    from outbound.goods_soh_models import SohReconciliation
    from tests.test_soh_reconciliation import DECLARATIONS, repeat_source

    proof = online_goods
    site = proof.world.sites[0]
    wire, _ = bill(proof)
    source = repeat_source(proof, qty=2, pause=True)
    ready, started, release = threading.Event(), threading.Event(), threading.Event()
    pids: dict[str, int] = {}
    body = copy.deepcopy(DECLARATIONS)

    def freeze() -> str:
        close_old_connections()
        try:
            with tenant_context(proof.world.tenant.pk):
                pids["freeze"] = backend_pid()
                def begin(run: CommandRun) -> Any:
                    row = counts.begin(run, proof.warehouse, source, body, source.revision)
                    ready.set()
                    assert release.wait(timeout=20)
                    return row
                row = command(proof.warehouse, counts.RUN, begin)
                return str(row.pk)
        finally:
            close_old_connections()

    def counter_work() -> tuple[int, str]:
        close_old_connections()
        try:
            assert ready.wait(timeout=20)
            with tenant_context(proof.world.tenant.pk):
                pids["counter"] = backend_pid()
                started.set()
                if operation == "sale":
                    local = copy.copy(proof)
                    local.manager = SimpleNamespace(user=User.objects.get(pk=proof.manager.user.pk),
                        session=ServerSession.objects.get(pk=proof.manager.session.pk))
                    response = post(OnlineFinaliseView, local, wire)
                    return response.status_code, str(response.data["code"])
                try:
                    resume_till(site, proof.manager.user)
                except TillError as error:
                    return error.status, error.code
                return 200, "resumed"
        finally:
            close_old_connections()

    before = sum(bounds(row.portion)[1] - bounds(row.portion)[0]
                 for row in Position.objects.filter(site=site, boundary="physical"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        frozen, competing = pool.submit(freeze), pool.submit(counter_work)
        try:
            assert ready.wait(timeout=20) and started.wait(timeout=20)
            assert_blocked_by(pids["counter"], pids["freeze"])
        finally:
            release.set()
        count_id = frozen.result(timeout=20)
        result = competing.result(timeout=20)
    assert result == (409, "UNDER_COUNT")
    assert SohReconciliation.objects.filter(pk=count_id, state="frozen").exists()
    assert str(SiteGuard.objects.get(site=site).freeze_id) == count_id
    assert not Sale.objects.exists() and not CashLedgerEntry.objects.exists()
    assert sum(bounds(row.portion)[1] - bounds(row.portion)[0]
               for row in Position.objects.filter(site=site, boundary="physical")) == before
    assert not read_shelf(site).quantities, "The freeze withholds sale eligibility without changing custody."


def test_actual_counter_resume_wins_and_snapshot_refuses_missing_pause(online_goods: Any) -> None:
    from outbound import goods_soh_reconciliation as counts
    from outbound.goods_soh_models import SohReconciliation
    from tests.test_soh_reconciliation import DECLARATIONS, repeat_source

    proof = online_goods
    site = proof.world.sites[0]
    source = repeat_source(proof, qty=2, pause=True)
    ready, started, release = threading.Event(), threading.Event(), threading.Event()
    pids: dict[str, int] = {}
    body = copy.deepcopy(DECLARATIONS)

    def resume() -> None:
        close_old_connections()
        try:
            with tenant_context(proof.world.tenant.pk), transaction.atomic():
                pids["resume"] = backend_pid()
                resume_till(site, proof.manager.user)
                ready.set()
                assert release.wait(timeout=20)
        finally:
            close_old_connections()

    def freeze() -> str:
        close_old_connections()
        try:
            assert ready.wait(timeout=20)
            with tenant_context(proof.world.tenant.pk):
                pids["freeze"] = backend_pid()
                started.set()
                try:
                    command(proof.warehouse, counts.RUN,
                        lambda run: counts.begin(run, proof.warehouse, source, body, source.revision))
                except Refusal as error:
                    return error.code
                return "unexpectedly_frozen"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        resumed, frozen = pool.submit(resume), pool.submit(freeze)
        try:
            assert ready.wait(timeout=20) and started.wait(timeout=20)
            assert_blocked_by(pids["freeze"], pids["resume"])
        finally:
            release.set()
        resumed.result(timeout=20)
        result = frozen.result(timeout=20)
    assert result == "TILL_PAUSE_REQUIRED"
    assert not SohReconciliation.objects.exists() and SiteGuard.objects.get(site=site).freeze_id is None
    assert not Sale.objects.exists() and sum(read_shelf(site).quantities.values()) == 3
