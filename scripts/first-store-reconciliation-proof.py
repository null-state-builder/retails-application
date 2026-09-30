"""Read-only reconciliation of the owned fictional first-store browser journey.

Run via first-store-proof.py run. Never activates the real workbook or writes
business/configuration rows; emits only fictional totals and evidence hashes.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def helper(name: str) -> Any:
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location("reconciliation_" + path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("The verified proof helper is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    proof = helper("first-store-onboarding-proof.py")
    claim = proof.initialise()
    commercial = helper("first-store-commercial-proof.py")
    from core.commands import database_now
    from core.gl import GLEntry
    from core.kernel_models import AuditEvent, PrivilegedReview
    from core.tenancy import tenant_context
    from django.db.models import Sum
    from finledger.models import CashLedgerEntry
    from masters.goods_models import ConfigVersion, SiteGuard
    from masters.models import Store
    from masters.store_feature_models import StoreFeatureSwitch
    from ptmapper.soh_models import SohImport, SohImportBatch
    from ptmapper.soh_services import opening_reconciliation
    from sell.cash_models import CashCount
    from sell.models import OnlineSaleSubmission, Sale, SaleTender
    from sell.services.goods_stock import read_shelf
    from stockledger.goods_models import Position

    with tenant_context(claim.tenant_id):
        site = Store.objects.get(pk=claim.first_store_id, tenant_id=claim.tenant_id, code="FIRST")
        if site.name != "First proof shop" or not claim.tenant.synthetic:
            raise RuntimeError("Only the independently identified fictional shop is permitted.")
        before = commercial.business_fingerprint(claim.tenant_id)
        source = opening_reconciliation(site)
        assert source["passed"] and source["source_count"] == 1 and source["quantity"] == 3
        bill = Sale.objects.get(store=site, doc_number="26-27/FIRST/SAL/1")
        assert Sale.objects.filter(store=site).count() == 1 and bill.net_paise == 90_000
        tenders = SaleTender.objects.filter(sale=bill)
        assert tenders.count() == 1 and tenders.get().amount_paise == 90_000
        accepted = OnlineSaleSubmission.objects.filter(tenant_id=claim.tenant_id, store=site, status="accepted")
        assert accepted.count() == 1 and accepted.get().sale_id == bill.pk
        cash = CashLedgerEntry.objects.filter(posted_by__tenant_id=claim.tenant_id, doc_number=bill.doc_number)
        assert cash.count() == 1 and cash.get().amount == 90_000
        value = GLEntry.objects.filter(doc_number=bill.doc_number)
        assert value.exists() and value.aggregate(total=Sum("amount"))["total"] == 0
        shelf = read_shelf(site, database_now())
        assert sum(shelf.quantities.values()) == 2
        count = CashCount.objects.get(store=site)
        assert count.bills.count() == 1 and count.bills.get().pk == bill.pk
        assert count.cash_sales_paise == count.expected_paise == count.counted_paise == 90_000
        assert count.variance_paise == 0
        for feature in ("cash-count", "document-series", "tax-settings"):
            event = AuditEvent.objects.filter(tenant_id=claim.tenant_id,
                action="masters.store_feature.switch", subject_key=f"store_feature:{site.pk}:{feature}",
                outcome="succeeded").order_by("-recorded_at", "-pk").first()
            assert event is not None
            reviews = PrivilegedReview.objects.filter(tenant_id=claim.tenant_id, audit_event=event)
            assert any(str(review.reviewer_id) in event.authority.get("independent_reviewers", [])
                       and review.reviewer_id != event.actor_id for review in reviews)
        preview = Store.objects.get(tenant_id=claim.tenant_id, code="SOH-PREVIEW")
        real_source = SohImport.objects.get(tenant_id=claim.tenant_id, site=preview,
            source_hash="033b5d6dbd7943ba3e8cd07861d1ea1ba0822df65e560698220b6745d8ffb715")
        assert real_source.state == "uploaded" and real_source.revision == 1
        assert not real_source.configuration and real_source.approval_request_id is None
        assert not SohImportBatch.objects.filter(source_import=real_source).exists()
        assert not Position.objects.filter(tenant_id=claim.tenant_id, site=preview).exists()
        assert not SiteGuard.objects.get(tenant_id=claim.tenant_id, site=preview).sell_ready
        after = commercial.business_fingerprint(claim.tenant_id)
        assert before == after, "The read-only reconciliation must not change business history."
        print(json.dumps({
            "proof_only": True, "initial_accepted_quantity": 3, "remaining_sellable_quantity": 2,
            "bills": 1, "accepted_intents": 1, "tenders": 1, "net_and_cash_paise": 90_000,
            "value_ledger_sum": 0, "cash_counts": 1, "counted_paise": 90_000, "variance_paise": 0,
            "current_feature_revisions_independently_reviewed": True,
            "real_workbook": "uploaded, unmapped, unapproved, unposted, inactive",
            "business_rows_unchanged_sha256": before,
            "configuration_versions": list(ConfigVersion.objects.filter(tenant_id=claim.tenant_id)
                .order_by("kind", "version").values("kind", "version", "id")),
            "first_store_features": list(StoreFeatureSwitch.objects.filter(tenant_id=claim.tenant_id, site=site)
                .order_by("feature_key").values("feature_key", "enabled", "revision")),
        }, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
