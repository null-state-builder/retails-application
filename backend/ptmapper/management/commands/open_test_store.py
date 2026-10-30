"""Open a test store from its PT files on the server, without the browser (synthetic tenant only).

    python manage.py open_test_store --store WRR --pt WARDROBE_PT_combined.xlsx \\
        --uploader debanjan@wardrobe.test --wait

stages the PT files as ``--uploader`` (exactly as the upload at /goods/opening does;
the same file again is the same source), then queues the store's ``opening_load``
job, which the goods worker runs as the store's opening preparer
(``ptmapper.pt_opening_services``). ``--wait`` follows the job until the stock is on
the floor, it fails, or it waits for an approval, then prints the totals and how long
each step took. ``--foreground`` runs the same steps in this process instead of the
worker (debugging; still under the source's advisory lock). Without ``--pt`` it
resumes the store's one open PT source.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Open a test store from its PT files: stage them and run the opening load (synthetic tenant only)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--store", required=True, help="Store code, e.g. WRR")
        parser.add_argument("--pt", nargs="+", help="PT workbook(s) to stage (leave out to resume the store's PT source)")
        parser.add_argument("--uploader", required=True,
                            help="Who uploads the files and starts the load (a store login, e.g. the store manager)")
        parser.add_argument("--wait", action="store_true", help="Follow the load until it is done, failed or waiting")
        parser.add_argument("--foreground", action="store_true", help="Run the load in this process, not the worker")
        parser.add_argument("--timeout", type=int, default=1800, help="Seconds --wait follows the load (default 1800)")

    def handle(self, *args: Any, **options: Any) -> None:
        from accounts.models import User
        from core.refusals import Refusal
        from core.tenancy import tenant_context
        from masters.goods_models import Tenant
        from masters.models import Store
        from ptmapper import pt_opening_services as opening
        from ptmapper.stock_sync_services import require_test_store

        tenants = list(Tenant.objects.filter(synthetic=True))
        if len(tenants) != 1:
            raise CommandError("Expected exactly one synthetic tenant on this deployment.")
        tenant = tenants[0]
        with tenant_context(tenant.pk):
            store = Store.objects.filter(tenant_id=tenant.pk, code=options["store"]).first()
            if store is None:
                raise CommandError(f"No store {options['store']} on the synthetic tenant.")
            try:
                require_test_store(store)
            except Refusal as exc:
                raise CommandError(f"{exc.code}: {exc.message}") from exc
            user = User.objects.filter(tenant_id=tenant.pk, email__iexact=options["uploader"], is_active=True).first()
            if user is None:
                raise CommandError(f"No active login {options['uploader']}.")
            uploader = opening.job_access(tenant.pk, user.human_id)
            source = self._stage(store, uploader, options["pt"]) if options["pt"] else self._open_source(store)
            meta = source.source_metadata
            self.stdout.write(f"Source {source.pk} ({source.source_name}): {meta.get('stocked_rows')} barcodes, "
                              f"{meta.get('quantity')} pieces, state {source.state}.")
            if opening.status(source).get("state") == "done":
                self._report(source)
                return
            began = time.monotonic()
            if options["foreground"]:
                state = self._foreground(source)
            else:
                if opening.running(source):
                    self.stdout.write("A load of this source is already queued or running; following it.")
                else:
                    try:
                        opening.request_load(uploader, source)
                    except Refusal as exc:
                        raise CommandError(f"{exc.code}: {exc.message}") from exc
                    self.stdout.write("Load queued for the goods worker.")
                if not options["wait"]:
                    return
                state = self._follow(source, options["timeout"])
            source.refresh_from_db()
            self._report(source, wall=time.monotonic() - began)
        if state == "waiting_approval":
            self.stdout.write(self.style.WARNING("Waiting for approval: an owner decides it in Approvals, then load again."))
        elif state != "done":
            raise CommandError(f"Load {state}: {opening.status(source).get('error')}")

    def _stage(self, store: Any, uploader: Any, paths: list[str]) -> Any:
        from accounts.test_store_loader import OpeningDriver
        from ptmapper import soh_services
        from ptmapper.pt_opening import evidence_workbook, parse_pt_files

        if not uploader.covers_all({soh_services.STAGE}, {(store.pk, None)}):
            raise CommandError(f"{store.code}: the uploader may not stage opening sources here (opening.import.stage).")
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
        driver = OpeningDriver(store.tenant, store, stager=lambda: uploader, preparer=lambda: uploader,
                               log=lambda line: self.stdout.write(line))
        return driver.stage(evidence_workbook(rows), f"PT opening - {name}.xlsx"[:200], metadata, rows)

    def _open_source(self, store: Any) -> Any:
        from ptmapper import pt_opening_services as opening
        from ptmapper.soh_models import SohImport

        sources = [s for s in SohImport.objects.filter(tenant_id=store.tenant_id, site=store)
                   .exclude(state="withdrawn").order_by("created_at")
                   if s.source_metadata.get("pt_opening") and opening.status(s).get("state") != "done"]
        if len(sources) != 1:
            raise CommandError(f"{store.code} has {len(sources)} open PT sources; pass --pt with the file to load.")
        return sources[0]

    def _foreground(self, source: Any) -> str:
        from ptmapper import pt_opening_services as opening

        def echo(text: str) -> None:
            self.stdout.write(f"{timezone.localtime():%H:%M:%S} {text}")

        with opening.source_lock(source.pk) as taken:
            if not taken:
                raise CommandError("This source is already loading (a worker job or another shell holds it).")
            opening.drive(source.pk, budget=None, echo=echo)
        source.refresh_from_db()
        return str(opening.status(source).get("state"))

    def _follow(self, source: Any, timeout: int) -> str:
        from ptmapper import pt_opening_services as opening

        deadline, last = time.monotonic() + timeout, None
        while True:
            source.refresh_from_db()
            state = opening.status(source)
            line = (state.get("state"), state.get("step"))
            if line != last:
                self.stdout.write(f"{timezone.localtime():%H:%M:%S} {state.get('state')}: {state.get('step') or ''}")
                last = line
            if state.get("state") in opening.FINISHED:
                return str(state["state"])
            if time.monotonic() > deadline:
                raise CommandError(f"Still {state.get('state')} after {timeout} s (is the goods worker running?).")
            time.sleep(2)

    def _report(self, source: Any, wall: float | None = None) -> None:
        from ptmapper import pt_opening_services as opening

        state = opening.status(source)
        if state.get("state") == "done":
            self.stdout.write(self.style.SUCCESS(
                f"Done: {state.get('loaded_qty')} pieces on the floor at {source.site.code} "
                f"({state.get('loaded_rows')} barcodes, {state.get('excluded_rows')} left out)."))
        timings = state.get("timings") or []
        if timings:
            self.stdout.write("Steps (seconds):")
            for row in timings:
                self.stdout.write(f"  {row['seconds']:>8.2f}  {row['step']}")
            self.stdout.write(f"  {sum(row['seconds'] for row in timings):>8.2f}  all steps")
        if wall is not None:
            self.stdout.write(f"Wall time {wall:.1f} s.")
