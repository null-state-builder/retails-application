"""Returned goods stay unavailable until scoped physical acceptance, exactly once."""
from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_models import RoleAssignment
from accounts.sessions import revoke_session
from masters.goods_models import Location, SiteGuard
from sell.models import SaleLine
from sell.services.goods_stock import read_shelf
from sell.services.returned_pieces import pending_returns
from sell.views import OnlineFinaliseView
from stockledger.goods_models import AcceptanceEvent, JournalBatch
from stockledger.goods_views import ReturnedPiecesView
from tests.test_first_store_exchange import exchange_wire
from tests.test_first_store_goods_operations import post as goods_post, wire
from tests.test_first_store_online import bill, post
from tests.test_first_store_online import online_goods as online_goods
from tests.test_so03_denials import worlds as worlds


def returned(proof: Any) -> SaleLine:
    original, _ = bill(proof)
    assert post(OnlineFinaliseView, proof, original).status_code == 201
    assert post(OnlineFinaliseView, proof, exchange_wire(proof, original, "good")).status_code == 201
    line = SaleLine.objects.get(direction="return")
    assert line.brand_ref_id == proof.world.brands[0].pk
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 1
    return line


def body(proof: Any, line: SaleLine) -> dict[str, Any]:
    return wire(site_id=proof.world.sites[0].pk, sale_line_ids=[line.pk], location_id=str(proof.location.pk))


def test_physical_acceptance_replays_once_and_preserves_original_origin(online_goods: Any) -> None:
    proof = online_goods
    line = returned(proof)
    before = JournalBatch.objects.count(), AcceptanceEvent.objects.count()
    command = body(proof, line)
    response = goods_post(ReturnedPiecesView, proof.manager, command)
    assert response.status_code == 200 and response.data["accepted_qty"] == 1, response.data
    assert not pending_returns({proof.world.sites[0].pk})
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 2
    assert JournalBatch.objects.count() == before[0] + 1
    assert AcceptanceEvent.objects.count() == before[1] + 1
    replay = goods_post(ReturnedPiecesView, proof.manager, command)
    assert replay.status_code == 200 and replay.data == response.data
    assert JournalBatch.objects.count() == before[0] + 1
    assert AcceptanceEvent.objects.count() == before[1] + 1
    again = goods_post(ReturnedPiecesView, proof.manager, body(proof, line))
    assert again.status_code == 404 and JournalBatch.objects.count() == before[0] + 1


@pytest.mark.parametrize("case", ["wrong_store", "system", "retired", "frozen", "brand", "unresolved", "logout"])
def test_unsafe_return_acceptance_changes_no_business_history(online_goods: Any, case: str) -> None:
    proof = online_goods
    line = returned(proof)
    command = body(proof, line)
    if case == "wrong_store":
        command["site_id"] = proof.world.sites[1].pk
    elif case == "system":
        command["location_id"] = str(Location.objects.get(site=proof.world.sites[0], kind="receiving").pk)
    elif case == "retired":
        from core.commands import database_now
        proof.location.retired_at = database_now()
        proof.location.save(update_fields=["retired_at"])
    elif case == "frozen":
        import uuid
        SiteGuard.objects.filter(site=proof.world.sites[0]).update(freeze_id=uuid.uuid4())
    elif case == "brand":
        RoleAssignment.objects.filter(human_id=proof.manager.human_id).update(all_brands=False, brand_ids=[proof.world.brands[1].pk])
    elif case == "unresolved":
        # An unresolved historical identity remains inactive; no label-based fallback.
        SaleLine.objects.filter(pk=line.pk).update(brand_ref=None)
    else:
        revoke_session(proof.manager.session)
    before = JournalBatch.objects.count(), AcceptanceEvent.objects.count()
    response = goods_post(ReturnedPiecesView, proof.manager, command)
    assert response.status_code in (401, 403, 404, 409, 422), response.data
    assert (JournalBatch.objects.count(), AcceptanceEvent.objects.count()) == before


def test_return_list_rechecks_revocation_before_delivery(online_goods: Any, monkeypatch: Any) -> None:
    proof = online_goods
    returned(proof)
    from accounts.principal import AccessContext
    original = AccessContext.revalidate_delivery

    def revoke(access: AccessContext) -> None:
        revoke_session(proof.manager.session)
        original(access)

    monkeypatch.setattr(AccessContext, "revalidate_delivery", revoke)
    request = APIRequestFactory().get("/proof/returned-pieces")
    force_authenticate(request, proof.manager.user, proof.manager.session)
    response = ReturnedPiecesView.as_view()(request)
    assert response.status_code == 401 and "barcode" not in str(response.data)
