"""Immutable PRD Appendix C V1–V17 answers at the real PT/transfer seams.

Expected paise and margins below are transcribed from docs/prd.md, Appendix C,
approved 14 September 2026. They are never generated from the implementation.
Rates/slabs are synthetic inputs, not real-tenant tax or product approval.
The unsaved configuration objects exercise price_line's serialization only;
the V17 journey separately uses reviewed, official and accepted proof stock.
"""
from __future__ import annotations

import io
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone
from openpyxl import Workbook

from approvals.goods_models import ApprovalRequest
from core.canonical import sha256_hex
from core.kernel_models import DocumentHead, OfficialLine
from core.numbering import prepare_series
from core.tenancy import tenant_context
from files.goods_services import stage_upload
from masters.goods_models import ConfigVersion, Location, SiteGuard
from outbound import transfers
from outbound.goods_models import TransferDispatch
from outbound.goods_transfer_views import DispatchAcceptView, DispatchCountView, TransferDispatchView
from ptmapper import goods_calc, goods_manifest_services as manifests, goods_pt_services, soh_services
from ptmapper.goods_models import GoodsPt
from stockledger import goods_acceptance
from stockledger.goods_models import JournalBatch, Origin, Position
from stockledger.goods_views import AcceptanceScanView
from tests.first_store_goods import _publish_tenant_config, actors, approve, command, live_access, reviewed_source
from tests.test_first_store_goods_operations import dispatch_wire, post, shelf, submitted_transfer, wire
from tests.test_so03_denials import _assign, _person
from tests.test_so03_denials import worlds as worlds


BASE = goods_calc.BASE_TO_TICKET
TICKET = goods_calc.TICKET_TO_PURCHASE
BOTH = goods_calc.BOTH_SUPPLIED
FIVE = Decimal("5")
TWELVE = Decimal("12")
ALL_FIVE = (goods_calc.Slab(0, None, True, False, FIVE, FIVE),)
ALL_TWELVE = (goods_calc.Slab(0, None, True, False, TWELVE, TWELVE),)
TWO_SLABS = (
    goods_calc.Slab(0, 100_000, True, True, FIVE, FIVE),
    goods_calc.Slab(100_000, None, False, False, TWELVE, TWELVE),
)
GAP_SLABS = (
    goods_calc.Slab(0, 100_000, True, True, FIVE, FIVE),
    goods_calc.Slab(110_000, None, True, False, TWELVE, TWELVE),
)


@dataclass(frozen=True)
class GoldenCase:
    id: str
    row: goods_calc.RowInput
    rates: goods_calc.Rates
    slabs: tuple[goods_calc.Slab, ...]
    cost: int | None
    mrp: int | None
    basic: int | None
    margin: str | None
    slab: int | None
    issues: frozenset[str] = frozenset()


def rates(transport: str = "5", margin: str = "30") -> goods_calc.Rates:
    return goods_calc.Rates(Decimal(transport), Decimal(margin))


# V6/V7 and invalid both-supplied rows do not use pricing margin or GST to
# derive cost/MRP. Their nonoperative profile fields are explicit test inputs.
CASES = (
    GoldenCase("V1", goods_calc.RowInput(BASE, basic_paise=9524), rates(), ALL_FIVE,
               10_000, 15_000, 9524, "33.33", 0),
    GoldenCase("V2", goods_calc.RowInput(BASE, basic_paise=1010), rates(), ALL_FIVE,
               1061, 1600, 1010, "33.69", 0),
    GoldenCase("V3", goods_calc.RowInput(BASE, basic_paise=9900), rates("0"), ALL_FIVE,
               9900, 14_900, 9900, "33.56", 0),
    GoldenCase("V4", goods_calc.RowInput(TICKET, mrp_paise=199_900), rates("0", "35"), ALL_TWELVE,
               116_013, 199_900, 116_013, "41.96", 0),
    GoldenCase("V5", goods_calc.RowInput(TICKET, mrp_paise=19_999), rates("0", "47.50"), ALL_FIVE,
               10_000, 19_999, 10_000, "50.00", 0),
    GoldenCase("V6", goods_calc.RowInput(BOTH, basic_paise=52_381, mrp_paise=99_900), rates("5", "0"), ALL_FIVE,
               55_000, 99_900, 52_381, "44.94", 0),
    GoldenCase("V7-opening", goods_calc.RowInput(BOTH, basic_paise=25_000, mrp_paise=49_900), rates("5", "0"), ALL_FIVE,
               26_250, 49_900, 25_000, "47.39", 0),
    GoldenCase("V8", goods_calc.RowInput(BASE, basic_paise=40_000), rates(), TWO_SLABS,
               42_000, 63_000, 40_000, "33.33", 0),
    GoldenCase("V9", goods_calc.RowInput(BASE, basic_paise=61_905), rates(), TWO_SLABS,
               65_000, 97_500, 61_905, "33.33", 0),
    GoldenCase("V10", goods_calc.RowInput(BASE, basic_paise=64_762), rates(), GAP_SLABS,
               68_000, None, 64_762, None, None, frozenset({"MRP_SLAB_GAP"})),
    GoldenCase("V11-MRP1000", goods_calc.RowInput(TICKET, mrp_paise=100_000), rates("0"), TWO_SLABS,
               66_667, 100_000, 66_667, "33.33", 0),
    GoldenCase("V11-MRP1001", goods_calc.RowInput(TICKET, mrp_paise=100_100), rates("0"), TWO_SLABS,
               62_563, 100_100, 62_563, "37.50", 1),
    GoldenCase("V12", goods_calc.RowInput(BOTH, basic_paise=100_000, mrp_paise=99_900), rates("5", "0"), ALL_FIVE,
               105_000, 99_900, 100_000, "-5.11", 0, frozenset({"COST_ABOVE_MRP"})),
    GoldenCase("V13-zero-cost", goods_calc.RowInput(BOTH, basic_paise=0, mrp_paise=15_000), rates("5", "0"), ALL_FIVE,
               0, 15_000, 0, "100.00", 0, frozenset({"COST_ZERO"})),
    GoldenCase("V13-zero-MRP", goods_calc.RowInput(BOTH, basic_paise=9524, mrp_paise=0), rates("5", "0"), ALL_FIVE,
               10_000, 0, 9524, None, 0, frozenset({"MRP_ZERO", "COST_ABOVE_MRP"})),
    GoldenCase("V14", goods_calc.RowInput(BASE, basic_paise=99_999_999_999), rates(), ALL_FIVE,
               104_999_999_999, None, 99_999_999_999, None, None, frozenset({"MONEY_OUT_OF_RANGE"})),
    GoldenCase("V15", goods_calc.RowInput(BASE, basic_paise=9524), rates("5", "100.00"), ALL_FIVE,
               None, None, 9524, None, None, frozenset({"PERCENT_OUT_OF_RANGE"})),
    GoldenCase("V16-zero-tolerance", goods_calc.RowInput(BASE, basic_paise=9524, check_p_rate_paise=10_001), rates(), ALL_FIVE,
               10_000, 15_000, 9524, "33.33", 0, frozenset({"DERIVED_MISMATCH"})),
    GoldenCase("V16-one-paisa-tolerance", goods_calc.RowInput(BASE, basic_paise=9524, check_p_rate_paise=10_001,
                                                         tolerance_minor_units=1), rates(), ALL_FIVE,
               10_000, 15_000, 9524, "33.33", 0),
)


def profile_context(case: GoldenCase) -> goods_pt_services.ProfileContext:
    instant = datetime(2026, 9, 14, tzinfo=UTC)
    return goods_pt_services.ProfileContext(
        version=ConfigVersion(id=uuid.uuid4(), payload={}),
        rates_version=ConfigVersion(id=uuid.uuid4(), payload={}),
        tax_version=ConfigVersion(id=uuid.uuid4(), payload={"hsn_rules": [{
            "hsn": "6205-G" if case.slabs == GAP_SLABS else "6205-T",
            "effective_from": instant.isoformat(),
            "slabs": [{
                "lower_paise": str(s.lower_paise),
                "upper_paise": None if s.upper_paise is None else str(s.upper_paise),
                "lower_inclusive": s.lower_inclusive, "upper_inclusive": s.upper_inclusive,
                "input_pct": str(s.input_pct), "output_pct": str(s.output_pct),
            } for s in case.slabs],
        }]}),
        rates=case.rates, directions=[BASE, TICKET, BOTH],
        tolerance=case.row.tolerance_minor_units, at=instant,
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_appendix_c_exact_answers_and_refusals_at_actual_calculation_and_pt_row_seam(case: GoldenCase) -> None:
    result = goods_calc.calculate(case.row, case.rates, case.slabs)
    assert (result.p_rate_paise, result.mrp_paise, result.basic_paise) == (case.cost, case.mrp, case.basic)
    assert result.margin_pct == (None if case.margin is None else Decimal(case.margin))
    assert result.slab_index == case.slab
    assert {issue.code for issue in result.issues} == case.issues
    assert result.valid is (not case.issues)
    if case.slab is not None:
        assert result.output_tax_pct == case.slabs[case.slab].output_pct

    profile = profile_context(case)
    supplied = {key: None if value is None else str(value) for key, value in {
        "basic_paise": case.row.basic_paise, "mrp_paise": case.row.mrp_paise,
        "check_p_rate_paise": case.row.check_p_rate_paise,
    }.items()}
    line = {"line_key": case.id, "hsn": profile.tax_version.payload["hsn_rules"][0]["hsn"],
            "supplied": supplied, "qty": 1}
    before = deepcopy(line)
    priced = goods_pt_services.price_line(line, profile, case.row.direction)
    assert line == before and priced["supplied"] == supplied
    assert priced["calculated"]["p_rate_paise"] == (None if case.cost is None else str(case.cost))
    assert priced["calculated"]["mrp_paise"] == (None if case.mrp is None else str(case.mrp))
    assert priced["calculated"]["basic_paise"] == (None if case.basic is None else str(case.basic))
    assert priced["calculated"]["margin_pct"] == case.margin
    assert {issue["code"] for issue in priced["issues"]} == case.issues
    assert all(issue["line_key"] == case.id for issue in priced["issues"])
    assert priced["profile_version_id"] == str(profile.version.pk)
    assert priced["rate_version_id"] == str(profile.rates_version.pk)
    assert priced["tax_version_id"] == str(profile.tax_version.pk)


def test_v15_invalid_margin_refuses_before_division(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_division(*args: Any, **kwargs: Any) -> int:
        pytest.fail("V15 must refuse the invalid percentage before deriving MRP.")
    monkeypatch.setattr(goods_calc, "mrp_from_cost", forbid_division)
    case = next(case for case in CASES if case.id == "V15")
    result = goods_calc.calculate(case.row, case.rates, case.slabs)
    assert not result.valid
    assert [issue.code for issue in result.issues] == ["PERCENT_OUT_OF_RANGE"]


def test_v17_canonical_transfer_copies_frozen_origin_values_without_purchase_formula(
    worlds: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        # Supply exactly V17's frozen origin values before any officialisation.
        # No accepted row, origin or previous policy is changed to fit the case.
        proof = reviewed_source(world, *actors(world), rows=[[
            "Shirt", "Old brand text", "M", "000123", "Shirt", "Legacy season", 3, 150, 100, 300,
        ]])
        # The shared helper's initial support note describes its default 1500
        # rupee source. Replace that unapproved review through canonical edits
        # with this vector's actual 300 rupee evidence before any approval.
        book = Workbook()
        assert book.active is not None
        book.active.append(["Fictional V17 evidence", "3 counted units; BASIC 100 INR each; source Amount 300 INR; MRP 150 INR"])
        output = io.BytesIO()
        book.save(output)
        evidence = stage_upload(proof.warehouse.principal(), command_id=uuid.uuid4(), data=output.getvalue(),
                                filename="v17-review.xlsx", kind="other", scope={"scope_kind": "sites",
                                "site_ids": [world.sites[0].pk], "brand_ids": [], "sensitive_fields": ["cost", "financial"]},
                                expected_sha256=sha256_hex(output.getvalue()), contains_fields=["cost", "financial"])
        config = {**proof.config, "valuation_evidence_id": str(evidence.pk), "reconciliation_evidence_id": str(evidence.pk),
                  "external_reconciliation_note": "3 fictional units, BASIC 100 INR each, source Amount 300 INR; no discrepancy"}
        proof.source = command(proof.warehouse, soh_services.PREPARE,
                               lambda run: soh_services.prepare(run, proof.warehouse, proof.source, config, proof.source.revision))
        proof.source = command(proof.warehouse, soh_services.PREPARE,
                               lambda run: soh_services.submit(run, proof.warehouse, proof.source, proof.source.revision))
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
        opening_version = DocumentHead.objects.get(document=identity).live_version
        assert opening_version is not None
        line = OfficialLine.objects.get(version=opening_version)
        proof.sku_id = uuid.UUID(line.payload["sku_id"])
        session, _ = command(proof.manager, "stock.accept", lambda run: goods_acceptance.open_session(
            run, site_id=world.sites[0].pk, version_id=opening_version.pk))
        response = post(AcceptanceScanView, proof.manager, wire(expected_revision=session.revision, observations=[{
            "scan_key": str(uuid.uuid4()), "official_line_id": str(line.pk), "alias_value": proof.barcode,
            "observed_ticket_mrp_paise": "15000", "label_evidence_id": str(proof.evidence.pk),
            "chosen_sku_id": str(proof.sku_id), "qty": 3, "condition": "good",
            "location_id": str(proof.location.pk), "outcome": "accepted_good", "actual_at": timezone.now().isoformat(),
        }]), pk=session.pk)
        assert response.status_code == 200, response.data
        origin = Origin.objects.get()
        assert (origin.unit_cost, origin.mrp) == (10_000, 15_000)
        saved_origins = [dict(row) for row in Origin.objects.order_by("pk").values()]
        saved_opening_lines = [dict(row) for row in OfficialLine.objects.filter(version=opening_version).values()]
        guard = SiteGuard.objects.get(site=world.sites[0])
        guard.goods_ready = True
        guard.lifecycle = SiteGuard.Lifecycle.ACTIVE
        guard.save(update_fields=["goods_ready", "lifecycle"])
        destination = world.sites[1]
        SiteGuard.objects.create(tenant=world.tenant, site=destination, stock_contract="goods_v1",
                                 goods_ready=True, lifecycle="active", selling_mode="online_alpha")
        for kind in Location.SYSTEM_KINDS:
            Location.objects.create(tenant=world.tenant, site=destination, name=kind, kind=kind, system=True)
        proof.destination_location = Location.objects.create(tenant=world.tenant, site=destination, name="Sales floor", kind="floor")
        receiver, receiver_human = _person(world, "v17-destination-person")
        _assign(world, receiver_human, "store_person", sites=(destination,), all_brands=True)
        proof.receiver = live_access(receiver)
        for kind in ("HLD", "REL", "TPT", "GRN"):
            prepare_series(world.tenant.pk, destination.gstin.legal_entity, kind)
        _publish_tenant_config(world.tenant, proof.owner.human_id, kind="approval", payload={
            "action": "pt.approve.transfer", "roles": ["owner"], "site_ids": [], "brand_ids": [],
            "require_distinct": True, "qty_max": 100, "value_max": "10000000", "step_up": True,
            "unknown_value": "refuse"}, label="v17-transfer-policy")

        def forbid_purchase_formula(*args: Any, **kwargs: Any) -> goods_calc.Calculated:
            pytest.fail("V17 transfer must copy frozen origin evidence, never reprice it.")
        monkeypatch.setattr(goods_calc, "calculate", forbid_purchase_formula)
        transfer = submitted_transfer(proof)
        body, line_key = dispatch_wire(proof, transfer)
        transfer_pt = GoodsPt.objects.get(transfer=transfer)
        transfer_head = DocumentHead.objects.get(document=transfer_pt.document)
        assert transfer_head.live_version_id is not None
        copied_lines = list(OfficialLine.objects.filter(version_id=transfer_head.live_version_id))
        assert len(copied_lines) == 1
        portions = copied_lines[0].payload["portions"]
        assert {piece["origin_id"] for piece in portions} == {str(origin.pk)}
        priced_lines = transfers.priced_lines(proof.owner, transfer, [copied_lines[0].payload])
        assert {(piece["unit_cost_paise"], piece["mrp_paise"]) for piece in priced_lines[0]["portions"]} == {("10000", "15000")}
        response = post(TransferDispatchView, proof.manager, body, pk=transfer.pk)
        assert response.status_code == 201, response.data
        dispatched = TransferDispatch.objects.get(transfer=transfer)
        response = post(DispatchCountView, proof.receiver, wire(lines=[{"line_key": line_key, "good": 2}], excess=[]),
                        pk=transfer.pk, dispatch_id=dispatched.pk)
        assert response.status_code == 200, response.data
        accept = wire(lines=[{"line_key": line_key, "qty": 2, "destination_location_id": str(proof.destination_location.pk)}])
        response = post(DispatchAcceptView, proof.receiver, accept, pk=transfer.pk, dispatch_id=dispatched.pk)
        assert response.status_code == 200, response.data
        before_replay = JournalBatch.objects.count()
        replay = post(DispatchAcceptView, proof.receiver, accept, pk=transfer.pk, dispatch_id=dispatched.pk)
        assert replay.status_code == 200 and replay.data == response.data
        assert JournalBatch.objects.count() == before_replay
        assert shelf(proof) == 1 and shelf(proof, 1) == 2
        assert set(Position.objects.filter(site=destination, accepted_event__isnull=False).values_list("origin_id", flat=True)) == {origin.pk}
        assert [dict(row) for row in Origin.objects.order_by("pk").values()] == saved_origins
        assert [dict(row) for row in OfficialLine.objects.filter(version=opening_version).values()] == saved_opening_lines
