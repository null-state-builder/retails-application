"""Closing the business day through the real online sale writer and cash count.

Behaviour: every open step refuses the close with its own sentence; a ready day
closes once with a manager's own PIN, audited, and asking again answers with
the same close; a bill accepted after the close belongs to the next business
day. Denials: a wrong or missing PIN, a person who is not a manager here, and a
login that is not this store's counter change nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, cast

import pytest
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.till_pin import hash_till_pin
from core.kernel_models import AuditEvent
from masters.store_feature_models import StoreFeatureSwitch
from sell.cash_views import CashCountsView
from sell.day_close_models import DayClose
from sell.day_close_views import DayCloseCloseView, DayCloseView, DaySummaryCheckView
from sell.models import HeldBill, Sale
from sell.services.day_close import CLOSE_ACTION, carried_in, dashboard_state, day_bills
from sell.services.register import register_state
from sell.views import OnlineFinaliseView
from tests.test_first_store_online import bill, online_goods as online_goods, post
from tests.test_so03_denials import _assign, _person, worlds as worlds

PIN = "2468"


@pytest.fixture
def ready(online_goods: Any, settings: Any) -> Any:
    """One accepted cash bill, the cash count switched on, and a manager with a PIN."""
    # The business day is the calendar day here, whatever hour the suite runs.
    settings.KDPS_CASH_DAY_CUTOFF_HOURS = 0
    proof = online_goods
    world, site = proof.world, proof.world.sites[0]
    wire, _ = bill(proof, discount=10000)
    assert post(OnlineFinaliseView, proof, wire).status_code == 201
    StoreFeatureSwitch.objects.create(
        tenant=world.tenant, site=site, feature_key="cash-count", enabled=True,
        updated_at=timezone.now(),
    )
    role = world.roles["store_person"]
    policy = dict(role.section_access)
    policy["sell"] = {"capability": "approve", "label": "Store manager"}
    role.section_access = policy
    role.save(update_fields=["section_access"])
    manager, human = _person(world, "day-close-manager")
    _assign(world, human, "store_person", sites=(site,), all_brands=True)
    manager.till_pin_hash = hash_till_pin(PIN)
    manager.save(update_fields=["till_pin_hash"])
    proof.closing_manager = manager
    proof.site = site
    return proof


def call(view: Any, proof: Any, body: dict[str, Any] | None = None, *, who: Any = None) -> Response:
    actor = who or proof.manager
    factory = APIRequestFactory()
    request = (
        factory.get("/api/sell/day-close")
        if body is None
        else factory.post("/api/sell/day-close", body, format="json")
    )
    force_authenticate(request, user=actor.user, token=actor.session)
    return cast(Response, view.as_view()(request))


def state(proof: Any) -> dict[str, Any]:
    response = call(DayCloseView, proof)
    assert response.status_code == 200, response.data
    return cast(dict[str, Any], response.data)


def count_cash(proof: Any) -> None:
    body = {"id": str(uuid.uuid4()), "notes": {"500": 1, "200": 2, "100": 0, "50": 0, "20": 0, "10": 0},
            "coins_paise": 0, "opening_paise": 0, "expected_paise": 90000,
            "till_number": proof.till.counter_id}
    saved = call(CashCountsView, proof, body)
    assert saved.status_code == 201 and saved.data["variance_paise"] == 0, saved.data


def tick(proof: Any) -> Response:
    return call(DaySummaryCheckView, proof, {"id": str(uuid.uuid4()),
                                             "business_day": state(proof)["business_day"]})


def close(proof: Any, *, pin: str = PIN, manager: Any = None, pending: int = 0) -> Response:
    chosen = manager if manager is not None else proof.closing_manager
    return call(DayCloseCloseView, proof, {
        "id": str(uuid.uuid4()), "business_day": state(proof)["business_day"],
        "approved_by": chosen.pk, "manager_pin": pin, "counter_pending": pending,
        "till_number": proof.till.counter_id,
    })


def steps(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in body["steps"]}


@pytest.mark.parametrize("open_step,code,sentence", [
    ("pending", "BILLS_UNSYNCED", "has 2 bills waiting. Sync"),
    ("hole", "BILLS_UNSYNCED", "Head office is missing 1 numbered bill."),
    ("held", "HELD_BILLS_OPEN", "1 bill is on hold. Resume or let go of each on Billing"),
    ("cash", "CASH_NOT_COUNTED", "The drawer is not counted yet."),
    ("summary", "SUMMARY_NOT_CHECKED", "Read the Day summary and tick it as checked"),
    ("changed", "SUMMARY_CHANGED", "Bills changed since the Day summary was checked."),
])
def test_each_open_step_refuses_the_close_and_says_what_to_do(
    ready: Any, open_step: str, code: str, sentence: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = ready
    if open_step != "cash":
        count_cash(proof)
    if open_step != "summary":
        assert tick(proof).status_code == 201
    if open_step == "hole":
        # A number the counter printed and head office never received. The
        # online writer refuses a gap, so the register's answer stands in for it.
        monkeypatch.setattr("sell.services.day_close.register_state",
                            lambda store: replace(register_state(store), holes=[2], hole_count=1))
    if open_step == "held":
        HeldBill.objects.create(store=proof.site, held_uuid=uuid.uuid4(), held_at=timezone.now())
        # A cart the store kept for tomorrow is an answer, and does not block.
        HeldBill.objects.create(store=proof.site, held_uuid=uuid.uuid4(), held_at=timezone.now(),
                                expires_policy=HeldBill.ExpiresPolicy.KEPT)
    if open_step == "changed":
        second, _ = bill(proof, seq=2)
        assert post(OnlineFinaliseView, proof, second).status_code == 201
    body = state(proof)
    failing = [row for row in body["steps"] if not row["done"] and row["key"] != "close"]
    if open_step == "pending":
        # The counter's own queue is on the device; the server hears it in the request.
        assert body["can_close"] is True and failing == []
    else:
        assert body["can_close"] is False and body["closed"] is False
        assert [row["code"] for row in failing] == [code], failing
    refused = close(proof, pending=2 if open_step == "pending" else 0)
    assert refused.status_code == 409 and refused.data["code"] == code, refused.data
    assert sentence in refused.data["error"], refused.data
    assert not DayClose.objects.filter(closed_at__isnull=False).exists()
    assert not AuditEvent.objects.filter(action=CLOSE_ACTION, outcome="succeeded").exists()


def test_ready_day_closes_once_with_manager_pin_and_repeat_answers_with_it(ready: Any) -> None:
    proof = ready
    count_cash(proof)
    ticked = tick(proof)
    assert ticked.status_code == 201 and steps(ticked.data)["summary"]["done"] is True
    # Ticking an unchanged summary again records nothing new.
    assert tick(proof).status_code == 200
    before = state(proof)
    assert before["can_close"] is True and before["summary"]["bills"] == 1
    assert all(row["done"] for row in before["steps"] if row["key"] != "close")

    first = close(proof)
    assert first.status_code == 201, first.data
    assert first.data["closed"] is True and steps(first.data)["close"]["done"] is True
    row = DayClose.objects.get()
    assert row.confirmed_by_id == proof.closing_manager.pk and row.closed_by_id == proof.manager.user.pk
    assert row.cash_count is not None and row.cash_count.counted_paise == 90000
    assert row.facts["bills"] == 1 and row.facts["net_paise"] == 90000
    assert row.facts["tenders"] == {"cash": 90000}
    audit = AuditEvent.objects.get(action=CLOSE_ACTION, outcome="succeeded")
    assert audit.site_id == proof.site.pk and PIN not in str(audit.after) and PIN not in str(audit.before)
    assert dashboard_state(proof.site)["state"] == "closed"

    again = close(proof, pin="0000")
    assert again.status_code == 200 and again.data["record"]["closed_at"] == first.data["record"]["closed_at"]
    assert AuditEvent.objects.filter(action=CLOSE_ACTION, outcome="succeeded").count() == 1
    late_tick = tick(proof)
    assert late_tick.status_code == 409 and late_tick.data["code"] == "DAY_CLOSED"

    # Billing goes on after the close, into the next business day. The closed
    # day's figures stay as they were frozen; the bill is carried forward.
    day = row.business_day
    late, _ = bill(proof, seq=2)
    assert post(OnlineFinaliseView, proof, late).status_code == 201
    assert day_bills(proof.site, day).count() == 1
    assert carried_in(proof.site, day.fromordinal(day.toordinal() + 1)).count() == 1
    after = state(proof)
    assert after["after_close"]["bills"] == 1 and after["record"]["facts"]["bills"] == 1
    assert Sale.objects.filter(store=proof.site).count() == 2


def test_wrong_or_missing_pin_and_non_manager_leave_the_day_open(ready: Any) -> None:
    proof = ready
    count_cash(proof)
    assert tick(proof).status_code == 201
    missing = close(proof, pin="")
    assert missing.status_code == 400 and missing.data["code"] == "MANAGER_PIN_NEEDED"
    outsider = close(proof, manager=proof.warehouse.user)
    assert outsider.status_code == 403 and outsider.data["code"] == "NOT_A_MANAGER"
    for _ in range(3):
        wrong = close(proof, pin="1357")
        assert wrong.status_code == 403 and wrong.data["code"] == "WRONG_PIN"
    paused = close(proof)
    assert paused.status_code == 429 and paused.data["code"] == "PIN_PAUSED"
    assert not DayClose.objects.filter(closed_at__isnull=False).exists()
    assert not any(PIN in str(event.before) or "1357" in str(event.before)
                   for event in AuditEvent.objects.filter(action=CLOSE_ACTION))


@pytest.mark.parametrize("who", ["owner", "warehouse"])
def test_login_that_is_not_this_counter_cannot_read_tick_or_close(ready: Any, who: str) -> None:
    proof = ready
    count_cash(proof)
    actor = getattr(proof, who)
    read = call(DayCloseView, proof, who=actor)
    assert read.status_code == 403 and "steps" not in read.data
    body = {"id": str(uuid.uuid4()), "business_day": timezone.localdate().isoformat()}
    ticked = call(DaySummaryCheckView, proof, body, who=actor)
    assert ticked.status_code == 403
    closed = call(DayCloseCloseView, proof, {**body, "approved_by": proof.closing_manager.pk,
                                             "manager_pin": PIN}, who=actor)
    assert closed.status_code == 403
    assert not DayClose.objects.exists()
