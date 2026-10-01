"""Reviewed opening stock through the real online sale and posting writer.

Only the positively identified disposable proof database is permitted by the
test harness. Commercial settings below are explicitly synthetic fixtures; the
application's real-store CA gate stays enforced.
"""
from __future__ import annotations

import copy
import uuid
from datetime import date, timedelta
from decimal import Decimal
from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.db.models import Sum
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.response import Response

from accounts.goods_admin_services import open_assignment
from accounts.goods_demo import _publish_tenant_config
from accounts.goods_models import Staff
from accounts.sessions import revoke_session
from core.fiscal import financial_year
from core.commands import CommandRun
from core.gl import GLAccount, GLEntry
from core.numbering import prepare_series
from core.tenancy import tenant_context
from finledger.models import CashLedgerEntry
from masters.first_store_readiness import selling_checks
from masters.goods_models import SiteGuard, Sbu
from masters.goods_identity_models import ProductSku
from masters.goods_services import append_master_version, apply_readiness_approval, compute_readiness_checks
from masters.document_series_models import DocumentPrefix, NumberingSetting
from masters.tax_setting_models import TaxSettingVersion
from masters.store_feature_models import StoreFeatureSwitch
from sell.models import OnlineSaleSubmission, Sale, SaleLine, SaleTender
from sell.cash_models import CashCount, CashCountBill
from sell.cash_views import CashCountsView, CashPositionView
from sell.serializers import SaleWriteSerializer
from sell.services.dataset import build_dataset
from sell.services.cash_count import record_count
from sell.services.goods_stock import read_shelf
from sell.services.invoice_numbers import till_numbering
from sell.services.online import finalise_submission, prepare_online_sale_series
from sell.services.till_authority import TillError
from sell.services.tax_rulebook import StoreTaxBooks
from sell.services.till_authority import register_till, renew_authority
from sell.views import DatasetView, OnlineFinaliseView, SaleListCreateView
from stockledger.goods_models import JournalBatch
from tests.first_store_goods import actors, command, opened_goods
from tests.test_so03_denials import worlds as worlds


def configure_online(proof: Any) -> Any:
    world, site = proof.world, proof.world.sites[0]
    now = timezone.now()
    # Opening and acceptance ran as a non-synthetic tenant. The commercial
    # fixture is now deliberately synthetic to exercise saved tax versions
    # without representing the absent real-shop CA sign-off as approval.
    world.tenant.synthetic = True
    world.tenant.save(update_fields=["synthetic"])
    staff = Staff.objects.create(tenant=world.tenant, human_id=proof.manager.human_id, salesperson=True)
    command(proof.owner, "proof.staff", lambda run: open_assignment(run, staff, site.pk, now - timedelta(days=1)))
    proof.staff = staff
    for kind, target in (("entity", site.gstin.legal_entity), ("registration", site.gstin), ("site", site)):
        def publish_master(run: CommandRun, kind: str = kind, target: Any = target) -> Any:
            return append_master_version(run, kind=kind, target_key=str(target.pk), revision=1,
                                         payload={"name": str(target), "brand_ids": [world.brands[0].pk]})
        command(proof.owner, f"proof.{kind}", publish_master)
    Sbu.objects.create(tenant=world.tenant, site=site, brand=world.brands[0], code="ONLINE-PROOF")
    business = _publish_tenant_config(world.tenant, proof.owner.human_id, kind="business_profile",
        payload={"categories": ["Shirt"], "identity_profile_id": str(proof.identity.pk), "expected_skus": 1,
                 "brands": 1, "sites": 1, "sbus": 1, "staff": 3, "documents_per_day": 10,
                 "evidence_bytes_per_year": 10000, "commercial_labels": ["Proof"],
                 "workforce_scope": "Disposable proof only", "tills_per_site": [{"site_id": str(site.pk), "count": 1}],
                 "accounting_interface": "Proof ledger"}, label="online-proof-business")
    world.tenant.business_profile_version = business
    world.tenant.save(update_fields=["business_profile_version"])
    _publish_tenant_config(world.tenant, proof.owner.human_id, kind="approval",
        payload={"action": "pt.approve.receipt", "roles": ["owner"], "purpose": "receipt", "site_ids": [],
                 "brand_ids": [], "require_distinct": True, "qty_max": 100, "value_max": "10000000",
                 "step_up": True, "unknown_value": "refuse"}, label="online-proof-receipt")
    _publish_tenant_config(world.tenant, proof.owner.human_id, kind="sell_policy",
        payload={"manual_discount_cap_percent": "10", "manual_discount_on_offer_lines": False,
                 "return_window_days": 15}, label="online-proof-policy")
    for kind in ("GRN", "CGRN", "RPT"):
        prepare_series(world.tenant.pk, site.gstin.legal_entity, kind)
    TaxSettingVersion.objects.create(tenant=world.tenant, version=2, applies_from=timezone.localdate(),
        rules=[{"kind": "flat_rate", "hsn_prefix": "6101", "name": "Synthetic proof tax", "rate": "5.00"}],
        unmatched_rate=Decimal("0"), note="Synthetic commercial proof, not CA sign-off", actor_id=proof.owner.human_id)
    for key in ("tax-settings", "document-series"):
        StoreFeatureSwitch.objects.create(tenant=world.tenant, site=site, feature_key=key, enabled=True, updated_at=now)
    DocumentPrefix.objects.create(tenant=world.tenant, site=site, code="PRF", updated_at=now)
    day = timezone.localdate()
    NumberingSetting.objects.create(tenant=world.tenant, new_format_from=date(day.year if day.month >= 4 else day.year - 1, 4, 1),
                                   till_block_size=30, updated_at=now)
    proof.till, proof.token = register_till(site, proof.owner.user)
    renew_authority(site)
    proof.numbering = till_numbering(site, proof.till, {})
    guard = SiteGuard.objects.get(site=site)
    setup = compute_readiness_checks(site, timezone.now())
    assert all(row["passed"] for row in setup), setup
    command(proof.owner, "proof.readiness.goods", lambda run: apply_readiness_approval(run, store=site, guard=guard,
        action="approve_goods", checks=setup, reason_code="PROOF", residual_decisions=[], approver_id=proof.owner.human_id))
    guard.refresh_from_db()
    assert guard.lifecycle == SiteGuard.Lifecycle.ACTIVE
    checks = selling_checks(site, timezone.now())
    assert all(row["passed"] for row in checks), checks
    command(proof.owner, "proof.readiness.sell", lambda run: apply_readiness_approval(run, store=site, guard=guard,
        action="approve_sell", checks=checks, reason_code="PROOF", residual_decisions=[], approver_id=proof.owner.human_id))
    return proof


@pytest.fixture
def online_goods(worlds: Any) -> Iterator[Any]:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        yield configure_online(opened_goods(world, *actors(world)))


def bill(proof: Any, *, qty: int = 1, discount: int = 0, seq: int = 1) -> tuple[dict[str, Any], dict[str, Any]]:
    site = proof.world.sites[0]
    payload = build_dataset(site, "")
    item = next(row for row in payload["items"] if row["barcode"] == proof.barcode)
    now = timezone.now()
    net = item["mrp_paise"] * qty - discount
    tax = StoreTaxBooks(site).at(now)
    split = tax.line_tax(item["hsn"], net, qty).split
    block = next(row for row in proof.numbering.blocks if row["month"] == timezone.localdate().strftime("%Y-%m"))
    wire = {"idempotency_uuid": str(uuid.uuid4()), "store": site.code, "fy": financial_year(), "till_seq": seq,
        "origin": "online", "billed_at": now.isoformat(), "commercial_revision": payload["commercial_revision"],
        "till_number": f"{proof.till.series_prefix}-{seq}", "tax_setting_version": tax.version,
        "tax_invoice_number": f"{block['prefix']}/{block['fy']}/{block['first'] + seq - 1}",
        "lines": [{"line_no": 1, "barcode": proof.barcode, "season": item["season"], "qty": qty,
                   "mrp_paise": item["mrp_paise"], "disc_paise": discount, "net_paise": net,
                   "gst_rate": str(split.rate), "gst_paise": split.gst_paise, "salesperson": str(proof.staff.pk)}],
        "tenders": [{"mode": "cash", "amount_paise": net}],
        "totals": {"gross_paise": item["mrp_paise"] * qty, "discount_paise": discount,
                   "net_paise": net, "gst_paise": split.gst_paise, "round_paise": 0}}
    parsed = SaleWriteSerializer(data=wire)
    assert parsed.is_valid(), parsed.errors
    return wire, dict(parsed.validated_data)


def post(view: Any, proof: Any, wire: dict[str, Any], *, token: str | None = None) -> Response:
    request = APIRequestFactory().post("/api/sell/sales/finalise-online", wire, format="json",
                                      HTTP_X_KDPS_DEVICE=proof.token if token is None else token)
    force_authenticate(request, user=proof.manager.user, token=proof.manager.session)
    return cast(Response, view.as_view()(request))


def test_reviewed_opening_to_accepted_sale_and_lost_response_exact_replay(online_goods: Any) -> None:
    proof = online_goods
    site = proof.world.sites[0]
    assert sum(read_shelf(site).quantities.values()) == 3
    wire, _ = bill(proof, discount=10000)
    first = post(OnlineFinaliseView, proof, wire)
    assert first.status_code == 201, first.data
    sale = Sale.objects.get(pk=first.data["id"])
    assert sale.lines.get().brand_ref_id == proof.world.brands[0].pk
    assert sum(read_shelf(site).quantities.values()) == 2
    counts = (Sale.objects.count(), SaleLine.objects.count(), SaleTender.objects.count(),
              JournalBatch.objects.count(), GLEntry.objects.count(), CashLedgerEntry.objects.count())
    # Simulate a lost post-commit response by replaying exactly the submitted
    # wire body. It must answer the original number and change no posting.
    replay = post(OnlineFinaliseView, proof, wire)
    assert replay.status_code == 200 and replay.data == first.data
    assert counts == (Sale.objects.count(), SaleLine.objects.count(), SaleTender.objects.count(),
                      JournalBatch.objects.count(), GLEntry.objects.count(), CashLedgerEntry.objects.count())
    assert OnlineSaleSubmission.objects.get().sale_id == sale.pk
    assert GLEntry.objects.filter(doc_number=sale.doc_number).aggregate(total=Sum("amount"))["total"] == 0
    cash = CashLedgerEntry.objects.get(doc_number=sale.doc_number)
    assert cash.amount == 90000
    assert GLEntry.objects.get(doc_number=sale.doc_number, account=GLAccount.CASH).amount == cash.amount
    assert sale.lines.get().cost_book == SaleLine.CostBook.OWN
    assert not sale.flags.exists()


@pytest.mark.parametrize("case,code", [("stale", "PRICING_STALE"), ("stock", "INSUFFICIENT_ELIGIBLE_STOCK"),
                                     ("device", "DEVICE_REQUIRED"), ("number", "BILL_NO_STALE")])
def test_actual_online_refusal_changes_no_business_effect(online_goods: Any, case: str, code: str) -> None:
    proof = online_goods
    wire, _ = bill(proof, qty=4 if case == "stock" else 1, seq=2 if case == "number" else 1)
    if case == "stale":
        wire["commercial_revision"] = "0" * 64
    before = (JournalBatch.objects.count(), GLEntry.objects.count(), CashLedgerEntry.objects.count())
    result = post(OnlineFinaliseView, proof, wire, token="unpaired" if case == "device" else None)
    assert result.status_code in (403, 409, 422) and result.data["code"] == code, result.data
    assert not Sale.objects.exists() and not SaleTender.objects.exists()
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 3
    assert before == (JournalBatch.objects.count(), GLEntry.objects.count(), CashLedgerEntry.objects.count())
    if result.status_code != 403:
        assert result.data["not_issued"] is True


def test_default_cashier_can_select_self_without_personal_names_or_pin_hashes(online_goods: Any) -> None:
    proof = online_goods
    request = APIRequestFactory().get("/api/sell/dataset")
    force_authenticate(request, user=proof.manager.user, token=proof.manager.session)
    response = DatasetView.as_view()(request)
    assert response.status_code == 200, response.data
    assert response.data["salespeople"] == [{"id": str(proof.staff.pk), "name": "You"}]
    assert response.data["managers"] == []
    assert not any("cost" in key for row in response.data["items"] for key in row)
    wire, _ = bill(proof)
    legacy = post(SaleListCreateView, proof, wire)
    assert legacy.status_code == 409 and legacy.data["code"] == "ONLINE_FINALISATION_REQUIRED"
    assert not Sale.objects.exists()


def test_changed_replay_and_revoked_readiness_cannot_issue_a_second_bill(online_goods: Any) -> None:
    proof = online_goods
    wire, data = bill(proof)
    assert post(OnlineFinaliseView, proof, wire).status_code == 201
    changed = copy.deepcopy(wire)
    changed["customer"] = {"name": "Different payload"}
    conflict = post(OnlineFinaliseView, proof, changed)
    assert conflict.status_code == 409 and conflict.data["code"] == "IDEMPOTENCY_CONFLICT"
    guard = SiteGuard.objects.get(site=proof.world.sites[0])
    guard.sell_ready = False
    guard.save(update_fields=["sell_ready"])
    # Withdrawal stops new issue, but a currently authorised exact accepted
    # outcome remains readable for timeout recovery.
    replay, refusal = finalise_submission(data, proof.manager.user, proof.manager, proof.token)
    assert replay.sale.pk == Sale.objects.get().pk and refusal is None
    new_wire = copy.deepcopy(wire)
    new_wire["idempotency_uuid"] = str(uuid.uuid4())
    result = post(OnlineFinaliseView, proof, new_wire)
    assert result.status_code == 409 and result.data["code"] == "SELL_NOT_READY"
    assert Sale.objects.count() == 1


@pytest.mark.parametrize("change,code", [("no_discount", "NO_DISCOUNT"), ("policy", "DISCOUNT_OVER_CAP"),
                                      ("field", "ACTION_DENIED"), ("revoked", "AUTH_REQUIRED")])
def test_live_governed_inputs_and_session_denials_preserve_opening(online_goods: Any, change: str, code: str) -> None:
    proof = online_goods
    if change == "no_discount":
        ProductSku.objects.filter(pk=proof.sku_id).update(no_discount=True)
    if change == "policy":
        _publish_tenant_config(proof.world.tenant, proof.owner.human_id, kind="sell_policy",
            payload={"manual_discount_cap_percent": "0", "manual_discount_on_offer_lines": False,
                     "return_window_days": 15}, label="online-proof-withdraw-discount")
    wire, _ = bill(proof, discount=10000 if change in ("no_discount", "policy") else 0)
    if change == "field":
        role = proof.world.roles["store_person"]
        role.field_access = []
        role.save(update_fields=["field_access"])
    if change == "revoked":
        revoke_session(proof.manager.session)
    before = JournalBatch.objects.count()
    response = post(OnlineFinaliseView, proof, wire)
    assert response.data["code"] == code, response.data
    assert not Sale.objects.exists() and not GLEntry.objects.exists() and not CashLedgerEntry.objects.exists()
    assert JournalBatch.objects.count() == before
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 3


def test_session_expiry_after_real_postings_rolls_every_effect_back(online_goods: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from accounts.goods_models import ServerSession
    from sell.services import accept
    proof = online_goods
    wire, _ = bill(proof)
    actual_post = accept._post_value
    def expire_after_post(*args: Any, **kwargs: Any) -> None:
        actual_post(*args, **kwargs)
        ServerSession.objects.filter(pk=proof.manager.session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    monkeypatch.setattr(accept, "_post_value", expire_after_post)
    before = JournalBatch.objects.count()
    response = post(OnlineFinaliseView, proof, wire)
    assert response.status_code == 401 and response.data["code"] == "SESSION_EXPIRED", response.data
    assert not Sale.objects.exists() and not GLEntry.objects.exists() and not CashLedgerEntry.objects.exists()
    assert not OnlineSaleSubmission.objects.exists()
    assert JournalBatch.objects.count() == before
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 3


def test_counter_never_adopts_unowned_or_foreign_same_code_series(worlds: Any) -> None:
    from core.documents import VoucherSeries
    world, other = worlds
    with tenant_context(world.tenant.pk):
        site = world.sites[0]
        SiteGuard.objects.create(tenant=world.tenant, site=site, selling_mode="online_alpha")
        foreign = VoucherSeries.objects.create(fy=financial_year(), store_code=site.code, doc_type="SAL",
                                              tenant=other.tenant, next_seq=12)
        with pytest.raises(TillError) as denied:
            prepare_online_sale_series(site)
        assert denied.value.code == "SERIES_RECONCILIATION_REQUIRED"
        foreign.refresh_from_db()
        assert foreign.next_seq == 12 and foreign.tenant_id == other.tenant.pk


def test_accepted_sale_day_close_counts_one_cash_tender_and_exact_retry(online_goods: Any) -> None:
    proof = online_goods
    wire, _ = bill(proof, discount=10000)
    assert post(OnlineFinaliseView, proof, wire).status_code == 201
    site = proof.world.sites[0]
    StoreFeatureSwitch.objects.create(tenant=proof.world.tenant, site=site, feature_key="cash-count",
                                      enabled=True, updated_at=timezone.now())
    request = APIRequestFactory().get("/api/sell/cash-count")
    force_authenticate(request, user=proof.manager.user, token=proof.manager.session)
    position = CashPositionView.as_view()(request)
    assert position.status_code == 200 and position.data["cash_sales_paise"] == 90000
    assert position.data["bills"] == 1 and position.data["tenders"]["cash"] == 90000
    count = {"id": str(uuid.uuid4()), "notes": {"500": 1, "200": 2, "100": 0, "50": 0, "20": 0, "10": 0},
             "coins_paise": 0, "opening_paise": 0, "expected_paise": 90000, "till_number": proof.till.counter_id}
    first = post(CashCountsView, proof, count)
    assert first.status_code == 201 and first.data["variance_paise"] == 0, first.data
    assert first.data["counted_by_name"] == ""
    replay = post(CashCountsView, proof, count)
    assert replay.status_code == 200 and replay.data == first.data
    assert CashCount.objects.count() == 1 and CashCountBill.objects.count() == 1
    assert CashLedgerEntry.objects.count() == 1 and SaleTender.objects.count() == 1


def test_protected_bill_history_denies_delayed_delivery_after_logout(online_goods: Any) -> None:
    from sell.views import SaleDetailView
    proof = online_goods
    wire, _ = bill(proof)
    response = post(OnlineFinaliseView, proof, wire)
    assert response.status_code == 201
    request = APIRequestFactory().get("/api/sell/sales/issued")
    force_authenticate(request, user=proof.manager.user, token=proof.manager.session)
    authorised = SaleDetailView.as_view()(request, doc_number=response.data["doc_number"])
    assert authorised.status_code == 200 and authorised.data["lines"][0]["salesman_name"] == ""
    revoke_session(proof.manager.session)
    delayed = APIRequestFactory().get("/api/sell/sales/issued")
    force_authenticate(delayed, user=proof.manager.user, token=proof.manager.session)
    denied = SaleDetailView.as_view()(delayed, doc_number=response.data["doc_number"])
    assert denied.status_code == 401 and "lines" not in denied.data


def test_staff_retired_after_cart_preparation_cannot_issue(online_goods: Any) -> None:
    proof = online_goods
    named = Staff.objects.create(tenant=proof.world.tenant, human_id=proof.warehouse.human_id, salesperson=True)
    command(proof.owner, "proof.named_staff", lambda run: open_assignment(
        run, named, proof.world.sites[0].pk, timezone.now() - timedelta(days=1)))
    wire, _ = bill(proof)
    wire["lines"][0]["salesperson"] = str(named.pk)
    Staff.objects.filter(pk=named.pk).update(retired_at=timezone.now())
    refused = post(OnlineFinaliseView, proof, wire)
    assert refused.status_code == 409 and refused.data["code"] == "SALESPERSON_STALE", refused.data
    assert not Sale.objects.exists() and not CashLedgerEntry.objects.exists()
    assert sum(read_shelf(proof.world.sites[0]).quantities.values()) == 3


def test_alpha_variance_requires_recorded_independent_approval_without_count(online_goods: Any) -> None:
    proof = online_goods
    site = proof.world.sites[0]
    StoreFeatureSwitch.objects.create(tenant=proof.world.tenant, site=site, feature_key="cash-count",
                                      enabled=True, updated_at=timezone.now())
    variance = {"id": str(uuid.uuid4()), "notes": {"500": 0, "200": 0, "100": 1, "50": 0, "20": 0, "10": 0}, "coins_paise": 0, "opening_paise": 0,
                "expected_paise": 0, "approved_by": proof.owner.user.pk, "manager_pin": "1234"}
    denied = post(CashCountsView, proof, variance)
    assert denied.status_code == 409 and denied.data["code"] == "INDEPENDENT_APPROVAL_REQUIRED", denied.data
    assert not CashCount.objects.exists() and not CashCountBill.objects.exists()


def test_cash_close_session_expiry_after_actual_count_rolls_back(online_goods: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from accounts.goods_models import ServerSession
    from sell import cash_views
    proof = online_goods
    site = proof.world.sites[0]
    StoreFeatureSwitch.objects.create(tenant=proof.world.tenant, site=site, feature_key="cash-count",
                                      enabled=True, updated_at=timezone.now())
    actual = record_count
    def expire_after_count(*args: Any, **kwargs: Any) -> Any:
        saved = actual(*args, **kwargs)
        ServerSession.objects.filter(pk=proof.manager.session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        return saved
    monkeypatch.setattr(cash_views, "record_count", expire_after_count)
    count = {"id": str(uuid.uuid4()), "notes": {"500": 0, "200": 0, "100": 0, "50": 0, "20": 0, "10": 0},
             "coins_paise": 0, "opening_paise": 0, "expected_paise": 0}
    denied = post(CashCountsView, proof, count)
    assert denied.status_code == 401 and denied.data["code"] == "SESSION_EXPIRED", denied.data
    assert not CashCount.objects.exists() and not CashCountBill.objects.exists()


def test_live_readiness_refuses_unresolved_source_ownership_before_actual_issue(online_goods: Any) -> None:
    from masters.models import Brand
    from sell.services.postings import resolve_goods_cost_plan
    from stockledger.goods_engine import eligible_portions_for_skus
    proof = online_goods
    site = proof.world.sites[0]
    wire, _ = bill(proof)
    portion = eligible_portions_for_skus(site.pk, [proof.sku_id], purpose="sell")[0]
    brand = proof.world.brands[0]
    brand.ownership = Brand.Ownership.BRAND_OWNED
    brand.save(update_fields=["ownership"])
    actual_plan = resolve_goods_cost_plan(brand_id=brand.pk, unit_cost_paise=int(portion.unit_cost or 0))
    assert not actual_plan.postable
    checks = selling_checks(site, timezone.now())
    gate = next(check for check in checks if check["key"] == "source_costing")
    assert gate["passed"] is False and gate["required"] is True and gate["overridable"] is False
    assert "cost" not in gate and "amount" not in gate
    before = JournalBatch.objects.count()
    # The readiness screen flags the whole store; the issue path refuses the
    # piece on this bill with the sale writer's own costing decision.
    refused = post(OnlineFinaliseView, proof, wire)
    assert refused.status_code == 409 and refused.data["code"] == "SOURCE_COSTING_REQUIRED", refused.data
    assert not Sale.objects.exists() and not CashLedgerEntry.objects.exists() and not GLEntry.objects.exists()
    assert JournalBatch.objects.count() == before and sum(read_shelf(site).quantities.values()) == 3


def test_issue_reads_only_its_pieces_and_reuses_an_unchanged_commercial_revision(online_goods: Any) -> None:
    from sell.services.online import commercial_marks, current_commercial_revision
    proof = online_goods
    site = proof.world.sites[0]
    full = read_shelf(site)
    narrow = read_shelf(site, barcodes=[proof.barcode, "NOT-A-CODE"])
    assert narrow.pieces == [piece for piece in full.pieces if piece.barcode == proof.barcode]
    assert narrow.quantities == {key: qty for key, qty in full.quantities.items() if key[0] == proof.barcode}
    assert current_commercial_revision(site) == build_dataset(site, "")["commercial_revision"]
    marks = commercial_marks(site)
    wire, _ = bill(proof)
    assert post(OnlineFinaliseView, proof, wire).status_code == 201
    # A sale changes no commercial input, so the next bill reuses the revision.
    assert commercial_marks(site) == marks
    assert current_commercial_revision(site) == build_dataset(site, "")["commercial_revision"]
    _publish_tenant_config(proof.world.tenant, proof.owner.human_id, kind="sell_policy",
        payload={"manual_discount_cap_percent": "5", "manual_discount_on_offer_lines": False,
                 "return_window_days": 15}, label="online-proof-policy-change")
    assert commercial_marks(site) != marks
    assert current_commercial_revision(site) == build_dataset(site, "")["commercial_revision"]
