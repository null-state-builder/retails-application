"""Bring the offer simulation's copy of sold lines up to date (store operations ticket 30).

    uv run python manage.py refresh_offer_simulation          # what changed since last time
    uv run python manage.py refresh_offer_simulation --full   # rebuild from the bills

The worker runs the same refresh every few minutes; this is for a first build,
a rebuild, a cron without the worker, and the browser suite.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = "Refresh the offer simulation's copy of sold lines (reads bills, writes only reports)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--full", action="store_true", help="Rebuild from nothing.")

    def handle(self, *args: Any, **options: Any) -> None:
        from reporting.offer_sim_facts import refresh

        result = refresh(full=options["full"])
        if not result.ran:
            self.stdout.write("Another refresh is running; nothing done.")
            return
        self.stdout.write(
            f"Refreshed {'in full' if result.full else 'what changed'}: "
            f"{result.documents} bill(s) in {result.took_ms} ms, as of {result.as_of}."
        )
