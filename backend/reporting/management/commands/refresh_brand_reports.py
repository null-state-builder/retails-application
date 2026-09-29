"""Bring the brand reports' bill-line copy up to date (store operations ticket 29).

    uv run python manage.py refresh_brand_reports [--full]

The worker runs the same refresh every few minutes; this is for a first build,
a cron without the worker, and the browser suite. The SOH reads the inventory
copy (``refresh_inventory_report``).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = "Refresh the brand reports' copy of bill lines (reads bills, writes only reports)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--full", action="store_true", help="Rebuild the copy from nothing.")

    def handle(self, *args: Any, **options: Any) -> None:
        from reporting.brand_sale_facts import refresh

        result = refresh(full=bool(options["full"]))
        if not result.ran:
            self.stdout.write("Another refresh is running; nothing done.")
            return
        self.stdout.write(
            f"Refreshed: {result.documents} document(s) in {result.took_ms} ms, "
            f"as of {result.as_of}."
        )
