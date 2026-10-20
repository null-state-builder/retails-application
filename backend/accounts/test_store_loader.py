"""Load one functional test store from a legacy SOH (store test run, synthetic only).

Builds a store and its linked warehouse on the synthetic tenant and brings the
legacy SOH in through the product's own reviewed opening path - stage, prepare,
verify, approve, opening PT, approve, accept onto the floor - with a different
synthetic person at each step, exactly as the proof fixtures do. Nothing here
writes stock directly.

Test-only decisions, recorded on every record they touch:

* sizes and seasons are taken as the legacy file spells them (one vocabulary
  value / season per distinct text); a blank season is the deliberate unknown
  historical season;
* HSN comes from a keyword map over the legacy category (the file has no HSN);
* the "physical verification" is the legacy quantity, not a count;
* rows with no positive cost/MRP, or cost above MRP, are excluded with a reason.

Refuses unless the tenant is synthetic.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.utils import timezone

from accounts.goods_demo import (
    CATALOGUE_IDENTITY_PROFILE,
    CATALOGUE_SIZES,
    SEED_REASON,
    SYNTHETIC_DOMAIN,
    PersonaSpec,
    SiteSpec,
    _build_sites,
    _ensure_ops_series,
    _ensure_working_locations,
    _open_ops_site,
    _owner_human_id,
    _publish_site_master_records,
    _publish_tenant_config as _publish_config,
    seed_goods_personas,
    seed_ops_selling,
)
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.refusals import Refusal

#: Keyword → HSN for the test tenant only (the legacy SOH carries no HSN).
HSN_RULES: tuple[tuple[str, str], ...] = (
    ("SAREE", "5407"),
    ("ACCE", "6217"),
    ("INNER", "6108"),
    ("NIGH", "6108"),
    ("SPORT", "6211"),
    ("WINTER", "6201"),
    ("BLZ", "6203"),
    ("SUT", "6203"),
    ("KUR", "6204"),
    ("TOP", "6206"),
)
DEFAULT_HSN = "6211"
#: Fixed so a re-run finds the same configuration instead of publishing a new one.
CONFIG_FROM = "2024-04-01T00:00:00+05:30"
TEST_NOTE = "Store test run: loaded from the legacy SOH on a synthetic tenant, not a reviewed real opening."


@dataclass(frozen=True)
class Plan:
    store_code: str
    store_name: str
    warehouse_code: str
    warehouse_name: str
    entity_code: str
    entity_name: str
    pan: str
    state_code: str
    state_name: str
    city: str
    prefix: str  # login prefix, e.g. "vas"


def _publish_tenant_config(tenant: Any, approver: Any, *, kind: str, payload: dict[str, Any], label: str) -> Any:
    """Publish once; on a re-run return the version already in force with this payload."""
    from core.commands import database_now
    from masters.goods_config import in_force
    from masters.goods_models import ConfigVersion

    from core.canonical import content_hash

    # The label keys the seed command: a changed payload is a new decision.
    version = _publish_config(tenant, approver, kind=kind, payload=payload, label=f"{label}-{content_hash(payload)[:12]}")
    if version is not None:
        return version

    def same(row: Any) -> bool:
        current = row.payload if isinstance(row.payload, dict) else {}
        return all(current.get(k) == v for k, v in payload.items() if k != "effective_from")

    found = [row for row in ConfigVersion.objects.filter(tenant_id=tenant.pk, kind=kind).order_by("-version")
             if in_force(row, database_now()) and same(row)]
    if not found:
        raise Refusal("STATE_CONFLICT", f"The test {kind} configuration ({label}) is not in force.")
    return found[0]


def _note_workbook() -> bytes:
    import io

    from openpyxl import Workbook

    book = Workbook()
    assert book.active is not None
    book.active.append(["Store test run evidence", TEST_NOTE])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def _hsn(category: str) -> str:
    text = category.upper()
    return next((hsn for key, hsn in HSN_RULES if key in text), DEFAULT_HSN)


def _key(text: str, prefix: str) -> str:
    clean = re.sub(r"[^A-Z0-9]+", "-", text.upper()).strip("-")
    return clean[:60] if clean else f"{prefix}-{hashlib.sha1(text.encode()).hexdigest()[:8]}"


def _slug(text: str, limit: int) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"
    return clean[:limit] if len(clean) <= limit else f"{clean[:limit - 9]}-{hashlib.sha1(text.encode()).hexdigest()[:8]}"


class Loader:
    def __init__(self, tenant: Any, plan: Plan, soh: bytes, log: Callable[[str], None]) -> None:
        self.tenant, self.plan, self.soh, self.log = tenant, plan, soh, log
        self.owner_id = _owner_human_id(tenant)

    # -- helpers -----------------------------------------------------------

    def seed_command(self, action: str, key: str, handler: Callable[[CommandRun], Any]) -> Any:
        from accounts.goods_setup import service_principal

        holder: dict[str, Any] = {}

        def run(cmd: CommandRun) -> CommandResult:
            holder["v"] = handler(cmd)
            return CommandResult(resource_type="test_store", resource_id=key[:60])

        execute_command(service_principal(self.tenant.pk, "seed"),
                        CommandSpec(action=f"seed.test_store_{action}",
                                    command_id=uuid.uuid5(self.tenant.deployment_key, f"test-store:{action}:{key}"),
                                    business_input={"key": key}), run)
        return holder.get("v")

    def access(self, email: str) -> Any:
        from accounts.goods_models import SecurityGuard, ServerSession
        from accounts.models import User
        from accounts.principal import resolve_access

        user = User.objects.get(email__iexact=email)
        now = timezone.now()
        guard, _ = SecurityGuard.objects.get_or_create(tenant=user.tenant, human=user.human, defaults={"epoch": 1})
        session = ServerSession.objects.create(
            tenant=user.tenant, user=user, token_hash=uuid.uuid4().hex * 2, csrf_hash=uuid.uuid4().hex * 2,
            issued_at=now, last_seen_at=now, expires_at=now + timedelta(hours=2), security_epoch=guard.epoch,
            step_up_at=now)
        from types import SimpleNamespace
        return resolve_access(SimpleNamespace(user=user, auth=session))

    @staticmethod
    def act(access: Any, action: str, handler: Callable[..., Any]) -> Any:
        from core.commands import CommandResult as Result
        holder: dict[str, Any] = {}

        def run(cmd: CommandRun) -> Result:
            holder["v"] = handler(cmd)
            return Result(resource_type="test_store", resource_id=str(getattr(holder["v"], "pk", "x")))

        execute_command(access.principal(), CommandSpec(action=action, command_id=uuid.uuid4(), business_input={}), run)
        return holder.get("v")

    def email(self, who: str) -> str:
        return f"{self.plan.prefix}.{who}@{SYNTHETIC_DOMAIN}"

    # -- steps -------------------------------------------------------------

    def sites(self) -> tuple[Any, Any]:
        p = self.plan
        specs = tuple(SiteSpec(code=code, name=name, kind=kind, entity_code=p.entity_code, entity_name=p.entity_name,
                               pan=p.pan, state_code=p.state_code, state_name=p.state_name, city=p.city)
                      for code, name, kind in ((p.warehouse_code, p.warehouse_name, "warehouse"),
                                               (p.store_code, p.store_name, "store")))
        sites = _build_sites(self.tenant, specs)
        warehouse, store = sites[p.warehouse_code], sites[p.store_code]
        for spec in specs:
            site = sites[spec.code]
            _ensure_working_locations(self.tenant.pk, site)
            _publish_site_master_records(self.tenant, spec, site)
            _ensure_ops_series(self.tenant, site)
        self._link(store, warehouse)
        self.log(f"Sites ready: {warehouse.code} (warehouse) and {store.code} (store), linked.")
        return warehouse, store

    def _link(self, store: Any, warehouse: Any) -> None:
        from core.kernel_models import SubjectRevision
        from masters.goods_services import append_master_version, latest_master_version, linked_warehouse_id, start_revision
        from masters.goods_views import _site_fallback

        if linked_warehouse_id(store) == warehouse.pk:
            return
        latest = latest_master_version(self.tenant.pk, "site", str(store.pk))
        payload = {**_site_fallback(store), **((latest.payload if latest else None) or {}),
                   "type": "store", "linked_warehouse_id": str(warehouse.pk)}

        def handler(run: CommandRun) -> None:
            if not SubjectRevision.objects.filter(tenant_id=run.tenant_id, family="master:site", subject_key=str(store.pk)).exists():
                start_revision(run.tenant_id, "master:site", str(store.pk))
            append_master_version(run, kind="site", target_key=str(store.pk),
                                  revision=(latest.revision + 1) if latest else 1, payload=payload, reason_code=SEED_REASON)

        self.seed_command("link", f"{store.pk}:{warehouse.pk}", handler)

    def people(self, warehouse: Any, store: Any) -> None:
        from accounts import warehouse_manager
        from accounts.goods_setup import seed_role_assignment
        from accounts.models import User

        sites = {warehouse.code: warehouse, store.code: store}
        p = self.plan.prefix
        personas = (
            PersonaSpec(f"{p}.store", f"{store.name} store person", "M-STR", site_code=store.code,
                        note="receives, verifies, accepts and sells at the store"),
            PersonaSpec(f"{p}.cashier", f"{store.name} cashier", "M-CSH", site_code=store.code, note="bills at the counter"),
            PersonaSpec(f"{p}.warehouse", f"{warehouse.name} operator", "C-WHO", site_code=warehouse.code,
                        note="warehouse operator"),
        )
        seed_goods_personas(self.tenant, sites, personas)
        extra = ((f"{p}.preparer", "Opening preparer (test load)"), (f"{p}.whm1", "Warehouse manager 1"),
                 (f"{p}.whm2", "Warehouse manager 2"))
        seed_goods_personas(self.tenant, sites, tuple(
            PersonaSpec(code, name, "C-WHO", site_code=None, extra=(("C-WHO", None),)) for code, name in extra))
        now = timezone.now()
        preparer = User.objects.get(email__iexact=self.email("preparer")).human
        # Opening preparation needs warehouse at the store (PT preparer) and catalogue authority.
        seed_role_assignment(self.tenant, preparer, "warehouse", source_key=f"test-store:{p}.preparer:wh",
                             site_ids=(store.pk, warehouse.pk), all_brands=True, effective_from=now)
        # Product-master authority is checked per brand with no site, so it is tenant-wide here.
        self._tenant_wide(preparer, "it_admin", now)
        warehouse_manager.ensure_policy(self.tenant)
        for who in ("whm1", "whm2"):
            human = User.objects.get(email__iexact=self.email(who)).human
            warehouse_manager.assign(self.tenant, human, site_ids=[warehouse.pk, store.pk])
        self.log("People ready: store, cashier, warehouse, preparer and two warehouse managers.")

    def _tenant_wide(self, human: Any, role_code: str, now: Any) -> None:
        """One open all-sites, all-brands assignment of ``role_code``; a narrower open one is closed."""
        from accounts.goods_models import RoleAssignment
        from accounts.models import Role

        role = Role.objects.get(tenant_id=self.tenant.pk, code=role_code)
        open_rows = RoleAssignment.objects.filter(tenant_id=self.tenant.pk, human_id=human.pk, role_id=role.pk,
                                                  effective_to__isnull=True, revoked_at__isnull=True)
        if any(row.all_sites and row.all_brands for row in open_rows):
            return
        open_rows.update(effective_to=now)
        RoleAssignment.objects.create(tenant_id=self.tenant.pk, human_id=human.pk, role_id=role.pk, all_sites=True,
                                      site_ids=[], all_brands=True, brand_ids=[], effective_from=now)

    def configuration(self, warehouse: Any, store: Any, rows: list[Any]) -> dict[str, Any]:
        from masters.goods_identity_services import vocabulary_value_id
        from masters.goods_services import ensure_site_sbus
        from masters.models import Brand, Season

        stocked = [r for r in rows if r.quantity > 0]
        # Sizes: keep the fixture's values, add every legacy spelling.
        values = list(CATALOGUE_SIZES)
        known = {v["value_key"] for v in values}
        size_map: dict[str, str] = {}
        for raw in sorted({str(r.source.get("size") or "").strip() for r in stocked} - {""}):
            key = _key(raw, "SZ")
            if key not in known:
                values.append({"value_key": key, "label": raw[:160], "sort_order": 100 + len(values), "retired": False})
                known.add(key)
            size_map[raw] = str(vocabulary_value_id("size", key))
        _publish_tenant_config(self.tenant, self.owner_id, kind="vocabulary",
                               payload={"dimension": "size", "values": values, "effective_from": CONFIG_FROM},
                               label=f"test-store-size-{len(values)}")
        identity = _publish_tenant_config(self.tenant, self.owner_id, kind="identity_profile",
                                          payload=CATALOGUE_IDENTITY_PROFILE, label="fashion-identity-profile")
        hsns = sorted({_hsn(str(r.source.get("category") or "")) for r in stocked})
        slabs = [{"lower_paise": "0", "upper_paise": "250000", "lower_inclusive": True, "upper_inclusive": True,
                  "input_pct": "5", "output_pct": "5"},
                 {"lower_paise": "250000", "upper_paise": None, "lower_inclusive": False, "upper_inclusive": False,
                  "input_pct": "18", "output_pct": "18"}]
        start = CONFIG_FROM
        tax = _publish_tenant_config(self.tenant, self.owner_id, kind="tax_rates", payload={
            "currency": "INR", "hsn_rules": [{"hsn": h, "effective_from": start, "slabs": slabs} for h in hsns]},
            label="test-store-tax")
        rates = _publish_tenant_config(self.tenant, self.owner_id, kind="rates",
                                       payload={"transport_pct": "0", "pricing_margin_pct": "0"}, label="test-store-rates")
        profile = _publish_tenant_config(self.tenant, self.owner_id, kind="profile", payload={
            "family": "fashion", "columns": [], "directions": ["both_supplied"], "allow_row_override": True,
            "rates_version_id": str(rates.pk), "tax_version_id": str(tax.pk),
            "opening_rules": {"season_required": True, "both_supplied": True}}, label="test-store-profile")
        for action, roles in (("pt.approve.opening", ["owner"]), ("pt.approve.receipt", ["owner"]),
                              ("pt.approve.transfer", ["owner", "warehouse_manager"])):
            _publish_tenant_config(self.tenant, self.owner_id, kind="approval", payload={
                "action": action, "roles": roles, "site_ids": [], "brand_ids": [], "require_distinct": True,
                "qty_max": 1_000_000, "value_max": "100000000000", "step_up": True, "unknown_value": "refuse"},
                label=f"test-store-{action}")
        # Brands: match by name, else create.
        by_name = {b.name.casefold(): b for b in Brand.objects.filter(tenant_id=self.tenant.pk)}
        brand_map: dict[str, str] = {}
        for raw in sorted({r.brand for r in stocked}):
            brand = by_name.get(raw.casefold())
            if brand is None:
                brand, _ = Brand.objects.get_or_create(tenant_id=self.tenant.pk, code=_slug(raw, 32),
                                                       defaults={"name": raw[:120] or "Unknown"})
            brand_map[raw] = str(brand.pk)
        brand_ids = sorted({int(v) for v in brand_map.values()})
        ensure_site_sbus(self.tenant.pk, store, brand_ids)
        ensure_site_sbus(self.tenant.pk, warehouse, brand_ids)
        # Seasons: one per legacy spelling; blank → the deliberate unknown historical season.
        unknown = Season.objects.get(historical_unknown=True)
        season_map: dict[str, str] = {"": str(unknown.pk)}
        for raw in sorted({str(r.source.get("season") or "").strip() for r in stocked} - {""}):
            code = f"L-{hashlib.sha1(raw.encode()).hexdigest()[:10]}"
            season, _ = Season.objects.get_or_create(code=code, defaults={"name": raw[:80]})
            season_map[raw] = str(season.pk)
        self._season_versions([int(v) for v in season_map.values()])
        overrides = {r.barcode: {"exclude_reason": "Store test run: no positive cost/MRP, or cost above MRP, in the legacy SOH."}
                     for r in stocked if not r.rate or not r.mrp or r.rate <= 0 or r.mrp <= 0 or r.rate > r.mrp}
        categories = {str(r.source.get("category") or "") for r in stocked}
        self.log(f"Masters ready: {len(brand_map)} brands, {len(season_map)} seasons, {len(size_map)} sizes, "
                 f"{len(hsns)} HSN codes; {len(overrides)} rows excluded for bad cost/MRP.")
        return {"brand_mappings": brand_map, "season_mappings": season_map, "size_mappings": size_map,
                "hsn_mappings": {c: _hsn(c) for c in categories}, "row_overrides": overrides,
                "unknown_season_sources": [""], "identity_profile_id": str(identity.pk),
                "profile_version_id": str(profile.pk), "rate_meaning": "basic_ex_tax",
                "source_store_confirmed": True, "fresh_source_confirmed": True,
                "quality_note": TEST_NOTE, "external_reconciliation_note": TEST_NOTE}

    def _season_versions(self, season_ids: list[int]) -> None:
        from core.kernel_models import SubjectRevision
        from masters.goods_models import MasterVersion
        from masters.goods_services import append_master_version, start_revision
        from masters.models import Season

        missing = [s for s in Season.objects.filter(pk__in=season_ids)
                   if not MasterVersion.objects.filter(tenant_id=self.tenant.pk, kind="season", target_key=str(s.pk)).exists()]
        if not missing:
            return

        def handler(run: CommandRun) -> None:
            for season in missing:
                if not SubjectRevision.objects.filter(tenant_id=run.tenant_id, family="master:season", subject_key=str(season.pk)).exists():
                    start_revision(run.tenant_id, "master:season", str(season.pk))
                append_master_version(run, kind="season", target_key=str(season.pk), revision=1,
                                      payload={"name": season.name, "historical_unknown": season.historical_unknown},
                                      reason_code=SEED_REASON)

        self.seed_command("seasons", f"{len(missing)}:{min(s.pk for s in missing)}", handler)

    def opening(self, store: Any, config: dict[str, Any]) -> None:
        from approvals.goods_models import ApprovalRequest
        from approvals.goods_services import decide
        from core.canonical import sha256_hex
        from core.commands import database_now
        from core.kernel_models import DocumentHead, OfficialLine
        from files.goods_services import stage_upload
        from masters.goods_models import Location, SiteGuard
        from ptmapper import goods_manifest_services as manifests
        from ptmapper import soh_services
        from ptmapper.soh_models import SohImport
        from ptmapper.soh_parser import parse_soh
        from stockledger import goods_acceptance

        stager, preparer = self.access(self.email("store")), self.access(self.email("preparer"))
        guard = SiteGuard.objects.get(site=store)
        if not guard.opening_setup_ready:
            guard.opening_setup_ready = True
            guard.lifecycle = SiteGuard.Lifecycle.OPENING
            guard.save(update_fields=["opening_setup_ready", "lifecycle"])

        def approve(request: ApprovalRequest) -> None:
            # A fresh session each time: approvals need a recent step-up.
            owner = self.access(f"syn.owner@{SYNTHETIC_DOMAIN}")
            self.act(owner, request.requested_action, lambda run: decide(
                run, access=owner, request_id=request.pk, decision="approve",
                reviewed_hash=request.reviewed_hash, reason_code=None))

        source: Any = SohImport.objects.filter(tenant_id=self.tenant.pk, site=store, source_hash=sha256_hex(self.soh)).first()
        if source is None:
            evidence = stage_upload(stager.principal(), command_id=uuid.uuid4(), data=self.soh, filename="legacy-soh.xlsx",
                                    kind="manifest", scope={"scope_kind": "sites", "site_ids": [store.pk], "brand_ids": [],
                                                            "sensitive_fields": ["cost", "financial"]},
                                    expected_sha256=sha256_hex(self.soh), contains_fields=["cost", "financial"])
            metadata, parsed = parse_soh(self.soh)
            source = self.act(stager, soh_services.STAGE, lambda run: soh_services.create_source(
                run, site_id=store.pk, evidence=evidence, metadata=metadata, parsed=parsed))
            self.log(f"SOH staged: {metadata['stocked_rows']} stocked rows, {metadata['quantity']} pieces.")
        if source.state == "uploaded":
            source = self.act(preparer, soh_services.PREPARE, lambda run, s=source: soh_services.claim_preparation(run, preparer, s, s.revision))
            note_bytes = _note_workbook()
            note = stage_upload(preparer.principal(), command_id=uuid.uuid4(), data=note_bytes, filename="test-load-note.xlsx",
                                kind="other", scope={"scope_kind": "sites", "site_ids": [store.pk], "brand_ids": [],
                                                     "sensitive_fields": ["cost", "financial"]},
                                expected_sha256=sha256_hex(note_bytes), contains_fields=["cost", "financial"])
            # After every configuration the source pins took effect, and in the past.
            import time
            cutoff = max(timezone.now(), database_now())
            while database_now() <= cutoff:
                time.sleep(0.01)
            full = {**config, "cutoff_at": cutoff.isoformat(), "valuation_evidence_id": str(note.pk),
                    "reconciliation_evidence_id": str(note.pk)}
            source = self.act(preparer, soh_services.PREPARE, lambda run, s=source: soh_services.prepare(run, preparer, s, full, s.revision))
            self.log("SOH prepared (mappings, costs, HSN).")
        if source.state == "review" and not source.rows.exclude(verification={}).exists():
            included = list(source.rows.filter(quantity__gt=0, exclusion_reason="").values_list("barcode", "quantity"))
            obs = [{"barcode": b, "observed_qty": q, "observed_condition": "good",
                    "reason": "Store test run: legacy SOH quantity, not a physical count"} for b, q in included]
            for start in range(0, len(obs), 5000):
                source.refresh_from_db()
                chunk = obs[start:start + 5000]
                source = self.act(stager, soh_services.STAGE, lambda run, c=chunk, s=source: soh_services.verify_rows(
                    run, access=stager, source=s, observations=c, expected=s.revision))
            source = self.act(preparer, soh_services.PREPARE, lambda run, s=source: soh_services.submit(run, preparer, s, s.revision))
            self.log(f"SOH verified ({len(obs)} rows) and submitted.")
        source.refresh_from_db()
        request: Any = source.approval_request
        if request is not None and request.state == "pending":
            approve(request)
            source.refresh_from_db()
            self.log("SOH approved by the owner.")
        batches = (source.rows.filter(quantity__gt=0, exclusion_reason="").count() + soh_services.MAX_BATCH - 1) // soh_services.MAX_BATCH
        floor = Location.objects.get(site=store, name="Floor")
        for index in range(1, batches + 1):
            stager, preparer = self.access(self.email("store")), self.access(self.email("preparer"))
            source.refresh_from_db()
            batch = source.batches.filter(batch_index=index).first()
            if batch is None:
                batch = self.act(preparer, soh_services.PREPARE, lambda run, i=index, s=source, a=preparer: soh_services.apply_batch(run, a, s, i, s.revision))
            manifest: Any = batch.manifest
            pending = ApprovalRequest.objects.filter(subject_kind="manifest", subject_key=str(manifest.pk), state="pending").first()
            if pending:
                approve(pending)
            manifest.refresh_from_db()
            from ptmapper.goods_models import GoodsPt
            pt: Any = GoodsPt.objects.filter(manifest_version__manifest=manifest).first()
            if pt is None:
                _identity, head, pt = self.act(preparer, "pt.prepare.opening", lambda run, m=manifest: manifests.create_opening_draft(
                    run, manifest=m, profile_version_id=config["profile_version_id"], evidence_id=source.source_evidence_id))
                self.act(preparer, "pt.prepare.opening", lambda run, h=head, g=pt: manifests.submit_opening(
                    run, h, reviewed_hash=h.draft_revision.content_hash, goods_pt=g))
            identity_id = pt.document_id
            pending = ApprovalRequest.objects.filter(subject_kind="document", subject_key=str(identity_id), state="pending").first()
            if pending:
                approve(pending)
            version: Any = DocumentHead.objects.get(document_id=identity_id).live_version
            session, _ = self.act(stager, "stock.accept", lambda run, v=version: goods_acceptance.open_session(run, site_id=store.pk, version_id=v.pk))
            lines = list(OfficialLine.objects.filter(version=version).order_by("line_no"))
            done = set(session.events.values_list("scan_key", flat=True)) if hasattr(session, "events") else set()
            scans: list[dict[str, Any]] = []
            for line in lines:
                key = uuid.uuid5(version.pk, f"accept:{line.pk}")
                if key in done:
                    continue
                payload = line.payload
                mrp = payload.get("mrp_paise") or (payload.get("supplied") or {}).get("mrp_paise")
                scans.append({"scan_key": str(key), "official_line_id": str(line.pk), "alias_value": payload["alias_as_used"],
                              "observed_ticket_mrp_paise": str(mrp), "chosen_sku_id": str(payload["sku_id"]),
                              "qty": int(payload["qty"]), "condition": "good", "location_id": str(floor.pk),
                              "outcome": "accepted_good", "actual_at": timezone.now().isoformat()})
            for start in range(0, len(scans), goods_acceptance.MAX_OBSERVATIONS):
                parsed_scans = goods_acceptance.parse_scans(scans[start:start + goods_acceptance.MAX_OBSERVATIONS])
                session.refresh_from_db()
                session = self.act(stager, "stock.accept", lambda run, c=parsed_scans, ss=session: goods_acceptance.scan(run, ss.pk, c, ss.revision))
            self.log(f"Batch {index}/{batches}: manifest and opening PT approved, {len(lines)} lines accepted onto the floor.")

    def open_and_sell(self, warehouse: Any, store: Any) -> None:
        from accounts.goods_demo import OPS_TILL_COUNTER
        from accounts.models import User
        from masters.goods_models import SiteGuard
        from sell.models import RegisteredTill

        for site in (warehouse, store):
            spec = SiteSpec(code=site.code, name=site.name, kind=site.store_type, entity_code=self.plan.entity_code,
                            entity_name=self.plan.entity_name, pan=self.plan.pan, state_code=self.plan.state_code,
                            state_name=self.plan.state_name, city=self.plan.city)
            _open_ops_site(self.tenant, spec, site, self.owner_id)
        guard = SiteGuard.objects.get(site=store)
        if guard.lifecycle == SiteGuard.Lifecycle.OPENING:
            # Every opening batch is approved and accepted above; a historical-mode store
            # has no product step that finishes opening, so the seed records it.
            def activate(run: CommandRun) -> None:
                row = SiteGuard.objects.get(site=store)
                row.lifecycle = SiteGuard.Lifecycle.ACTIVE
                row.revision += 1
                row.save()

            self.seed_command("activate", str(store.pk), activate)
            guard.refresh_from_db()
        if not RegisteredTill.objects.filter(store=store, active=True).exists():
            RegisteredTill.objects.create(store=store, counter_id=OPS_TILL_COUNTER, active=True,
                                          registered_by=User.objects.get(email__iexact=f"syn.owner@{SYNTHETIC_DOMAIN}"),
                                          series_prefix=f"{store.code}-{OPS_TILL_COUNTER}")
        seed_ops_selling(store)
        self.log(f"Opened for business: {warehouse.code} goods-ready; {store.code} goods- and sell-ready "
                 f"(lifecycle {guard.lifecycle}), counter {OPS_TILL_COUNTER} registered.")

    def run(self) -> tuple[Any, Any]:
        from core.tenancy import tenant_context
        from masters.store_features import is_real_store
        from ptmapper.soh_parser import parse_soh

        _metadata, rows = parse_soh(self.soh)
        with tenant_context(self.tenant.pk):
            warehouse, store = self.sites()
            if is_real_store(store):
                raise Refusal("TEST_TENANT_ONLY", "The test store loader runs only on a synthetic tenant.")
            self.people(warehouse, store)
            config = self.configuration(warehouse, store, rows)
            self.opening(store, config)
            self.open_and_sell(warehouse, store)
        return warehouse, store
