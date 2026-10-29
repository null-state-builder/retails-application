"""A test store opens with no second person while its opening policy says so (plan §1, §2, §5).

The opening preparer is the maker of all three requests of an opening - the source,
each manifest and each opening PT - and confirms each with
``decide_without_second_person`` under a ``pt.approve.opening`` policy flagged
``auto_approve_test_store_opening``. Every decision is still recorded, naming the
policy. A later PT opens only barcodes the store never opened; the same PT twice is
the same source.
"""
from __future__ import annotations

import io
import time
import uuid
from decimal import Decimal
from typing import Any

import pytest
from openpyxl import Workbook

from accounts.goods_demo import _owner_human_id, _publish_tenant_config
from accounts.test_store_loader import (PT_NOTE, PT_REASON, OpeningDriver, WaitingForApproval, access_for, act,
                                        catalogue_config)
from alerts.goods_models import GoodsException
from approvals import goods_services
from approvals.goods_models import ApprovalDecision, ApprovalRequest
from approvals.goods_services import decide_without_second_person
from core.refusals import Refusal
from core.tenancy import tenant_context
from masters.models import Store
from ptmapper import opening_auto, soh_services
from ptmapper import pt_opening_services as quick
from ptmapper.goods_models import OpeningManifestRow
from ptmapper.pt_opening import evidence_workbook, parse_pt_files
from ptmapper.soh_models import SohImport
from sell.services.goods_stock import read_shelf
from tests.test_pt_opening import PT_HEADER, pt_row, store as store  # noqa: F401 - the fixture

pytestmark = pytest.mark.django_db

STORE_PERSON = "wrr.store@synthetic.demo"
PREPARER = "wrr.preparer@synthetic.demo"


class Stop(Exception):
    """Ends a driver run at the first request of a given subject kind, leaving it pending."""


class AutoDriver(OpeningDriver):
    """The opening as the job runs it: no approver, so the preparer confirms each request it made
    (``OpeningDriver._decide`` → ``decide_without_second_person``). ``stop_at`` names a subject
    kind (``soh_import``, ``manifest``, ``document``) whose request is left pending."""

    stop_at: str | None = None

    def _decide(self, request_id: Any, what: str) -> None:
        if self.stop_at is not None and ApprovalRequest.objects.get(pk=request_id).subject_kind == self.stop_at:
            raise Stop
        super()._decide(request_id, what)


def driver(store: Store, stop_at: str | None = None, configure: Any = None) -> AutoDriver:
    found = AutoDriver(store.tenant, store, stager=lambda: access_for(STORE_PERSON), preparer=lambda: access_for(PREPARER),
                       log=lambda _line: None, configure=configure, note=PT_NOTE, reason=PT_REASON)
    found.stop_at = stop_at
    return found


def opening_policy(store: Store, *, auto: bool, qty_max: int = 1_000_000) -> Any:
    """Publish (or keep) the store's ``pt.approve.opening`` policy, with or without the flag."""
    payload: dict[str, Any] = {"action": "pt.approve.opening", "roles": ["owner"], "site_ids": [], "brand_ids": [],
                               "require_distinct": True, "qty_max": qty_max, "value_max": "100000000000",
                               "step_up": True, "unknown_value": "refuse"}
    payload[opening_auto.FLAG] = auto
    version = _publish_tenant_config(store.tenant, _owner_human_id(store.tenant), kind="approval", payload=payload,
                                     label=f"test-opening-policy-{uuid.uuid4()}")
    time.sleep(0.01)  # the next command's time is after the policy took effect
    return version


def pt_book(rows: list[list[Any]], bill: str = "B-1426") -> bytes:
    """A PT work sheet with its bill number on every line, as the normalised PT file has."""
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append([*PT_HEADER, "BillNo"])
    for row in rows:
        sheet.append([*(float(v) if isinstance(v, Decimal) else v for v in row), bill])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def stage(store: Store, book: bytes, name: str = "pt.xlsx") -> SohImport:
    metadata, rows, _problems = parse_pt_files([(name, book)])
    source: SohImport = driver(store).stage(evidence_workbook(rows), f"PT opening - {name}", metadata, rows)
    return source


def config_for(store: Store, source: SohImport) -> dict[str, Any]:
    """The catalogue the opening needs (it republishes the policies, so the flag goes on after)."""
    from masters.goods_services import linked_warehouse_id

    sites = [store, *Store.objects.filter(pk=linked_warehouse_id(store) or 0)]
    return catalogue_config(store.tenant, _owner_human_id(store.tenant), sites, quick._rows(source), cost="pt",
                            log=lambda _line: None)


def run_opening(store: Store, source: SohImport, *, auto: bool = True, stop_at: str | None = None) -> dict[str, Any]:
    config = config_for(store, source)
    opening_policy(store, auto=auto)
    driver(store, stop_at, configure=lambda: config).run(source.pk)
    return config


def waiting_code(waiting: pytest.ExceptionInfo[WaitingForApproval]) -> str | None:
    """The refusal the driver turned into "waiting for approval"."""
    cause = waiting.value.__cause__
    return cause.code if isinstance(cause, Refusal) else None


def requests_of(source: SohImport) -> list[ApprovalRequest]:
    from ptmapper.goods_models import GoodsPt

    manifests = [str(batch.manifest_id) for batch in source.batches.all()]
    documents = [str(pt.document_id) for pt in GoodsPt.objects.filter(manifest_version__manifest_id__in=manifests)]
    return list(ApprovalRequest.objects.filter(subject_key__in=[str(source.pk), *manifests, *documents]))


def test_flagged_test_store_opens_with_the_preparer_confirming_every_step(store: Store) -> None:
    with tenant_context(store.tenant_id):
        book = pt_book([pt_row("A1", 3), pt_row("A2", 2, size="L")])
        source = stage(store, book)
        run_opening(store, source)
        source.refresh_from_db()
        assert source.state == "applied" and sum(read_shelf(store).quantities.values()) == 5
        assert soh_services.opening_reconciliation(store)["passed"]
        requests = requests_of(source)
        assert {r.requested_action for r in requests} == {"opening.manifest.approve", "pt.approve.opening"}
        assert len(requests) == 3 and {r.state for r in requests} == {"approved"}
        preparer = access_for(PREPARER).human_id
        assert {r.maker_id for r in requests} == {preparer}  # one maker for the source, manifest and PT
        policy = opening_auto.policy_version(_Now(store), store.pk)  # type: ignore[arg-type]
        for decision in ApprovalDecision.objects.filter(request__in=requests):
            assert decision.checker_id == preparer and decision.reason_code == "NO_APPROVAL_REQUIRED"
            assert decision.result is not None and decision.result["auto"] is True and decision.result["policy_version_id"] == str(policy)
        # The PT bill is the pieces' older origin: no per-row origin investigation.
        rows = OpeningManifestRow.objects.filter(manifest_version__manifest__site=store)
        assert {row.payload["older_origin_ref"] for row in rows} == {"PT bill B-1426"}
        assert not GoodsException.objects.filter(kind="opening_origin_unavailable", site_id=store.pk).exists()


class _Now:
    """Enough of a command run for a read-only rule check outside a command."""

    def __init__(self, store: Store) -> None:
        from django.utils import timezone

        self.tenant_id, self.now = store.tenant_id, timezone.now()


def test_without_the_flag_each_step_waits_for_its_approver(store: Store) -> None:
    with tenant_context(store.tenant_id):
        source = stage(store, pt_book([pt_row("A1", 1)]))
        with pytest.raises(WaitingForApproval) as waiting:
            run_opening(store, source, auto=False)
        assert waiting_code(waiting) == "AUTO_APPROVAL_NOT_ALLOWED"
        source.refresh_from_db()
        assert source.approval_request is not None
        assert source.state == "submitted" and source.approval_request.state == "pending"
        assert not ApprovalDecision.objects.filter(request=source.approval_request).exists()


def test_a_real_store_never_skips_its_approver(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    from masters import store_features

    with tenant_context(store.tenant_id):
        source = stage(store, pt_book([pt_row("A1", 1)]))
        monkeypatch.setattr(store_features, "is_real_store", lambda _store: True)
        with pytest.raises(WaitingForApproval) as waiting:
            run_opening(store, source)
        assert waiting_code(waiting) == "AUTO_APPROVAL_NOT_ALLOWED"
        source.refresh_from_db()
        assert source.approval_request is not None and source.approval_request.state == "pending"


def test_only_the_maker_confirms(store: Store) -> None:
    with tenant_context(store.tenant_id):
        source = stage(store, pt_book([pt_row("A1", 1)]))
        with pytest.raises(Stop):
            run_opening(store, source, stop_at="soh_import")
        source.refresh_from_db()
        request = source.approval_request
        assert request is not None
        for email in (STORE_PERSON, "syn.owner@synthetic.demo"):
            other = access_for(email)
            with pytest.raises(Refusal) as denied:
                act(other, request.requested_action, lambda run, o=other: decide_without_second_person(run, request.pk, access=o))
            assert denied.value.code == "ACTION_DENIED"
        request.refresh_from_db()
        assert request.state == "pending"
        act(access_for(PREPARER), request.requested_action, lambda run: decide_without_second_person(run, request.pk))
        request.refresh_from_db()
        assert request.state == "approved"
        with pytest.raises(Refusal) as again:
            act(access_for(PREPARER), request.requested_action, lambda run: decide_without_second_person(run, request.pk))
        assert again.value.code == "STATE_CONFLICT"


@pytest.mark.parametrize("change", ["flag_removed", "policy_replaced"])
def test_a_policy_change_after_submission_stops_the_opening_pt(store: Store, change: str) -> None:
    with tenant_context(store.tenant_id):
        source = stage(store, pt_book([pt_row("A1", 2)]))
        with pytest.raises(Stop):
            run_opening(store, source, stop_at="document")
        pending = ApprovalRequest.objects.get(requested_action="pt.approve.opening", state="pending")
        assert pending.policy_version_id is not None
        if change == "flag_removed":
            opening_policy(store, auto=False)
        else:
            opening_policy(store, auto=True, qty_max=999_999)  # flagged, but not the pinned version
        with pytest.raises(Refusal) as refused:
            act(access_for(PREPARER), pending.requested_action, lambda run: decide_without_second_person(run, pending.pk))
        assert refused.value.code == ("AUTO_APPROVAL_NOT_ALLOWED" if change == "flag_removed" else "APPROVAL_STALE")
        pending.refresh_from_db()
        assert pending.state == "pending" and sum(read_shelf(store).quantities.values()) == 0


def test_transfers_keep_their_own_rule() -> None:
    rules = goods_services._AUTO_RULES
    assert ("transfer", "pt.approve.transfer") not in rules
    assert {key for key, (_rule, flag) in rules.items() if flag == opening_auto.FLAG} == {
        ("soh_import", "opening.manifest.approve"), ("manifest", "opening.manifest.approve"),
        ("document", "pt.approve.opening")}


def test_the_same_pt_twice_is_the_same_source(store: Store) -> None:
    with tenant_context(store.tenant_id):
        book = pt_book([pt_row("A1", 3)])
        first = stage(store, book, "ratu road.xlsx")
        assert stage(store, book, "ratu road.xlsx").pk == first.pk
        assert stage(store, book, "ratu road (copy).xlsx").pk == first.pk  # same content, another file name
        assert SohImport.objects.filter(site=store).count() == 1


def test_a_later_pt_opens_only_barcodes_the_store_never_opened(store: Store) -> None:
    with tenant_context(store.tenant_id):
        first = stage(store, pt_book([pt_row("A1", 3), pt_row("A2", 2, size="L")]))
        run_opening(store, first)
        assert sum(read_shelf(store).quantities.values()) == 5

        later = stage(store, pt_book([pt_row("A1", 4), pt_row("N1", 6, prate=Decimal("800"))], bill="B-2001"), "later.xlsx")
        assert later.pk != first.pk
        assert soh_services.requires_inventory_reconciliation(later)  # A1 is still included before review
        run_opening(store, later)
        later.refresh_from_db()
        assert later.rows.get(barcode="A1").exclusion_reason == soh_services.ALREADY_OPENED
        assert later.rows.get(barcode="N1").exclusion_reason == ""
        assert later.state == "applied" and not soh_services.requires_inventory_reconciliation(later)
        assert sum(read_shelf(store).quantities.values()) == 11  # 5 + only N1's 6
