"""Run the alert checks and sync the alerts table (#77).

Meant to run daily on a schedule. Idempotent — safe to
run by hand, or more than once on the same day.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from alerts.checks import run_alert_checks


class Command(BaseCommand):
    help = (
        "Run the in-transit-aging, return-window, stock-ageing, broken-size and SOR-ageing alert "
        "checks, then refresh the size-balancing suggestions."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        from core.tenancy import tenant_context
        from masters.tenancy_middleware import deployment_tenant_id

        # Stores and brands are tenant-walled; a job reads as the deployment's tenant.
        with tenant_context(deployment_tenant_id()):
            counts = run_alert_checks()
        self.stdout.write(
            self.style.SUCCESS(
                f"Alerts synced — in-transit aging: {counts['in_transit_aging']}, "
                f"return window: {counts['return_window']}, "
                f"stock ageing: {counts['stock_ageing']}, "
                f"broken sizes: {counts['broken_size']}, "
                f"SOR ageing: {counts['sor_ageing']}, "
                f"size-balancing suggestions waiting: {counts['size_balancing']}"
            )
        )
