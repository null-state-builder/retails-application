"""A platform superuser has no implicit tenant business authority (SO-03)."""

from __future__ import annotations

import uuid

import pytest

from accounts.goods_models import HumanIdentity
from accounts.models import Role, ScopeType, User
from approvals.services import holds_approver_role
from core.tenancy import tenant_context
from finledger.payables import may_read as may_read_payables
from inbound.debit_notes import may_read as may_read_debit_notes
from masters.goods_document_series_views import may_change_numbering
from masters.goods_store_feature_views import may_change_switches
from masters.goods_tax_settings_views import may_change_tax_settings
from masters.goods_models import Tenant
from outbound.count_schedule_views import may_read as may_read_count_schedule
from reporting.staff_report import sees_team
from sell.services.customer_rights import may_read as may_read_customers
from sell.services.petty_cash import may_read as may_read_petty_cash
from storefront.checklists import may_read_templates
from vendors.open_to_buy import may_set as may_set_budget


@pytest.mark.django_db
def test_superuser_without_assignment_cannot_use_business_gates() -> None:
    tenant = Tenant.objects.create(
        code=f"platform-{uuid.uuid4().hex[:8]}",
        name="Platform denial proof",
        deployment_key=uuid.uuid4(),
        timezone="Asia/Kolkata",
        currency="INR",
        locale="en-IN",
        synthetic=True,
    )
    with tenant_context(tenant.pk):
        human = HumanIdentity.objects.create(tenant=tenant, staff_code="PLATFORM", display_name="Support")
        role = Role.objects.create(
            tenant=tenant,
            code="owner",
            name="Owner",
            section_access={
                section: {"capability": "manage"}
                for section in ("money", "sell", "setup", "stock_count")
            },
        )
        user = User.objects.create(
            username=f"platform-{uuid.uuid4().hex[:8]}",
            tenant=tenant,
            human=human,
            role=role,
            scope_type=ScopeType.ALL,
            is_superuser=True,
            is_staff=True,
        )

        assert not holds_approver_role(user, ["owner"])
        assert not may_read_debit_notes(user)
        assert not may_read_payables(user)
        assert not may_read_petty_cash(user)
        assert not may_read_customers(user)
        assert not may_read_count_schedule(user)
        assert not may_read_templates(user)
        assert not sees_team(user)
        assert not may_change_numbering(user)
        assert not may_change_tax_settings(user)
        assert not may_change_switches(user, 1)
        assert not may_set_budget(user, site_id=1, brand_id=1)
