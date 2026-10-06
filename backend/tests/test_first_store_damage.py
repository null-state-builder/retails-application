"""DMG-01 at the canonical stock, damage-review and online-sale writers.

Reviewed opening stock is accepted by the shared real-backend fixture. Damage
and correction effects below always pass through the mounted command handlers.
Synthetic commercial configuration is not a real-shop approval.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from accounts.goods_models import RoleAssignment
from accounts.sessions import revoke_session
from core.canonical import content_hash
from core.gl import GLEntry
from core.kernel_models import DocumentHead, DocumentIdentity, OfficialLine, OfficialVersion
from core.numbering import prepare_series
from finledger.models import CashLedgerEntry
from masters.goods_models import Location
from outbound.goods_models import DamageReport, GoodsMovement
from outbound.goods_views import DamageReportDecideView, MarkDamagedView
from sell.models import OnlineSaleSubmission, Sale, SaleLine, SaleTender
from sell.views import OnlineFinaliseView
from stockledger.goods_models import ActiveHold, ActiveReservation, JournalBatch, Origin, Position, QuantityLeg, ValueLeg
from tests.test_first_store_goods_operations import damage_wire, post, shelf, wire
from tests.test_first_store_online import bill, online_goods as online_goods, post as sale_post
from tests.test_so03_denials import worlds as worlds


def protected() -> str:
    """Refusal evidence may append; protected stock/documents/money may not."""
    models = (DocumentIdentity, DocumentHead, OfficialVersion, OfficialLine,
              Origin, Position, ActiveHold, ActiveReservation, JournalBatch,
              QuantityLeg, ValueLeg, DamageReport, GoodsMovement,
              Sale, SaleLine, SaleTender, GLEntry, CashLedgerEntry)
    return content_hash({model._meta.label: list(model.objects.order_by("pk").values())
                         for model in models})


def prepare_damage(proof: Any) -> None:
    site = proof.world.sites[0]
    for kind in ("HLD", "REL"):
        prepare_series(proof.world.tenant.pk, site.gstin.legal_entity, kind)


@pytest.mark.parametrize("boundary", ["site", "brand", "logout"])
def test_damage_scope_refusal_has_no_protected_mutation(online_goods: Any, boundary: str) -> None:
    proof = online_goods
    prepare_damage(proof)
    if boundary == "logout":
        revoke_session(proof.manager.session)
    else:
        assignment = RoleAssignment.objects.get(human_id=proof.manager.human_id)
        if boundary == "site":
            assignment.site_ids = [proof.world.sites[1].pk]
        else:
            assignment.all_brands = False
            assignment.brand_ids = [proof.world.brands[1].pk]
        assignment.save()
    before = protected()
    response = post(MarkDamagedView, proof.manager, damage_wire(proof))
    # A hidden read-back must never mask a command that already changed stock.
    assert protected() == before, response.data
    assert response.status_code == (401 if boundary == "logout" else 404), response.data
    assert response.data["code"] == ("AUTH_REQUIRED" if boundary == "logout" else "NOT_FOUND"), response.data
    assert shelf(proof) == 3


def test_damage_report_exact_replay_and_changed_replay_preserve_original_cost(online_goods: Any) -> None:
    proof = online_goods
    prepare_damage(proof)
    origins = content_hash(list(Origin.objects.order_by("id").values()))
    value_legs = content_hash(list(ValueLeg.objects.order_by("id").values()))
    accepted = list(Position.objects.filter(site=proof.world.sites[0], boundary="physical")
                    .values_list("lot_id", "origin_id", "accepted_event_id"))
    before_batches = JournalBatch.objects.count()
    body = damage_wire(proof)
    first = post(MarkDamagedView, proof.manager, body)
    assert first.status_code == 201, first.data
    report = DamageReport.objects.get(reporter_id=proof.manager.human_id)
    quarantine = Location.objects.get(site=proof.world.sites[0], kind="quarantine")
    damaged = Position.objects.get(site=proof.world.sites[0], condition="damaged")
    assert damaged.portion.upper - damaged.portion.lower == 1
    assert damaged.location_id == quarantine.pk and damaged.boundary == "physical"
    assert (damaged.lot_id, damaged.origin_id, damaged.accepted_event_id) in accepted
    assert report.state == "pending" and report.quantity == 1
    assert shelf(proof) == 2 and ActiveHold.objects.filter(lot_id=damaged.lot_id, kind="damage").exists()
    assert JournalBatch.objects.count() == before_batches + 1
    assert content_hash(list(Origin.objects.order_by("id").values())) == origins
    assert content_hash(list(ValueLeg.objects.order_by("id").values())) == value_legs
    held = protected()
    repeated = post(MarkDamagedView, proof.manager, body)
    assert repeated.status_code == 201, repeated.data
    assert {key: value for key, value in repeated.data.items() if key != "as_of"} == {
        key: value for key, value in first.data.items() if key != "as_of"}
    changed = copy.deepcopy(body)
    changed["lines"][0]["qty"] = 2
    conflict = post(MarkDamagedView, proof.manager, changed)
    assert conflict.status_code == 409 and conflict.data["code"] == "COMMAND_CONFLICT", conflict.data
    assert protected() == held


@pytest.mark.parametrize("decision", ["confirm", "reject"])
def test_only_independent_authorised_damage_decision_may_release_and_replay_once(
    online_goods: Any, decision: str,
) -> None:
    proof = online_goods
    prepare_damage(proof)
    original = content_hash(list(Origin.objects.order_by("id").values()))
    assert post(MarkDamagedView, proof.manager, damage_wire(proof)).status_code == 201
    report = DamageReport.objects.get(reporter_id=proof.manager.human_id)
    held = protected()
    denied = post(DamageReportDecideView, proof.manager,
                  wire(decision="reject", reason="Unauthorized release"), pk=report.pk)
    assert denied.status_code == 403 and denied.data["code"] == "ACTION_DENIED", denied.data
    assert protected() == held
    proof.owner.session.step_up_at = None
    proof.owner.session.save(update_fields=["step_up_at"])
    denied = post(DamageReportDecideView, proof.owner,
                  wire(decision=decision, reason="No password confirmation"), pk=report.pk)
    assert denied.status_code == 403 and denied.data["code"] == "STEP_UP_REQUIRED", denied.data
    assert protected() == held
    # Restore only the fixture's pre-existing confirmation time: no workflow
    # shortcut is under test; browser proof separately enters the password.
    proof.owner.session.step_up_at = proof.owner.session.issued_at
    proof.owner.session.save(update_fields=["step_up_at"])
    body = wire(decision=decision, reason="Independent physical review")
    batches, values = JournalBatch.objects.count(), ValueLeg.objects.count()
    first = post(DamageReportDecideView, proof.owner, body, pk=report.pk)
    assert first.status_code == 200, first.data
    report.refresh_from_db()
    assert report.state == ("confirmed" if decision == "confirm" else "rejected")
    assert report.reporter_id == proof.manager.human_id and report.reviewer_id == proof.owner.human_id
    assert shelf(proof) == (2 if decision == "confirm" else 3)
    assert JournalBatch.objects.count() == batches + (decision == "reject")
    assert ValueLeg.objects.count() == values
    assert content_hash(list(Origin.objects.order_by("id").values())) == original
    if decision == "reject":
        assert report.release_movement_id is not None
        assert not Position.objects.filter(condition="damaged").exists()
    else:
        assert report.release_movement_id is None
        assert Position.objects.filter(condition="damaged").exists()
    decided = protected()
    assert post(DamageReportDecideView, proof.owner, body, pk=report.pk).status_code == 200
    again = post(DamageReportDecideView, proof.owner,
                 wire(decision=decision, reason="Another command"), pk=report.pk)
    assert again.status_code == 409 and again.data["code"] == "STATE_CONFLICT", again.data
    assert protected() == decided


def test_online_sale_cannot_consume_quarantined_portion_or_post_money(online_goods: Any) -> None:
    proof = online_goods
    prepare_damage(proof)
    assert post(MarkDamagedView, proof.manager, damage_wire(proof)).status_code == 201
    assert shelf(proof) == 2
    body, _ = bill(proof, qty=3)
    held = protected()
    first = sale_post(OnlineFinaliseView, proof, body)
    assert first.status_code == 422 and first.data["code"] == "INSUFFICIENT_ELIGIBLE_STOCK", first.data
    submission = OnlineSaleSubmission.objects.get(idempotency_uuid=body["idempotency_uuid"])
    assert submission.status == "rejected" and submission.sale_id is None
    assert submission.rejection_code == "INSUFFICIENT_ELIGIBLE_STOCK"
    assert protected() == held
    again = sale_post(OnlineFinaliseView, proof, body)
    assert again.status_code == first.status_code and again.data == first.data
    assert OnlineSaleSubmission.objects.count() == 1
    assert protected() == held and shelf(proof) == 2
