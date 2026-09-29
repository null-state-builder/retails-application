"""Remove customers with no purchase for the retention period (store operations
ticket 17, §23). The nightly ``sell_daily_check`` runs the same step; this runs
it alone. See ``sell.services.customer_retention`` for who goes and what stays:
bills are never touched, and nothing runs where the switch is off - so never at
a real store while the retention period is only proposed.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Remove customers with no purchase for the retention period (bills stay)."

    def handle(self, *args: Any, **options: Any) -> None:
        from core.tenancy import tenant_context
        from masters.tenancy_middleware import deployment_tenant_id
        from sell.services.customer_retention import remove_lapsed

        # Stores are tenant-walled; a job reads as the deployment's tenant.
        with tenant_context(deployment_tenant_id()):
            removed = remove_lapsed()
        months = settings.KDPS_CUSTOMER_RETENTION_MONTHS
        noun = "customer" if removed == 1 else "customers"
        self.stdout.write(
            f"{removed} {noun} with no purchase for {months} months removed. Bills stay."
        )
