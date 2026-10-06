"""Independently review fictional FIRST commercial/cash features via normal APIs.

Use first-store-proof.py run. Preserves later configuration and all stock/bills;
never grants a role, edits model rows or emits credentials.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def main() -> None:
    source = Path(__file__).with_name("first-store-onboarding-proof.py")
    spec = importlib.util.spec_from_file_location("first_store_day_fixture", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("The verified onboarding helper is unavailable.")
    proof: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proof)
    record = proof.load()
    claim = proof.initialise()
    from core.tenancy import tenant_context
    from masters.models import Store
    from masters.store_features import StoreFeatureSwitch

    with tenant_context(claim.tenant_id):
        site = Store.objects.get(pk=claim.first_store_id, tenant_id=claim.tenant_id, code="FIRST")
        markers = {
            "cash-count": ("alpha-cash-close-switch", "alpha-cash-close-reviewable-switch"),
            "document-series": ("commercial-number-switch-initial", "alpha-number-feature-reviewable-switch"),
            "tax-settings": ("commercial-tax-switch-initial", "alpha-tax-feature-reviewable-switch"),
        }
        for feature, (historical, current) in markers.items():
            switch = StoreFeatureSwitch.objects.filter(tenant_id=claim.tenant_id, site=site, feature_key=feature).first()
            if switch is not None and not switch.enabled:
                raise RuntimeError("A later disabled proof feature was preserved; separate review is required.")
            if switch is None and feature != "cash-count":
                raise RuntimeError("The owned commercial fixture must configure its feature first.")
            # Append an independently reviewable current revision to the known
            # early fixture. Preserve its original event and every later edit.
            if switch is None or switch.revision == 1 and historical in record.get("commands", {}):
                admin = proof.login(record, "admin")
                proof.request(admin, "post", "/api/auth/step-up", {"password": record["admin"]["password"]})
                # Creation has no revision to fence. Preserve an earlier invalid
                # zero-revision payload under its original command identity.
                command_key = current if switch else f"{current}-initial-v2"
                revision = {"expected_revision": switch.revision} if switch else {}
                proof.request(admin, "post", "/api/goods-v1/masters/store-features/switch", proof.command(
                    record, command_key, **revision,
                    store_id=site.pk, feature_key=feature, enabled=True,
                ))
        owner = proof.login(record, "owner")
        proof.request(owner, "post", "/api/auth/step-up", {"password": record["owner"]["password"]})
        events = proof.request(owner, "get", "/api/auth/admin/privileged-changes", {"limit": "100"})
        reviewed = 0
        for event in events["items"]:
            if "review" in event.get("allowed_actions", []) and event["data"].get("subject_key") in {f"store_feature:{site.pk}:{feature}" for feature in markers}:
                proof.request(owner, "post", f"/api/auth/admin/privileged-changes/{event['id']}/review", proof.command(
                    record, f"alpha-review-cash-switch-{event['id']}",
                    note="Independent review of the current fictional FIRST feature revision; original fixture history retained, no real-store activation or CA sign-off.",
                ))
                reviewed += 1
        print(f"Fictional FIRST feature revisions reviewed through normal APIs; independent Owner reviews appended: {reviewed}.")


if __name__ == "__main__":
    main()
