"""Fully accepted live PTs keep their existing session reachable until completion."""
from __future__ import annotations

from typing import Any

from rest_framework.test import APIRequestFactory, force_authenticate

from stockledger.goods_models import AcceptanceEvent, AcceptanceSession, JournalBatch, Origin, Position
from stockledger.goods_views import AcceptanceCompleteView, AcceptanceScanView, AcceptanceSessionCreateView, PendingAcceptanceView
from tests.first_store_goods import live_access
from tests.test_first_store_goods_operations import observation, post, receiving_goods as receiving_goods, wire
from tests.test_so03_denials import _assign, _person
from tests.test_so03_denials import worlds as worlds


def pending(actor: Any, site_id: int) -> Any:
    request = APIRequestFactory().get("/api/goods-v1/stockledger/pending-acceptance", {"site_id": site_id})
    force_authenticate(request, actor.user, actor.session)
    return PendingAcceptanceView.as_view()(request)


def stock_truth() -> tuple[Any, ...]:
    return (list(JournalBatch.objects.order_by("pk").values()), list(Origin.objects.order_by("pk").values()),
            list(Position.objects.order_by("pk").values()), list(AcceptanceEvent.objects.order_by("pk").values()))


def test_accepted_open_session_resumes_same_id_in_scope_then_completion_hides_it_without_posting(receiving_goods: Any) -> None:
    proof = receiving_goods
    site = proof.world.sites[0]
    scanned = post(AcceptanceScanView, proof.manager,
                   wire(expected_revision=proof.session.revision, observations=[observation(proof)]), pk=proof.session.pk)
    assert scanned.status_code == 200, scanned.data
    proof.session.refresh_from_db()
    assert proof.session.state == "open"
    before, session_count = stock_truth(), AcceptanceSession.objects.count()
    visible = pending(proof.manager, site.pk)
    assert visible.status_code == 200, visible.data
    assert [(row["official_version_id"], row["remaining_qty"]) for row in visible.data["items"]] == [(str(proof.version.pk), 0)]
    assert stock_truth() == before

    other, identity = _person(proof.world, "fictional-other-receiver")
    _assign(proof.world, identity, "store_person", sites=(proof.world.sites[1],), all_brands=True)
    receiver = live_access(other)
    hidden = pending(receiver, site.pk)
    assert hidden.status_code == 200 and hidden.data["items"] == []
    denied = post(AcceptanceSessionCreateView, receiver,
                  wire(site_id=site.pk, source_version_id=str(proof.version.pk)))
    assert denied.status_code == 404 and denied.data["code"] == "NOT_FOUND", denied.data
    assert stock_truth() == before and AcceptanceSession.objects.count() == session_count

    resumed = post(AcceptanceSessionCreateView, proof.manager,
                   wire(site_id=site.pk, source_version_id=str(proof.version.pk)))
    assert resumed.status_code == 201 and resumed.data["id"] == str(proof.session.pk), resumed.data
    assert stock_truth() == before and AcceptanceSession.objects.count() == session_count
    completed = post(AcceptanceCompleteView, proof.manager,
                     wire(expected_revision=proof.session.revision, confirm_complete=False), pk=proof.session.pk)
    assert completed.status_code == 200 and completed.data["data"]["state"] == "completed", completed.data
    assert pending(proof.manager, site.pk).data["items"] == []
    assert stock_truth() == before and AcceptanceSession.objects.count() == session_count
    proof.session.refresh_from_db()
    assert proof.session.state == "completed" and proof.session.events.exists()
