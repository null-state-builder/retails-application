"""Run the scheduled-count check once (store operations ticket 35).

The worker runs it on its own clock; this is the same check by hand. ``--today``
asks about another day, so a missed count can be shown without waiting for one.
Safe to run as often as you like.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils.dateparse import parse_date


class Command(BaseCommand):
    help = "Open missed scheduled counts, close counted ones, and sync the count-due alerts."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--today", help="The day to check as (YYYY-MM-DD).")

    def handle(self, *args: Any, **options: Any) -> None:
        from core.tenancy import tenant_context
        from masters.tenancy_middleware import deployment_tenant_id
        from outbound.count_schedules import run_check

        today = parse_date(options["today"]) if options.get("today") else None
        if options.get("today") and today is None:
            raise CommandError("--today must be a date (YYYY-MM-DD).")
        tenant_id = deployment_tenant_id()
        if tenant_id is None:
            raise CommandError("This deployment has no tenant yet.")
        with tenant_context(tenant_id):
            counts = run_check(tenant_id, today)
        self.stdout.write(
            self.style.SUCCESS(
                f"Scheduled counts checked - due today: {counts['due_today']}, "
                f"missed opened: {counts['opened']}, closed: {counts['resolved']}"
            )
        )
