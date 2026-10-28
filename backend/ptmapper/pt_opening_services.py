"""Store test run: "Approve and load" for a staged opening source (synthetic only).

A test store opens from its PT files (``ptmapper.pt_opening``), and the nightly
SOH sync stages new barcodes as a top-up source. Mapping hundreds of brands and
sizes by hand, verifying with a CSV and scanning every line is not what a test
run needs, so one owner click runs the source through the same reviewed opening
path the test-store loader uses (``accounts.test_store_loader.OpeningDriver``):

* the uploader records the file quantities and accepts onto the floor;
* the store's opening preparer (``<prefix>.preparer``) maps, values and drafts;
* the owner who clicked approves the source, each manifest and each opening PT.

Three different people, as the opening path requires. A load can take minutes,
so it runs on a thread; its progress lives on the source (``reconciliation``,
which the review hash does not cover) so every API worker can report it, and a
re-run resumes where an interrupted one stopped.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from django.db import connection
from django.utils import timezone

from accounts.principal import AccessContext
from core.refusals import Refusal
from ptmapper import soh_services
from ptmapper.soh_models import SohImport

logger = logging.getLogger(__name__)

#: A run that has not reported a step for this long is taken as stopped (process restart).
STALE = timedelta(minutes=30)


def status(source: SohImport) -> dict[str, Any]:
    return dict((source.reconciliation or {}).get("quick_load") or {})


def _record(source_id: uuid.UUID, **fields: Any) -> None:
    source = SohImport.objects.get(pk=source_id)
    current = dict(source.reconciliation or {})
    current["quick_load"] = {**dict(current.get("quick_load") or {}), **fields, "updated_at": timezone.now().isoformat()}
    SohImport.objects.filter(pk=source_id).update(reconciliation=current)


def running(source: SohImport) -> bool:
    state = status(source)
    if state.get("state") != "running":
        return False
    updated = state.get("updated_at")
    return isinstance(updated, str) and timezone.now() - datetime.fromisoformat(updated) < STALE


def _email(human_id: Any) -> str:
    from accounts.models import User

    user = User.objects.filter(human_id=human_id, is_active=True).first()
    if user is None:
        raise Refusal("STATE_CONFLICT", "That person has no active login.", status=409)
    return str(user.email)


def _preparer_email(source: SohImport, exclude: set[str]) -> str:
    """The store's opening preparer: the named one if preparation started, else a ``*preparer@`` test login."""
    from accounts.models import User
    from accounts.test_store_loader import _is_preparer, access_for

    if source.prepared_by_id is not None:
        return _email(source.prepared_by_id)
    for user in User.objects.filter(tenant_id=source.tenant_id, is_active=True,
                                    email__icontains="preparer@").order_by("email"):
        if not _is_preparer(user.email) or str(user.human_id) in exclude:
            continue
        access = access_for(user.email)
        cells = {(source.site_id, None)}
        if any(access.covers_all({soh_services.PREPARE}, cells, {field}) for field in ("cost", "cost_own_pt")):
            return str(user.email)
    raise Refusal("OPENING_PREPARER_MISSING",
                  "This store has no opening preparer login. Run load_test_store for it to create one.", status=409)


def check(access: AccessContext, source: SohImport) -> tuple[str, str, str]:
    """Who stages, prepares and approves; refuses unless this caller may load this source now."""
    people = loaders(access, source)
    access.require_step_up()
    return people


def loaders(access: AccessContext, source: SohImport) -> tuple[str, str, str]:
    """``check`` without the web step-up: a server shell (``load_pt_opening``) has no password prompt."""
    from ptmapper.stock_sync_services import require_test_store

    require_test_store(source.site)
    if source.state == "withdrawn":
        raise Refusal("SOH_SOURCE_WITHDRAWN", "This source was withdrawn.", status=409)
    if status(source).get("state") == "done":
        raise Refusal("STATE_CONFLICT", "This source is already loaded.", status=409)
    if not access.covers_all({soh_services.APPROVE}, soh_services.source_cells(source), {"cost", "financial"}):
        raise Refusal("ACTION_DENIED", "Only an owner who approves opening stock can load it.", status=403)
    if str(access.human_id) in {str(source.uploaded_by_id), str(source.prepared_by_id)}:
        raise Refusal("SELF_APPROVAL", "The person who uploaded or prepared this source cannot approve it.", status=403)
    if running(source):
        raise Refusal("STATE_CONFLICT", "This source is already loading.", status=409)
    caller = _email(access.human_id)
    return _email(source.uploaded_by_id), _preparer_email(source, {str(access.human_id), str(source.uploaded_by_id)}), caller


def _rows(source: SohImport) -> list[Any]:
    return [SimpleNamespace(barcode=r.barcode, brand=r.source_brand, quantity=r.quantity, rate=r.source_rate_paise,
                            mrp=r.mrp_paise, source=r.source) for r in source.rows.order_by("ordinal")]


def load(source_id: uuid.UUID, *, stager: str, preparer: str, approver: str,
         echo: Callable[[str], None] = lambda _text: None) -> None:
    """Run the whole opening for one source; records progress, then ``done`` or ``failed``.

    ``echo`` also receives each step and log line (``load_pt_opening`` prints them).
    """
    from accounts.test_store_loader import (PT_NOTE, PT_REASON, SOH_REASON, TEST_NOTE, OpeningDriver, access_for,
                                            catalogue_config)
    from accounts.goods_demo import _owner_human_id
    from masters.goods_services import linked_warehouse_id
    from masters.models import Store

    source = SohImport.objects.select_related("site").get(pk=source_id)
    tenant = source.site.tenant
    pt = bool(source.source_metadata.get("pt_opening"))
    lines: list[str] = []

    def log(text: str) -> None:
        lines.append(text)
        _record(source_id, log=lines[-20:])
        echo(text)

    def step(text: str) -> None:
        _record(source_id, step=text)
        echo(text)

    try:
        _record(source_id, state="running", step="Creating brands, sizes and tax rules", error=None,
                started_at=timezone.now().isoformat(), log=[])
        store = source.site
        sites = [store, *Store.objects.filter(pk=linked_warehouse_id(store) or 0)]
        config = catalogue_config(tenant, _owner_human_id(tenant), sites, _rows(source),
                                  cost="pt" if pt else "soh", log=log)
        driver = OpeningDriver(tenant, store, stager=lambda: access_for(stager), preparer=lambda: access_for(preparer),
                               approver=lambda: access_for(approver), log=log)
        driver.load(source, config, note=PT_NOTE if pt else TEST_NOTE, reason=PT_REASON if pt else SOH_REASON,
                    step=step)
        included = source.rows.filter(quantity__gt=0, exclusion_reason="")
        _record(source_id, state="done", step="Stock is on the floor and ready to sell",
                finished_at=timezone.now().isoformat(),
                loaded_rows=included.count(), loaded_qty=sum(included.values_list("quantity", flat=True)),
                excluded_rows=source.rows.filter(quantity__gt=0).exclude(exclusion_reason="").count())
    except Refusal as exc:
        _record(source_id, state="failed", error=f"{exc.code}: {exc.message}", finished_at=timezone.now().isoformat())
    except Exception as exc:  # the thread has no caller; keep the failure visible on the source
        logger.exception("Quick load of opening source %s failed", source_id)
        _record(source_id, state="failed", error=f"{type(exc).__name__}: {exc}"[:500], finished_at=timezone.now().isoformat())


def start(access: AccessContext, source: SohImport) -> None:
    """Check, mark running and load on a background thread (it may take minutes)."""
    from core.tenancy import tenant_context

    stager, preparer, approver = check(access, source)
    _record(source.pk, state="running", step="Starting", error=None, by=approver)
    tenant_id = source.tenant_id

    def work() -> None:
        try:
            with tenant_context(tenant_id):
                load(source.pk, stager=stager, preparer=preparer, approver=approver)
        finally:
            connection.close()

    threading.Thread(target=work, name=f"opening-load-{source.pk}", daemon=True).start()
