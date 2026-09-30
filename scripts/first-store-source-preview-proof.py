"""Create an isolated, unconfirmed source-preview destination in ALPHA proof.

Run only through first-store-proof.py run. This calls ordinary Owner store
administration, never uploads a workbook, maps a real store identity, expands
assignments, approves readiness or posts stock. Canonical empty system locations
and the fallback unit are retained; no business location or brand is configured.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CODE = "SOH-PREVIEW"
NAME = "Source preview — destination unconfirmed"
COMMAND = "source-preview-create"


def helper() -> Any:
    spec = importlib.util.spec_from_file_location(
        "first_store_onboarding_proof", ROOT / "scripts/first-store-onboarding-proof.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("The guarded onboarding proof helper is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fingerprint(claim: Any, record: dict[str, Any]) -> str:
    """Read-only protected-state comparison; contents are never logged or saved."""
    from accounts.goods_models import RoleAssignment
    from accounts.models import User
    from masters.goods_models import ConfigVersion, MasterVersion, SiteGuard
    from masters.models import Store
    from stockledger.goods_models import CustodyLot, JournalBatch, Origin, Position

    manager = User.objects.get(tenant_id=claim.tenant_id, email=record["manager"]["email"])
    if not manager.email.endswith(".example.test") or manager.human_id is None:
        raise RuntimeError("The generated proof manager identity is unresolved.")
    values = {
        "first_store": list(Store.objects.filter(tenant_id=claim.tenant_id, pk=claim.first_store_id).values(
            "id", "code", "name", "gstin_id", "store_type", "city", "is_active",
        )),
        "first_guard": list(SiteGuard.objects.filter(tenant_id=claim.tenant_id, site_id=claim.first_store_id).values()),
        "first_master": list(MasterVersion.objects.filter(
            tenant_id=claim.tenant_id, kind="site", target_key=str(claim.first_store_id),
        ).order_by("pk").values("pk", "revision", "row_hash")),
        "positions": list(Position.objects.filter(tenant_id=claim.tenant_id).order_by("pk").values()),
        "lots": list(CustodyLot.objects.filter(tenant_id=claim.tenant_id).order_by("pk").values("pk", "row_hash")),
        "origins": list(Origin.objects.filter(tenant_id=claim.tenant_id).order_by("pk").values("pk", "row_hash")),
        "postings": list(JournalBatch.objects.filter(tenant_id=claim.tenant_id).order_by("pk").values("pk", "row_hash")),
        "configuration": list(ConfigVersion.objects.filter(tenant_id=claim.tenant_id).order_by("pk").values("pk", "row_hash")),
        "assignments": list(RoleAssignment.objects.filter(tenant_id=claim.tenant_id).order_by("pk").values()),
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def main() -> None:
    proof = helper()
    claim, record = proof.initialise(), proof.load()
    from core.tenancy import tenant_context
    from masters.goods_models import (
        Location,
        MasterVersion,
        Sbu,
        SiteCapabilityEvent,
        SiteGuard,
    )
    from masters.models import Store
    from sell.models import RegisteredTill
    from stockledger.goods_models import CustodyLot, Origin, Position

    if claim.summary["owner"]["email"] != record["owner"]["email"] or "manager" not in record:
        raise RuntimeError("Private generated proof people do not match the owned registration.")
    with tenant_context(claim.tenant_id):
        first = Store.objects.select_related("gstin__legal_entity").get(
            tenant_id=claim.tenant_id, pk=claim.first_store_id, code="FIRST",
        )
        if first.name != "First proof shop" or first.gstin.legal_entity.name != "Alpha Proof Private Limited":
            raise RuntimeError("The fictional first-store entity changed; no preview was created.")
        before = fingerprint(claim, record)
        owner = proof.login(record, "owner")
        proof.request(owner, "post", "/api/auth/step-up", {"password": record["owner"]["password"]})
        body = proof.command(
            record, COMMAND, code=CODE, name=NAME, type="store",
            entity_id=str(first.gstin.legal_entity_id), registration_id=str(first.gstin_id),
            city="", country="IN", permitted_operations=[], brand_ids=[], counter_count=0,
        )
        existing = Store.objects.filter(tenant_id=claim.tenant_id, code=CODE).first()
        if existing is None:
            response = proof.request(owner, "post", "/api/goods-v1/masters/stores", body)
            site = Store.objects.get(tenant_id=claim.tenant_id, pk=response["id"], code=CODE)
        else:
            site = existing
        authored = MasterVersion.objects.filter(
            tenant_id=claim.tenant_id, kind="site", target_key=str(site.pk),
            command_key__command_id=body["command_id"],
        ).first()
        expected = {key: value for key, value in body.items() if key not in {"command_id", "contract_version"}}
        if authored is None or authored.payload != expected:
            raise RuntimeError("An existing preview destination has different provenance; it was preserved.")
        if site.name != NAME or site.gstin_id != first.gstin_id:
            raise RuntimeError("The proof preview identity changed; no existing destination was relinked.")
        guard = SiteGuard.objects.get(tenant_id=claim.tenant_id, site=site)
        if (guard.lifecycle != "planned" or guard.stock_contract != "goods_v1"
                or guard.opening_setup_ready or guard.goods_ready or guard.sell_ready
                or SiteCapabilityEvent.objects.filter(tenant_id=claim.tenant_id, site=site).exists()):
            raise RuntimeError("The source preview must remain planned and unapproved.")
        if (Sbu.objects.filter(tenant_id=claim.tenant_id, site=site, brand__isnull=False).exists()
                or Location.objects.filter(tenant_id=claim.tenant_id, site=site, system=False).exists()
                or Position.objects.filter(tenant_id=claim.tenant_id, site=site).exists()
                or CustodyLot.objects.filter(tenant_id=claim.tenant_id, initial_site=site).exists()
                or Origin.objects.filter(tenant_id=claim.tenant_id, site=site).exists()
                or RegisteredTill.objects.filter(store=site).exists()):
            raise RuntimeError("The source-preview destination has business setup or stock; it was preserved.")
        if fingerprint(claim, record) != before:
            raise RuntimeError("Protected FIRST stock, configuration or assignment state changed; inspect proof evidence.")
        record["source_preview"] = {"site_id": str(site.pk), "name": NAME, "destination_confirmed": False}
        proof.save(record)
    print(json.dumps({"site_id": str(site.pk), "name": site.name}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- Never disclose generated credentials or framework request bodies.
        print(str(error) if isinstance(error, RuntimeError) else f"Proof preview failed ({type(error).__name__}); review the owned resource before retrying.")
        raise SystemExit(1) from None
