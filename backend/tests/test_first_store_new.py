"""Jointly declared new-store acceptance on an empty disposable ledger.

Registration and readiness use their actual services/APIs. Approved commercial
configuration is a canonical synthetic fixture, never real-shop CA approval.
No receipt, opening posting or stock projection is seeded for an empty store.
"""

from __future__ import annotations

import copy
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import UserSettingsHolder
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_admin_services import open_assignment
from accounts.goods_models import Staff
from accounts.models import Role, User
from accounts.registration_models import InstallationRegistration
from accounts.registration_services import confirm_registration, stage_registration
from accounts.sessions import issue_session
from accounts.views import ChangePasswordView
from core.canonical import sha256_hex
from core.fiscal import financial_year
from core.gl import GLEntry
from core.numbering import prepare_series
from core.refusals import Refusal
from core.tenancy import tenant_context
from files.goods_services import stage_upload
from finledger.models import CashLedgerEntry
from masters.document_series_models import DocumentPrefix, NumberingSetting
from masters.first_store_readiness import selling_checks
from masters.first_store_views import FirstStoreCounterView
from masters.goods_config import withdraw as withdraw_config
from masters.goods_models import ConfigVersion, SiteCapabilityEvent, SiteGuard, Tenant
from masters.goods_services import compute_readiness_checks
from masters.goods_views import GoodsSiteReadinessView
from masters.models import Brand, Store
from masters.store_feature_models import StoreFeatureSwitch
from masters.tax_setting_models import TaxSettingVersion
from ptmapper import soh_services
from ptmapper.soh_models import SohImport
from ptmapper.soh_parser import parse_soh
from sell.models import OnlineSaleSubmission, RegisteredTill, Sale, SaleLine, SaleTender
from sell.serializers import SaleWriteSerializer
from sell.services.dataset import build_dataset
from sell.services.goods_stock import read_shelf
from sell.services.invoice_numbers import till_numbering
from sell.services.tax_rulebook import StoreTaxBooks
from sell.services.till_authority import renew_authority
from sell.views import OnlineFinaliseView
from stockledger.goods_models import CustodyLot, JournalBatch, Origin, Position
from tests.first_store_goods import command, live_access, workbook
from tests.test_first_store_configuration import _business_profile, _version
from tests.test_installation_registration import credentials
from tests.test_installation_registration import allow_disposable_test_flush as allow_disposable_test_flush
from tests.test_installation_registration import proposal as registration_proposal  # noqa: F401 -- pytest registers the imported fixture
from tests.test_so03_denials import TenantWorld, _assign, _person

pytestmark = pytest.mark.django_db(transaction=True)


class _PrivateProposal(dict[str, Any]):
    def __repr__(self) -> str:
        return "<isolated first-store fixture; credentials redacted>"


@pytest.fixture
def proposal(registration_proposal: dict[str, Any]) -> dict[str, Any]:  # noqa: F811 -- fixture dependency uses its registered name
    return _PrivateProposal(registration_proposal)


@pytest.fixture(autouse=True)
def isolated_evidence(settings: UserSettingsHolder, tmp_path: Path) -> None:
    settings.KDPS_OFFBOX_ROOT = str(tmp_path / "write-once")


def _request(user: User, path: str, body: dict[str, Any]) -> Any:
    session = issue_session(user).session
    session.step_up_at = timezone.now()
    session.save(update_fields=["step_up_at"])
    request = APIRequestFactory().post(path, body, format="json")
    force_authenticate(request, user, session)
    return request


def _register(data: dict[str, Any], kind: str) -> Any:
    data = copy.deepcopy(data)
    data["store"].update(setup_kind=kind, source_system="" if kind == "new" else "Prior software")
    staged = stage_registration(data)
    assert staged["summary"]["store"]["setup_kind"] == kind
    first = confirm_registration(credentials(data, "owner", staged["summary_hash"]))
    assert first["confirmed"] == {"owner": True, "admin": False}
    assert not Tenant.objects.exists() and not Store.objects.exists()
    confirmed = confirm_registration(credentials(data, "admin", staged["summary_hash"]))
    claim = InstallationRegistration.objects.get()
    assert claim.completed_at is not None and claim.summary_hash == staged["summary_hash"]
    assert confirmed["summary"]["store"]["setup_kind"] == kind
    assert {entry["role"] for entry in claim.confirmation_history if entry["event"] == "confirmed"} == {"owner", "admin"}
    assert claim.tenant_id is not None and claim.first_store_id is not None
    tenant = Tenant.objects.get(pk=claim.tenant_id)
    with tenant_context(tenant.pk):
        site = Store.objects.get(pk=claim.first_store_id)
        initial_people = []
        for who in ("owner", "admin"):
            user = User.objects.get(email=data[who]["email"])
            response = ChangePasswordView.as_view()(_request(user, "/api/auth/change-password", _PrivateProposal({
                "current_password": data[who]["temporary_password"],
                "new_password": f"New-proof-{uuid.uuid4().hex}!",
            })))
            assert response.status_code == 200 and response.data["reauthentication_required"]
            user.refresh_from_db()
            assert not user.must_change_password
            initial_people.append(user)
        owner, admin = initial_people
        assert owner.human_id != admin.human_id
        brand = Brand.objects.create(tenant=tenant, code="NEW-PROOF", name="Disposable catalogue brand")
        world = TenantWorld(tenant, (site,), (brand,), {role.code: role for role in Role.objects.all()})
        manager_user, manager_human = _person(world, "empty-store-manager")
        _assign(world, manager_human, "store_person", sites=(site,), all_brands=True)
        return SimpleNamespace(world=world, site=site, claim=claim, owner=live_access(owner),
                               admin=live_access(admin), manager=live_access(manager_user))


def _approve(proof: Any, action: str) -> Any:
    guard = SiteGuard.objects.get(site=proof.site)
    return GoodsSiteReadinessView.as_view()(_request(proof.owner.user, "/readiness", {
        "command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
        "expected_revision": guard.revision, "action": action, "reason_code": "EMPTY-PROOF",
        "residual_decisions": [],
    }), pk=proof.site.pk)


def _configured_empty(data: dict[str, Any], *, kind: str = "new", approve_sell: bool = True) -> Any:
    proof = _register(data, kind)
    world, site = proof.world, proof.site
    with tenant_context(world.tenant.pk):
        # Saved test tax rules are explicitly synthetic; the real-shop CA gate
        # and deployment restrictions remain independently enforced.
        world.tenant.synthetic = True
        world.tenant.save(update_fields=["synthetic"])
        human = proof.owner.user.human
        assert human is not None
        _version(world, human, "working_calendar", {
            "timezone": "Asia/Kolkata", "working_weekdays": [1, 2, 3, 4, 5, 6, 7], "excluded_dates": [],
        })
        business = _business_profile(world, human)
        world.tenant.business_profile_version = business
        world.tenant.save(update_fields=["business_profile_version"])
        rates = _version(world, human, "rates", {"transport_pct": "0", "pricing_margin_pct": "0"})
        tax = _version(world, human, "tax_rates", {
            "currency": "INR", "hsn_rules": [{"hsn": "6101", "effective_from": timezone.now().isoformat(),
                "slabs": [{"lower_paise": "0", "upper_paise": None, "lower_inclusive": True,
                           "upper_inclusive": False, "input_pct": "5", "output_pct": "5"}]}],
        })
        _version(world, human, "profile", {
            "family": "proof", "columns": [], "directions": ["both_supplied"], "allow_row_override": False,
            "rates_version_id": str(rates.pk), "tax_version_id": str(tax.pk),
            "opening_rules": {"season_required": True, "both_supplied": True},
        })
        _version(world, human, "approval", {
            "action": "pt.approve.receipt", "roles": ["owner"], "site_ids": [],
            "brand_ids": [], "require_distinct": True, "qty_max": 100, "value_max": "10000000",
            "step_up": True, "unknown_value": "refuse",
        })
        proof.policy = _version(world, human, "sell_policy", {
            "manual_discount_cap_percent": "10", "manual_discount_on_offer_lines": False,
            "return_window_days": 15,
        })
        for document_kind in ("GRN", "CGRN", "RPT"):
            prepare_series(world.tenant.pk, site.gstin.legal_entity, document_kind)
        proof.staff = Staff.objects.create(tenant=world.tenant, human_id=proof.manager.human_id, salesperson=True)
        command(proof.owner, "proof.empty.staff", lambda run: open_assignment(
            run, proof.staff, site.pk, run.now - timedelta(days=1),
        ))
        now = timezone.now()
        TaxSettingVersion.objects.create(tenant=world.tenant, version=2, applies_from=timezone.localdate(),
            rules=[{"kind": "flat_rate", "hsn_prefix": "6101", "name": "Synthetic proof tax", "rate": "5.00"}],
            unmatched_rate=Decimal("0"), note="Synthetic empty-store test, never real CA sign-off",
            actor_id=proof.owner.human_id)
        for key in ("tax-settings", "document-series"):
            StoreFeatureSwitch.objects.create(tenant=world.tenant, site=site, feature_key=key, enabled=True, updated_at=now)
        DocumentPrefix.objects.create(tenant=world.tenant, site=site, code="NEW", updated_at=now)
        day = timezone.localdate()
        NumberingSetting.objects.create(tenant=world.tenant,
            new_format_from=date(day.year if day.month >= 4 else day.year - 1, 4, 1), till_block_size=30, updated_at=now)
        guard = SiteGuard.objects.get(site=site)
        counter = FirstStoreCounterView.as_view()(_request(proof.owner.user, "/counter", {
            "command_id": str(uuid.uuid4()), "contract_version": "goods-v1", "expected_revision": guard.revision,
        }), pk=site.pk)
        assert counter.status_code == 201 and counter.data["token_issued"]
        proof.token = counter.data["device_token"]
        proof.till = RegisteredTill.objects.get(store=site)
        renew_authority(site)
        proof.numbering = till_numbering(site, proof.till, {})
        assert all(row["passed"] for row in compute_readiness_checks(site, timezone.now()))
        for action in ("approve_opening_setup", "approve_goods"):
            response = _approve(proof, action)
            assert response.status_code == 200, response.data
        if approve_sell:
            response = _approve(proof, "approve_sell")
            assert response.status_code == 200, response.data
    return proof


def _wire(proof: Any) -> dict[str, Any]:
    payload = build_dataset(proof.site, "")
    assert payload["items"] == []
    now = timezone.now()
    tax = StoreTaxBooks(proof.site).at(now)
    split = tax.line_tax("6101", 100000, 1).split
    block = next(row for row in proof.numbering.blocks if row["month"] == timezone.localdate().strftime("%Y-%m"))
    wire = {"idempotency_uuid": str(uuid.uuid4()), "store": proof.site.code, "fy": financial_year(),
        "till_seq": 1, "origin": "online", "billed_at": now.isoformat(),
        "commercial_revision": payload["commercial_revision"], "till_number": f"{proof.till.series_prefix}-1",
        "tax_setting_version": tax.version,
        "tax_invoice_number": f"{block['prefix']}/{block['fy']}/{block['first']}",
        "lines": [{"line_no": 1, "barcode": "NEVER-RECEIVED", "season": "NO-SOURCE", "qty": 1,
                   "mrp_paise": 100000, "disc_paise": 0, "net_paise": 100000,
                   "gst_rate": str(split.rate), "gst_paise": split.gst_paise,
                   "salesperson": str(proof.staff.pk)}],
        "tenders": [{"mode": "cash", "amount_paise": 100000}],
        "totals": {"gross_paise": 100000, "discount_paise": 0, "net_paise": 100000,
                   "gst_paise": split.gst_paise, "round_paise": 0}}
    parsed = SaleWriteSerializer(data=wire)
    assert parsed.is_valid(), parsed.errors
    return wire


def _issue(proof: Any, wire: dict[str, Any], *, token: str | None = None) -> Any:
    request = APIRequestFactory().post("/api/sell/sales/finalise-online", wire, format="json",
        HTTP_X_KDPS_DEVICE=proof.token if token is None else token)
    force_authenticate(request, proof.manager.user, proof.manager.session)
    return OnlineFinaliseView.as_view()(request)


def _no_business_effects() -> None:
    for model in (Position, CustodyLot, Origin, JournalBatch, Sale, SaleLine, SaleTender,
                  GLEntry, CashLedgerEntry):
        assert model.objects.count() == 0, model.__name__
    assert not OnlineSaleSubmission.objects.exclude(status="rejected").exists()


def test_jointly_declared_new_store_can_start_empty_but_cannot_issue_nonexistent_stock(
    proposal: dict[str, Any],
) -> None:
    proof = _configured_empty(proposal)
    with tenant_context(proof.world.tenant.pk):
        assert all(row["passed"] for row in selling_checks(proof.site, timezone.now()))
        assert not SohImport.objects.exists()
        assert read_shelf(proof.site).quantities == {}
        _no_business_effects()
        refused = _issue(proof, _wire(proof))
        assert refused.status_code == 422 and refused.data["code"] == "LINE_UNRESOLVED", refused.data
        assert refused.data["not_issued"] is True
        _no_business_effects()


def test_existing_declaration_requires_reconciliation_even_on_an_empty_ledger(
    proposal: dict[str, Any],
) -> None:
    proof = _configured_empty(proposal, kind="existing", approve_sell=False)
    with tenant_context(proof.world.tenant.pk):
        checks = selling_checks(proof.site, timezone.now())
        assert {row["key"] for row in checks if not row["passed"]} == {"opening_reconciled"}
        before = SiteCapabilityEvent.objects.count()
        response = _approve(proof, "approve_sell")
        assert response.status_code == 409 and response.data["code"] == "READINESS_UNOVERRIDABLE"
        guard = SiteGuard.objects.get(site=proof.site)
        assert not guard.sell_ready and SiteCapabilityEvent.objects.count() == before
        _no_business_effects()


def test_changing_existing_to_new_declaration_requires_both_people_to_reconfirm(
    proposal: dict[str, Any],
) -> None:
    staged = stage_registration(proposal)
    confirm_registration(credentials(proposal, "owner", staged["summary_hash"]))
    revised_input = copy.deepcopy(proposal)
    revised_input.update(command_id=uuid.uuid4(), current_owner_password=proposal["owner"]["temporary_password"])
    revised_input["store"].update(setup_kind="new", source_system="")
    revised = stage_registration(revised_input, edit=True)
    assert revised["summary_hash"] != staged["summary_hash"]
    with pytest.raises(Refusal) as stale:
        confirm_registration(credentials(proposal, "admin", staged["summary_hash"]))
    assert stale.value.code == "REVISION_SUPERSEDED"
    first = confirm_registration(credentials(revised_input, "admin", revised["summary_hash"]))
    assert first["confirmed"] == {"owner": False, "admin": True} and not Tenant.objects.exists()
    complete = confirm_registration(credentials(revised_input, "owner", revised["summary_hash"]))
    assert complete["state"] == "registered" and complete["summary"]["store"]["setup_kind"] == "new"


def test_later_source_upload_requires_review_and_independent_withdrawal_preserves_empty_stock(
    proposal: dict[str, Any],
) -> None:
    proof = _configured_empty(proposal)
    with tenant_context(proof.world.tenant.pk):
        wire = _wire(proof)
        raw = workbook()
        metadata, rows = parse_soh(raw)
        evidence = stage_upload(proof.manager.principal(), command_id=uuid.uuid4(), data=raw,
            filename="disposable-new-store-source.xlsx", kind="manifest",
            scope={"scope_kind": "sites", "site_ids": [proof.site.pk], "brand_ids": [],
                   "sensitive_fields": ["cost", "financial"]},
            expected_sha256=sha256_hex(raw), contains_fields=["cost", "financial"])
        source = command(proof.manager, soh_services.STAGE, lambda run: soh_services.create_source(
            run, site_id=proof.site.pk, evidence=evidence, metadata=metadata, parsed=rows,
        ))
        checks = selling_checks(proof.site, timezone.now())
        assert {row["key"] for row in checks if not row["passed"]} == {"opening_reconciled"}
        refused = _issue(proof, wire)
        assert refused.status_code == 409 and refused.data["code"] == "SELL_NOT_READY"
        with pytest.raises(Refusal) as same_person:
            command(proof.manager, soh_services.APPROVE, lambda run: soh_services.withdraw(
                run, proof.manager, source, "Abandon unconfirmed opening source", source.revision,
            ))
        assert same_person.value.code in {"SELF_APPROVAL", "ACTION_DENIED", "NOT_FOUND"}
        source.refresh_from_db()
        assert source.state == "uploaded"
        command(proof.owner, soh_services.APPROVE, lambda run: soh_services.withdraw(
            run, proof.owner, source, "Independent review: store remains new and empty", source.revision,
        ))
        source.refresh_from_db()
        assert source.state == "withdrawn" and source.rows.count() == 1
        assert all(row["passed"] for row in selling_checks(proof.site, timezone.now()))
        _no_business_effects()


@pytest.mark.parametrize("gap", ["calendar", "tax", "real_tax_gate", "device_scope"])
def test_empty_start_never_bypasses_current_configuration_or_device_scope(
    proposal: dict[str, Any], gap: str,
) -> None:
    proof = _configured_empty(proposal)
    with tenant_context(proof.world.tenant.pk):
        wire = _wire(proof)
        token = None
        expected_key = None
        if gap == "calendar":
            calendar = ConfigVersion.objects.get(kind="working_calendar")
            command(proof.owner, "proof.empty.calendar", lambda run: withdraw_config(
                run, calendar, reason_code="PROOF-WITHDRAWAL",
            ))
            expected_key = "current_setup"
        elif gap == "tax":
            StoreFeatureSwitch.objects.filter(site=proof.site, feature_key="tax-settings").update(enabled=False)
            expected_key = "tax_configuration"
        elif gap == "real_tax_gate":
            proof.world.tenant.synthetic = False
            proof.world.tenant.save(update_fields=["synthetic"])
            expected_key = "tax_configuration"
        else:
            other = Store.objects.create(tenant=proof.world.tenant, gstin=proof.site.gstin,
                                         code="OTHER", name="Other proof shop")
            guard = SiteGuard.objects.create(tenant=proof.world.tenant, site=other,
                                            selling_mode="online_alpha")
            counter = FirstStoreCounterView.as_view()(_request(proof.owner.user, "/other-counter", {
                "command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
                "expected_revision": guard.revision,
            }), pk=other.pk)
            assert counter.status_code == 201 and counter.data["token_issued"]
            token = counter.data["device_token"]
        if expected_key:
            failed = {row["key"] for row in selling_checks(proof.site, timezone.now()) if not row["passed"]}
            assert expected_key in failed
        refused = _issue(proof, wire, token=token)
        assert (refused.status_code, refused.data["code"]) == (
            (403, "DEVICE_REQUIRED") if gap == "device_scope" else (409, "SELL_NOT_READY")
        ), refused.data
        _no_business_effects()
