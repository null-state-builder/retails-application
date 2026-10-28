"""The Wardrobe, Ratu Road (owner decisions, 28 Oct 2026).

One store and its warehouse built from ``docs/wardrobe-ratu-road/setup.json``
with the owner's named people, an Operations head role, and warehouse → store
transfers that need no second person. Synthetic tenant only.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.utils import timezone

from accounts import operations_head
from accounts.goods_demo import owner_user, seed_working_calendar
from accounts.goods_models import RoleAssignment
from accounts.goods_setup import bootstrap_deployment
from accounts.models import Role, User
from accounts.registration_services import deployment_key
from accounts.test_store_loader import Loader, OpeningDriver, access_for, catalogue_config, plan_from_setup
from accounts.unified_policy import role_actions
from approvals.goods_models import ApprovalDecision
from core.refusals import Refusal
from core.tenancy import tenant_context
from masters.goods_config import effective_at
from masters.models import Season, Store
from outbound import transfer_authority
from outbound.goods_models import GoodsTransfer, TransferDispatch
from outbound.goods_transfer_views import (
    DispatchAcceptView, DispatchCountView, DispatchSessionScanView, TransferDispatchView,
    TransferListCreateView, TransferPreparationOpenView, TransferSubmitView,
)
from ptmapper import pt_opening_services as quick
from ptmapper.pt_opening import evidence_workbook, parse_pt_files
from ptmapper.soh_parser import SourceRow
from sell.services.salespeople import active_salespeople
from sell.models import RegisteredTill
from sell.services.goods_stock import read_shelf
from stockledger.goods_models import ActiveReservation
from tests.first_store_goods import _publish_tenant_config, command
from tests.test_first_store_goods_operations import operational_goods as operational_goods
from tests.test_first_store_goods_operations import post, receiving_goods as receiving_goods, shelf, transfer_wire, wire
from tests.test_pt_opening import pt_book, pt_row, use_auto_decisions
from tests.test_so03_denials import worlds as worlds

pytestmark = pytest.mark.django_db

SETUP = Path(__file__).resolve().parents[2] / "docs" / "wardrobe-ratu-road" / "setup.json"
OWNER = "kdsingh@wardrobe.test"


@pytest.fixture
def wardrobe() -> Any:
    Season.objects.get_or_create(historical_unknown=True,
                                 defaults={"code": "UNKNOWN-HIST", "name": "Unknown historical season", "status": "closed"})
    tenant = bootstrap_deployment(deployment_key=deployment_key(), code="KDPS-TEST", name="The Wardrobe (test)",
                                  timezone_name="Asia/Kolkata", currency="INR", locale="en-IN", synthetic=True,
                                  admin_email=OWNER, admin_name="K D Singh", admin_staff_code="OWN-01",
                                  admin_password="Bootstrap@123")
    with tenant_context(tenant.pk):
        seed_working_calendar(tenant, User.objects.get(email=OWNER).human_id)
    loader = Loader(tenant, plan_from_setup(SETUP), None, lambda _line: None)
    warehouse, store = loader.run()
    return SimpleNamespace(tenant=tenant, store=store, warehouse=warehouse, loader=loader)


def grants(email: str) -> set[tuple[str, bool, tuple[int, ...]]]:
    human = User.objects.get(email=email).human_id
    return {(row.role.code, row.all_sites, tuple(sorted(row.site_ids))) for row in RoleAssignment.objects.filter(
        human_id=human, effective_to__isnull=True, revoked_at__isnull=True).select_related("role")}


def test_setup_file_builds_one_store_its_warehouse_and_the_named_people(wardrobe: Any) -> None:
    store, warehouse, tenant = wardrobe.store, wardrobe.warehouse, wardrobe.tenant
    with tenant_context(tenant.pk):
        assert (store.code, store.name, warehouse.code) == ("WRR", "The Wardrobe", "RNC-WH")
        assert Store.objects.filter(tenant=tenant, is_active=True).count() == 2  # one store, one warehouse
        owner = owner_user(tenant)
        assert owner.email == OWNER and owner.full_name == "K D Singh"  # the owner is found by role, not email
        assert RegisteredTill.objects.get(store=store, active=True).registered_by == owner
        assert grants("debanjan@wardrobe.test") == {("store_person", False, (store.pk,)),
                                                     ("warehouse", False, (warehouse.pk,))}
        assert grants("cashier2@wardrobe.test") == {("store_person", False, (store.pk,))}
        assert grants("priyo@wardrobe.test") == {("operations_head", True, ())}
        assert grants("mahender@wardrobe.test") == {("warehouse", False, (warehouse.pk,))}
        assert grants("preparer@wardrobe.test") == {("warehouse", False, tuple(sorted((store.pk, warehouse.pk)))),
                                                    ("it_admin", True, ())}
        assert User.objects.get(email="tanmay@wardrobe.test").full_name == "Tanmay"
        assert len(wardrobe.loader.created) == 7
        transfer = [v.payload for v in effective_at(tenant.pk, "approval", timezone.now())
                    if v.payload.get("action") == "pt.approve.transfer"]
        assert transfer and transfer[-1][transfer_authority.AUTO_FLAG] is True
        assert "operations_head" in transfer[-1]["roles"]

        sellers = ["Cashier 1", "Cashier 2", "Debanjan"]  # the named store people, no placeholder counters
        assert sorted(row.display_name for row in active_salespeople(store)) == sellers

        again = Loader(tenant, plan_from_setup(SETUP), None, lambda _line: None)
        again.run()
        assert again.created == {}  # a re-run creates nobody
        assert sorted(row.display_name for row in active_salespeople(store)) == sellers


def test_the_setup_file_opens_the_store_with_no_second_person(wardrobe: Any) -> None:
    from accounts.test_store_loader import OPENING_AUTO_FLAG, approval_policies, opening_auto_in_force

    tenant = wardrobe.tenant
    with tenant_context(tenant.pk):
        opening = [v.payload for v in effective_at(tenant.pk, "approval", timezone.now())
                   if v.payload.get("action") == "pt.approve.opening"]
        assert opening and opening[-1][OPENING_AUTO_FLAG] is True  # "opening": {"approval": false}
        owner_id = User.objects.get(email=OWNER).human_id
        approval_policies(tenant, owner_id)  # a later load keeps the store's decision ...
        assert opening_auto_in_force(tenant.pk)
        approval_policies(tenant, owner_id, auto_opening=False)  # ... and turning approvals back on removes it
        assert not opening_auto_in_force(tenant.pk)
        transfer = [v.payload for v in effective_at(tenant.pk, "approval", timezone.now())
                    if v.payload.get("action") == "pt.approve.transfer"]
        assert transfer[-1][transfer_authority.AUTO_FLAG] is True  # the transfer decision is untouched


def test_the_named_people_open_the_store_from_its_pt_files(wardrobe: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from approvals.goods_models import ApprovalRequest
    from core.outbox import drain

    use_auto_decisions(monkeypatch)
    store = wardrobe.store
    with tenant_context(wardrobe.tenant.pk):
        metadata, rows, _problems = parse_pt_files([("1426.xlsx", pt_book({"Sheet1": [pt_row("K1", 2)]}))])
        debanjan = "debanjan@wardrobe.test"
        driver = OpeningDriver(store.tenant, store, stager=lambda: access_for(debanjan),
                               preparer=lambda: access_for(debanjan), log=lambda _line: None)
        source = driver.stage(evidence_workbook(rows), "PT opening.xlsx", metadata, rows)
        for who in ("priyo@wardrobe.test", "cashier1@wardrobe.test", OWNER):
            with pytest.raises(Refusal) as denied:
                quick.request_load(access_for(who), source)
            assert denied.value.code == "ACTION_DENIED", who  # the uploader loads; no approval to give
        quick.request_load(access_for(debanjan), source)
        drain(wardrobe.tenant.pk)
        source.refresh_from_db()
        assert quick.status(source)["state"] == "done", quick.status(source)
        assert sum(read_shelf(store).quantities.values()) == 2
        preparer = User.objects.get(email="preparer@wardrobe.test").human_id
        assert source.prepared_by_id == preparer  # the job acts as the store's opening preparer
        makers = set(ApprovalRequest.objects.filter(site_id=store.pk).values_list("maker_id", flat=True))
        assert makers == {preparer}


def test_legacy_season_spellings_become_the_two_seasons_a_year(wardrobe: Any) -> None:
    tenant, store = wardrobe.tenant, wardrobe.store
    spellings = ["AUTUMN WINTER(Oct-26)", "AW26", "AW-26", "AUTUMN WINTER(Jan-26)", "SS25", "", "CORE"]
    rows = [SourceRow(n, f"k{n}", f"B{n}", "KILLER", 1, 219900, 137638,
                      {"season": raw, "hsn": "62052000", "size": "M", "category": "CASUAL WEAR"})
            for n, raw in enumerate(spellings, start=1)]
    with tenant_context(tenant.pk):
        owner = owner_user(tenant).human_id
        config = catalogue_config(tenant, owner, [store], rows, cost="pt", log=lambda _line: None)
        seasons = {raw: Season.objects.get(pk=config["season_mappings"][raw]) for raw in spellings}
        assert {seasons[raw].code for raw in spellings[:3]} == {"AW-26"}
        assert seasons["AW26"].name == "Autumn-Winter 2026"
        assert seasons["AUTUMN WINTER(Jan-26)"].code == "AW-25"
        assert seasons["AUTUMN WINTER(Jan-26)"].status == Season.Status.CLOSED
        assert seasons["SS25"].code == "SS-25"
        assert seasons[""].historical_unknown and seasons["CORE"].historical_unknown
        assert set(config["unknown_season_sources"]) == {"", "CORE"}
        before = Season.objects.count()
        catalogue_config(tenant, owner, [store], rows, cost="pt", log=lambda _line: None)
        assert Season.objects.count() == before  # a re-run creates no seasons


def test_operations_head_reads_and_approves_transfers_and_counts_but_holds_no_money_or_setup(wardrobe: Any) -> None:
    with tenant_context(wardrobe.tenant.pk):
        role = Role.objects.get(tenant_id=wardrobe.tenant.pk, code=operations_head.ROLE_CODE)
        actions = role_actions(role)
        assert not role.is_system and role.field_access == ["cost"]
        assert {"pt.approve.transfer", "count.review", "section.sell.view", "section.reports.view",
                "stock.view"} <= actions
        assert not {"section.sell.operate", "section.setup.view", "section.money.view", "pt.approve.opening",
                    "opening.manifest.approve"} & actions


# -- warehouse → store transfers -----------------------------------------------


def auto_route(proof: Any) -> None:
    """Replace the in-force transfer route with one that lets warehouse → store through on submit."""
    from masters.goods_config import withdraw
    from masters.goods_models import ConfigVersion, EffectiveVersionPeriod

    withdrawn = EffectiveVersionPeriod.objects.filter(withdrawn_at__isnull=False).values_list("target_id", flat=True)
    for old in ConfigVersion.objects.filter(kind="approval", payload__action="pt.approve.transfer").exclude(pk__in=withdrawn):
        def change(run: Any, old: ConfigVersion = old) -> Any:
            return withdraw(run, old, reason_code="PROOF_REVIEWED_REPLACEMENT")
        command(proof.owner, "proof.policy.withdraw", change)
    _publish_tenant_config(proof.world.tenant, proof.owner.human_id, kind="approval", payload={
        "action": "pt.approve.transfer", "roles": ["owner"], "site_ids": [], "brand_ids": [],
        "require_distinct": True, "qty_max": 100, "value_max": "10000000", "step_up": True,
        "unknown_value": "refuse", transfer_authority.AUTO_FLAG: True}, label="wardrobe-auto-transfer")


def submit(proof: Any, qty: int = 2) -> GoodsTransfer:
    response = post(TransferListCreateView, proof.manager, transfer_wire(proof, qty))
    assert response.status_code == 201, response.data
    transfer = GoodsTransfer.objects.get(pk=response.data["id"])
    response = post(TransferSubmitView, proof.manager, wire(), pk=transfer.pk)
    assert response.status_code == 200, response.data
    transfer.refresh_from_db()
    return transfer


def test_a_warehouse_to_store_transfer_needs_no_second_person(operational_goods: Any) -> None:
    proof = operational_goods
    Store.objects.filter(pk=proof.world.sites[0].pk).update(store_type="warehouse")
    auto_route(proof)
    transfer = submit(proof)
    assert transfer.state == "approved"  # the sender's submit approved it
    request = transfer_authority.request_for(transfer)
    decision = ApprovalDecision.objects.get(request=request, outcome="approved")
    assert request.state == "approved" and decision.reason_code == transfer_authority.AUTO_REASON
    assert decision.checker_id == proof.manager.human_id and decision.result["auto"] is True

    response = post(TransferPreparationOpenView, proof.manager, wire(), pk=transfer.pk)
    assert response.status_code == 201, response.data
    preparation = response.data
    line = preparation["lines"][0]["line_key"]
    response = post(DispatchSessionScanView, proof.manager, wire(expected_revision=preparation["revision"], observations=[{
        "scan_key": str(uuid.uuid4()), "line_key": line, "qty": 2, "alias_value": proof.barcode}]),
        pk=uuid.UUID(preparation["id"]))
    assert response.status_code == 200, response.data
    scanned = response.data
    assert ActiveReservation.objects.exists() and shelf(proof) == 1  # reserved, exactly as after an approval
    response = post(TransferDispatchView, proof.manager, wire(
        lines=[{"line_key": line, "qty": 2}], dispatch_session_id=scanned["id"],
        dispatch_session_revision=scanned["revision"], dispatch_session_hash=scanned["content_hash"]), pk=transfer.pk)
    assert response.status_code == 201, response.data
    dispatched = TransferDispatch.objects.get(transfer=transfer)
    response = post(DispatchCountView, proof.receiver, wire(lines=[{"line_key": line, "good": 2}], excess=[]),
                    pk=transfer.pk, dispatch_id=dispatched.pk)
    assert response.status_code == 200, response.data
    response = post(DispatchAcceptView, proof.receiver, wire(lines=[{
        "line_key": line, "qty": 2, "destination_location_id": str(proof.destination_location.pk)}]),
        pk=transfer.pk, dispatch_id=dispatched.pk)
    assert response.status_code == 200, response.data
    assert shelf(proof, 1) == 2


def test_other_routes_still_wait_for_a_second_person(operational_goods: Any) -> None:
    proof = operational_goods
    auto_route(proof)  # both sites are stores here: a store → store move is not the exempt route
    transfer = submit(proof)
    assert transfer.state == "submitted" and not ActiveReservation.objects.exists()
    assert transfer_authority.request_for(transfer).state == "pending"
    for kind in ({"corrective_for_id": uuid.uuid4()}, {"custody": "quarantine"}):
        moved = SimpleNamespace(**{"corrective_for_id": None, "custody": "normal",
                                   "source_site": SimpleNamespace(store_type="warehouse"),
                                   "destination_site": SimpleNamespace(store_type="store"), **kind})
        assert not transfer_authority.auto_route(moved)  # type: ignore[arg-type]


def test_the_exemption_is_only_for_single_step_transfer_approval() -> None:
    from masters.goods_services import _check_rules

    base = {"roles": ["owner"], "site_ids": [], "brand_ids": [], "require_distinct": True,
            transfer_authority.AUTO_FLAG: True}
    with pytest.raises(Refusal) as other:
        _check_rules("approval", {**base, "action": "pt.approve.opening"}, None, None, timezone.now())
    assert other.value.code == "CONFIG_INVALID"
    with pytest.raises(Refusal) as stepped:
        _check_rules("approval", {**base, "action": "pt.approve.transfer",
                                  "steps": [{"label": "A", "roles": ["owner"]}, {"label": "B", "roles": ["owner"]}]},
                     None, None, timezone.now())
    assert stepped.value.code == "CONFIG_INVALID"
