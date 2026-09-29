"""Take today's stock snapshot and bring received up to date (store operations ticket 43).

    uv run python manage.py refresh_inventory_report [--full]

The worker runs the same refresh every few minutes; this is for a first build,
a cron without the worker, and the browser suite. ``--full`` re-reads every
acceptance event; today's snapshot is always taken afresh.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = "Refresh the inventory report's copy (reads goods stock, writes only reports)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--full", action="store_true", help="Re-read every received piece.")

    def handle(self, *args: Any, **options: Any) -> None:
        from reporting.inventory_facts import refresh

        result = refresh(full=bool(options["full"]))
        if not result.ran:
            self.stdout.write("Another refresh is running; nothing done.")
            return
        self.stdout.write(
            f"Refreshed: {result.stores} store snapshot(s), {result.events} acceptance "
            f"event(s) in {result.took_ms} ms, as of {result.as_of}."
        )
