"""Explicit synthetic commercial fixture for the owned first-store browser proof.

This is not tenant setup or tax approval. Run only via first-store-proof.py run.
It refuses every database except the named disposable rehearsal, verifies the
actually accepted fictional three-unit opening, and preserves business history.
Tax, switches, prefix, numbering and counter setup then use ordinary guarded
APIs. The production 1 April rule is unchanged; existing past-start installations
remain blocked instead of adopting a midyear numbering fixture.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / ".local/first-store-commercial-proof.json"
LABEL = "Synthetic commercial browser proof only; no real-shop CA sign-off."


def helper() -> Any:
    spec = importlib.util.spec_from_file_location(
        "first_store_onboarding_proof", ROOT / "scripts/first-store-onboarding-proof.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("The guarded proof helper is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verified_rehearsal() -> dict[str, str]:
    """Resolve the exact positively identified private proof, never arbitrary databases."""
    path = ROOT / ".local/first-store-proof.json"
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("The first-store proof identity must be private.")
    spec = importlib.util.spec_from_file_location("commercial_first_store_proof", ROOT / "scripts/first-store-proof.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The owned first-store identity verifier is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verified_record()


def require_owned_connection(record: dict[str, str], actual: Any, *, host: str, port: str) -> None:
    """Refuse mismatched environment, cluster or endpoint before fixture mutation."""
    database = record.get("database", "")
    system_id = record.get("system_identifier", "")
    if (not database.startswith("kdps_rehearsal_first_store_") or not system_id
            or os.environ.get("KDPS_PROOF_MODE") != "1"
            or os.environ.get("KDPS_REHEARSAL_DB") != database
            or actual != (database, "kdps_proof", system_id)
            or host != "127.0.0.1" or port != "55433"):
        raise RuntimeError("This commercial fixture only owns its exact verified first-store proof.")


def business_fingerprint(tenant_id: Any) -> str:
    """Hash business rows without printing their content or credentials."""
    from core.canonical import content_hash
    from core.gl import GLEntry
    from finledger.models import CashLedgerEntry
    from ptmapper.soh_models import (
        SohImport,
        SohImportBatch,
        SohImportReview,
        SohImportRow,
    )
    from sell.models import OnlineSaleSubmission, Sale, SaleLine, SaleTender
    from stockledger.goods_models import CustodyLot, JournalBatch, Origin, Position

    models = (SohImport, SohImportBatch, SohImportReview, SohImportRow,
              CustodyLot, JournalBatch, Origin, Position, Sale, SaleLine, SaleTender,
              OnlineSaleSubmission, GLEntry, CashLedgerEntry)
    values = {}
    for model in models:
        query = model.objects.all()
        if any(field.name == "tenant" for field in model._meta.fields):
            query = query.filter(tenant_id=tenant_id)
        values[model._meta.label] = list(query.order_by("pk").values())
    return content_hash(values)


def main() -> None:
    rehearsal = verified_rehearsal()
    database, system_id = rehearsal["database"], rehearsal["system_identifier"]
    proof = helper()
    claim, record = proof.initialise(), proof.load()
    from accounts.goods_models import SecurityGuard
    from accounts.models import User
    from core.tenancy import tenant_context
    from django.db import connection, transaction
    from django.utils import timezone
    from masters.document_series import numbering_setting, switch_change_problem
    from masters.document_series_models import DocumentPrefix
    from masters.goods_models import SiteGuard
    from masters.models import Store
    from masters.store_feature_models import StoreFeatureSwitch
    from masters.tax_setting_models import TaxSettingVersion
    from ptmapper.soh_services import opening_reconciliation

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user, system_identifier::text FROM pg_control_system()")
        require_owned_connection(rehearsal, cursor.fetchone(),
            host=str(connection.settings_dict["HOST"]), port=str(connection.settings_dict["PORT"]))
    with tenant_context(claim.tenant_id):
        site = Store.objects.select_related("gstin__legal_entity").get(
            tenant_id=claim.tenant_id, pk=claim.first_store_id, code="FIRST",
        )
        if (site.name != "First proof shop" or site.gstin.legal_entity.name != "Alpha Proof Private Limited"
                or claim.tenant.code != "ALPHA"):
            raise RuntimeError("The owned fictional company/store identity changed.")
        for who in ("owner", "admin", "manager"):
            person = record.get(who, {})
            if not str(person.get("email", "")).endswith(".example.test"):
                raise RuntimeError("Only generated example.test proof identities are accepted.")
            user = User.objects.get(tenant_id=claim.tenant_id, email=person["email"])
            if not user.is_active or user.human_id is None or user.must_change_password:
                raise RuntimeError("The owned proof person's active stable identity is unresolved.")
        reconciliation = opening_reconciliation(site)
        if not reconciliation["passed"] or reconciliation["source_count"] != 1 or reconciliation["quantity"] != 3:
            raise RuntimeError("The real opening writer must first independently approve and accept the exact fictional three-unit source.")
        guard = SiteGuard.objects.get(tenant_id=claim.tenant_id, site=site)
        if guard.lifecycle != SiteGuard.Lifecycle.ACTIVE or not guard.goods_ready or guard.freeze_id is not None:
            raise RuntimeError("The accepted fictional store must have ordinary approved goods readiness and remain unfrozen.")
        preview = SiteGuard.objects.filter(site__code="SOH-PREVIEW", tenant_id=claim.tenant_id).first()
        if preview is not None and (preview.lifecycle != "planned" or preview.goods_ready or preview.sell_ready):
            raise RuntimeError("The real-workbook preview must remain inactive.")
        before = business_fingerprint(claim.tenant_id)
        marker = record.setdefault("commercial_fixture", {
            "database": database, "system_identifier": system_id, "site_id": str(site.pk),
            "synthetic_only": True, "label": LABEL, "initial_quantity": 3,
            "initial_business_fingerprint": before,
        })
        if (marker.get("database") != database or marker.get("system_identifier") != system_id
                or marker.get("site_id") != str(site.pk) or marker.get("label") != LABEL
                or marker.get("synthetic_only") is not True):
            raise RuntimeError("Existing commercial fixture provenance changed; nothing was replaced.")
        # Persist ownership before any fixture mutation, so an interrupted run
        # can resume without adopting an unrelated row or rewinding history.
        proof.save(record)
        with transaction.atomic():
            tenant = type(claim.tenant).objects.select_for_update().get(pk=claim.tenant_id)
            tenant.synthetic = True
            tenant.save(update_fields=["synthetic"])
            switch = StoreFeatureSwitch.objects.select_for_update().filter(tenant_id=claim.tenant_id,
                site=site, feature_key="document-series").first()
            if switch is None and switch_change_problem(claim.tenant_id, timezone.localdate()) is not None:
                raise RuntimeError("An existing past-start numbering configuration refuses a midyear switch; no fixture bypass is permitted.")
            if switch is not None and (not switch.enabled or switch.mode != "manual"):
                raise RuntimeError("An existing document-series switch changed; it was preserved.")

        def administrator() -> Any:
            client = proof.login(record, "admin")
            proof.request(client, "post", "/api/auth/step-up", {"password": record["admin"]["password"]})
            return client

        admin = administrator()

        def change_switch(key: str, command_key: str) -> Any:
            """A switch ends every old session; the next command starts a new one."""
            admin_user = User.objects.get(tenant_id=claim.tenant_id, email=record["admin"]["email"])
            before_epoch = SecurityGuard.objects.get(human_id=admin_user.human_id).epoch
            proof.request(admin, "post", "/api/goods-v1/masters/store-features/switch", proof.command(
                record, command_key, store_id=site.pk, feature_key=key, enabled=True,
            ))
            after_epoch = SecurityGuard.objects.get(human_id=admin_user.human_id).epoch
            persisted = StoreFeatureSwitch.objects.get(tenant_id=claim.tenant_id, site=site, feature_key=key)
            old_session = admin.get("/api/auth/me", HTTP_HOST="127.0.0.1")
            if (after_epoch != before_epoch + 1 or not persisted.enabled
                    or old_session.status_code != 401):
                raise RuntimeError("The successful fictional switch must invalidate the old Admin session exactly once.")
            marker.setdefault("switch_session_invalidations", []).append({
                "feature_key": key, "before_epoch": before_epoch, "after_epoch": after_epoch,
                "old_session_status": old_session.status_code,
            })
            proof.save(record)
            return administrator()

        if switch is None:
            admin = change_switch("document-series", "commercial-number-switch-initial")
        feature = StoreFeatureSwitch.objects.filter(tenant_id=claim.tenant_id, site=site, feature_key="tax-settings").first()
        if feature is None:
            admin = change_switch("tax-settings", "commercial-tax-switch-initial")
        elif not feature.enabled:
            raise RuntimeError("A later disabled proof tax switch was preserved; re-review is required.")
        current_tax = proof.request(admin, "get", "/api/goods-v1/masters/tax-settings")
        tax = TaxSettingVersion.objects.filter(tenant_id=claim.tenant_id, note=LABEL).first()
        if tax is None:
            proof.request(admin, "post", "/api/goods-v1/masters/tax-settings/versions", proof.command(
                record, "commercial-tax-version", expected_revision=current_tax["latest_version"],
                applies_from=timezone.localdate().isoformat(), rules=[{"kind": "flat_rate", "hsn_prefix": "6101",
                "name": "Synthetic proof tax", "rate": "5.00"}], unmatched_rate="0.00", note=LABEL,
            ))
            tax = TaxSettingVersion.objects.get(tenant_id=claim.tenant_id, note=LABEL)
        if tax.version != proof.request(admin, "get", "/api/goods-v1/masters/tax-settings")["latest_version"]:
            raise RuntimeError("A later independently edited tax version was preserved; this fixture cannot overwrite it.")
        prefix = DocumentPrefix.objects.filter(tenant_id=claim.tenant_id, site=site).first()
        if prefix is None:
            proof.request(admin, "post", "/api/goods-v1/masters/document-series/prefixes", proof.command(
                record, "commercial-prefix-initial", site_id=site.pk, code="PRF",
            ))
        elif prefix.code != "PRF":
            raise RuntimeError("A different document prefix was preserved.")
        setting = numbering_setting(claim.tenant_id)
        if setting.revision == 0:
            proof.request(admin, "post", "/api/goods-v1/masters/document-series/setting", proof.command(
                record, "commercial-numbering-initial", new_format_from=setting.new_format_from.isoformat(),
                till_block_size=30,
            ))
        elif setting.till_block_size != 30:
            raise RuntimeError("A later numbering setting was preserved.")
        owner = proof.login(record, "owner")
        proof.request(owner, "post", "/api/auth/step-up", {"password": record["owner"]["password"]})
        counter_path = f"/api/goods-v1/masters/stores/{site.pk}/counter"
        counter = proof.request(owner, "get", counter_path)
        if not counter["registered"]:
            counter = proof.request(owner, "post", counter_path, proof.command(
                record, "commercial-counter", expected_revision=counter["revision"], replace=False,
                reason="Owned synthetic browser proof counter; no real store activation.",
            ))
            if not counter.get("device_token"):
                raise RuntimeError("The new proof counter did not return its one-time pairing credential.")
            marker["device_id"] = counter["device_id"]
            marker["device_token"] = counter["device_token"]
            proof.save(record)
        elif marker.get("device_id") != counter["device_id"] or not marker.get("device_token"):
            raise RuntimeError("An existing counter has no owned private pairing provenance; no credential was replaced.")
        after = business_fingerprint(claim.tenant_id)
        if after != before:
            raise RuntimeError("Business history changed during commercial fixture setup; inspect the disposable proof before continuing.")
        public = {key: value for key, value in marker.items() if key != "device_token"}
        public.update({"tax_version": tax.version, "business_fingerprint_before": before,
                       "business_fingerprint_after": after, "history_preserved": True,
                       "sell_readiness_approved": False,
                       "fixture_exceptions": ["synthetic tenant CA-gate fixture"],
                       "format_start": numbering_setting(claim.tenant_id).new_format_from.isoformat(),
                       "ordinary_apis": ["tax switch", "tax version", "prefix", "unchanged format date", "Owner counter"]})
        descriptor, temporary = tempfile.mkstemp(prefix="commercial-proof-", dir=EVIDENCE.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(public, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, EVIDENCE)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print("Synthetic commercial fixture configured for the exact disposable FIRST browser proof. Business history is unchanged; ordinary sell readiness approval and Manager pairing remain required.")
    print(f"Credential-free fixture evidence: {EVIDENCE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- do not expose private generated credentials in framework tracebacks.
        print(str(error) if isinstance(error, RuntimeError) else f"Commercial proof failed ({type(error).__name__}); review the isolated fixture before retrying.")
        raise SystemExit(1) from None
