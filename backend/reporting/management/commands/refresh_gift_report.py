"""Bring the Gift Stock report's reporting copy up to date (store operations ticket 14).

    uv run python manage.py refresh_gift_report

The worker runs the same refresh every few minutes; this is for a first build,
a cron without the worker, and the browser suite. The copy is always rebuilt
in full (``reporting.gift_facts`` says why); ``--full`` is accepted and changes
nothing.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = (
        "Refresh the Gift Stock report's copy of the gift tags (reads bills, writes only reports)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--full", action="store_true", help="Accepted; always in full.")

    def handle(self, *args: Any, **options: Any) -> None:
        from reporting.gift_facts import refresh

        result = refresh()
        if not result.ran:
            self.stdout.write("Another refresh is running; nothing done.")
            return
        self.stdout.write(
            f"Refreshed in full: {result.documents} gift piece row(s) in {result.took_ms} ms, "
            f"as of {result.as_of}."
        )
