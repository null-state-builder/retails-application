"""Preview or apply SO-03's conservative role-assignment conversion."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from accounts.assignment_migration import apply_migration_plan, build_migration_plan
from core.tenancy import tenant_context
from masters.goods_models import Tenant


class Command(BaseCommand):
    help = "Report legacy access conversion; --apply inserts only unambiguous assignments."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--tenant", type=str, required=True, help="The explicitly selected tenant UUID.")
        parser.add_argument("--apply", action="store_true", help="Apply clean assignments and initial role policies.")
        parser.add_argument("--expect-fingerprint", type=str, help="Required for apply: the reviewed dry-run source_fingerprint.")
        parser.add_argument("--allow-seeded-tenant-admin", action="store_true", help="Explicitly include verified seeded tenant administrators.")
        parser.add_argument("--output", type=Path, help="Write the full JSON reconciliation report here.")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["apply"] and not options.get("expect_fingerprint"):
            raise CommandError("--apply requires --expect-fingerprint from the reviewed dry run.")
        selected = options.get("tenant")
        try:
            tenant_id = uuid.UUID(selected) if selected else None
        except ValueError as exc:
            raise CommandError("--tenant must be a UUID") from exc
        tenants = Tenant.objects.filter(pk=tenant_id) if tenant_id else Tenant.objects.all()
        if tenant_id and not tenants.exists():
            raise CommandError("Tenant was not found.")
        reports = []
        for tenant in tenants.order_by("pk"):
            with tenant_context(tenant.pk):
                plan = build_migration_plan(tenant.pk, allow_seeded_tenant_admin=options["allow_seeded_tenant_admin"])
                report = plan.report()
                report["mode"] = "apply" if options["apply"] else "dry_run"
                if options["apply"]:
                    if options["expect_fingerprint"] != plan.source_fingerprint():
                        raise CommandError("The migration report is stale; generate and review a new dry run.")
                    report["result"] = apply_migration_plan(plan)
                reports.append(report)
        rendered = json.dumps({"schema_version": 1, "tenants": reports}, indent=2, sort_keys=True) + "\n"
        if options.get("output"):
            path: Path = options["output"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            self.stdout.write(f"Wrote SO-03 access reconciliation to {path}")
        else:
            self.stdout.write(rendered)
