"""SOH staging/review into the retained opening writer. No balance is copied."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.utils.dateparse import parse_datetime

from accounts.principal import AccessContext
from approvals.goods_models import ApprovalRequest
from approvals.goods_services import DecisionContext, create_request, register_subject_cells, register_subject_handler, supersede_pending
from core.canonical import content_hash
from core.commands import CommandRun, LockRank
from core.refusals import Refusal
from files.goods_models import EvidenceObject
from masters.goods_config import candidate
from masters.goods_identity_models import GovernanceState, ProductSku, SkuAlias, Style
from masters.goods_identity_services import IdentityProfile, VocabularyValue, parse_attrs, profile_from_version, record_master_version, save_master, sku_identity, vocabulary
from masters.goods_models import ConfigVersion, MasterVersion, SiteGuard
from masters.models import Brand, Season, Store
from ptmapper.soh_models import SohImport, SohImportBatch, SohImportReview, SohImportRow
from ptmapper.soh_parser import SourceRow

STAGE = "opening.import.stage"
PREPARE = "pt.prepare.opening"
APPROVE = "opening.manifest.approve"
MAX_BATCH = 5000
FINANCIAL_NOTES = frozenset({"external_reconciliation_note", "quality_note"})


@dataclass(frozen=True)
class MaterialisationSnapshot:
    """Exact pinned identity config and read-only vocabulary within one command."""
    profile_version: ConfigVersion
    profile: IdentityProfile
    vocabulary: dict[str, list[VocabularyValue]]


def source_cells(source: SohImport) -> set[tuple[int | None, int | None]]:
    # Whole-source previews include the catalogue/exclusion totals. Unresolved
    # rows therefore need explicit all-brand authority, never authority borrowed
    # from the other rows whose stable links happen to have been completed.
    linked = set(source.rows.values_list("mapping__brand_id", flat=True).distinct())
    brand_ids = {int(brand) for brand in linked if brand is not None}
    cells: set[tuple[int | None, int | None]] = {(source.site_id, brand) for brand in brand_ids}
    if not linked or None in linked:
        cells.add((source.site_id, None))
    return cells


def _require(access: AccessContext, action: str, source: SohImport, *, fields: tuple[str, ...] = (), claim: bool = False) -> None:
    cells = source_cells(source)
    allowed = access.covers_all({action}, cells, fields)
    if fields == ("cost",) and action == PREPARE and (source.prepared_by_id == access.human_id or (claim and source.prepared_by_id is None)):
        allowed = allowed or access.covers_all({action}, cells, {"cost_own_pt"})
    if not allowed:
        raise Refusal("ACTION_DENIED", "This source contains a resource or field outside your assignment.", status=403)


def create_source(run: CommandRun, *, site_id: int, evidence: EvidenceObject,
                  metadata: dict[str, Any], parsed: list[SourceRow]) -> SohImport:
    # Concurrent uploads by different people still bind one snapshot per store.
    # The stable store lock exists before the first parent import exists.
    if not run.lock(LockRank.SITE, Store.objects.filter(tenant_id=run.tenant_id, pk=site_id, is_active=True)):
        raise Refusal("NOT_FOUND", "That store was not found.")
    found = SohImport.objects.filter(tenant_id=run.tenant_id, site_id=site_id, source_hash=evidence.sha256).first()
    if found:
        run.audit_after = {"source_import_id": str(found.pk), "unchanged": True}
        return found
    assert run.principal.human_id
    source = SohImport.objects.create(tenant_id=run.tenant_id, site_id=site_id, source_evidence=evidence,
                                      source_hash=evidence.sha256, source_name=evidence.filename,
                                      source_metadata=metadata, uploaded_by_id=run.principal.human_id)
    SohImportRow.objects.bulk_create([
        SohImportRow(tenant_id=run.tenant_id, source_import=source, ordinal=row.ordinal,
                     source_row_key=row.key, barcode=row.barcode, source_brand=row.brand,
                     quantity=row.quantity, mrp_paise=row.mrp, source_rate_paise=row.rate, source=row.source)
        for row in parsed
    ], batch_size=1000)
    run.audit_after = {"source_import_id": str(source.pk), "source_hash": evidence.sha256,
                       "rows": len(parsed), "quantity": metadata["quantity"]}
    return source


def locked_source(run: CommandRun, source: SohImport, expected: int | None) -> SohImport:
    found = run.lock(LockRank.DOCUMENT, SohImport.objects.filter(tenant_id=run.tenant_id, pk=source.pk))
    if not found:
        raise Refusal("NOT_FOUND", "That SOH source was not found.")
    source = found[0]
    if expected != source.revision:
        raise Refusal("REVISION_SUPERSEDED", "This import changed; reload its current revision.", status=409)
    if source.state == "withdrawn":
        raise Refusal("SOH_SOURCE_WITHDRAWN", "This source is retained as withdrawn evidence; upload the reviewed replacement.", status=409)
    if source.batches.exists():
        raise Refusal("SOH_ALREADY_APPLIED", "Child manifests already exist; use the governed PT correction or reversal workflow.", status=409)
    return source


def _configuration(raw: dict[str, Any], source: SohImport, now: datetime) -> dict[str, Any]:
    allowed = {"brand_mappings", "season_mappings", "size_mappings", "hsn_mappings", "identity_profile_id", "profile_version_id",
               "cutoff_at", "rate_meaning", "valuation_evidence_id", "reconciliation_evidence_id", "source_store_confirmed",
               "external_reconciliation_note", "quality_note", "fresh_source_confirmed", "row_overrides", "unknown_season_sources"}
    if set(raw) - allowed:
        raise Refusal("INVALID_REQUEST", "Unknown SOH configuration fields.")
    cutoff = parse_datetime(str(raw.get("cutoff_at") or ""))
    if cutoff is None or cutoff.tzinfo is None or cutoff > now:
        raise Refusal("SOH_NOT_READY", "Record the actual source cutoff timestamp and timezone.", status=422)
    if raw.get("fresh_source_confirmed") is not True:
        raise Refusal("SOH_NOT_READY", "Confirm a fresh cutover source and no unrecorded movements against its reviewed cutoff.", status=422)
    for field in ("brand_mappings", "season_mappings", "size_mappings", "hsn_mappings", "row_overrides"):
        if not isinstance(raw.get(field, {}), dict):
            raise Refusal("INVALID_REQUEST", f"{field} must be a mapping.")
    if not isinstance(raw.get("unknown_season_sources", []), list) or any(not isinstance(value, str) for value in raw.get("unknown_season_sources", [])):
        raise Refusal("INVALID_REQUEST", "unknown_season_sources must list explicitly reviewed source values.")
    for field, kind in (("identity_profile_id", "identity_profile"), ("profile_version_id", "profile")):
        try:
            version_id = uuid.UUID(str(raw.get(field)))
        except ValueError:
            raise Refusal("SOH_NOT_READY", f"Choose an approved {kind}.", status=422) from None
        version = ConfigVersion.objects.filter(tenant_id=source.tenant_id, pk=version_id, kind=kind).first()
        if version is None or candidate(version).state(now) != "effective":
            raise Refusal("SOH_NOT_READY", f"The selected {kind} is not currently effective.", status=422)
        if kind == "profile":
            from ptmapper.goods_manifest_services import manifest_target
            from ptmapper.goods_pt_services import load_profile
            try:
                brand_ids = {int(value) for value in raw.get("brand_mappings", {}).values()}
            except (TypeError, ValueError):
                raise Refusal("SOH_MAPPING_INCOMPLETE", "Brand mappings must name stable brand IDs.", status=422) from None
            load_profile(source.tenant_id, version_id, manifest_target(cutoff, source.site_id, brand_ids), code="SOH_NOT_READY")
    if raw.get("rate_meaning") not in {"basic_ex_tax", "reviewed_row_values"}:
        raise Refusal("SOH_NOT_READY", "Rate remains unconfirmed. Record its supported basic-value meaning or explicitly reviewed row values.", status=422)
    for field in ("valuation_evidence_id", "reconciliation_evidence_id"):
        try:
            evidence_id = uuid.UUID(str(raw.get(field)))
        except ValueError:
            raise Refusal("SOH_NOT_READY", f"Provide {field}.", status=422) from None
        evidence = EvidenceObject.objects.filter(tenant_id=source.tenant_id, pk=evidence_id).first()
        if evidence is None:
            raise Refusal("SOH_NOT_READY", f"Unknown {field}.", status=422)
        if evidence.pk == source.source_evidence_id or not {"cost", "financial"} <= set(evidence.contains_fields) or str(source.site_id) not in {str(site) for site in evidence.scope.get("site_ids", [])}:
            raise Refusal("SOH_NOT_READY", f"{field} must be separate protected cost/financial evidence explicitly scoped to this store.", status=422)
    if raw.get("source_store_confirmed") is not True or not str(raw.get("external_reconciliation_note") or "").strip() or not str(raw.get("quality_note") or "").strip():
        raise Refusal("SOH_NOT_READY", "Confirm the source store and record source quality and external-book reconciliation.", status=422)
    return {**raw, "cutoff_at": cutoff.isoformat()}


def prepare(run: CommandRun, access: AccessContext, source: SohImport, configuration: dict[str, Any], expected: int | None) -> SohImport:
    source = locked_source(run, source, expected)
    if source.prepared_by_id != access.human_id:
        raise Refusal("SOH_PREPARER_REQUIRED", "Start preparation as this source's named scoped preparer before authoring valuation.", status=403)
    _require(access, PREPARE, source, fields=("cost",))
    if not isinstance(configuration, dict):
        raise Refusal("INVALID_REQUEST", "SOH configuration must be an object.")
    # These are author-provided evidence declarations, not authority to read or
    # change financial records. A projected DTO omits stored financial notes;
    # an unrelated mapping edit must preserve their exact saved content.
    configuration = {**{key: source.configuration[key] for key in FINANCIAL_NOTES
                       if key not in configuration and key in source.configuration}, **configuration}
    config = _configuration(configuration, source, run.now)
    brand_map = config.get("brand_mappings", {})
    brands = {brand.pk: brand for brand in Brand.objects.filter(tenant_id=run.tenant_id, pk__in=list(brand_map.values()), is_active=True)}
    # Explicit stable links only. Text may suggest an option in the UI, never grant it.
    rows = list(source.rows.order_by("ordinal"))
    profile_version = ConfigVersion.objects.get(tenant_id=run.tenant_id, pk=config["identity_profile_id"], kind="identity_profile")
    profile = profile_from_version(profile_version)
    if profile is None:
        raise Refusal("SOH_NOT_READY", "The selected identity profile is incomplete.", status=422)
    current_vocabulary = vocabulary(run.tenant_id, run.now)
    season_ids = {str(value) for value in config.get("season_mappings", {}).values()}
    season_ids.update(str(value.get("season_id")) for value in config.get("row_overrides", {}).values() if isinstance(value, dict) and value.get("season_id"))
    if any(not value.isdigit() for value in season_ids):
        raise Refusal("SOH_MAPPING_INCOMPLETE", "Season mappings must name reviewed stable season IDs.", status=422)
    seasons = {str(season.pk): season for season in Season.objects.filter(pk__in=season_ids)}
    season_versions: dict[str, MasterVersion] = {}
    for version in MasterVersion.objects.filter(tenant_id=run.tenant_id, kind="season", target_key__in=season_ids).order_by("target_key", "-revision"):
        season_versions.setdefault(version.target_key, version)
    aliases_by_barcode: dict[str, list[SkuAlias]] = {}
    for alias in SkuAlias.objects.filter(tenant_id=run.tenant_id, alias_type="barcode", value__in=[r.barcode for r in rows],
                                        governance_state=GovernanceState.EFFECTIVE, effective_from__lte=run.now,
                                        sku__governance_state=GovernanceState.EFFECTIVE,
                                        sku__style__governance_state=GovernanceState.EFFECTIVE).filter(models_q_for_alias(source.site_id, run.now)).select_related("sku__style"):
        aliases_by_barcode.setdefault(alias.value, []).append(alias)
    for row in rows:
        override = config.get("row_overrides", {}).get(row.barcode, {})
        if not isinstance(override, dict):
            raise Refusal("INVALID_REQUEST", "Each row override must be an object.")
        if set(override) - {"exclude_reason", "basic_paise", "season_id", "season_unknown_historical", "hsn"}:
            raise Refusal("INVALID_REQUEST", "Unknown row override fields.")
        excluded = str(override.get("exclude_reason") or "").strip()[:500]
        if row.quantity <= 0:
            row.exclusion_reason = "Catalogue only: zero source quantity" if row.quantity == 0 else "Negative source quantity requires reconciliation"
            row.mapping = {}
            continue
        if excluded:
            row.exclusion_reason, row.mapping = excluded, {}
            continue
        try:
            brand_id = int(brand_map.get(row.source_brand, 0))
        except (TypeError, ValueError):
            brand_id = 0
        if brand_id not in brands:
            raise Refusal("SOH_MAPPING_INCOMPLETE", f"Map source brand {row.source_brand} to a reviewed stable brand ID.", status=422)
        if not (access.covers_all({PREPARE}, {(source.site_id, brand_id)}, {"cost"}) or
                access.covers_all({PREPARE}, {(source.site_id, brand_id)}, {"cost_own_pt"})):
            raise Refusal("FIELD_DENIED", "The valuation preparer needs own-PT cost authority on every mapped resource.", status=403)
        access.require("product.master.manage", brand_id=brand_id)
        season_raw = override.get("season_id") or config.get("season_mappings", {}).get(str(row.source.get("season") or ""))
        season = seasons.get(str(season_raw))
        season_version = season_versions.get(str(season_raw))
        if season is None or season_version is None or season_version.retired or season_version.effective_from > run.now or (season_version.effective_to is not None and season_version.effective_to <= run.now):
            raise Refusal("SOH_MAPPING_INCOMPLETE", f"Barcode {row.barcode} needs a reviewed real season or deliberate unknown historical season.", status=422)
        if season.historical_unknown and override.get("season_unknown_historical") is not True and str(row.source.get("season") or "") not in config.get("unknown_season_sources", []):
            raise Refusal("SOH_MAPPING_INCOMPLETE", f"Barcode {row.barcode}: deliberately declare unknown historical season in its row override.", status=422)
        hsn = str(override.get("hsn") or config.get("hsn_mappings", {}).get(str(row.source.get("category") or "")) or "").strip()
        basic_raw = override.get("basic_paise") if "basic_paise" in override else row.source_rate_paise if config["rate_meaning"] == "basic_ex_tax" else None
        try:
            basic = int(str(basic_raw))
        except (TypeError, ValueError):
            basic = 0
        mrp = row.mrp_paise
        if basic <= 0 or mrp is None or mrp <= 0 or basic > mrp or len(hsn) not in {4, 6, 8} or not hsn.isdigit():
            raise Refusal("SOH_MAPPING_INCOMPLETE", f"Barcode {row.barcode} needs evidenced positive cost/MRP and HSN, or an owned exclusion.", status=422)
        row.exclusion_reason = ""
        aliases = aliases_by_barcode.get(row.barcode, [])
        if aliases and (len({alias.sku_id for alias in aliases}) != 1 or aliases[0].sku.style.brand_id != brand_id):
            raise Refusal("SOH_IDENTITY_CONFLICT", f"Barcode {row.barcode} has conflicting stable links; resolve them before review.", status=422)
        sku_id = aliases[0].sku_id if aliases else uuid.uuid5(source.pk, f"sku:{row.barcode}")
        row.mapping = {"brand_id": brand_id, "season_id": season.pk, "season_unknown_historical": season.historical_unknown,
                       "hsn": hsn, "basic_paise": str(basic), "identity_profile_id": str(config.get("identity_profile_id") or ""), "sku_id": str(sku_id)}
        if not aliases:
            prospective = Style(id=uuid.uuid5(source.pk, f"style:{row.barcode}"), tenant_id=run.tenant_id, brand_id=brand_id,
                                style_code=f"SOH-{uuid.uuid5(source.pk, f'style:{row.barcode}').hex}", profile_family=profile.family, attrs=[])
            sku_identity(run.tenant_id, prospective, profile, parse_attrs(_attributes(config, row, profile)), run.now, current_vocabulary=current_vocabulary)
    SohImportRow.objects.bulk_update(rows, ["mapping", "exclusion_reason"], batch_size=1000)
    source.configuration = config
    source.cutoff_at = parse_datetime(config["cutoff_at"])
    source.prepared_by_id = run.principal.human_id
    source.approved_by = None
    source.approval_request = None
    source.state = "review"
    source.revision += 1
    source.reviewed_hash = review_hash(source, rows)
    source.save()
    _record_review(run, source, rows)
    supersede_pending(run, "soh_import", str(source.pk), APPROVE)
    run.audit_after = {"source_import_id": str(source.pk), "revision": source.revision, "reviewed_hash": source.reviewed_hash}
    return source


def review_hash(source: SohImport, rows: list[SohImportRow] | None = None) -> str:
    return content_hash(review_payload(source, rows))


def review_payload(source: SohImport, rows: list[SohImportRow] | None = None) -> dict[str, Any]:
    return {"source_hash": source.source_hash, "source_metadata": source.source_metadata,
                         "uploaded_by_id": str(source.uploaded_by_id), "prepared_by_id": str(source.prepared_by_id) if source.prepared_by_id else None,
                         "configuration": source.configuration,
                         "rows": [{"key": r.source_row_key, "source_input_hash": content_hash({
                                       "ordinal": r.ordinal, "barcode": r.barcode, "quantity": r.quantity,
                                       "mrp_paise": r.mrp_paise, "source_rate_paise": r.source_rate_paise, "source": r.source}),
                                   "mapping": r.mapping, "verification": r.verification,
                                   "excluded": r.exclusion_reason} for r in (rows if rows is not None else list(source.rows.order_by("ordinal")))]}


def _record_review(run: CommandRun, source: SohImport, rows: list[SohImportRow] | None = None) -> None:
    run.record(SohImportReview(source_import=source, revision=source.revision,
                              content_hash=source.reviewed_hash, payload=review_payload(source, rows)))


def claim_preparation(run: CommandRun, access: AccessContext, source: SohImport, expected: int | None) -> SohImport:
    """Record document ownership before own-PT cost may be read or authored."""
    source = locked_source(run, source, expected)
    _require(access, PREPARE, source, fields=("cost",), claim=True)
    if source.prepared_by_id is not None:
        if source.prepared_by_id != access.human_id:
            raise Refusal("SOH_PREPARER_ASSIGNED", "A different person already owns this preparation; use independent source withdrawal or governed correction.", status=409)
        return source
    source.prepared_by_id = access.human_id
    source.revision += 1
    source.reviewed_hash = review_hash(source)
    source.save(update_fields=["prepared_by", "revision", "reviewed_hash"])
    _record_review(run, source)
    run.audit_after = {"source_import_id": str(source.pk), "revision": source.revision, "preparer_id": str(access.human_id)}
    return source


def verify_rows(run: CommandRun, access: AccessContext, source: SohImport, observations: list[dict[str, Any]], expected: int | None) -> SohImport:
    source = locked_source(run, source, expected)
    _require(access, STAGE, source)
    if not isinstance(observations, list) or not 1 <= len(observations) <= MAX_BATCH:
        raise Refusal("INVALID_REQUEST", "Provide 1 to 5,000 physical observations.")
    keyed = {str(raw.get("barcode")): raw for raw in observations if isinstance(raw, dict)}
    if len(keyed) != len(observations):
        raise Refusal("INVALID_REQUEST", "Observations must name distinct barcodes.")
    rows = list(source.rows.filter(barcode__in=keyed))
    if len(rows) != len(keyed):
        raise Refusal("NOT_FOUND", "An observed barcode is outside this source.")
    for row in rows:
        raw = keyed[row.barcode]
        qty, condition = raw.get("observed_qty"), raw.get("observed_condition")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0 or condition not in {"good", "damaged", "wrong", "unidentified"}:
            raise Refusal("INVALID_REQUEST", "Physical observations need a whole nonnegative quantity and known condition.")
        if not str(raw.get("reason") or "").strip():
            raise Refusal("INVALID_REQUEST", "Record the physical-verification evidence/reason.")
        row.verification = {"observed_qty": qty, "observed_condition": condition, "notes": str(raw["reason"])[:500],
                            "observer_id": str(run.principal.human_id), "observed_at": run.now.isoformat()}
    SohImportRow.objects.bulk_update(rows, ["verification"], batch_size=1000)
    source.approved_by = None
    source.approval_request = None
    source.state = "review" if source.configuration else "uploaded"
    source.revision += 1
    source.reviewed_hash = review_hash(source)
    source.save()
    _record_review(run, source)
    supersede_pending(run, "soh_import", str(source.pk), APPROVE)
    run.audit_after = {"source_import_id": str(source.pk), "observed_rows": len(rows), "revision": source.revision}
    return source


def submit(run: CommandRun, access: AccessContext, source: SohImport, expected: int | None) -> SohImport:
    source = locked_source(run, source, expected)
    _require(access, PREPARE, source, fields=("cost",))
    if source.prepared_by_id != run.principal.human_id or not source.configuration:
        raise Refusal("SOH_NOT_READY", "The named valuation preparer must submit the reviewed import.", status=422)
    rows = list(source.rows.filter(quantity__gt=0, exclusion_reason=""))
    zero_snapshot = (not rows and requires_inventory_reconciliation(source)
                     and source.rows.exists() and not source.rows.exclude(quantity=0).exists()
                     and all(row.verification.get("observed_qty") == 0 for row in source.rows.all()))
    if (not rows and not zero_snapshot) or any(not row.mapping or not row.verification for row in rows):
        raise Refusal("SOH_NOT_READY", "Every included stocked row needs reviewed mapping and actual physical verification.", status=422)
    _configuration(source.configuration, source, run.now)
    source.reviewed_hash = review_hash(source)
    source.approval_request = create_request(run, subject_kind="soh_import", subject_key=str(source.pk),
                                             revision=source.revision, reviewed_hash=source.reviewed_hash,
                                             requested_action=APPROVE, site_id=source.site_id,
                                             require_distinct=True, required_roles=["owner"],
                                             title=f"Review SOH migration source {source.source_name}")
    source.state = "submitted"
    source.save()
    run.audit_after = {"source_import_id": str(source.pk), "reviewed_hash": source.reviewed_hash}
    return source


def _decide(run: CommandRun, context: DecisionContext) -> dict[str, Any]:
    sources = run.lock(LockRank.DOCUMENT, SohImport.objects.filter(tenant_id=run.tenant_id, pk=context.request.subject_key))
    if not sources:
        raise Refusal("NOT_FOUND", "That SOH import was not found.")
    source = sources[0]
    _require(context.access, APPROVE, source, fields=("cost", "financial"))
    observers = {r.verification.get("observer_id") for r in source.rows.filter(exclusion_reason="") if r.verification}
    if str(context.checker_id) in {str(source.prepared_by_id), str(source.uploaded_by_id), *observers}:
        raise Refusal("SELF_APPROVAL", "The source uploader, preparer and physical verifier cannot approve this import.", status=403)
    if source.state != "submitted" or source.revision != context.request.revision or review_hash(source) != context.request.reviewed_hash:
        raise Refusal("REVISION_SUPERSEDED", "The import changed after review.", status=409)
    if context.decision == "reject":
        source.state = "review"
    else:
        _configuration(source.configuration, source, run.now)
        source.state, source.approved_by_id = "approved", context.checker_id
    source.save()
    return {"state": source.state, "source_hash": source.source_hash}


def approved_source_for_opening(tenant_id: uuid.UUID, site_id: int, source_evidence_id: Any = None) -> SohImport | None:
    sources = SohImport.objects.filter(tenant_id=tenant_id, site_id=site_id, state__in=["approved", "applied"], approved_by__isnull=False)
    if source_evidence_id is not None:
        sources = sources.filter(source_evidence_id=source_evidence_id)
    return sources.order_by("-created_at").first()


def _attributes(config: dict[str, Any], row: SohImportRow, profile: Any) -> list[dict[str, Any]]:
    attrs: list[dict[str, Any]] = []
    for dimension in profile.dimensions:
        source_text = str(row.source.get(dimension.casefold().replace("_", "")) or "")
        if dimension == profile.size_dimension:
            value_id = config.get("size_mappings", {}).get(str(row.source.get("size") or ""))
            if source_text and not value_id:
                raise Refusal("SOH_MAPPING_INCOMPLETE", f"Map size {source_text} to a stable vocabulary value.", status=422)
            attrs.append({"field_id": dimension, "vocabulary_value_id": str(value_id)} if value_id else {"field_id": dimension, "unknown": True})
        elif source_text:
            attrs.append({"field_id": dimension, "supplied_text": source_text})
        else:
            attrs.append({"field_id": dimension, "unknown": True})
    return attrs


def _materialisation_snapshot(run: CommandRun, source: SohImport) -> MaterialisationSnapshot:
    profile_version = ConfigVersion.objects.filter(tenant_id=run.tenant_id, pk=source.configuration["identity_profile_id"], kind="identity_profile").first()
    if profile_version is None or candidate(profile_version).state(run.now) != "effective":
        raise Refusal("SOH_NOT_READY", "The approved identity profile must still be effective.", status=422)
    profile = profile_from_version(profile_version)
    if profile is None:
        raise Refusal("SOH_NOT_READY", "The approved identity profile is incomplete.", status=422)
    return MaterialisationSnapshot(profile_version, profile, vocabulary(run.tenant_id, run.now))


def _materialise(run: CommandRun, access: AccessContext, source: SohImport, row: SohImportRow,
                 *, snapshot: MaterialisationSnapshot | None = None) -> uuid.UUID:
    mapping = row.mapping
    access.require("product.master.manage", brand_id=int(mapping["brand_id"]))
    snapshot = snapshot or _materialisation_snapshot(run, source)
    profile_version, profile = snapshot.profile_version, snapshot.profile
    if profile_version.tenant_id != run.tenant_id or str(profile_version.pk) != str(mapping["identity_profile_id"]):
        raise Refusal("SOH_IDENTITY_CONFLICT", "The row does not match the exact approved identity profile.", status=422)
    # SKU reuse requires an already effective exact stable alias with same reviewed brand.
    aliases = list(SkuAlias.objects.filter(tenant_id=run.tenant_id, alias_type="barcode", value=row.barcode,
                                          governance_state=GovernanceState.EFFECTIVE, effective_from__lte=run.now,
                                          sku__governance_state=GovernanceState.EFFECTIVE,
                                          sku__style__governance_state=GovernanceState.EFFECTIVE)
                   .filter(models_q_for_alias(source.site_id, run.now)).select_related("sku__style"))
    if aliases:
        ids = {alias.sku_id for alias in aliases}
        if len(ids) != 1 or aliases[0].sku.style.brand_id != int(mapping["brand_id"]) or str(aliases[0].sku_id) != mapping["sku_id"]:
            raise Refusal("SOH_IDENTITY_CONFLICT", f"Barcode {row.barcode} has conflicting reviewed identity.", status=422)
        return aliases[0].sku_id
    style_id = uuid.uuid5(source.pk, f"style:{row.barcode}")
    style = Style.objects.filter(pk=style_id).first()
    if style is None:
        style = Style(id=style_id, tenant_id=run.tenant_id, brand_id=int(mapping["brand_id"]),
                      style_code=f"SOH-{style_id.hex}", profile_family=profile.family,
                      attrs=[{"field_id": "source_design", "supplied_text": str(row.source.get("designno") or row.barcode)}],
                      governance_state=GovernanceState.EFFECTIVE)
        save_master(style, conflict="The reviewed SOH style conflicts with an existing identity.")
        record_master_version(run, "style", style, approval_id=source.approval_request_id)
    attrs = _attributes(source.configuration, row, profile)
    key, stored = sku_identity(run.tenant_id, style, profile, parse_attrs(attrs), run.now, current_vocabulary=snapshot.vocabulary)
    sku = ProductSku.objects.filter(tenant_id=run.tenant_id, identity_key=key).first()
    if sku is None:
        sku = ProductSku(id=uuid.UUID(mapping["sku_id"]), tenant_id=run.tenant_id, style=style,
                         identity_key=key, identity_profile=profile_version, attrs=stored, governance_state=GovernanceState.EFFECTIVE)
        save_master(sku, conflict="The reviewed SKU identity conflicts.")
        record_master_version(run, "sku", sku, approval_id=source.approval_request_id)
    assert source.cutoff_at is not None
    alias = SkuAlias(id=uuid.uuid5(source.pk, f"barcode:{row.barcode}"), tenant_id=run.tenant_id, sku=sku,
                     issuer_key=f"soh:{source.pk}", alias_type="barcode", value=row.barcode, site_id=source.site_id,
                     effective_from=source.cutoff_at, governance_state=GovernanceState.EFFECTIVE)
    save_master(alias, conflict="The reviewed source barcode conflicts.")
    record_master_version(run, "alias", alias, approval_id=source.approval_request_id)
    return sku.pk


def models_q_for_alias(site_id: int, at: datetime) -> Any:
    from django.db.models import Q
    return (Q(site_id=site_id) | Q(site__isnull=True)) & (Q(effective_to__isnull=True) | Q(effective_to__gt=at))


def requires_inventory_reconciliation(source: SohImport) -> bool:
    """A fresh full snapshot must never add another whole opening balance.

    The first reviewed source owns all of its deterministic children, even when
    interrupted. A different source or prior custody/stock history requires an
    append-only delta workflow, not another opening. Historical evidence keeps
    this true even after every original piece has been sold or moved away.
    """
    from ptmapper.goods_models import OpeningManifest
    from stockledger.goods_models import CustodyLot, Origin, Position
    own_manifests = source.batches.values_list("manifest_id", flat=True)
    if OpeningManifest.objects.filter(tenant_id=source.tenant_id, site_id=source.site_id).exclude(pk__in=own_manifests).exists():
        return True
    if source.batches.exists():
        return False  # Same approved initial parent may finish its remaining batches.
    return (CustodyLot.objects.filter(tenant_id=source.tenant_id, initial_site_id=source.site_id).exists()
            or Origin.objects.filter(tenant_id=source.tenant_id, site_id=source.site_id).exists()
            or Position.objects.filter(tenant_id=source.tenant_id, site_id=source.site_id, boundary="physical").exists())


def apply_batch(run: CommandRun, access: AccessContext, source: SohImport, batch_index: int, expected: int | None = None) -> SohImportBatch:
    from ptmapper import goods_manifest_services
    # This is the same stable site guard the canonical movement/sale writers
    # lock. Claim it before the source document so concurrent fresh snapshots
    # cannot each establish an initial opening parent.
    if not run.lock(LockRank.SITE, SiteGuard.objects.filter(tenant_id=run.tenant_id, site_id=source.site_id)):
        raise Refusal("OPENING_NOT_READY", "That store has no governed opening capability.", status=409)
    found_source = run.lock(LockRank.DOCUMENT, SohImport.objects.filter(tenant_id=run.tenant_id, pk=source.pk))
    if not found_source:
        raise Refusal("NOT_FOUND", "That SOH source was not found.")
    source = found_source[0]
    if expected is not None and expected != source.revision:
        raise Refusal("REVISION_SUPERSEDED", "This import changed; reload its current revision.", status=409)
    if isinstance(batch_index, bool) or not isinstance(batch_index, int) or batch_index < 1:
        raise Refusal("INVALID_REQUEST", "batch_index must be a positive whole number.")
    _require(access, PREPARE, source, fields=("cost",))
    if source.state not in {"approved", "applied"} or not source.approved_by_id or source.reviewed_hash != review_hash(source):
        raise Refusal("SOH_NOT_READY", "The exact migration source must be independently approved before application.", status=422)
    found = source.batches.filter(batch_index=batch_index).first()
    if found:
        return found
    if requires_inventory_reconciliation(source):
        raise Refusal("SOH_RECONCILIATION_REQUIRED", "This store already has opening or stock history. Keep this new snapshot inactive until its reviewed inventory delta is applied; another full opening would duplicate stock.", status=409)
    _configuration(source.configuration, source, run.now)
    rows = list(source.rows.filter(quantity__gt=0, exclusion_reason="").order_by("ordinal")[(batch_index - 1) * MAX_BATCH : batch_index * MAX_BATCH])
    if not rows:
        raise Refusal("INVALID_REQUEST", "Unknown import batch.")
    snapshot = _materialisation_snapshot(run, source)
    payloads = []
    for row in rows:
        sku_id = _materialise(run, access, source, row, snapshot=snapshot)
        mapped = row.mapping
        payloads.append({"row": {"source_row_key": row.source_row_key, "site_id": source.site_id, "condition": "good",
                                  "identity": {"sku_id": str(sku_id), "raw_alias": row.barcode,
                                               "description": str(row.source.get("itemname") or f"{source.source_name}!{row.source_row_key}")[:240], "attributes": []},
                                  "qty": row.quantity, "basic_paise": mapped["basic_paise"], "mrp_paise": str(row.mrp_paise),
                                  "hsn": mapped["hsn"], "season_id": mapped["season_id"], "season_unknown_historical": mapped["season_unknown_historical"]},
                         "verification": {key: row.verification[key] for key in ("observed_qty", "observed_condition", "notes")}})
    assert source.cutoff_at is not None
    manifest = goods_manifest_services.create_manifest(run, body={"site_id": source.site_id, "batch_key": f"soh-{source.pk}-{batch_index}",
                                                                  "dataset_key": f"soh:{source.source_hash}", "cutoff_at": source.cutoff_at.isoformat(),
                                                                  "source_evidence_id": str(source.source_evidence_id),
                                                                  "profile_version_id": source.configuration["profile_version_id"],
                                                                  "rows": payloads, "_soh_import_id": str(source.pk)})
    # Reuse never extends validity: recheck every exact pinned config after the
    # potentially lengthy canonical row writers and before this command commits.
    _configuration(source.configuration, source, run.now)
    batch = SohImportBatch.objects.create(tenant_id=run.tenant_id, source_import=source, batch_index=batch_index,
                                         manifest=manifest, row_count=len(rows), quantity=sum(r.quantity for r in rows))
    count = source.rows.filter(quantity__gt=0, exclusion_reason="").count()
    if source.batches.count() == (count + MAX_BATCH - 1) // MAX_BATCH:
        source.state = "applied"
        source.save(update_fields=["state"])
    run.audit_after = {"source_import_id": str(source.pk), "batch": batch_index, "manifest_id": str(manifest.pk), "quantity": batch.quantity}
    return batch


def opening_reconciliation(site: Any) -> dict[str, Any]:
    from core.kernel_models import DocumentHead
    from ptmapper.goods_models import GoodsPt
    from stockledger.goods_acceptance import line_progress
    from ptmapper.goods_manifest_services import accepted_rows, reconcile_opening
    from core.kernel_models import OfficialLine
    candidates = SohImport.objects.filter(tenant_id=site.tenant_id, site_id=site.pk).exclude(state="withdrawn")
    # A later staged snapshot is evidence for a future reconciliation. Uploading
    # it alone neither changes the established opening nor disables live selling.
    # Before any source is materialised, every pending source stays unready.
    materialised = candidates.filter(batches__isnull=False).distinct()
    sources = list(materialised if materialised.exists() else candidates)
    failures: list[str] = []
    approved_qty = 0
    for source in sources:
        if not source.approved_by_id or source.reviewed_hash != review_hash(source):
            failures.append(f"Import {source.pk} no longer matches its independently reviewed source")
        if source.state != "applied":
            failures.append(f"Import {source.pk} has unapplied or unapproved batches")
        for batch in source.batches.all():
            pt = GoodsPt.objects.filter(manifest_version_id=batch.manifest.approved_version_id).first() if batch.manifest.approved_version_id else None
            head = DocumentHead.objects.filter(document_id=pt.document_id).first() if pt else None
            if head is None or head.state != "official" or head.live_version_id is None:
                failures.append(f"Batch {batch.batch_index} has no official opening PT")
            else:
                version = head.live_version
                assert version is not None
                approved_version = batch.manifest.approved_version
                assert approved_version is not None
                official = list(OfficialLine.objects.filter(version=version))
                lines = [line.payload for line in official]
                if not reconcile_opening(approved_version.pk, lines, version.content_hash)["passed"]:
                    failures.append(f"Batch {batch.batch_index} has a source/official quantity difference")
                source_rows = {row.source_row_key: row for row in source.rows.filter(quantity__gt=0, exclusion_reason="")}
                manifest_rows = {str(row.pk): row for row in approved_version.rows.all()}
                for line in lines:
                    covered = line.get("coverage_requests") or []
                    manifest_row = manifest_rows.get(str(covered[0].get("manifest_row_id"))) if len(covered) == 1 else None
                    source_row = source_rows.get(manifest_row.source_row_key) if manifest_row else None
                    supplied = line.get("supplied") or {}
                    if source_row is None or str(supplied.get("basic_paise")) != str(source_row.mapping.get("basic_paise")) or str(supplied.get("mrp_paise")) != str(source_row.mrp_paise):
                        failures.append(f"Batch {batch.batch_index} has an unexplained source/official value difference")
                        break
                progress = line_progress(version)
                if any(row["remaining_qty"] or row["accepted_qty"] + row["damaged_qty"] != row["expected_qty"] for row in progress):
                    failures.append(f"Batch {batch.batch_index} has stock awaiting physical acceptance or unexplained acceptance totals")
                approved_qty += sum(accepted_rows(batch.manifest).values())
    return {"passed": bool(sources) and not failures, "quantity": approved_qty, "reasons": failures, "source_count": len(sources)}


def is_reconciled(site: Any) -> bool:
    return bool(opening_reconciliation(site)["passed"])


def withdraw(run: CommandRun, access: AccessContext, source: SohImport, reason: str, expected: int | None) -> SohImport:
    source = locked_source(run, source, expected)
    _require(access, APPROVE, source)
    access.require_step_up()
    if str(access.human_id) in {str(source.uploaded_by_id), str(source.prepared_by_id)}:
        raise Refusal("SELF_APPROVAL", "A different independently authorised owner must withdraw this source.", status=403)
    if not reason.strip():
        raise Refusal("INVALID_REQUEST", "Record why this source is superseded or withdrawn.")
    supersede_pending(run, "soh_import", str(source.pk), APPROVE)
    source.state = "withdrawn"
    source.revision += 1
    source.save()
    run.audit_after = {"source_import_id": str(source.pk), "state": "withdrawn", "reason": reason[:500]}
    return source


def _request_cells(requests: list[ApprovalRequest]) -> dict[Any, frozenset[tuple[int | None, int | None]]]:
    return {request.pk: frozenset(source_cells(source))
            for request in requests for source in SohImport.objects.filter(pk=request.subject_key)}


def install() -> None:
    register_subject_handler("soh_import", APPROVE, _decide, policy_governed=False)
    register_subject_cells("soh_import", APPROVE, _request_cells)
