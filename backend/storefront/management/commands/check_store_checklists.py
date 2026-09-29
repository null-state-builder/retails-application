"""Run the store checklist check once (store operations ticket 49).

The worker runs it on its own clock; this is the same check by hand. ``--now``
asks as of another moment, so a missed list can be shown without waiting for its
time to pass. Safe to run as often as you like.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone
from django.utils.dateparse import parse_datetime


class Command(BaseCommand):
    help = "Record checklists missed at each store and sync the checklist-missed alerts."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--now", help="The moment to check as (YYYY-MM-DDTHH:MM, India time if no zone)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from core.tenancy import tenant_context
        from masters.tenancy_middleware import deployment_tenant_id
        from storefront.checklists import run_check

        now = None
        if options.get("now"):
            now = parse_datetime(options["now"])
            if now is None:
                raise CommandError("--now must be a date and time (YYYY-MM-DDTHH:MM).")
            if timezone.is_naive(now):
                now = timezone.make_aware(now)
        tenant_id = deployment_tenant_id()
        if tenant_id is None:
            raise CommandError("This deployment has no tenant yet.")
        with tenant_context(tenant_id):
            counts = run_check(tenant_id, now)
        self.stdout.write(
            self.style.SUCCESS(
                f"Store checklists checked - missed lists recorded: {counts['recorded']}, "
                f"alerts open: {counts['alerts']}"
            )
        )
