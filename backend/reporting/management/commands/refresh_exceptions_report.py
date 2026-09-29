"""Rebuild the exceptions report's reporting copy (store operations ticket 48).

    uv run python manage.py refresh_exceptions_report

The worker runs the same refresh every few minutes; this is for a first build,
a cron without the worker, and the browser suite. A run re-reads the bills
changed since the last one; ``--full`` rebuilds the copy from nothing.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = (
        "Rebuild the exceptions report's copy (reads bills, counts and flags; writes only reports)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--full", action="store_true", help="Rebuild from nothing.")

    def handle(self, *args: Any, **options: Any) -> None:
        from reporting.exception_facts import refresh

        result = refresh(full=options["full"])
        if not result.ran:
            self.stdout.write("Another refresh is running; nothing done.")
            return
        self.stdout.write(
            f"Refreshed {'in full' if result.full else 'what changed'}: "
            f"{result.documents} bill(s) in {result.took_ms} ms, as of {result.as_of}."
        )
