"""Run the goods-v1 outbox worker: ceilings, anchors, notifications, emails, exports."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from core import dbroles
from core.outbox import drain, loop


class Command(BaseCommand):
    help = "Claim and run due outbox jobs for this deployment's tenant."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="Drain due jobs and exit.")

    def handle(self, *args: Any, **options: Any) -> None:
        from masters.tenancy_middleware import deployment_tenant_id

        # The worker is a served process: it runs as the restricted application role.
        dbroles.force_runtime_role(True)
        from django.db import connections

        for connection in connections.all():
            connection.close()
        tenant_id = deployment_tenant_id()
        if tenant_id is None:
            raise CommandError("This deployment has no tenant yet; run bootstrap_deployment first.")
        if options["once"]:
            count = drain(tenant_id)
            self.stdout.write(f"Ran {count} job(s).")
            return
        loop(tenant_id)
