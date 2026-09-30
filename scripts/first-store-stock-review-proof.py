"""Append explicitly fictional count-review inputs to the owned browser proof.

No tenant balances or documents are changed here. This is a fixture, not reviewed
real-shop configuration: policy/identity setup is isolated from browser decisions.
Existing roles, permissions, users, mappings and proof stock are never rewritten.
"""
from __future__ import annotations

import importlib.util
import secrets
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def helper() -> Any:
    spec = importlib.util.spec_from_file_location("stock_review_proof", ROOT / "scripts/first-store-onboarding-proof.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The owned proof helper is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    proof = helper()
    claim, record = proof.initialise(), proof.load()
    from accounts.goods_models import HumanIdentity, RoleAssignment
    from accounts.models import Role, User
    from core.commands import database_now
    from core.numbering import prepare_series
    from core.tenancy import tenant_context
    from django.db import transaction
    from masters.goods_models import ConfigVersion, SiteGuard
    from masters.models import Store
    from sell.services.goods_stock import read_shelf
    from tests.first_store_goods import _publish_tenant_config

    with tenant_context(claim.tenant_id), transaction.atomic():
        site = Store.objects.get(tenant_id=claim.tenant_id, pk=claim.first_store_id, code="FIRST", name="First proof shop")
        if not claim.tenant.synthetic or SiteGuard.objects.get(site=site).selling_mode != "online_alpha":
            raise RuntimeError("Only the synthetic online proof shop is permitted.")
        owner = User.objects.get(tenant_id=claim.tenant_id, email=record["owner"]["email"])
        checker = record.setdefault("count_checker", {"email": f"count-review-{uuid.uuid4().hex[:12]}@first.example.test", "password": secrets.token_urlsafe(24) + "A1!"})
        proof.save(record)  # Private 0600 record; no credential is emitted.
        user = User.objects.filter(tenant_id=claim.tenant_id, email=checker["email"]).first()
        if user is None:
            human = HumanIdentity.objects.create(tenant_id=claim.tenant_id, staff_code="COUNT-CHECKER-PROOF", display_name="Independent fictional count checker")
            fixture_role = Role.objects.get(tenant_id=claim.tenant_id, code="owner")
            user = User.objects.create_user(username=checker["email"], email=checker["email"], password=checker["password"],
                tenant_id=claim.tenant_id, human=human, role=fixture_role, full_name=human.display_name, must_change_password=False)
            RoleAssignment.objects.create(tenant_id=claim.tenant_id, human=human, role=fixture_role, all_sites=True, all_brands=True, effective_from=database_now())
        if not user.is_active or user.human_id in (owner.human_id, User.objects.get(email=record["manager"]["email"]).human_id) or not user.check_password(checker["password"]):
            raise RuntimeError("The separate generated proof checker identity changed; preserved, no claim made.")
        payloads = {
            "approval": {"action": "count.review", "roles": ["owner"], "require_distinct": True, "site_ids": [], "brand_ids": [],
                "qty_max": 100, "value_max": "10000000", "step_up": True, "unknown_value": "refuse"},
            "reasons": {"action": "count.run", "codes": [{"code": "SOURCE_COUNT", "label": "Fictional independently reviewed physical count", "retired": False}]},
        }
        for kind, payload in payloads.items():
            rows = list(ConfigVersion.objects.filter(tenant_id=claim.tenant_id, kind=kind, payload__action=payload["action"]))
            if not rows:
                _publish_tenant_config(claim.tenant, owner.human_id, kind=kind, payload=payload, label=f"Synthetic browser stock-review {kind} proof only")
            elif not any(row.payload == payload for row in rows):
                raise RuntimeError("Existing proof configuration differs; preserved without replacement.")
        prepare_series(claim.tenant_id, site.gstin.legal_entity, "CNT")
        quantity = sum(read_shelf(site).quantities.values())
        record["stock_review_fixture"] = {"synthetic_only": True, "site_id": str(site.pk), "matching_quantity": quantity, "barcode": "ALPHA000123"}
        proof.save(record)
        print("Synthetic stock-review inputs appended; existing stock, bills and permissions preserved. Real-store configuration remains unapproved.")


if __name__ == "__main__":
    main()
