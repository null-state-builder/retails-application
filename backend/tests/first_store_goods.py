"""Disposable proof fixtures using the actual SOH → OPT → acceptance writers."""
from __future__ import annotations

import io
import time
import uuid
from datetime import datetime, timedelta
from functools import partial
from types import SimpleNamespace
from collections.abc import Callable
from typing import Any, cast

from django.utils import timezone
from openpyxl import Workbook

from accounts.goods_demo import CATALOGUE_IDENTITY_PROFILE, CATALOGUE_SIZES, _publish_tenant_config as _publish_config
from accounts.goods_models import SecurityGuard, ServerSession
from accounts.principal import AccessContext, resolve_access
from accounts.models import User
from approvals.goods_models import ApprovalRequest
from approvals.goods_services import decide
from core.canonical import sha256_hex
from core.commands import CommandResult, CommandRun, CommandSpec, database_now, execute_command
from core.kernel_models import DocumentHead, OfficialLine
from core.numbering import prepare_series
from files.goods_services import stage_upload
from masters.goods_identity_services import vocabulary_value_id
from masters.goods_models import ConfigVersion, Location, SiteGuard, Tenant
from masters.goods_config import normalise_scope
from masters.goods_services import append_master_version, validate_config_payload
from masters.models import Season
from ptmapper import goods_manifest_services as manifests, soh_services
from ptmapper.soh_parser import parse_soh
from stockledger import goods_acceptance
from tests.test_so03_denials import TenantWorld, _assign, _person


def source_cutoff() -> datetime:
    """A source cutoff after earlier writes, strictly before the next database stamp.

    Till pauses stamp the app clock, while command time, journal events and
    ``SohImport.created_at`` stamp the database clock; the proof database can lag or
    lead by more than a fixture step takes. Take the later clock, then let the
    database pass it.
    """
    cutoff = max(timezone.now(), database_now())
    while database_now() <= cutoff:
        time.sleep(0.001)
    return cutoff


def _publish_tenant_config(tenant: Tenant, approver_human_id: uuid.UUID | None, *, kind: str, payload: dict[str, Any], label: str) -> ConfigVersion:
    """Proof inputs obey the same closed configuration schema as administration."""
    validate_config_payload(kind, payload, tenant_id=tenant.pk, scope=normalise_scope({"scope_kind": "tenant"}), as_of=timezone.now())
    publish = cast(Callable[..., ConfigVersion], _publish_config)
    return publish(tenant, approver_human_id, kind=kind, payload=payload, label=label)


def workbook(rows: list[list[Any]] | None = None, footer: list[Any] | None = None) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Sheet1"
    sheet.append(["Item Name", "Brand", "Size", "Barcode", "Category", "Season", "Tqty", "Mrp", "Rate", "Amount"])
    for row in rows or [["Shirt", "Old brand text", "M", "000123", "Shirt", "Legacy season", 3, 1000, 500, 1500]]:
        sheet.append(row)
    if footer is not None:
        sheet.append(footer)
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def live_access(user: User) -> AccessContext:
    assert user.tenant is not None and user.human is not None
    moment = timezone.now()
    guard, _ = SecurityGuard.objects.get_or_create(tenant=user.tenant, human=user.human, defaults={"epoch": 1})
    session = ServerSession.objects.create(tenant=user.tenant, user=user, token_hash=uuid.uuid4().hex * 2,
        csrf_hash=uuid.uuid4().hex * 2, issued_at=moment, last_seen_at=moment,
        expires_at=moment + timedelta(hours=1), security_epoch=guard.epoch, step_up_at=moment)
    return resolve_access(SimpleNamespace(user=user, auth=session))


def command(access: AccessContext, action: str, handler: Callable[[CommandRun], Any], *, body: Any = None, command_id: uuid.UUID | None = None) -> Any:
    holder: dict[str, Any] = {}
    def invoke(run: CommandRun) -> CommandResult:
        holder["result"] = handler(run)
        result = holder["result"]
        return CommandResult(resource_type="proof", resource_id=str(getattr(result, "pk", "fixture")))
    execute_command(access.principal(), CommandSpec(action=action, command_id=command_id or uuid.uuid4(), business_input=body or {}), invoke)
    return holder.get("result")


def actors(world: TenantWorld) -> tuple[User, User, User]:
    owner, oh = _person(world, "soh-owner")
    warehouse, wh = _person(world, "soh-warehouse-admin")
    manager, mh = _person(world, "soh-manager")
    _assign(world, oh, "owner", all_sites=True, all_brands=True)
    # Catalogue authority and own-PT valuation remain in separate assignments.
    _assign(world, wh, "it_admin", all_sites=True, all_brands=True)
    _assign(world, wh, "warehouse", sites=(world.sites[0],), all_brands=True)
    _assign(world, mh, "store_person", sites=(world.sites[0],), all_brands=True)
    return owner, warehouse, manager


def reviewed_source(world: TenantWorld, owner_user: User, warehouse_user: User, manager_user: User, *, rows: list[list[Any]] | None = None) -> SimpleNamespace:
    owner, warehouse, manager = map(live_access, (owner_user, warehouse_user, manager_user))
    site = world.sites[0]
    # This is a real-mode proof tenant, never a deployed shop.
    world.tenant.synthetic = False
    world.tenant.save(update_fields=["synthetic"])
    SiteGuard.objects.create(tenant=world.tenant, site=site, opening_setup_ready=True,
        stock_contract="goods_v1", selling_mode="online_alpha", lifecycle="opening")
    for kind in Location.SYSTEM_KINDS:
        Location.objects.create(tenant=world.tenant, site=site, name=kind, kind=kind, system=True)
    location = Location.objects.create(tenant=world.tenant, site=site, name="Sales floor", kind="floor")
    prepare_series(world.tenant.pk, site.gstin.legal_entity, "OPT")
    _publish_tenant_config(world.tenant, owner.human_id, kind="working_calendar", payload={"timezone": "Asia/Kolkata", "working_weekdays": [1, 2, 3, 4, 5, 6], "excluded_dates": []}, label="soh-calendar")
    vocabulary = _publish_tenant_config(world.tenant, owner.human_id, kind="vocabulary",
        payload={"dimension": "size", "values": CATALOGUE_SIZES, "effective_from": timezone.now().isoformat()}, label="soh-size")
    identity = _publish_tenant_config(world.tenant, owner.human_id, kind="identity_profile", payload=CATALOGUE_IDENTITY_PROFILE, label="soh-identity")
    rates = _publish_tenant_config(world.tenant, owner.human_id, kind="rates", payload={"transport_pct": "0", "pricing_margin_pct": "0"}, label="soh-rates")
    tax = _publish_tenant_config(world.tenant, owner.human_id, kind="tax_rates", payload={"currency": "INR", "hsn_rules": [{"hsn": "6101", "effective_from": timezone.now().isoformat(), "slabs": [{"lower_paise": "0", "upper_paise": None, "lower_inclusive": True, "upper_inclusive": False, "input_pct": "5", "output_pct": "5"}]}]}, label="soh-tax")
    profile = _publish_tenant_config(world.tenant, owner.human_id, kind="profile", payload={"family": "fashion", "columns": [], "directions": ["both_supplied"], "allow_row_override": False, "rates_version_id": str(rates.pk), "tax_version_id": str(tax.pk), "opening_rules": {"season_required": True, "both_supplied": True}}, label="soh-profile")
    _publish_tenant_config(world.tenant, owner.human_id, kind="approval", payload={"action": "pt.approve.opening", "roles": ["owner"], "site_ids": [], "brand_ids": [], "require_distinct": True, "qty_max": 100, "value_max": "10000000", "step_up": True, "unknown_value": "refuse"}, label="soh-approval")
    season = Season.objects.create(code=f"SOH-{uuid.uuid4().hex[:8]}", name="Reviewed real season")
    command(owner, "proof.season", lambda run: append_master_version(run, kind="season", target_key=str(season.pk), revision=1, payload={"name": season.name, "historical_unknown": False}))
    cutoff = source_cutoff()
    raw = workbook(rows)
    evidence = stage_upload(manager.principal(), command_id=uuid.uuid4(), data=raw, filename="proof-soh.xlsx", kind="manifest",
        scope={"scope_kind": "sites", "site_ids": [site.pk], "brand_ids": [], "sensitive_fields": ["cost", "financial"]}, expected_sha256=sha256_hex(raw), contains_fields=["cost", "financial"])
    review_book = Workbook()
    assert review_book.active is not None
    review_book.active.append(["Proof evidence", "Three physically counted units; source basic amount 1500 INR; no difference"])
    review_bytes = io.BytesIO()
    review_book.save(review_bytes)
    supporting_bytes = review_bytes.getvalue()
    supporting = stage_upload(warehouse.principal(), command_id=uuid.uuid4(), data=supporting_bytes, filename="review.xlsx", kind="other",
        scope={"scope_kind": "sites", "site_ids": [site.pk], "brand_ids": [], "sensitive_fields": ["cost", "financial"]}, expected_sha256=sha256_hex(supporting_bytes), contains_fields=["cost", "financial"])
    metadata, parsed = parse_soh(raw)
    source = command(manager, soh_services.STAGE, lambda run: soh_services.create_source(run, site_id=site.pk, evidence=evidence, metadata=metadata, parsed=parsed))
    source = command(warehouse, soh_services.PREPARE, lambda run: soh_services.claim_preparation(run, warehouse, source, source.revision))
    config = {"brand_mappings": {"Old brand text": str(world.brands[0].pk)}, "season_mappings": {"Legacy season": str(season.pk)},
        "size_mappings": {"M": str(vocabulary_value_id("size", "M"))}, "hsn_mappings": {"Shirt": "6101"}, "row_overrides": {},
        "identity_profile_id": str(identity.pk), "profile_version_id": str(profile.pk), "cutoff_at": cutoff.isoformat(),
        "rate_meaning": "basic_ex_tax", "valuation_evidence_id": str(supporting.pk), "reconciliation_evidence_id": str(supporting.pk),
        "source_store_confirmed": True, "fresh_source_confirmed": True, "quality_note": "Proof physical tags and source reviewed",
        "external_reconciliation_note": "Three units, basic value 1500 INR, source Amount agrees exactly", "unknown_season_sources": []}
    source = command(warehouse, soh_services.PREPARE, lambda run: soh_services.prepare(run, warehouse, source, config, source.revision))
    observations = [{"barcode": row.barcode, "observed_qty": row.quantity, "observed_condition": "good", "reason": "Independent proof physical count"} for row in parsed if row.quantity > 0]
    for start in range(0, len(observations), 5000):
        source = command(manager, soh_services.STAGE, partial(soh_services.verify_rows, access=manager, source=source, observations=observations[start:start + 5000], expected=source.revision))
    source = command(warehouse, soh_services.PREPARE, lambda run: soh_services.submit(run, warehouse, source, source.revision))
    return SimpleNamespace(world=world, source=source, owner=owner, warehouse=warehouse, manager=manager, profile=profile,
        identity=identity, vocabulary=vocabulary, config=config, location=location, barcode="000123", evidence=evidence)


def approve(access: AccessContext, request: ApprovalRequest) -> Any:
    return command(access, request.requested_action, lambda run: decide(run, access=access, request_id=request.pk,
        decision="approve", reviewed_hash=request.reviewed_hash, reason_code=None))


def opened_goods(world: TenantWorld, owner_user: User, warehouse_user: User, manager_user: User) -> SimpleNamespace:
    proof = reviewed_source(world, owner_user, warehouse_user, manager_user)
    approve(proof.owner, proof.source.approval_request)
    proof.source.refresh_from_db()
    batch = command(proof.warehouse, soh_services.PREPARE, lambda run: soh_services.apply_batch(run, proof.warehouse, proof.source, 1, proof.source.revision))
    proof.manifest = batch.manifest
    approve(proof.owner, ApprovalRequest.objects.get(subject_kind="manifest", subject_key=str(proof.manifest.pk), state="pending"))
    proof.manifest.refresh_from_db()
    identity, head, pt = command(proof.warehouse, "pt.prepare.opening", lambda run: manifests.create_opening_draft(run,
        manifest=proof.manifest, profile_version_id=proof.profile.pk, evidence_id=proof.evidence.pk))
    command(proof.warehouse, "pt.prepare.opening", lambda run: manifests.submit_opening(run, head,
        reviewed_hash=head.draft_revision.content_hash, goods_pt=pt))
    approve(proof.owner, ApprovalRequest.objects.get(subject_kind="document", subject_key=str(identity.pk), state="pending"))
    head = DocumentHead.objects.get(document=identity)
    proof.official_version = head.live_version
    session, _ = command(proof.manager, "stock.accept", lambda run: goods_acceptance.open_session(run, site_id=world.sites[0].pk, version_id=proof.official_version.pk))
    line = OfficialLine.objects.get(version=proof.official_version)
    proof.sku_id = uuid.UUID(line.payload["sku_id"])
    scans = goods_acceptance.parse_scans([{"scan_key": str(uuid.uuid4()), "official_line_id": str(line.pk), "alias_value": proof.barcode,
        "observed_ticket_mrp_paise": "100000", "label_evidence_id": str(proof.evidence.pk), "chosen_sku_id": str(proof.sku_id),
        "qty": 3, "condition": "good", "location_id": str(proof.location.pk), "outcome": "accepted_good", "actual_at": timezone.now().isoformat()}])
    proof.acceptance_session = command(proof.manager, "stock.accept", lambda run: goods_acceptance.scan(run, session.pk, scans, session.revision))
    proof.acceptance_scans = scans
    proof.source.refresh_from_db()
    return proof
