"""Rebuild the shrinkage report's reporting copy (store operations ticket 44).

    uv run python manage.py refresh_shrinkage_report

The worker runs the same refresh every few minutes; this is for a first build,
a cron without the worker, and the browser suite. Every run rebuilds the copy
whole from the approved documents.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Rebuild the shrinkage report's copy (reads goods documents, writes only reports)."

    def handle(self, *args: Any, **options: Any) -> None:
        from reporting.shrinkage_facts import refresh

        result = refresh()
        if not result.ran:
            self.stdout.write("Another refresh is running; nothing done.")
            return
        self.stdout.write(
            f"Rebuilt: {result.documents} document(s) in {result.took_ms} ms, as of {result.as_of}."
        )
