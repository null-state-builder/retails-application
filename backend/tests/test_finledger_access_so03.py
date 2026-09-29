"""SO-03 denial checks for legacy Money books without tenant columns."""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_models import HumanIdentity, RoleAssignment, ServerSession
from accounts.models import Role, User
from accounts.sessions import issue_session
from core.gl import GLAccount, GLEntry
from core.tenancy import tenant_context
from files.models import StoredFile
from finledger import access as money_access
from finledger.models import (
    BankStatementImport,
    BankStatementLine,
    CashLedgerEntry,
    PartnerLedgerEntry,
    VendorLedgerEntry,
)
from finledger.views import CashMovementView, VendorBalancesView, _line_dict, _visible_candidate_ids
from finledger.health import BooksHealthView
from masters.goods_models import Tenant
from masters.models import Gstin, LegalEntity, Store
from outbound.views import PartnerSettlementsView
from stockledger.models import StockOnHand
from vendors.models import Vendor


def test_money_book_authority_needs_manage_financial_and_global_scope_on_one_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    human_id = uuid.uuid4()
    money_manage = SimpleNamespace(
        code="owner", is_active=True, permissions_map={},
        section_access={"money": {"capability": "manage"}},
        field_access=[],
    )
    financial = SimpleNamespace(
        code="accounts", is_active=True, permissions_map={},
        section_access={"money": {"capability": "view"}},
        field_access=["financial"],
    )
    rows = [
        SimpleNamespace(role=money_manage, all_sites=True, all_brands=True),
        SimpleNamespace(role=financial, all_sites=True, all_brands=True),
    ]
    monkeypatch.setattr(money_access, "require_tenant_id", lambda: tenant_id)
    monkeypatch.setattr(money_access, "database_now", timezone.now)
    monkeypatch.setattr(money_access, "effective_assignments", lambda _human, _at: rows)
    user = SimpleNamespace(
        is_authenticated=True, is_active=True, tenant_id=tenant_id, human_id=human_id
    )
    assert not money_access.keeps_books(user)
    rows[0].role.field_access = ["financial"]
    rows[0].all_sites = False
    assert not money_access.keeps_books(user)
    rows[0].all_sites = True
    assert money_access.keeps_books(user)
    user.tenant_id = uuid.uuid4()
    assert not money_access.keeps_books(user)


def _tenant(label: str) -> tuple[Tenant, User, Vendor, Store]:
    tenant = Tenant.objects.create(
        code=f"money-{label}-{uuid.uuid4().hex[:6]}",
        name=f"Money {label}", deployment_key=uuid.uuid4(), timezone="Asia/Kolkata",
        currency="INR", locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        user = User.objects.create(username=f"money-{label}-{uuid.uuid4().hex[:6]}", tenant=tenant)
        vendor = Vendor.objects.create(tenant=tenant, code=f"v-{label}", name=f"Vendor {label}")
        entity = LegalEntity.objects.create(tenant=tenant, code=f"e-{label}", name=f"Entity {label}")
        gstin = Gstin.objects.create(
            tenant=tenant, legal_entity=entity,
            gstin=f"10{uuid.uuid4().int % 10**13:013d}", state_code="10", state_name="Bihar",
        )
        store = Store.objects.create(
            tenant=tenant, gstin=gstin, code=f"s-{label}", name=f"Store {label}", is_partner=True,
        )
    return tenant, user, vendor, store


def _books_session(tenant: Tenant, user: User) -> ServerSession:
    human = HumanIdentity.objects.create(
        tenant=tenant, staff_code=f"BOOKS-{uuid.uuid4().hex[:8]}", display_name="Bookkeeper",
    )
    user.human = human
    user.save(update_fields=["human"])
    role = Role.objects.create(
        tenant=tenant, code="accounts", name="Accounts",
        section_access={"money": {"capability": "manage"}},
        field_access=["financial"],
    )
    RoleAssignment.objects.create(
        tenant=tenant, human=human, role=role, all_sites=True, all_brands=True,
        effective_from=timezone.now() - timedelta(days=1),
    )
    return cast(ServerSession, issue_session(user).session)


@pytest.mark.django_db
def test_money_book_queries_exclude_foreign_and_unowned_rows() -> None:
    first, user_a, vendor_a, store_a = _tenant("a")
    second, user_b, vendor_b, store_b = _tenant("b")
    with tenant_context(first.pk):
        own_vendor = VendorLedgerEntry.objects.create(
            vendor=vendor_a, amount=100, kind="bill", doc_number="V-A", posted_by=user_a,
        )
        own_cash = CashLedgerEntry.objects.create(
            account="BANK", amount=100, kind="receipt", doc_number="C-A", posted_by=user_a,
        )
        orphan_cash = CashLedgerEntry.objects.create(
            account="BANK", amount=200, kind="receipt", doc_number="C-ORPHAN",
        )
        own_partner = PartnerLedgerEntry.objects.create(
            store=store_a, amount=100, kind="payment", doc_number="P-A", posted_by=user_a,
        )
        own_file = StoredFile.objects.create(
            kind=StoredFile.Kind.BANK_STATEMENT, filename="a.csv", content_type="text/csv",
            content=b"", uploaded_by=user_a,
        )
        own_batch = BankStatementImport.objects.create(file=own_file, uploaded_by=user_a)
    with tenant_context(second.pk):
        foreign_vendor = VendorLedgerEntry.objects.create(
            vendor=vendor_b, amount=300, kind="bill", doc_number="V-B", posted_by=user_b,
        )
        foreign_cash = CashLedgerEntry.objects.create(
            account="BANK", amount=300, kind="receipt", doc_number="C-B", posted_by=user_b,
        )
        foreign_partner = PartnerLedgerEntry.objects.create(
            store=store_b, amount=300, kind="payment", doc_number="P-B", posted_by=user_b,
        )
        foreign_file = StoredFile.objects.create(
            kind=StoredFile.Kind.BANK_STATEMENT, filename="b.csv", content_type="text/csv",
            content=b"", uploaded_by=user_b,
        )
        foreign_batch = BankStatementImport.objects.create(file=foreign_file, uploaded_by=user_b)
    with tenant_context(first.pk):
        own_line = BankStatementLine.objects.create(
            import_batch=own_batch, txn_date="2026-09-29", candidates=[
                {"entry_id": own_cash.pk, "doc_number": "C-A"},
                {"entry_id": foreign_cash.pk, "doc_number": "C-B"},
            ],
        )
        foreign_line = BankStatementLine.objects.create(
            import_batch=foreign_batch, txn_date="2026-09-29",
        )
        assert set(money_access.vendor_entries().values_list("pk", flat=True)) == {own_vendor.pk}
        assert set(money_access.cash_entries().values_list("pk", flat=True)) == {own_cash.pk}
        assert orphan_cash.pk not in money_access.cash_entries().values_list("pk", flat=True)
        assert set(money_access.partner_entries().values_list("pk", flat=True)) == {own_partner.pk}
        assert set(money_access.bank_imports().values_list("pk", flat=True)) == {own_batch.pk}
        assert set(money_access.bank_lines().values_list("pk", flat=True)) == {own_line.pk}
        visible = _visible_candidate_ids([own_line])
        assert visible == {own_cash.pk}
        assert _line_dict(own_line, visible)["candidates"] == [
            {"entry_id": own_cash.pk, "doc_number": "C-A"}
        ]
        request = APIRequestFactory().get("/api/finledger/vendor/balances")
        force_authenticate(request, user=user_a)
        response = VendorBalancesView.as_view()(request)
        assert response.status_code == 403  # a login alone grants no Money authority
    assert foreign_vendor.pk != own_vendor.pk
    assert foreign_partner.pk != own_partner.pk
    assert foreign_line.pk != own_line.pk


@pytest.mark.django_db
def test_books_health_uses_only_tenant_owned_gl_and_stock() -> None:
    first, user_a, _vendor_a, store_a = _tenant("health-a")
    second, user_b, _vendor_b, store_b = _tenant("health-b")
    with tenant_context(first.pk):
        human = HumanIdentity.objects.create(
            tenant=first, staff_code="HEALTH-A", display_name="Health A",
        )
        user_a.human = human
        user_a.save(update_fields=["human"])
        role = Role.objects.create(
            tenant=first, code="accounts", name="Accounts",
            section_access={"money": {"capability": "manage"}},
            field_access=["financial"],
        )
        RoleAssignment.objects.create(
            tenant=first, human=human, role=role, all_sites=True, all_brands=True,
            effective_from=timezone.now() - timedelta(days=1),
        )
        GLEntry.objects.create(
            account=GLAccount.CASH, doc_type="TEST", doc_number="HA", line_no=1,
            amount=100, posted_by=user_a,
        )
        GLEntry.objects.create(
            account=GLAccount.VENDOR_PAYABLE, doc_type="TEST", doc_number="HA", line_no=2,
            amount=-100, posted_by=user_a,
        )
        StockOnHand.objects.create(
            store=store_a, sku_code="A-STRANDED", net_qty=0, net_value_paise=25,
        )
    with tenant_context(second.pk):
        GLEntry.objects.create(
            account=GLAccount.CASH, doc_type="TEST", doc_number="HB", line_no=1,
            amount=900, posted_by=user_b,
        )
        StockOnHand.objects.create(
            store=store_b, sku_code="B-STRANDED", net_qty=0, net_value_paise=900,
        )
        GLEntry.objects.create(
            account=GLAccount.CASH, doc_type="TEST", doc_number="ORPHAN", line_no=1,
            amount=700,
        )
    with tenant_context(first.pk):
        request = APIRequestFactory().get("/api/finledger/health")
        force_authenticate(request, user=user_a)
        response = BooksHealthView.as_view()(request)
        assert response.status_code == 200
        assert response.data["leg_count"] == 2
        assert response.data["voucher_count"] == 1
        assert response.data["trial_balance_paise"] == 0
        assert response.data["stranded_stock_value"]["row_count"] == 1
        assert response.data["stranded_stock_value"]["value_paise"] == 25


@pytest.mark.django_db
def test_money_write_rolls_back_if_session_changes_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, user, _vendor, _store = _tenant("revocation")
    with tenant_context(tenant.pk):
        session = _books_session(tenant, user)

        def post_then_revoke(*_args: object, **_kwargs: object) -> CashLedgerEntry:
            entry = CashLedgerEntry.objects.create(
                account="CASH", amount=100, kind="receipt", doc_number="REVOKED",
                posted_by=user,
            )
            ServerSession.objects.filter(pk=session.pk).update(revoked_at=timezone.now())
            return entry

        monkeypatch.setattr("finledger.views.post_cash_movement", post_then_revoke)
        request = APIRequestFactory().post(
            "/api/finledger/cash/movement",
            {"direction": "in", "amount": "1.00"}, format="json",
        )
        force_authenticate(request, user=user, token=cast(Any, session))
        response = CashMovementView.as_view()(request)

        assert response.status_code == 401
        assert response.data["code"] == "AUTH_REQUIRED"
        assert not CashLedgerEntry.objects.filter(doc_number="REVOKED").exists()


@pytest.mark.django_db
def test_partner_write_rolls_back_if_assignment_changes_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, user, _vendor, store = _tenant("partner-rv")
    with tenant_context(tenant.pk):
        session = _books_session(tenant, user)

        def post_then_revoke(*_args: object, **_kwargs: object) -> PartnerLedgerEntry:
            entry = PartnerLedgerEntry.objects.create(
                store=store, amount=100, kind="payment", doc_number="P-REVOKED",
                posted_by=user,
            )
            RoleAssignment.objects.filter(human_id=user.human_id).update(
                effective_to=timezone.now() - timedelta(seconds=1)
            )
            return entry

        monkeypatch.setattr("outbound.views.post_partner_settlement", post_then_revoke)
        request = APIRequestFactory().post(
            "/api/outbound/partner-settlements",
            {"store_id": store.pk, "amount": "1.00"}, format="json",
        )
        force_authenticate(request, user=user, token=cast(Any, session))
        response = PartnerSettlementsView.as_view()(request)

        assert response.status_code == 403
        assert response.data["code"] == "ACTION_DENIED"
        assert not PartnerLedgerEntry.objects.filter(doc_number="P-REVOKED").exists()
