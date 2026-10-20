"""Prepare and check one test store and its linked warehouse (store test run).

The test runs on a synthetic tenant beside the legacy software. Everything that
needs a reviewed decision - sites, locations, approval policies, tax, numbering,
opening stock - is made on its own screen by the people who own it; this
command never self-approves them. What it does:

* ensures the reusable Warehouse manager role (``accounts.warehouse_manager``)
  and assigns it, for the warehouse and the store, to each ``--manager`` email;
* checks every gate the test needs and prints, for each one that fails, the
  screen that fixes it. Exit status 1 while anything is still missing.

    python manage.py setup_store_test_run --store VAS-SGMR --warehouse RAN-WH \\
        --manager wh.manager@example.com
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.tenancy import tenant_context

FIX = {
    # compute_readiness_checks
    "legal_entity": "/setup/organisation (legal entity)",
    "registration": "/setup/organisation (GST registration)",
    "series": "/setup/configuration (series GRN, CGRN, RPT, TPT)",
    "locations": "/setup/organisation (site locations; add a floor)",
    "sbus": "/setup/organisation (brands at the site)",
    "business_profile": "/setup/configuration (business profile)",
    "working_calendar": "/setup/configuration (working calendar)",
    "receipt_configuration": "/setup/configuration (receipt configuration)",
    "transfer_approval": "/setup/configuration (approval: pt.approve.transfer, roles incl. warehouse_manager, both sites)",
    "staff": "/setup/people-access (staff assigned to the site)",
    # selling_checks
    "goods_active": "/setup/organisation (readiness → approve goods)",
    "current_setup": "/setup/organisation (readiness checks above)",
    "opening_reconciled": "/goods/opening (SOH opening import) or declare empty",
    "selling_policy": "/setup/settings (sell policy)",
    "tax_configuration": "/setup/tax-settings and /setup/feature-switches (tax-settings)",
    "invoice_prefix": "/setup/document-numbering",
    "bill_number_series": "/setup/document-numbering",
    "registered_counter": "/setup/first-store (register counter), then pair in Till & Sync",
    "authorised_cashier": "/setup/people-access (store_person with all brands, password changed)",
    "sellable_item_inputs": "/setup/products (brand, MRP and HSN on every item)",
    "source_costing": "/goods/opening (reviewed costs)",
}


class Command(BaseCommand):
    help = "Prepare and check a test store and its linked warehouse (synthetic tenant only)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--store", required=True, help="Store code")
        parser.add_argument("--warehouse", required=True, help="Warehouse code")
        parser.add_argument("--manager", action="append", default=[], help="Warehouse manager email (repeatable)")

    def handle(self, *args: Any, **options: Any) -> None:
        from accounts import warehouse_manager
        from accounts.goods_models import RoleAssignment
        from accounts.models import User
        from masters.first_store_readiness import selling_checks
        from masters.goods_models import SiteGuard
        from masters.goods_services import compute_readiness_checks, linked_warehouse_id
        from masters.models import Store
        from masters.store_features import is_feature_on, is_real_store

        store = Store.objects.select_related("gstin", "tenant").filter(code=options["store"]).first()
        warehouse = Store.objects.select_related("gstin").filter(code=options["warehouse"]).first()
        if store is None or warehouse is None:
            raise CommandError("Store or warehouse code not found. Create the warehouse at /setup/organisation first.")
        if is_real_store(store):
            raise CommandError("This tenant is real. The store test run is for a synthetic test tenant only.")
        if warehouse.tenant_id != store.tenant_id or warehouse.store_type != "warehouse":
            raise CommandError(f"{warehouse.code} is not a warehouse of the same tenant.")
        tenant = store.tenant
        now = timezone.now()
        failures: list[tuple[str, str, str]] = []

        def check(where: str, key: str, passed: bool, reason: str) -> None:
            mark = self.style.SUCCESS("ok  ") if passed else self.style.ERROR("MISS")
            self.stdout.write(f"  {mark} {where:<10} {key:<24} {'' if passed else reason}")
            if not passed:
                failures.append((where, key, reason))

        with tenant_context(tenant.pk):
            role = warehouse_manager.ensure_policy(tenant)
            self.stdout.write(f"Role policy ready: {role.name} ({warehouse_manager.ROLE_CODE})")
            for email in options["manager"]:
                user = User.objects.filter(email__iexact=email, tenant=tenant).first()
                if user is None or user.human is None:
                    raise CommandError(f"No user {email} in this tenant. Create them at /setup/people-access first.")
                warehouse_manager.assign(tenant, user.human, site_ids=[warehouse.pk, store.pk])
                self.stdout.write(f"Assigned warehouse manager: {email} → {warehouse.code} + {store.code}")

            self.stdout.write("\nChecks")
            check("store", "linked_warehouse", linked_warehouse_id(store) == warehouse.pk,
                  f"Link {store.code} to {warehouse.code}")
            if not warehouse.gstin_id or not store.gstin_id or warehouse.gstin.legal_entity_id != store.gstin.legal_entity_id:
                check("both", "same_legal_entity", False, "Store and warehouse must belong to one legal entity")
            for site, where in ((warehouse, "warehouse"), (store, "store")):
                guard = SiteGuard.objects.filter(site=site).first()
                check(where, "goods_v1", bool(guard and guard.stock_contract == "goods_v1"), "Site is not on goods-v1 stock")
                for row in compute_readiness_checks(site, now):
                    check(where, row["key"], bool(row["passed"]), str(row.get("reason") or ""))
            guard = SiteGuard.objects.filter(site=store).first()
            check("store", "online_selling", bool(guard and guard.selling_mode == "online_alpha"),
                  "Set the store's selling mode to online")
            for row in selling_checks(store, now):
                check("store", row["key"], bool(row["passed"]), str(row.get("reason") or ""))
            check("store", "sell_ready", bool(guard and guard.sell_ready), "Approve selling at /setup/organisation readiness")
            for key in ("sales-report", "transfer-documents"):
                check("store", f"feature:{key}", is_feature_on(store, key), "Switch on at /setup/feature-switches")
            managers = RoleAssignment.objects.filter(
                tenant_id=tenant.pk, role=role, effective_to__isnull=True, revoked_at__isnull=True)
            check("both", "warehouse_managers", managers.count() >= 2,
                  "Two warehouse managers are needed: the drafter of a transfer cannot approve it")

        if failures:
            self.stdout.write(self.style.WARNING(f"\n{len(failures)} item(s) to finish:"))
            for where, key, reason in failures:
                self.stdout.write(f"  - {where} {key}: {FIX.get(key, reason)}")
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(f"\n{store.code} and {warehouse.code} are ready for the store test run."))
