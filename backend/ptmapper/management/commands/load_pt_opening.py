"""Load a test store's opening from its PT files on the server, without the browser.

Synthetic tenant only (store test run). It runs the same reviewed path as the
owner's "Approve and load" at /goods/opening (``ptmapper.pt_opening_services``),
in the foreground, and records progress on the source so the page shows it too.

    python manage.py load_pt_opening --store WRR

resumes the store's PT source already uploaded at /goods/opening (a failed or
interrupted load picks up where it stopped). With no source uploaded yet, stage
the files first, as the person who would upload them:

    python manage.py load_pt_opening --store WRR --pt WARDROBE_PT_combined.xlsx --stager debanjan@wardrobe.test

The owner approves (``--approver`` for another owner login). A server shell
needs no web password; that is the only check the page has that this skips.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Load a test store's opening from its PT files, on the server (synthetic tenant only)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--store", required=True, help="Store code, e.g. WRR")
        parser.add_argument("--pt", nargs="+", help="PT workbook(s) to stage when the store has no PT source yet")
        parser.add_argument("--stager", help="Who uploads the --pt files (a store login, not the owner)")
        parser.add_argument("--source", help="The PT source to load, when the store has more than one open")
        parser.add_argument("--approver", help="Owner login that approves (default: the tenant owner)")

    def handle(self, *args: Any, **options: Any) -> None:
        from accounts.goods_demo import owner_user
        from accounts.test_store_loader import OpeningDriver, access_for
        from core.refusals import Refusal
        from core.tenancy import tenant_context
        from masters.goods_models import Tenant
        from masters.models import Store
        from ptmapper import pt_opening_services as quick
        from ptmapper.soh_models import SohImport

        tenants = list(Tenant.objects.filter(synthetic=True))
        if len(tenants) != 1:
            raise CommandError("Expected exactly one synthetic tenant on this deployment.")
        tenant = tenants[0]
        with tenant_context(tenant.pk):
            store = Store.objects.filter(tenant_id=tenant.pk, code=options["store"]).first()
            if store is None:
                raise CommandError(f"No store {options['store']} on the synthetic tenant.")
            owner = owner_user(tenant)
            approver = options["approver"] or (owner.email if owner else None)
            if not approver:
                raise CommandError("No owner login found; pass --approver.")
            open_sources = [s for s in SohImport.objects.filter(tenant_id=tenant.pk, site=store)
                            .exclude(state="withdrawn").order_by("source_name", "pk")
                            if s.source_metadata.get("pt_opening") and quick.status(s).get("state") != "done"]
            if options["source"]:
                open_sources = [s for s in open_sources if str(s.pk) == options["source"]]
                if not open_sources:
                    raise CommandError(f"{options['source']} is not an open PT source at {store.code}.")
            if options["pt"]:
                if open_sources:
                    raise CommandError(f"{store.code} already has a PT source uploaded ({open_sources[0].source_name}); "
                                       "run without --pt to load it, or withdraw it first.")
                source = self._stage(store, options["pt"], options["stager"], approver, OpeningDriver, access_for)
            elif len(open_sources) == 1:
                source = open_sources[0]
            elif not open_sources:
                raise CommandError(f"{store.code} has no PT source waiting to load; upload one, or pass --pt and --stager.")
            else:
                raise CommandError(f"{store.code} has {len(open_sources)} open PT sources; pass --source with one of: "
                                   + ", ".join(str(s.pk) for s in open_sources))
            meta = source.source_metadata
            self.stdout.write(f"Source {source.pk} ({source.source_name}): {meta.get('stocked_rows')} barcodes, "
                              f"{meta.get('quantity')} pieces, state {source.state}.")
            try:
                stager, preparer, approver = quick.loaders(access_for(approver), source)
            except Refusal as exc:
                raise CommandError(f"{exc.code}: {exc.message}") from exc
            self.stdout.write(f"Stager {stager}, preparer {preparer}, approver {approver}.")
            quick.load(source.pk, stager=stager, preparer=preparer, approver=approver,
                       echo=lambda text: self.stdout.write(f"{timezone.localtime():%H:%M:%S} {text}"))
            source.refresh_from_db()
            state = quick.status(source)
        if state.get("state") != "done":
            raise CommandError(f"Load {state.get('state')}: {state.get('error')}")
        self.stdout.write(self.style.SUCCESS(
            f"Done: {state.get('loaded_qty')} pieces on the floor at {store.code} "
            f"({state.get('loaded_rows')} barcodes, {state.get('excluded_rows')} left out)."))

    def _stage(self, store: Any, paths: list[str], stager: str | None, approver: str,
               driver_class: Any, access_for: Any) -> Any:
        from ptmapper.pt_opening import evidence_workbook, parse_pt_files

        if not stager:
            raise CommandError("--pt needs --stager: the store login that uploads the files.")
        if stager.casefold() == approver.casefold():
            raise CommandError("The owner who approves cannot also upload the files; pass a store login as --stager.")
        files = []
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                raise CommandError(f"PT file not found: {path}")
            files.append((path.name, path.read_bytes()))
        metadata, rows, problems = parse_pt_files(files)
        pt = metadata["pt_opening"]
        self.stdout.write(f"Read {len(files)} file(s): {metadata['stocked_rows']} barcodes, {metadata['quantity']} pieces, "
                          f"{len(problems)} left out, {pt['conflict_count']} conflicts, {pt['unreadable_count']} unreadable rows.")
        name = Path(files[0][0]).stem if len(files) == 1 else f"{len(files)} PT files"
        driver = driver_class(store.tenant, store, stager=lambda: access_for(stager), preparer=lambda: access_for(stager),
                              approver=lambda: access_for(approver), log=lambda line: self.stdout.write(line))
        return driver.stage(evidence_workbook(rows), f"PT opening - {name}.xlsx"[:200], metadata, rows)
