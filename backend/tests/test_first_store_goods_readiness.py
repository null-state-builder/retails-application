"""Goods readiness cannot skip reviewed physical opening through lifecycle labels.

Synthetic proof only: the normal readiness handler, opening writers, stock
acceptance and retained evidence run together. Prior unsafe capability evidence
is kept as a historical fixture, never repaired by deleting or rewriting it.
"""
from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_admin_services import open_assignment
from accounts.goods_models import Staff
from approvals.goods_models import ApprovalRequest
from core.canonical import sha256_hex
from core.commands import CommandRun, database_now
from core.kernel_models import DocumentHead, OfficialLine
from core.numbering import prepare_series
from core.tenancy import tenant_context
from files.goods_services import stage_upload
from masters.first_store_readiness import opening_ready
from masters.goods_models import MasterVersion, SiteCapabilityEvent, SiteGuard
from masters.goods_services import READINESS_SERIES, append_master_version, compute_readiness_checks, ensure_site_sbus
from masters.goods_views import GoodsSiteReadinessView
from ptmapper import goods_manifest_services as manifests, soh_services
from ptmapper.soh_models import SohImport, SohImportReview, SohImportRow
from ptmapper.soh_parser import parse_soh
from stockledger import goods_acceptance
from stockledger.goods_models import JournalBatch, Origin, Position, QuantityLeg, ValueLeg
from stockledger.goods_views import AcceptanceScanView
from tests.first_store_goods import _publish_tenant_config, actors, approve, command, reviewed_source, workbook
from tests.test_first_store_goods_operations import post, wire
from tests.test_so03_denials import worlds as worlds


def readiness(proof: Any, action: str, *, override_opening: bool = False) -> Any:
    guard = SiteGuard.objects.get(site=proof.world.sites[0])
    request = APIRequestFactory().post("/api/goods-v1/masters/sites/readiness", wire(
        expected_revision=guard.revision, action=action, reason_code="FICTIONAL-OPENING-PROOF",
        residual_decisions=[{"code": "opening_reconciled", "message": "Try to bypass the incomplete source"}]
        if override_opening else [],
    ), format="json")
    force_authenticate(request, proof.owner.user, proof.owner.session)
    return GoodsSiteReadinessView.as_view()(request, pk=proof.world.sites[0].pk)


def protected_state() -> dict[str, list[dict[str, Any]]]:
    models: tuple[Any, ...] = (SiteGuard, SiteCapabilityEvent, SohImport, SohImportRow, SohImportReview,
                               DocumentHead, OfficialLine, Origin, Position, JournalBatch, QuantityLeg, ValueLeg)
    return {model._meta.label: [dict(row) for row in model.objects.order_by("pk").values()] for model in models}


def assert_opening_denied(proof: Any) -> None:
    before = protected_state()
    assert not opening_ready(proof.world.sites[0])
    for override in (False, True):
        response = readiness(proof, "approve_goods", override_opening=override)
        assert response.status_code == 409, response.data
        assert response.data["code"] == "READINESS_UNOVERRIDABLE"
        assert any(item.get("field") == "opening_reconciled" for item in response.data["details"]["issues"])
        assert "data" not in response.data
        assert protected_state() == before


def configure_current_setup(proof: Any) -> None:
    world, site = proof.world, proof.world.sites[0]
    for kind, target in (("entity", site.gstin.legal_entity), ("registration", site.gstin)):
        def record_master(run: CommandRun, *, kind: str = kind, key: str = str(target.pk)) -> MasterVersion:
            return append_master_version(run, kind=kind, target_key=key, revision=1, payload={"code": key})
        command(proof.owner, f"proof.readiness.{kind}", record_master)
    for doc_type in READINESS_SERIES:
        prepare_series(world.tenant.pk, site.gstin.legal_entity, doc_type)
    command(proof.owner, "proof.readiness.units", lambda run: ensure_site_sbus(world.tenant.pk, site, [world.brands[0].pk]))
    business = _publish_tenant_config(world.tenant, proof.owner.human_id, kind="business_profile", payload={
        "categories": ["Fictional proof clothing"], "identity_profile_id": str(proof.identity.pk),
        "expected_skus": 1, "brands": 2, "sites": 2, "sbus": 2, "staff": 3, "documents_per_day": 10,
        "evidence_bytes_per_year": 1000, "commercial_labels": ["Fictional proof stock"],
        "workforce_scope": "Generated example.test people only", "tills_per_site": [{"site_id": str(site.pk), "count": 1}],
        "accounting_interface": "Proof only; no real external books"}, label="readiness-business")
    world.tenant.business_profile_version = business
    world.tenant.save(update_fields=["business_profile_version"])
    _publish_tenant_config(world.tenant, proof.owner.human_id, kind="approval", payload={
        "action": "pt.approve.receipt", "roles": ["owner"], "site_ids": [], "brand_ids": [], "require_distinct": True,
        "qty_max": 100, "value_max": "10000000", "step_up": True, "unknown_value": "refuse"}, label="readiness-receipt")
    staff = Staff.objects.create(tenant=world.tenant, human_id=proof.manager.human_id, salesperson=True)
    command(proof.owner, "proof.readiness.staff", lambda run: open_assignment(
        run, staff, site.pk, run.now - timedelta(days=1)))
    assert all(row["passed"] for row in compute_readiness_checks(site, database_now()))


@pytest.mark.parametrize("lifecycle", [SiteGuard.Lifecycle.PLANNED, SiteGuard.Lifecycle.OPENING, SiteGuard.Lifecycle.ACTIVE])
def test_online_goods_readiness_requires_reviewed_and_fully_accepted_opening_in_every_lifecycle(
    worlds: Any, lifecycle: str,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        proof = reviewed_source(world, *actors(world))
        configure_current_setup(proof)
        site = world.sites[0]
        guard = SiteGuard.objects.get(site=site)
        guard.lifecycle = lifecycle
        guard.save(update_fields=["lifecycle"])
        # This input represents a retained event from the old unsafe revision.
        # The current handler must retain it, while preventing its reuse after
        # revocation from becoming fresh approval for an unaccepted source.
        historical = None
        if lifecycle == SiteGuard.Lifecycle.ACTIVE:
            historical = command(proof.owner, "proof.historical.unsafe-readiness", lambda run: run.record(SiteCapabilityEvent(
                site=site, operation=SiteCapabilityEvent.Operation.GOODS, outcome=SiteCapabilityEvent.Outcome.APPROVED,
                site_revision=guard.revision, checks=[], reason_code="RETAINED-OLD-UNSAFE-EVENT", approver_id=proof.owner.human_id)))
            guard.goods_ready = True
            guard.capability_event = historical
            guard.save(update_fields=["goods_ready", "capability_event"])
            response = readiness(proof, "revoke_goods")
            assert response.status_code == 200, response.data
        saved_events = [dict(row) for row in SiteCapabilityEvent.objects.order_by("pk").values()]
        assert_opening_denied(proof)  # independently unapproved source

        approve(proof.owner, proof.source.approval_request)
        proof.source.refresh_from_db()
        batch = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(
            run, proof.warehouse, proof.source, 1, proof.source.revision))
        manifest = batch.manifest
        approve(proof.owner, ApprovalRequest.objects.get(subject_kind="manifest", subject_key=str(manifest.pk), state="pending"))
        manifest.refresh_from_db()
        identity, head, pt = command(proof.warehouse, "pt.prepare.opening", lambda run: manifests.create_opening_draft(
            run, manifest=manifest, profile_version_id=proof.profile.pk, evidence_id=proof.evidence.pk))
        assert head.state == DocumentHead.State.DRAFT
        assert_opening_denied(proof)  # the exact unsafe draft-PT trigger
        command(proof.warehouse, "pt.prepare.opening", lambda run: manifests.submit_opening(
            run, head, reviewed_hash=head.draft_revision.content_hash, goods_pt=pt))
        approve(proof.owner, ApprovalRequest.objects.get(subject_kind="document", subject_key=str(identity.pk), state="pending"))
        version = DocumentHead.objects.get(document=identity).live_version
        assert version is not None
        line = OfficialLine.objects.get(version=version)
        session, _ = command(proof.manager, "stock.accept", lambda run: goods_acceptance.open_session(run, site_id=site.pk, version_id=version.pk))
        assert_opening_denied(proof)  # official PT still physically unaccepted
        observation = {"scan_key": str(uuid.uuid4()), "official_line_id": str(line.pk), "alias_value": proof.barcode,
                       "observed_ticket_mrp_paise": "100000", "label_evidence_id": str(proof.evidence.pk),
                       "chosen_sku_id": line.payload["sku_id"], "qty": 2, "condition": "good",
                       "location_id": str(proof.location.pk), "outcome": "accepted_good", "actual_at": timezone.now().isoformat()}
        response = post(AcceptanceScanView, proof.manager, wire(expected_revision=session.revision, observations=[observation]), pk=session.pk)
        assert response.status_code == 200, response.data
        assert_opening_denied(proof)  # partial acceptance cannot open goods
        session.refresh_from_db()
        remainder = {**observation, "scan_key": str(uuid.uuid4()), "qty": 1}
        response = post(AcceptanceScanView, proof.manager, wire(expected_revision=session.revision, observations=[remainder]), pk=session.pk)
        assert response.status_code == 200, response.data
        assert opening_ready(site)
        stock_before = {key: value for key, value in protected_state().items() if key not in {
            SiteGuard._meta.label, SiteCapabilityEvent._meta.label}}
        response = readiness(proof, "approve_goods")
        assert response.status_code == 200, response.data
        guard.refresh_from_db()
        assert guard.goods_ready and guard.lifecycle == SiteGuard.Lifecycle.ACTIVE
        assert guard.capability_event_id is not None
        event = SiteCapabilityEvent.objects.get(pk=guard.capability_event_id)
        opening_check = next(row for row in event.checks if row["key"] == "opening_reconciled")
        assert opening_check["passed"] and not opening_check["overridable"]
        assert [dict(row) for row in SiteCapabilityEvent.objects.filter(pk__in=[row["id"] for row in saved_events]).order_by("pk").values()] == saved_events
        assert {key: value for key, value in protected_state().items() if key in stock_before} == stock_before
        if historical is not None:
            historical.refresh_from_db()
            assert historical.reason_code == "RETAINED-OLD-UNSAFE-EVENT" and historical.checks == []

        # A later staged snapshot is reconciliation work; uploading it does not
        # reinterpret or deactivate this established, physically accepted start.
        raw = workbook([["Shirt", "Old brand text", "M", "000123", "Shirt", "Legacy season", 4, 1000, 500, 2000]])
        evidence = stage_upload(proof.manager.principal(), command_id=uuid.uuid4(), data=raw, filename="later-proof.xlsx", kind="manifest",
                                scope={"scope_kind": "sites", "site_ids": [site.pk], "brand_ids": [], "sensitive_fields": ["cost", "financial"]},
                                expected_sha256=sha256_hex(raw), contains_fields=["cost", "financial"])
        metadata, parsed = parse_soh(raw)
        later = command(proof.manager, soh_services.STAGE, lambda run: soh_services.create_source(
            run, site_id=site.pk, evidence=evidence, metadata=metadata, parsed=parsed))
        assert later.state != "applied" and later.pk != proof.source.pk
        assert opening_ready(site) and opening_ready(site, whole_store=False)
        origins_before = deepcopy([dict(row) for row in Origin.objects.values()])
        response = readiness(proof, "approve_goods")
        assert response.status_code == 200, response.data
        assert [dict(row) for row in Origin.objects.values()] == origins_before
