"""Load one functional test store and its linked warehouse, from a legacy SOH or empty.

Synthetic tenant only. See ``accounts.test_store_loader`` for what it does and
the test-only decisions it records.

    python manage.py load_test_store --soh ../docs/VAS-SGMR_SOH_28-09-2026.xlsx

Without ``--soh`` the store is sell-ready with no stock; its opening comes from
its PT files at /goods/opening:

    python manage.py load_test_store --store WRR --store-name "Wardrobe Ratu Road (test)" \
        --warehouse RNC-WH --warehouse-name "Warehouse Ranchi (test)" --prefix wrr
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Load a functional test store + linked warehouse, from a legacy SOH or empty (synthetic tenant only)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--soh", help="Legacy SOH .xlsx; leave out to open the store empty (PT files at /goods/opening)")
        parser.add_argument("--store", default="VAS-SGMR")
        parser.add_argument("--store-name", default="Vaishnavi Singh More (test)")
        parser.add_argument("--warehouse", default="VAS-WH")
        parser.add_argument("--warehouse-name", default="Vaishnavi Warehouse (test)")
        parser.add_argument("--prefix", default="vas", help="Login prefix for the people created")

    def handle(self, *args: Any, **options: Any) -> None:
        from accounts.test_store_loader import Loader, Plan
        from masters.goods_models import Tenant

        path = Path(options["soh"]) if options["soh"] else None
        if path is not None and not path.is_file():
            raise CommandError(f"SOH file not found: {path}")
        tenants = list(Tenant.objects.filter(synthetic=True))
        if len(tenants) != 1:
            raise CommandError("Expected exactly one synthetic tenant on this deployment.")
        plan = Plan(store_code=options["store"], store_name=options["store_name"], warehouse_code=options["warehouse"],
                    warehouse_name=options["warehouse_name"], entity_code="syn-vas",
                    entity_name="Vaishnavi Test Retail Pvt Ltd (synthetic)", pan="AAAVT4444D", state_code="20",
                    state_name="Jharkhand (test)", city="Ranchi", prefix=options["prefix"])
        Loader(tenants[0], plan, path.read_bytes() if path else None, lambda line: self.stdout.write(line)).run()
        self.stdout.write(self.style.SUCCESS(
            f"Done. Sign in as {plan.prefix}.store@synthetic.demo / Synthetic@123 to sell at {plan.store_code}."))
