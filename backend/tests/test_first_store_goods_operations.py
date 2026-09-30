"""Actual receipt acceptance, damage and transfer writers on disposable stock.

These cases start with reviewed SOH and an approved opening PT. Every stock
effect under test passes through its mounted goods handler and command kernel;
no legacy balance or mocked journal supplies the result.
"""
from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_models import RoleAssignment
from accounts.sessions import revoke_session
from approvals.goods_models import ApprovalRequest
from core.kernel_models import DocumentHead, OfficialLine
from core.numbering import prepare_series
from core.tenancy import tenant_context
from inbound.goods_models import CountSession, GoodsGrn
from inbound.goods_views import GoodsArrivalListCreateView, GoodsArrivalSessionView, GoodsGrnListCreateView, GoodsObservationView
from masters.goods_identity_models import ProductSku, SkuAlias, Style
from masters.goods_models import Location, SiteGuard
from outbound.goods_models import DamageReport, GoodsTransfer, TransferDispatch
from outbound.goods_transfer_views import (
    DispatchAcceptView, DispatchCountView, DispatchSessionScanView,
    TransferApproveView, TransferDispatchView, TransferListCreateView,
    TransferPreparationOpenView, TransferSubmitView,
)
from outbound.goods_views import DamageReportDecideView, MarkDamagedView
from ptmapper import goods_manifest_services as manifests, soh_services
from sell.services.goods_stock import barcode_aliases, read_shelf, sku_for_barcode
from stockledger import goods_acceptance
from stockledger.goods_models import ActiveHold, ActiveReservation, JournalBatch, Origin, Position
from stockledger.goods_views import AcceptanceScanView
from tests.first_store_goods import actors, approve, command, live_access, reviewed_source
from tests.test_so03_denials import _assign, _person
from tests.test_so03_denials import worlds as worlds
from vendors.models import Vendor


def post(view: Any, actor: Any, body: dict[str, Any], **kwargs: Any) -> Any:
    request = APIRequestFactory().post("/proof/goods-operations", body, format="json")
    force_authenticate(request, actor.user, actor.session)
    return view.as_view()(request, **kwargs)


def wire(**body: Any) -> dict[str, Any]:
    return {"command_id": str(uuid.uuid4()), "contract_version": "goods-v1", **body}


def shelf(proof: Any, index: int = 0) -> int:
    return sum(read_shelf(proof.world.sites[index]).quantities.values())


def truth() -> tuple[int, int, int, int]:
    return (JournalBatch.objects.count(), Origin.objects.count(),
            ActiveHold.objects.count(), ActiveReservation.objects.count())


def accepted_quantity(proof: Any) -> int:
    return sum(row.portion.upper - row.portion.lower for row in Position.objects.filter(
        site=proof.world.sites[0], accepted_event__isnull=False, boundary="physical"))


@pytest.fixture
def receiving_goods(worlds: Any) -> Iterator[Any]:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = reviewed_source(world, *actors(world))
        proof.foreign_world = worlds[1]
        approve(proof.owner, proof.source.approval_request)
        proof.source.refresh_from_db()
        batch = command(proof.warehouse, soh_services.PREPARE,
                        lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 1, proof.source.revision))
        manifest = batch.manifest
        approve(proof.owner, ApprovalRequest.objects.get(subject_kind="manifest", subject_key=str(manifest.pk), state="pending"))
        manifest.refresh_from_db()
        identity, head, pt = command(proof.warehouse, "pt.prepare.opening", lambda run: manifests.create_opening_draft(
            run, manifest=manifest, profile_version_id=proof.profile.pk, evidence_id=proof.evidence.pk))
        command(proof.warehouse, "pt.prepare.opening", lambda run: manifests.submit_opening(
            run, head, reviewed_hash=head.draft_revision.content_hash, goods_pt=pt))
        approve(proof.owner, ApprovalRequest.objects.get(subject_kind="document", subject_key=str(identity.pk), state="pending"))
        proof.version = DocumentHead.objects.get(document=identity).live_version
        proof.line = OfficialLine.objects.get(version=proof.version)
        proof.sku_id = uuid.UUID(proof.line.payload["sku_id"])
        proof.session, _ = command(proof.manager, "stock.accept", lambda run: goods_acceptance.open_session(
            run, site_id=world.sites[0].pk, version_id=proof.version.pk))
        yield proof


def observation(proof: Any, qty: int = 3) -> dict[str, Any]:
    return {"scan_key": str(uuid.uuid4()), "official_line_id": str(proof.line.pk),
            "alias_value": proof.barcode, "observed_ticket_mrp_paise": "100000",
            "label_evidence_id": str(proof.evidence.pk), "chosen_sku_id": str(proof.sku_id),
            "qty": qty, "condition": "good", "location_id": str(proof.location.pk),
            "outcome": "accepted_good", "actual_at": timezone.now().isoformat()}


@pytest.fixture
def operational_goods(receiving_goods: Any) -> Iterator[Any]:
    proof = receiving_goods
    accepted = post(AcceptanceScanView, proof.manager,
                    wire(expected_revision=proof.session.revision, observations=[observation(proof)]), pk=proof.session.pk)
    assert accepted.status_code == 200, accepted.data
    proof.session.refresh_from_db()
    guard = SiteGuard.objects.get(site=proof.world.sites[0])
    guard.goods_ready = True
    guard.lifecycle = SiteGuard.Lifecycle.ACTIVE
    guard.save(update_fields=["goods_ready", "lifecycle"])
    destination = proof.world.sites[1]
    SiteGuard.objects.create(tenant=proof.world.tenant, site=destination, stock_contract="goods_v1",
                             goods_ready=True, lifecycle="active", selling_mode="online_alpha")
    for kind in Location.SYSTEM_KINDS:
        Location.objects.create(tenant=proof.world.tenant, site=destination, name=kind, kind=kind, system=True)
    proof.destination_location = Location.objects.create(tenant=proof.world.tenant, site=destination, name="Sales floor", kind="floor")
    receiver, receiver_human = _person(proof.world, "proof-destination-manager")
    _assign(proof.world, receiver_human, "store_person", sites=(destination,), all_brands=True)
    proof.receiver = live_access(receiver)
    for kind in ("HLD", "REL", "TPT", "GRN"):
        prepare_series(proof.world.tenant.pk, destination.gstin.legal_entity, kind)
    yield proof


def test_acceptance_compares_entire_batch_before_any_stock_effect(receiving_goods: Any) -> None:
    proof = receiving_goods
    before = truth()
    valid, invalid = observation(proof, 1), observation(proof, 1)
    invalid["observed_ticket_mrp_paise"] = "99999"
    response = post(AcceptanceScanView, proof.manager,
                    wire(expected_revision=proof.session.revision, observations=[valid, invalid]), pk=proof.session.pk)
    assert response.status_code == 422, response.data
    assert response.data["code"] == "TAG_MISMATCH"
    assert truth() == before and shelf(proof) == 0
    proof.session.refresh_from_db()
    assert proof.session.revision == 1


def test_acceptance_replay_preserves_one_effect_and_disagreed_scan_is_refused(receiving_goods: Any) -> None:
    proof = receiving_goods
    scan = observation(proof)
    original = wire(expected_revision=proof.session.revision, observations=[scan])
    assert post(AcceptanceScanView, proof.manager, original, pk=proof.session.pk).status_code == 200
    after = truth()
    assert accepted_quantity(proof) == 3
    # A retry with the same command, and a re-delivery of the same scan with a
    # new command at the current revision, neither move the stock again.
    assert post(AcceptanceScanView, proof.manager, original, pk=proof.session.pk).status_code == 200
    assert post(AcceptanceScanView, proof.manager, wire(expected_revision=2, observations=[scan]), pk=proof.session.pk).status_code == 200
    changed = {**scan, "qty": 2}
    denied = post(AcceptanceScanView, proof.manager, wire(expected_revision=2, observations=[changed]), pk=proof.session.pk)
    assert denied.status_code == 422 and denied.data["code"] == "ACCEPTANCE_INVALID", denied.data
    assert truth() == after and accepted_quantity(proof) == 3


@pytest.mark.parametrize("boundary", ["site", "brand", "logout"])
def test_acceptance_denies_changed_assignment_and_session_without_stock_effect(receiving_goods: Any, boundary: str) -> None:
    proof = receiving_goods
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
    before = truth()
    response = post(AcceptanceScanView, proof.manager, wire(expected_revision=1, observations=[observation(proof)]), pk=proof.session.pk)
    assert response.status_code in (401, 403, 404), response.data
    assert truth() == before and shelf(proof) == 0


def damage_wire(proof: Any, qty: int = 1) -> dict[str, Any]:
    return wire(site_id=proof.world.sites[0].pk, reason_code="FOUND_DAMAGED", lines=[{
        "line_key": str(uuid.uuid4()), "sku_id": str(proof.sku_id), "qty": qty,
        "source_location_id": str(proof.location.pk),
    }])


@pytest.mark.parametrize("decision", ["confirm", "reject"])
def test_damage_reduces_availability_immediately_then_only_independent_decision_changes_it(
    operational_goods: Any, decision: str,
) -> None:
    proof = operational_goods
    mark = damage_wire(proof)
    before = truth()
    response = post(MarkDamagedView, proof.manager, mark)
    assert response.status_code == 201, response.data
    report = DamageReport.objects.get(reporter_id=proof.manager.human_id)
    assert shelf(proof) == 2 and report.state == "pending"
    marked = truth()
    assert marked[0] == before[0] + 1 and marked[1] == before[1]
    assert post(MarkDamagedView, proof.manager, mark).status_code == 201
    assert truth() == marked
    # This person is independently authorised as Owner too, but remains the
    # reporter; a second assignment cannot turn them into another human.
    RoleAssignment.objects.create(tenant=proof.world.tenant, human_id=proof.manager.human_id,
        role=proof.world.roles["owner"], all_sites=True, all_brands=True,
        effective_from=timezone.now())
    own = post(DamageReportDecideView, proof.manager, wire(decision=decision, reason="Reviewed physically"), pk=report.pk)
    assert own.status_code == 403 and own.data["code"] == "SELF_APPROVAL", own.data
    assert truth() == marked and shelf(proof) == 2
    response = post(DamageReportDecideView, proof.owner, wire(decision=decision, reason="Separate physical review"), pk=report.pk)
    assert response.status_code == 200, response.data
    report.refresh_from_db()
    assert report.state == ("confirmed" if decision == "confirm" else "rejected")
    assert report.reporter_id == proof.manager.human_id and report.reviewer_id == proof.owner.human_id
    assert shelf(proof) == (2 if decision == "confirm" else 3)
    assert JournalBatch.objects.count() == marked[0] + (decision == "reject")
    decided = truth()
    duplicate = post(DamageReportDecideView, proof.owner, wire(decision=decision, reason="Again"), pk=report.pk)
    assert duplicate.status_code == 409 and duplicate.data["code"] == "STATE_CONFLICT", duplicate.data
    assert truth() == decided


def test_damage_can_be_reported_during_count_but_decisions_wait(operational_goods: Any) -> None:
    proof = operational_goods
    guard = SiteGuard.objects.get(site=proof.world.sites[0])
    guard.freeze_id = uuid.uuid4()
    guard.save(update_fields=["freeze_id"])
    response = post(MarkDamagedView, proof.manager, damage_wire(proof))
    assert response.status_code == 201, response.data
    assert sum(row.portion.upper - row.portion.lower for row in Position.objects.filter(
        site=proof.world.sites[0], boundary="physical", condition="damaged")) == 1
    assert shelf(proof) == 0  # The whole site remains frozen for selling.
    report = DamageReport.objects.get(reporter_id=proof.manager.human_id)
    marked = truth()
    response = post(DamageReportDecideView, proof.owner, wire(decision="reject", reason="Wait for count"), pk=report.pk)
    assert response.status_code == 409 and response.data["code"] == "UNDER_COUNT", response.data
    assert truth() == marked
    report.refresh_from_db()
    assert report.state == "pending"


def transfer_wire(proof: Any, qty: int = 2) -> dict[str, Any]:
    return wire(source_site_id=proof.world.sites[0].pk, destination_site_id=proof.world.sites[1].pk,
                lines=[{"line_key": str(uuid.uuid4()), "sku_id": str(proof.sku_id), "qty": qty}])


def submitted_transfer(proof: Any, qty: int = 2) -> GoodsTransfer:
    response = post(TransferListCreateView, proof.manager, transfer_wire(proof, qty))
    assert response.status_code == 201, response.data
    transfer = GoodsTransfer.objects.get(pk=response.data["id"])
    assert post(TransferSubmitView, proof.manager, wire(), pk=transfer.pk).status_code == 200
    return transfer


def dispatch_wire(proof: Any, transfer: GoodsTransfer, qty: int = 2) -> tuple[dict[str, Any], str]:
    assert post(TransferApproveView, proof.owner, wire(reason="Separate proof review"), pk=transfer.pk).status_code == 200
    response = post(TransferPreparationOpenView, proof.manager, wire(), pk=transfer.pk)
    assert response.status_code == 201, response.data
    preparation = response.data
    line = preparation["lines"][0]["line_key"]
    response = post(DispatchSessionScanView, proof.manager,
        wire(expected_revision=preparation["revision"], observations=[{
            "scan_key": str(uuid.uuid4()), "line_key": line, "qty": qty, "alias_value": proof.barcode,
        }]), pk=uuid.UUID(preparation["id"]))
    assert response.status_code == 200, response.data
    scanned = response.data
    return wire(lines=[{"line_key": line, "qty": qty}], dispatch_session_id=scanned["id"],
                dispatch_session_revision=scanned["revision"], dispatch_session_hash=scanned["content_hash"]), line


def test_transfer_reserves_dispatches_receives_and_accepts_original_cost_once(operational_goods: Any) -> None:
    proof = operational_goods
    origin_ids = set(Origin.objects.values_list("pk", flat=True))
    transfer = submitted_transfer(proof)
    assert shelf(proof) == 3 and not ActiveReservation.objects.exists()
    body, line = dispatch_wire(proof, transfer)
    assert shelf(proof) == 1 and ActiveReservation.objects.exists()
    response = post(TransferDispatchView, proof.manager, body, pk=transfer.pk)
    assert response.status_code == 201, response.data
    dispatched = TransferDispatch.objects.get(transfer=transfer)
    before_replay = truth()
    assert post(TransferDispatchView, proof.manager, body, pk=transfer.pk).status_code == 201
    assert truth() == before_replay and TransferDispatch.objects.filter(transfer=transfer).count() == 1
    assert shelf(proof, 1) == 0 and not ActiveReservation.objects.exists()
    # The source manager can read the transfer but cannot receive for its destination.
    receive = wire(lines=[{"line_key": line, "good": 2}], excess=[])
    denied = post(DispatchCountView, proof.manager, receive, pk=transfer.pk, dispatch_id=dispatched.pk)
    assert denied.status_code in (403, 404) and denied.data["code"] in {"ACTION_DENIED", "NOT_FOUND"}, denied.data
    assert truth() == before_replay
    response = post(DispatchCountView, proof.receiver, receive, pk=transfer.pk, dispatch_id=dispatched.pk)
    assert response.status_code == 200, response.data
    assert shelf(proof, 1) == 0  # Receiving a piece does not yet put it on the shelf.
    accept = wire(lines=[{"line_key": line, "qty": 2, "destination_location_id": str(proof.destination_location.pk)}])
    response = post(DispatchAcceptView, proof.receiver, accept, pk=transfer.pk, dispatch_id=dispatched.pk)
    assert response.status_code == 200, response.data
    after = truth()
    assert post(DispatchAcceptView, proof.receiver, accept, pk=transfer.pk, dispatch_id=dispatched.pk).status_code == 200
    assert truth() == after and shelf(proof) == 1 and shelf(proof, 1) == 2
    assert sku_for_barcode(proof.world.sites[1], proof.barcode) == proof.sku_id
    assert set(Origin.objects.values_list("pk", flat=True)) == origin_ids
    assert Position.objects.filter(site=proof.world.sites[1], boundary="physical").exists()
    dispatched.refresh_from_db()
    assert dispatched.state == "accepted"


def test_transfer_approval_rechecks_physical_stock_after_damage(operational_goods: Any) -> None:
    proof = operational_goods
    transfer = submitted_transfer(proof, 3)
    response = post(MarkDamagedView, proof.manager, damage_wire(proof))
    assert response.status_code == 201, response.data
    before = truth()
    response = post(TransferApproveView, proof.owner, wire(), pk=transfer.pk)
    assert response.status_code == 409 and response.data["code"] == "INSUFFICIENT_ELIGIBLE_STOCK", response.data
    assert truth() == before and shelf(proof) == 2 and not ActiveReservation.objects.exists()


def test_transfer_dispatch_refuses_a_changed_preparation_without_effect(operational_goods: Any) -> None:
    proof = operational_goods
    transfer = submitted_transfer(proof)
    body, _line = dispatch_wire(proof, transfer)
    before = truth()
    stale = copy.deepcopy(body)
    stale["dispatch_session_hash"] = "0" * 64
    response = post(TransferDispatchView, proof.manager, stale, pk=transfer.pk)
    assert response.status_code == 409 and response.data["code"] == "REVISION_SUPERSEDED", response.data
    assert truth() == before and not TransferDispatch.objects.exists() and shelf(proof) == 1


@pytest.fixture
def received_transfer(operational_goods: Any) -> Iterator[Any]:
    proof = operational_goods
    transfer = submitted_transfer(proof)
    body, line = dispatch_wire(proof, transfer)
    response = post(TransferDispatchView, proof.manager, body, pk=transfer.pk)
    assert response.status_code == 201, response.data
    dispatched = TransferDispatch.objects.get(transfer=transfer)
    response = post(DispatchCountView, proof.receiver, wire(lines=[{"line_key": line, "good": 2}], excess=[]),
                    pk=transfer.pk, dispatch_id=dispatched.pk)
    assert response.status_code == 200, response.data
    assert sku_for_barcode(proof.world.sites[1], proof.barcode) is None
    assert shelf(proof, 1) == 0
    response = post(DispatchAcceptView, proof.receiver,
                    wire(lines=[{"line_key": line, "qty": 2, "destination_location_id": str(proof.destination_location.pk)}]),
                    pk=transfer.pk, dispatch_id=dispatched.pk)
    assert response.status_code == 200, response.data
    assert shelf(proof, 1) == 2
    yield proof


@pytest.mark.parametrize("boundary", ["retired_alias", "expired_alias", "pending_sku", "local_conflict", "other_label"])
def test_transferred_labels_do_not_bypass_current_identity_governance_or_ambiguity(received_transfer: Any, boundary: str) -> None:
    proof = received_transfer
    site = proof.world.sites[1]
    alias = SkuAlias.objects.get(sku_id=proof.sku_id)
    if boundary == "retired_alias":
        alias.governance_state = "retired"
        alias.save(update_fields=["governance_state"])
    elif boundary == "expired_alias":
        alias.effective_to = timezone.now()
        alias.save(update_fields=["effective_to"])
    elif boundary == "pending_sku":
        ProductSku.objects.filter(pk=proof.sku_id).update(governance_state="pending")
    else:
        sku = ProductSku.objects.get(pk=proof.sku_id)
        if boundary == "local_conflict":
            sku = ProductSku.objects.create(tenant=proof.world.tenant, style=sku.style,
                identity_key=uuid.uuid4().hex * 2, attrs=sku.attrs, identity_profile=sku.identity_profile, governance_state="effective")
        SkuAlias.objects.create(tenant=proof.world.tenant, sku=sku, issuer_key="reviewed-local-proof", site=site,
            alias_type="barcode", value=proof.barcode if boundary == "local_conflict" else "ANOTHER-REVIEWED-TAG",
            effective_from=timezone.now(), governance_state="effective")
    before = truth()
    assert shelf(proof, 1) == 0 and sku_for_barcode(site, proof.barcode) is None
    assert truth() == before


def test_routine_receiving_records_actual_count_without_replaying_opening_stock(operational_goods: Any) -> None:
    proof = operational_goods
    vendor = Vendor.objects.create(tenant=proof.world.tenant, code="proof-receiving", name="Proof incoming vendor")
    before = truth()
    arrived = post(GoodsArrivalListCreateView, proof.manager,
                   wire(site_id=proof.world.sites[0].pk, brand_id=proof.world.brands[0].pk,
                        vendor_id=vendor.pk, actual_arrival_at=timezone.now().isoformat(), transporter_ref="Proof delivery"))
    assert arrived.status_code == 201, arrived.data
    assert truth() == before and shelf(proof) == 3
    counted = post(GoodsArrivalSessionView, proof.manager,
                   wire(counter_id=str(proof.manager.human_id), entry_user_id=str(proof.manager.human_id)),
                   pk=uuid.UUID(arrived.data["id"]))
    assert counted.status_code == 201, counted.data
    session_id = uuid.UUID(counted.data["id"])
    observed = post(GoodsObservationView, proof.manager, wire(expected_revision=counted.data["revision"], observations=[
        {"scan_key": str(uuid.uuid4()), "sku_id": str(proof.sku_id), "description": "Counted original SKU",
         "alias_value": proof.barcode, "condition": "good", "qty": 1},
        {"scan_key": str(uuid.uuid4()), "sku_id": str(proof.sku_id), "description": "Damaged original SKU",
         "alias_value": proof.barcode, "condition": "damaged", "qty": 1},
    ]), pk=session_id)
    assert observed.status_code == 200, observed.data
    assert truth() == before and shelf(proof) == 3  # Count evidence is not stock.
    issue = wire(expected_revision=observed.data["revision"], count_session_id=str(session_id), reviewed_hash=observed.data["content_hash"])
    issued = post(GoodsGrnListCreateView, proof.manager, issue)
    assert issued.status_code == 201, issued.data
    session = CountSession.objects.get(pk=session_id)
    assert session.state == "issued" and GoodsGrn.objects.filter(count_session_id=session_id).count() == 1
    assert Origin.objects.count() == before[1]  # GRN counts quantity, never purchase value.
    assert shelf(proof) == 3  # Neither receiving nor quarantine bypasses PT and acceptance.
    report = DamageReport.objects.get(disposition__document_id=session.grn_id)
    assert report.state == "pending" and report.quantity == 1 and report.reporter_id == proof.manager.human_id
    after = truth()
    assert post(GoodsGrnListCreateView, proof.manager, issue).status_code == 201
    assert truth() == after and shelf(proof) == 3


def test_origin_label_does_not_propagate_to_other_sites_or_tenants(operational_goods: Any) -> None:
    proof = operational_goods
    assert barcode_aliases(proof.world.sites[0], timezone.now()) == {proof.sku_id: proof.barcode}
    assert barcode_aliases(proof.world.sites[1], timezone.now()) == {}
    before = truth()
    foreign = proof.foreign_world
    # The same printed label at another tenant is a different stable identity,
    # never a reason to borrow this origin or its accepted quantity.
    with tenant_context(foreign.tenant.pk):
        style = Style.objects.create(tenant=foreign.tenant, brand=foreign.brands[0], style_code="foreign-style",
                                     profile_family="fashion", governance_state="effective")
        foreign_sku = ProductSku.objects.create(tenant=foreign.tenant, style=style, identity_key=uuid.uuid4().hex * 2,
                                               governance_state="effective")
        SkuAlias.objects.create(tenant=foreign.tenant, sku=foreign_sku, issuer_key="foreign-proof-only",
            site=foreign.sites[0], alias_type="barcode", value=proof.barcode,
            effective_from=timezone.now(), governance_state="effective")
        assert barcode_aliases(foreign.sites[0], timezone.now()) == {foreign_sku.pk: proof.barcode}
        assert read_shelf(foreign.sites[0]).pieces == []
    assert truth() == before
