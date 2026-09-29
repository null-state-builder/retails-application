"""The nightly reconciliation, run once a night (#188).

Four questions that can only be asked once a day is over - did every bill arrive,
was the customer charged what the rulebook says, is anything still unpriced, and
who took the returns. See `sell.services.daily_check` for what each one means and
why every one of them is idempotent.

It replaces `sell_age_deferred`, which was the ageing step on a schedule of its
own while this did not exist; that step is now one of the four. Anything the old
command would have flagged tonight, this flags tonight.

**A clean day prints nothing new**, which is the point: this is the pilot's
go/no-go gate, so a run that finds something is a run that found something real.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from core.dates import parse_day
from sell.services.daily_check import run


class Command(BaseCommand):
    help = "Reconcile a trading day: bill continuity, reprice, ageing, returns per seller."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--date",
            default="",
            help="The day to check (YYYY-MM-DD). Default: yesterday - the day the "
            "nightly run is about, since the cron fires in the small hours.",
        )
        parser.add_argument(
            "--store", default="", help="One store code. Default: every active store."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        asked = (options["date"] or "").strip()
        day = parse_day(asked) if asked else None
        if asked and day is None:
            raise CommandError(f"'{asked}' is not a date (use 2026-07-31).")
        from core.tenancy import tenant_context
        from masters.tenancy_middleware import deployment_tenant_id

        # Stores are tenant-walled; a job reads as the deployment's tenant.
        with tenant_context(deployment_tenant_id()):
            report = run(day, (options["store"] or "").strip())
            # Ticket 04: month end for offline invoice number blocks. Idempotent,
            # and it acts only on months that have ended, so nightly is enough.
            from sell.services.invoice_numbers import close_month

            cancelled = close_month()
            # Ticket 17: customers with no purchase for the retention period. Runs
            # only where its switch is on, which its gate keeps off at real stores.
            from sell.services.customer_retention import remove_lapsed

            # Not a per-store question: skipped when one store is asked for.
            lapsed = 0 if (options["store"] or "").strip() else remove_lapsed()
        for line in report.lines():
            self.stdout.write(line)
        if cancelled:
            self.stdout.write(
                f"  {cancelled} unused invoice number(s) from offline blocks recorded as cancelled"
            )
        if lapsed:
            self.stdout.write(
                f"  {lapsed} customer(s) with no purchase for the retention period removed"
            )
        self.stdout.write(
            self.style.WARNING(f"{report.total_raised} new exception(s).")
            if report.total_raised
            else self.style.SUCCESS("Clean day - nothing new on any store's list.")
        )
