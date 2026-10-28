"""Upload (and optionally apply) a legacy SOH export at a test store, from the server shell.

Synthetic tenant only (store test run). It runs the same commands as the
Legacy sync page (``ptmapper.stock_sync_services``), as the uploader's own login,
which must hold ``opening.import.stage`` at the store:

    python manage.py sync_soh --store WRR --soh SOH.xlsx --exported-at 2026-10-29T09:30+05:30 \\
        --uploader debanjan@wardrobe.test            # stage and print the preview
    python manage.py sync_soh ... --apply            # then bring stock to the SOH

Uploading the same file with the same export time again is a no-op that
returns the existing sync. An export at or before the last applied one is
refused (``STOCK_SYNC_STALE``). A server shell needs no web password; that is
the only check the page has that this skips.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime


def _act(access: Any, action: str, site_id: int, handler: Callable[[Any], Any]) -> Any:
    """Run one command as this login, refused unless it may take ``action`` at the store."""
    from core.commands import CommandResult, CommandRun, CommandSpec, execute_command

    access.require(action, site_id=site_id)
    holder: dict[str, Any] = {}

    def run(cmd: CommandRun) -> CommandResult:
        holder["value"] = handler(cmd)
        return CommandResult(resource_type="stock_sync", resource_id=str(getattr(holder["value"], "pk", "x")))

    execute_command(access.principal(), CommandSpec(action=action, command_id=uuid.uuid4(), business_input={},
                                                    subject_key=f"site:{site_id}"), run)
    return holder.get("value")


def book_units(store: Any) -> int:
    from core.goods_fields import bounds
    from stockledger import ranges
    from stockledger.goods_models import Position

    return sum(ranges.length(bounds(span)) for span in Position.objects.filter(
        tenant_id=store.tenant_id, site_id=store.pk, boundary="physical").values_list("portion", flat=True))


class Command(BaseCommand):
    help = "Upload a legacy SOH export at a test store and, with --apply, bring its stock to it."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--store", required=True, help="Store code, e.g. WRR")
        parser.add_argument("--soh", required=True, help="The legacy SOH export (.xlsx)")
        parser.add_argument("--exported-at", required=True,
                            help="When the legacy software exported it, ISO 8601 with its timezone")
        parser.add_argument("--uploader", required=True,
                            help="Login that uploads (store manager, warehouse or operations head at the store)")
        parser.add_argument("--apply", action="store_true", help="Apply it after the preview")

    def handle(self, *args: Any, **options: Any) -> None:
        from accounts.test_store_loader import access_for
        from core.refusals import Refusal
        from core.tenancy import tenant_context
        from files.goods_services import XLSX, check_workbook, detect_media_type, stage_upload
        from masters.goods_models import Tenant
        from masters.models import Store
        from ptmapper import stock_sync_services as services
        from ptmapper.soh_parser import parse_soh
        from ptmapper.stock_sync_models import StockSync

        cutoff = parse_datetime(options["exported_at"])
        if cutoff is None or cutoff.tzinfo is None:
            raise CommandError("--exported-at must be an ISO time with its timezone, e.g. 2026-10-29T09:30+05:30.")
        path = Path(options["soh"])
        if not path.is_file():
            raise CommandError(f"SOH file not found: {path}")
        data = path.read_bytes()
        tenants = list(Tenant.objects.filter(synthetic=True))
        if len(tenants) != 1:
            raise CommandError("Expected exactly one synthetic tenant on this deployment.")
        with tenant_context(tenants[0].pk):
            store = Store.objects.filter(tenant_id=tenants[0].pk, code=options["store"]).first()
            if store is None:
                raise CommandError(f"No store {options['store']} on the synthetic tenant.")
            try:
                if detect_media_type(data, path.name) != XLSX:
                    raise CommandError("The SOH must be an XLSX workbook.")
                check_workbook(data)
                metadata, parsed = parse_soh(data)
                access = access_for(options["uploader"])
                access.require(services.ACTION, site_id=store.pk)
                services.require_test_store(store)
                evidence = stage_upload(
                    access.principal(), command_id=uuid.uuid4(), data=data, filename=path.name, kind="manifest",
                    scope={"scope_kind": "sites", "site_ids": [store.pk], "brand_ids": [],
                           "sensitive_fields": ["cost", "financial"]},
                    expected_sha256=hashlib.sha256(data).hexdigest(), contains_fields=["cost", "financial"])
                sync: StockSync = _act(access, services.ACTION, store.pk, lambda run: services.create_sync(
                    run, site_id=store.pk, evidence=evidence, cutoff_at=cutoff, metadata=metadata, parsed=parsed))
                self.stdout.write(f"Stock sync {sync.pk} ({sync.source_name}), exported {sync.cutoff_at.isoformat()}, "
                                  f"state {sync.state}: {len(parsed)} SOH rows.")
                if sync.state == StockSync.State.PREVIEWED:
                    self._preview(services.preview(sync))
                if not options["apply"]:
                    return
                if sync.state != StockSync.State.PREVIEWED:
                    self.stdout.write(f"Already {sync.state}; nothing to apply.")
                    self._outcome(sync.outcome, before=None, after=book_units(store), seconds=None)
                    return
                before = book_units(store)
                started = time.monotonic()
                applied: StockSync = _act(access, services.ACTION, store.pk, lambda run: services.apply(run, sync))
                seconds = time.monotonic() - started
                self._outcome(applied.outcome, before=before, after=book_units(store), seconds=seconds)
            except Refusal as exc:
                raise CommandError(f"{exc.code}: {exc.message}") from exc

    def _counts(self, counts: dict[str, int]) -> str:
        return ", ".join(f"{name} {count}" for name, count in counts.items())

    def _preview(self, preview: dict[str, Any]) -> None:
        self.stdout.write(
            f"Preview: book {preview['book_qty']} units, SOH {preview['soh_qty']}; {preview['remove_qty']} to take out, "
            f"{preview['add_qty']} to add, {preview['soh_only_qty']} SOH-only (flagged, not stocked); "
            f"{preview['test_bills_to_reverse']} test bills to put back.")
        self.stdout.write(f"Flagged rows {preview['flagged_rows']}: {self._counts(preview['category_counts'])}.")

    def _outcome(self, outcome: dict[str, Any], *, before: int | None, after: int, seconds: float | None) -> None:
        took = f" in {seconds:.1f}s" if seconds is not None else ""
        start = f"{before} -> " if before is not None else ""
        self.stdout.write(self.style.SUCCESS(
            f"Applied{took}: book units {start}{after}, SOH total {outcome.get('soh_qty')}; "
            f"removed {outcome.get('removed_qty')}, added {outcome.get('added_qty')}, "
            f"SOH-only {outcome.get('soh_only_qty')} pieces over {outcome.get('soh_only_barcodes')} barcodes "
            f"(not stocked); {outcome.get('exceptions')} exceptions."))
        self.stdout.write(f"Flagged rows {outcome.get('flagged_rows')}: "
                          f"{self._counts(outcome.get('category_counts') or {})}.")
