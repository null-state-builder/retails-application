"""Load the real KDPS vocabulary and the starter mapping rules into the goods-v1 rulebook.

Run: ``python manage.py load_goods_rulebook``. Idempotent: a second run adds nothing.
Synthetic (demo) tenants only - see ``ptmapper.goods_rulebook_seed``. The dev stack
runs it after the other seeds (``scripts/dev.sh``). Every rule that cannot be carried
because its target is not an approved value (or, for BRAND, not a brand master) is
listed, never forced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.refusals import Refusal


class Command(BaseCommand):
    help = "Load the KDPS vocabulary and starter mapping rules into the goods-v1 rulebook."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--tenant", help="Tenant code (default: this deployment's tenant).")
        parser.add_argument(
            "--approver",
            help="Email of the person the seeded configuration is recorded under "
            "(default: the synthetic owner).",
        )
        parser.add_argument(
            "--path",
            action="append",
            help="A KDPS master-sheet workbook; repeat for several (default: the two in "
            "docs/data-from-kdps).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        import uuid

        from core.tenancy import tenant_context
        from masters.goods_models import Tenant
        from ptmapper.goods_rulebook_seed import load_rulebook

        if options.get("tenant"):
            tenant = Tenant.objects.filter(code=options["tenant"]).first()
        else:
            key = uuid.UUID(str(settings.KDPS_DEPLOYMENT_KEY))
            tenant = Tenant.objects.filter(deployment_key=key).first()
        if tenant is None:
            raise CommandError("No such tenant. Run seed_foundation first.")
        paths = [Path(p) for p in options["path"]] if options.get("path") else None
        try:
            with tenant_context(tenant.pk):
                report = load_rulebook(
                    tenant,
                    approver_email=options.get("approver"),
                    paths=paths,
                )
        except Refusal as refusal:
            raise CommandError(refusal.message) from refusal
        for missing in report.missing_sheets:
            self.stderr.write(f"Master sheet not found at {missing} - skipped.")
        if not report.sheets:
            raise CommandError("No KDPS master sheet was found; nothing was loaded.")
        added = ", ".join(f"{dim} +{n}" for dim, n in sorted(report.vocabulary_added.items()))
        self.stdout.write(f"Vocabulary from {len(report.sheets)} sheet(s): {added}.")
        self.stdout.write(
            f"Rules: {report.created} carried, {report.already} already in the rulebook, "
            f"{report.redundant} not needed (the source is already an approved value), "
            f"{len(report.not_carried)} not carried."
        )
        for skipped in report.not_carried:
            self.stdout.write(f"  not carried: {skipped.line()}")
        self.stdout.write(self.style.SUCCESS("Goods-v1 rulebook loaded."))
