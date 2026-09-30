"""Prepare fictional opening-stock browser inputs through governed APIs only.

Run via first-store-proof.py run. The helper refuses other databases, tenants
and people. It never imports the user's real workbook, seeds business rows or
changes a feature gate. Public artefacts describe a three-unit test scenario.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / ".local" / "first-store-opening-browser"


def helper() -> Any:
    spec = importlib.util.spec_from_file_location("first_store_onboarding_proof", ROOT / "scripts/first-store-onboarding-proof.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The guarded onboarding proof helper is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_once(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise RuntimeError(f"Existing proof artefact differs: {path.name}; it was preserved.")
        return
    with path.open("xb") as stream:
        stream.write(data)


def main() -> None:
    proof = helper()
    claim, record = proof.initialise(), proof.load()
    from approvals.goods_models import ApprovalRequest
    from core.documents import VoucherSeries
    from core.fiscal import financial_year
    from core.numbering import publish_ceiling, series_store_code
    from core.tenancy import tenant_context
    from django.utils import timezone
    from masters.goods_identity_services import vocabulary_value_id
    from masters.goods_models import ConfigVersion
    from openpyxl import Workbook

    def actor(who: str) -> Any:
        client = proof.login(record, who)
        proof.request(client, "post", "/api/auth/step-up", {"password": record[who]["password"]})
        return client

    def publish(key: str, kind: str, payload: dict[str, Any], scope: dict[str, Any] | None = None) -> ConfigVersion:
        admin = actor("admin")
        draft = proof.request(admin, "post", "/api/goods-v1/masters/configurations", proof.command(record, f"opening-{key}-create", kind=kind, scope=scope or {"scope_kind": "tenant"}, effective_from=timezone.now().isoformat(), payload=payload))
        existing = ConfigVersion.objects.filter(tenant_id=claim.tenant_id, draft_id=draft["id"]).first()
        if existing is not None:
            return existing
        proof.request(admin, "post", f"/api/goods-v1/masters/configurations/{draft['id']}/submit", proof.command(record, f"opening-{key}-submit", expected_revision=draft["revision"], reviewed_hash=draft["content_hash"]))
        approval = ApprovalRequest.objects.get(subject_kind="configuration", subject_key=f"configdraft:{draft['id']}")
        proof.request(actor("owner"), "post", f"/api/goods-v1/approvals/{approval.pk}/decide", proof.command(record, f"opening-{key}-approve", expected_revision=approval.revision, reviewed_hash=approval.reviewed_hash, decision="approve"))
        return ConfigVersion.objects.get(tenant_id=claim.tenant_id, draft_id=draft["id"])

    with tenant_context(claim.tenant_id):
        owner = actor("owner")
        brand = proof.request(owner, "post", "/api/goods-v1/masters/brands", proof.command(record, "opening-proof-brand", code="ALPHA-PROOF", name="Fictional Alpha proof brand"))
        season = proof.request(owner, "post", "/api/goods-v1/masters/seasons", proof.command(record, "opening-proof-season", code="ALPHA-PROOF", name="Fictional Alpha proof season"))
        publish("size", "vocabulary", {"dimension": "size", "values": [{"value_key": "M", "label": "M", "sort_order": 1, "retired": False}], "effective_from": timezone.now().isoformat()})
        identity = publish("identity", "identity_profile", {"family": "fashion", "distinguishing_dimensions": [], "size_dimension": "size", "colour_dimension": None, "grade_dimension": None, "allowed_size_values": [], "allowed_colour_values": [], "allowed_grade_values": []})
        rates = publish("rates", "rates", {"transport_pct": "0", "pricing_margin_pct": "0"})
        tax = publish("tax", "tax_rates", {"currency": "INR", "hsn_rules": [{"hsn": "6101", "effective_from": timezone.now().isoformat(), "slabs": [{"lower_paise": "0", "upper_paise": None, "lower_inclusive": True, "upper_inclusive": False, "input_pct": "5", "output_pct": "5"}]}]})
        profile = publish("profile", "profile", {"family": "fashion", "columns": [], "directions": ["both_supplied"], "allow_row_override": False, "rates_version_id": str(rates.pk), "tax_version_id": str(tax.pk), "opening_rules": {"season_required": True, "both_supplied": True}})
        for purpose in ("opening", "receipt"):
            publish(f"approval-{purpose}", "approval", {"action": f"pt.approve.{purpose}", "roles": ["owner"], "purpose": purpose, "site_ids": [], "brand_ids": [], "require_distinct": True, "qty_max": 100, "value_max": "10000000", "step_up": True, "unknown_value": "refuse"}, {"scope_kind": "tenant", "purposes": [purpose]})
        publish("sell-policy", "sell_policy", {"manual_discount_cap_percent": "10", "manual_discount_on_offer_lines": False, "return_window_days": 30})
        business = publish("business", "business_profile", {"categories": ["Fictional test clothing"], "identity_profile_id": str(identity.pk), "expected_skus": 1, "brands": 1, "sites": 1, "sbus": 1, "staff": 3, "documents_per_day": 10, "evidence_bytes_per_year": 1000000, "commercial_labels": ["Fictional proof stock"], "workforce_scope": "Generated example.test proof people only", "tills_per_site": [{"site_id": str(claim.first_store_id), "count": 1}], "accounting_interface": "Proof only; no real external accounting connection"})
        owner = actor("owner")
        tenant = proof.request(owner, "get", "/api/goods-v1/masters/tenant")
        if tenant["data"]["business_profile_version_id"] != str(business.pk):
            proof.request(owner, "patch", "/api/goods-v1/masters/tenant", proof.command(record, "opening-business-bind", **{**tenant["data"], "business_profile_version_id": str(business.pk)}, expected_revision=tenant["revision"]))
        site_path = f"/api/goods-v1/masters/stores/{claim.first_store_id}"
        site = proof.request(owner, "get", site_path)
        if str(brand["id"]) not in {str(value) for value in site["data"].get("brand_ids", [])}:
            proof.request(owner, "patch", site_path, proof.command(record, "opening-site-brands", brand_ids=[*site["data"].get("brand_ids", []), str(brand["id"])], expected_revision=site["revision"]))
        locations_path = f"{site_path}/locations"
        locations = proof.request(owner, "get", locations_path)
        if not any(item["data"]["kind"] == "floor" for item in locations["items"]):
            site = proof.request(owner, "get", site_path)
            proof.request(owner, "post", locations_path, proof.command(record, "opening-proof-floor", name="Fictional proof sales floor", kind="floor", expected_revision=site["revision"]))
        entity_id = claim.summary["store"].get("entity_id")
        if entity_id is None:
            from masters.models import Store
            entity_id = Store.objects.get(pk=claim.first_store_id, tenant_id=claim.tenant_id).gstin.legal_entity_id
        for doc_type in ("GRN", "CGRN", "RPT", "OPT"):
            publish(f"series-{doc_type}", "series", {"entity_id": str(entity_id), "type": doc_type, "fy": financial_year(), "ceiling_block_size": 100}, {"scope_kind": "entity", "entity_id": str(entity_id)})
            # Run only the canonical ceiling-publisher job for these four
            # governed proof series. Do not drain unrelated delivery jobs.
            series = VoucherSeries.objects.get(tenant_id=claim.tenant_id, store_code=series_store_code(int(entity_id)), fy=financial_year(), doc_type=doc_type, scope_version="entity_v1")
            publish_ceiling(claim.tenant_id, series.pk, target=series.ceiling_block_size)
        plan = {"proof_only": True, "site_id": str(claim.first_store_id), "barcode": "ALPHA000123", "quantity": 3,
                "brand_mappings": {"Fictional source brand": str(brand["id"])}, "season_mappings": {"Fictional source season": str(season["id"])},
                "size_mappings": {"M": str(vocabulary_value_id("size", "M"))}, "hsn_mappings": {"Shirt": "6101"},
                "identity_profile_id": str(identity.pk), "profile_version_id": str(profile.pk), "business_profile_version_id": str(business.pk),
                "rate_meaning": "basic_ex_tax", "physical_note": "Generated test scenario only; these figures are not a physical count of a real shop."}
        record.setdefault("opening_cutoff_at", timezone.now().isoformat())
        proof.save(record)
        plan["cutoff_at"] = record["opening_cutoff_at"]
        ARTIFACTS.mkdir(exist_ok=True)
        # XLSX ZIP timestamps make independent regeneration differ, so retain the
        # first checked artifact instead of overwriting or pretending a new hash.
        source_path = ARTIFACTS / "fictional-opening-soh.xlsx"
        if not source_path.exists():
            book = Workbook()
            assert book.active is not None
            book.active.append(["ItemName", "Brand", "Size", "Barcode", "Category", "Season", "Tqty", "Mrp", "Rate", "Amount"])
            book.active.append(["Fictional proof shirt", "Fictional source brand", "M", plan["barcode"], "Shirt", "Fictional source season", 3, 1000, 500, 1500])
            stream = io.BytesIO()
            book.save(stream)
            write_once(source_path, stream.getvalue())
        review_path = ARTIFACTS / "fictional-valuation-reconciliation.xlsx"
        if not review_path.exists():
            book = Workbook()
            assert book.active is not None
            book.active.append(["Fictional proof evidence", "No real stock or real valuation is asserted."])
            book.active.append(["Scenario", "3 units, reviewed test basic value500INR, MRP1000INR, source amount1500INR; zero discrepancy."])
            stream = io.BytesIO()
            book.save(stream)
            write_once(review_path, stream.getvalue())
        write_once(ARTIFACTS / "fictional-physical-count.csv", b"barcode,observed_qty,observed_condition,reason\nALPHA000123,3,good,Generated fictional proof scenario - not a real physical count\n")
        write_once(ARTIFACTS / "mapping-plan.json", json.dumps(plan, indent=2, sort_keys=True).encode())
    print("Fictional browser source/mapping inputs prepared through independently approved canonical configuration APIs. No source, stock, feature gate or real-shop data was changed.")
    print(f"Public proof artefacts: {ARTIFACTS}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- private fixture credentials must never appear in framework tracebacks.
        print(str(error) if isinstance(error, RuntimeError) else f"Opening proof failed ({type(error).__name__}); inspect its governed resource before retrying.")
        raise SystemExit(1) from None
