"""Read-only totals for the separately owned fictional sale/exchange browser copy."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any


def helper(name: str) -> Any:
    spec = importlib.util.spec_from_file_location("operating_reconciliation_" + name.replace("-", "_"), Path(__file__).with_name(name))
    if spec is None or spec.loader is None:
        raise RuntimeError("The owned proof helper is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    wrapper = helper("first-store-operating-proof.py")
    record = wrapper.verified(wrapper.helper())
    if os.environ.get("KDPS_PROOF_MODE") != "1" or os.environ.get("KDPS_REHEARSAL_DB") != record["database"]:
        raise RuntimeError("Only the identified fictional operating wrapper may run this check.")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from accounts.registration_models import InstallationRegistration
    from core.gl import GLEntry
    from core.tenancy import tenant_context
    from django.db import connection
    from django.db.models import Sum
    from finledger.models import CashLedgerEntry
    from masters.models import Store
    from ptmapper.soh_services import opening_reconciliation
    from sell.cash_models import CashCount, CashCountBill
    from sell.models import OnlineSaleSubmission, Sale, SaleLine, SaleTender
    from sell.services.cash_count import cash_position
    from sell.services.goods_stock import read_shelf
    from sell.services.returned_pieces import pending_returns

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user, system_identifier::text FROM pg_control_system()")
        assert cursor.fetchone() == (record["database"], "kdps_proof", record["system_identifier"])
    claim = InstallationRegistration.objects.select_related("tenant").get(completed_at__isnull=False)
    assert claim.tenant is not None and claim.tenant.synthetic and claim.tenant.code == "ALPHA"
    commercial = helper("first-store-commercial-proof.py")
    with tenant_context(claim.tenant_id):
        site = Store.objects.get(pk=claim.first_store_id, code="FIRST")
        before = commercial.business_fingerprint(claim.tenant_id)
        opening = opening_reconciliation(site)
        assert opening["passed"] and opening["quantity"] == 3 and opening["source_count"] == 1
        bills = list(Sale.objects.filter(store=site).order_by("till_seq"))
        assert len(bills) == 3 and [int(row.net_paise) for row in bills] == [90_000, 100_000, 0]
        assert len({row.doc_number for row in bills}) == 3
        assert OnlineSaleSubmission.objects.filter(store=site, status="accepted").count() == 3
        assert SaleTender.objects.filter(sale__store=site).count() == 2
        assert SaleTender.objects.filter(sale__store=site).aggregate(total=Sum("amount_paise"))["total"] == 190_000
        assert CashLedgerEntry.objects.filter(posted_by__tenant_id=claim.tenant_id).aggregate(total=Sum("amount"))["total"] == 190_000
        assert GLEntry.objects.filter(doc_number__in=[row.doc_number for row in bills]).aggregate(total=Sum("amount"))["total"] == 0
        assert sum(read_shelf(site, cash_position(site).window_to).quantities.values()) == 1
        assert not pending_returns({site.pk})
        returned = SaleLine.objects.get(sale=bills[2], direction="return")
        original = bills[1].lines.get()
        assert returned.brand_ref_id == original.brand_ref_id
        assert returned.goods_allocations == original.goods_allocations
        saved = CashCount.objects.get(store=site)
        assert saved.counted_paise == saved.expected_paise == 90_000 and saved.variance_paise == 0
        assert CashCountBill.objects.filter(count=saved).count() == 1
        position = cash_position(site)
        assert position.bills == 2 and position.cash_sales_paise == 100_000 and position.opening_paise == 90_000
        assert commercial.business_fingerprint(claim.tenant_id) == before
        print(json.dumps({"proof_only": True, "database": record["database"], "initial_accepted_qty": 3,
                          "sold_qty": 3, "returned_and_accepted_qty": 1, "remaining_sellable_qty": 1,
                          "bills": 3, "accepted_intents": 3, "cash_tenders_paise": 190_000,
                          "cash_ledger_paise": 190_000, "gl_sum": 0,
                          "retained_count_paise": 90_000, "later_uncounted_bills": 2,
                          "next_expected_cash_paise": 190_000, "unexplained_stock_or_money_difference": 0,
                          "new_day_close_verified": False, "business_fingerprint": before}, sort_keys=True))


if __name__ == "__main__":
    main()
