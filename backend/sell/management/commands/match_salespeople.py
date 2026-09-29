"""Run the salesperson matching rule again over the old rows still unmatched (ticket 07).

For after the move, once the missing staff records exist: the rule places what
it now can, each match audited, and reports what is left for Admin on Setup,
Salesperson Matches. It never changes a row that is already matched.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Match old salesperson rows to staff records by the rule, where it now can."

    def handle(self, *args: Any, **options: Any) -> None:
        from core.tenancy import tenant_context
        from masters.tenancy_middleware import deployment_tenant_id
        from sell.models import SalespersonMatch
        from sell.services.salespeople import rematch_unmatched

        tenant_id = deployment_tenant_id()
        if tenant_id is None:
            raise CommandError("No deployment tenant is bound (KDPS_DEPLOYMENT_KEY).")
        with tenant_context(tenant_id):
            matched = rematch_unmatched(tenant_id)
            left = SalespersonMatch.objects.filter(staff__isnull=True).count()
        self.stdout.write(
            f"{matched} old salesperson row(s) matched by the rule; {left} left for Admin."
        )
