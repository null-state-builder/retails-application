"""Month end for offline invoice number blocks (store operations ticket 04).

Every number in a till's block for a month that has ended, and that no bill
used, is recorded as cancelled, ready for GSTR-1 Table 13 (baseline, CA to
confirm). The nightly ``sell_daily_check`` runs this too, so no schedule of its
own is needed; it is idempotent, so running it again changes nothing.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from core.dates import parse_day


class Command(BaseCommand):
    help = "Cancel the unused numbers of every till number block for a month that has ended."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--today",
            default="",
            help="Treat this day (YYYY-MM-DD) as today: blocks for months before its "
            "month are closed. Default: today.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        asked = (options["today"] or "").strip()
        today = parse_day(asked) if asked else None
        if asked and today is None:
            raise CommandError(f"'{asked}' is not a date (use 2026-10-01).")
        from core.tenancy import tenant_context
        from masters.tenancy_middleware import deployment_tenant_id
        from sell.services.invoice_numbers import close_month

        with tenant_context(deployment_tenant_id()):
            cancelled = close_month(today)
        self.stdout.write(f"{cancelled} unused invoice number(s) recorded as cancelled.")
