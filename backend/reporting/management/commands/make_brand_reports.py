"""Make a month's brand reports now, as the worker does once a month (ticket 29).

    uv run python manage.py make_brand_reports --month 2026-08 [--store DEO]

For every store where brand reports are switched on (or the one named), every
brand with a bill line or stock there in the month gets its Sale and SOH file,
kept as made. A store whose month is already made is left alone and said.
Nothing waits for the copies here: bring them up to date first
(``refresh_brand_reports``, ``refresh_inventory_report``) if the month is recent.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from core.refusals import Refusal


class Command(BaseCommand):
    help = "Make and keep a month's brand reports at every store where they are on."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--month", required=True, help="The month, YYYY-MM.")
        parser.add_argument(
            "--store", default="", help="One store's code; every store if left out."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from core.tenancy import tenant_context
        from masters.goods_models import Tenant
        from reporting import brand_reports

        try:
            when = brand_reports.period(options["month"])
        except Refusal as refusal:
            raise CommandError(refusal.message) from None
        if when.running:
            raise CommandError("That month has not ended yet; make it on demand instead.")
        wanted = str(options["store"]).strip().upper()
        found = False
        for tenant_id in Tenant.objects.order_by("id").values_list("id", flat=True):
            with tenant_context(tenant_id):
                for store in brand_reports.stores_switched_on(tenant_id):
                    if wanted and store.code.upper() != wanted:
                        continue
                    found = True
                    result = brand_reports.make_month(store, when.first)
                    if result.made:
                        self.stdout.write(
                            f"{store.code} {when.label}: {result.files} file(s) "
                            f"in {result.took_ms} ms."
                        )
                    else:
                        self.stdout.write(f"{store.code} {when.label}: already made; left alone.")
        if wanted and not found:
            raise CommandError(f"No store {wanted} with brand reports switched on.")
