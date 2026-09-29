"""SO-03 denial matrix for the unified role-assignment authority.

Each assertion names a PRD rule: tenant wall, indivisible scope/field grants,
explicit all membership, effective periods, protected fields and revocation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.request import Request

from accounts.goods_models import HumanIdentity, RoleAssignment, SecurityGuard, ServerSession
from accounts.models import Role, User
from accounts.principal import AccessContext, effective_grants, resolve_access
from accounts.rbac_matrix import section_access_for
from accounts.role_assignments import INITIAL_FIELD_ACCESS, effective_assignments
from accounts.sessions import bump_security_epoch
from accounts.unified_policy import initial_step_actions, role_actions, role_fields
from core.commands import CommandRun, Principal
from core.refusals import Refusal
from core.tenancy import tenant_context
from masters.goods_models import Tenant
from masters.models import Brand, Gstin, LegalEntity, Season, Store


@dataclass
class TenantWorld:
    tenant: Tenant
    sites: tuple[Store, ...]
    brands: tuple[Brand, ...]
    roles: dict[str, Role]


def _tenant_world(label: str) -> TenantWorld:
    tenant = Tenant.objects.create(
        code=f"so03-{label}-{uuid.uuid4().hex[:6]}",
        name=f"SO-03 {label}", deployment_key=uuid.uuid4(),
        timezone="Asia/Kolkata", currency="INR", locale="en-IN", synthetic=True,
    )
    with tenant_context(tenant.pk):
        entity = LegalEntity.objects.create(
            tenant=tenant, code=f"ent-{label}", name=f"Entity {label}",
            pan=f"ABC{label.upper()}1234F"[:10],
        )
        gstin = Gstin.objects.create(
            tenant=tenant, legal_entity=entity,
            gstin=f"10{uuid.uuid4().int % 10**13:013d}",
            state_code="10", state_name="Bihar",
        )
        sites = tuple(
            Store.objects.create(
                tenant=tenant, gstin=gstin, code=f"{label}-s{index}",
                name=f"{label} Site {index}",
            )
            for index in (1, 2)
        )
        brands = tuple(
            Brand.objects.create(tenant=tenant, code=f"{label}-b{index}", name=f"{label} Brand {index}")
            for index in (1, 2)
        )
        roles = {
            code: Role.objects.create(
                tenant=tenant, code=code, name=code,
                section_access=section_access_for(code),
                field_access=INITIAL_FIELD_ACCESS[code],
                permissions_map={
                    "step_actions": initial_step_actions(code)
                },
            )
            for code in INITIAL_FIELD_ACCESS
        }
    return TenantWorld(tenant, sites, brands, roles)


@pytest.fixture
def worlds(db: None) -> tuple[TenantWorld, TenantWorld]:
    return _tenant_world("a"), _tenant_world("b")


def _person(world: TenantWorld, label: str) -> tuple[User, HumanIdentity]:
    human = HumanIdentity.objects.create(
        tenant=world.tenant, staff_code=f"{label}-{uuid.uuid4().hex[:6]}",
        display_name=label,
    )
    user = User.objects.create(
        username=f"{label}-{uuid.uuid4().hex[:8]}",
        tenant=world.tenant, human=human, full_name=label,
        # Legacy role deliberately disagrees with the assignments. It must
        # never add authority to the unified evaluator.
        role=world.roles["it_admin"],
    )
    return user, human


def _assign(
    world: TenantWorld,
    human: HumanIdentity,
    role_code: str,
    *,
    all_sites: bool = False,
    sites: tuple[Store, ...] = (),
    all_brands: bool = False,
    brands: tuple[Brand, ...] = (),
    starts: timedelta = timedelta(days=-1),
    ends: timedelta | None = None,
) -> RoleAssignment:
    moment = timezone.now()
    return RoleAssignment.objects.create(
        tenant=world.tenant, human=human, role=world.roles[role_code],
        all_sites=all_sites, site_ids=[site.pk for site in sites],
        all_brands=all_brands, brand_ids=[brand.pk for brand in brands],
        effective_from=moment + starts,
        effective_to=moment + ends if ends is not None else None,
    )


def _access(user: User, human: HumanIdentity) -> AccessContext:
    assert user.tenant_id is not None
    return AccessContext(
        user=user, human_id=human.pk, tenant_id=user.tenant_id,
        session=None, grants=effective_grants(human.pk),
    )


def test_till_scope_and_pin_list_use_current_store_person_assignment(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    """SO-03: the offline dataset and PIN hashes cannot follow legacy user scope."""
    from accounts.till_pin import may_hold_till_pin
    from sell.services.dataset import _managers, resolve_till_store

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "counter")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        assert resolve_till_store(user).pk == world.sites[0].pk
        assert not may_hold_till_pin(user, site_id=world.sites[0].pk)
        role = world.roles["store_person"]
        policy = dict(role.section_access)
        policy["sell"] = {"capability": "approve", "label": "Store manager"}
        role.section_access = policy
        role.save(update_fields=["section_access"])
        user.till_pin_hash = "pbkdf2_sha256$1$salt$hash"
        user.save(update_fields=["till_pin_hash"])
        assert may_hold_till_pin(user, site_id=world.sites[0].pk)
        assert not may_hold_till_pin(user, site_id=world.sites[1].pk)
        assert [item["user_id"] for item in _managers(world.sites[0])] == [user.pk]
        assert _managers(world.sites[1]) == []

        _assign(world, human, "store_person", sites=(world.sites[1],), all_brands=True)
        from sell.services.dataset import TillScopeError

        with pytest.raises(TillScopeError):
            resolve_till_store(user)


def test_historical_blob_download_requires_owner_scope_and_protected_field(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    """SO-03: an integer file ID alone cannot reveal a booking receipt."""
    from files.models import StoredFile
    from files.views import _readable
    from vendors.models import Booking, Vendor

    world, other = worlds
    with tenant_context(world.tenant.pk):
        owner, owner_human = _person(world, "file-owner")
        cashier, cashier_human = _person(world, "file-cashier")
        _assign(world, owner_human, "owner", sites=(world.sites[0],), all_brands=True)
        _assign(world, cashier_human, "store_person", sites=(world.sites[0],), all_brands=True)
        blob = StoredFile.objects.create(
            filename="booking.pdf", content_type="application/pdf", content=b"%PDF-proof",
            kind=StoredFile.Kind.BOOKING_RECEIPT, uploaded_by=owner,
        )
        vendor = Vendor.objects.create(tenant=world.tenant, code="blob-vendor", name="Blob Vendor")
        season = Season.objects.create(code=f"S{uuid.uuid4().hex[:7]}", name="Proof")
        Booking.objects.create(
            number=f"B-{uuid.uuid4().hex[:8]}", vendor=vendor, brand=world.brands[0],
            season=season, destination_store=world.sites[0], source_file=blob,
        )
        assert _readable(_access(owner, owner_human), blob)
        assert not _readable(_access(cashier, cashier_human), blob)
        owner_human.active = False
        owner_human.save(update_fields=["active"])
        assert not _readable(_access(owner, owner_human), blob)
    with tenant_context(other.tenant.pk):
        outsider, outsider_human = _person(other, "file-outsider")
        _assign(other, outsider_human, "owner", all_sites=True, all_brands=True)
        assert not _readable(_access(outsider, outsider_human), blob)


def test_legacy_booking_requires_every_scope_cell_and_projects_cost(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    """SO-03: a brand or old User.role cannot widen a booking or reveal its cost."""
    from vendors.models import Booking, BookingLine, Vendor
    from vendors.scoping import scope_bookings
    from vendors.serializers import BookingSerializer

    world, other = worlds
    with tenant_context(world.tenant.pk):
        cashier, cashier_human = _person(world, "booking-cashier")
        buyer, buyer_human = _person(world, "booking-buyer")
        owner, owner_human = _person(world, "booking-owner")
        _assign(world, cashier_human, "store_person", sites=(world.sites[0],), brands=(world.brands[0],))
        _assign(world, buyer_human, "brand_manager", all_sites=True, brands=(world.brands[0],))
        _assign(world, owner_human, "owner", sites=(world.sites[0],), brands=(world.brands[0],))
        vendor = Vendor.objects.create(tenant=world.tenant, code="booking-vendor", name="Vendor")
        season = Season.objects.create(code=f"S{uuid.uuid4().hex[:7]}", name="Proof season")
        booking = Booking.objects.create(
            number=f"BK-{uuid.uuid4().hex[:8]}", vendor=vendor, brand=world.brands[0],
            season=season, destination_store=world.sites[0], created_by=cashier,
        )
        BookingLine.objects.create(
            booking=booking, store=world.sites[0], style_code="A", size="M",
            booked_qty=1, cost_paise=300, mrp_paise=700,
        )
        assert list(scope_bookings(Booking.objects.all(), cashier, minimum="view")) == [booking]
        blind = BookingSerializer(booking, context={"request": SimpleNamespace(user=cashier)}).data
        assert "cost_total_paise" not in blind
        assert "cost_paise" not in blind["lines"][0]
        costed = BookingSerializer(booking, context={"request": SimpleNamespace(user=buyer)}).data
        assert costed["cost_total_paise"] == 300

        BookingLine.objects.create(
            booking=booking, store=world.sites[1], style_code="B", size="L",
            booked_qty=1, cost_paise=400, mrp_paise=800,
        )
        assert not scope_bookings(Booking.objects.all(), cashier, minimum="view").exists()
        assert not scope_bookings(Booking.objects.all(), owner, minimum="view").exists()
        assert list(scope_bookings(Booking.objects.all(), buyer, minimum="view")) == [booking]
        _assign(world, owner_human, "owner", sites=(world.sites[1],), brands=(world.brands[0],))
        assert list(scope_bookings(Booking.objects.all(), owner, minimum="view")) == [booking]
        assert BookingSerializer(booking, context={"request": SimpleNamespace(user=owner)}).data[
            "cost_total_paise"
        ] == 700
        other_brand = Booking.objects.create(
            number=f"BK-{uuid.uuid4().hex[:8]}", vendor=vendor, brand=world.brands[1],
            season=season, destination_store=world.sites[0], created_by=cashier,
        )
        BookingLine.objects.create(
            booking=other_brand, store=world.sites[0], style_code="C", size="M",
            booked_qty=1, cost_paise=500,
        )
        assert not scope_bookings(Booking.objects.filter(pk=other_brand.pk), buyer, minimum="view").exists()

    with tenant_context(other.tenant.pk):
        outsider, outsider_human = _person(other, "booking-outsider")
        _assign(other, outsider_human, "owner", all_sites=True, all_brands=True)
        assert not scope_bookings(Booking.objects.filter(pk=booking.pk), outsider, minimum="view").exists()


def test_outbound_nested_cost_and_transfer_pt_require_every_scope_cell(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    from accounts.sessions import issue_session
    from outbound.models import StoreTransfer, StoreTransferLine, TransferPT
    from outbound.serializers import StoreTransferReadSerializer, TransferPTSerializer
    from outbound.views import TransferPTCsvView
    from rest_framework.test import APIRequestFactory, force_authenticate

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        cashier, cashier_human = _person(world, "transfer-cashier")
        owner, owner_human = _person(world, "transfer-owner")
        _assign(world, cashier_human, "store_person", all_sites=True, all_brands=True)
        _assign(world, owner_human, "owner", sites=(world.sites[0],), brands=(world.brands[0],))
        transfer = StoreTransfer.objects.create(
            doc_number=f"TR-{uuid.uuid4().hex[:8]}",
            source_store=world.sites[0], destination_store=world.sites[1],
            partner_billing_value_paise=500,
        )
        StoreTransferLine.objects.create(
            transfer=transfer, sku_code="TRANSFER-COST", brand=world.brands[0].name,
            brand_ref=world.brands[0],
            unit_cost_paise=250, qty_dispatched=2,
        )
        pt = TransferPT.objects.create(
            transfer=transfer,
            rows=[{"BARCODE": "TRANSFER-COST", "P RATE": "2.50", "MARGIN": "25.00"}],
        )

        def request_for(user: User) -> SimpleNamespace:
            return SimpleNamespace(user=user, auth=issue_session(user).session)

        cashier_request = request_for(cashier)
        hidden = StoreTransferReadSerializer(transfer, context={"request": cashier_request}).data
        assert "unit_cost_paise" not in hidden["lines"][0]
        assert "partner_billing_value_paise" not in hidden
        hidden_pt = TransferPTSerializer(pt, context={"request": cashier_request}).data
        assert "P RATE" not in hidden_pt["columns"]
        assert "P RATE" not in hidden_pt["rows"][0]
        assert "MARGIN" not in hidden_pt["rows"][0]
        csv_request = APIRequestFactory().get(f"/api/outbound/transfers/{transfer.pk}/pt.csv")
        force_authenticate(csv_request, user=cashier, token=cashier_request.auth)
        csv_response = TransferPTCsvView.as_view()(csv_request, pk=transfer.pk)
        assert csv_response.status_code == 200
        assert b"P RATE" not in csv_response.content
        assert b"2.50" not in csv_response.content

        owner_request = request_for(owner)
        partial = StoreTransferReadSerializer(transfer, context={"request": owner_request}).data
        assert "unit_cost_paise" not in partial["lines"][0]
        _assign(world, owner_human, "owner", sites=(world.sites[1],), brands=(world.brands[0],))
        full = StoreTransferReadSerializer(transfer, context={"request": request_for(owner)}).data
        assert full["lines"][0]["unit_cost_paise"] == 250
        assert full["partner_billing_value_paise"] == 500
        full_pt = TransferPTSerializer(pt, context={"request": request_for(owner)}).data
        assert full_pt["rows"][0]["P RATE"] == "2.50"
        csv_request = APIRequestFactory().get(f"/api/outbound/transfers/{transfer.pk}/pt.csv")
        force_authenticate(csv_request, user=owner, token=request_for(owner).auth)
        csv_response = TransferPTCsvView.as_view()(csv_request, pk=transfer.pk)
        assert csv_response.status_code == 200
        assert b"P RATE" in csv_response.content
        assert b"2.50" in csv_response.content

        # A single foreign-brand line makes the whole priced document invisible
        # to this selected-brand Owner, even when the voucher number is known.
        StoreTransferLine.objects.create(
            transfer=transfer, sku_code="OTHER-BRAND", brand=world.brands[1].name,
            unit_cost_paise=900, qty_dispatched=1,
        )
        denied_request = APIRequestFactory().get(f"/api/outbound/transfers/{transfer.pk}/pt.csv")
        force_authenticate(denied_request, user=owner, token=request_for(owner).auth)
        assert TransferPTCsvView.as_view()(denied_request, pk=transfer.pk).status_code == 404


def test_outbound_write_rejects_nested_protected_money() -> None:
    from outbound.serializers import StoreTransferWriteSerializer

    attempt = StoreTransferWriteSerializer(data={
        "source_store": 1, "destination_store": 2,
        "lines": [{"sku_code": "BARCODE", "qty_planned": 1, "unit_cost_paise": 1}],
    })
    assert not attempt.is_valid()
    assert "protected_fields" in attempt.errors


def test_legacy_master_lists_and_counts_stay_inside_tenant(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    """SO-03: master pickers and summary never reveal the other tenant's IDs."""
    from masters.serializers import StoreSerializer
    from masters.views import (
        BrandListView, GstinListView, LegalEntityListView, LocationListView,
        SeasonListView, SkuLookupView, StoreDetailView, SummaryView,
    )

    world, other = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "master-reader")
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        request = cast(Request, SimpleNamespace(
            user=user, auth=SimpleNamespace(token_hash="proof"), query_params={},
        ))
        for view_type, expected in (
            (BrandListView, {brand.pk for brand in world.brands}),
            (StoreDetailView, {site.pk for site in world.sites}),
            (LocationListView, {site.pk for site in world.sites}),
            (GstinListView, {world.sites[0].gstin_id}),
            (LegalEntityListView, {world.sites[0].gstin.legal_entity_id}),
        ):
            view = view_type()
            view.request = request
            assert set(view.get_queryset().values_list("pk", flat=True)) == expected
        summary = SummaryView().get(request).data
        assert summary["stores"] == 2 and summary["brands"] == 2
        assert summary["entities"] == 1 and summary["gstins"] == 1
        assert summary["seasons"] == 0 and summary["open_season"] is None
        seasons = SeasonListView()
        seasons.request = request
        shared_season = Season.objects.create(
            code=f"S{uuid.uuid4().hex[:7]}", name="Shared legacy season",
        )
        assert seasons.get_queryset().filter(pk=shared_season.pk).exists()
        unassigned, _ = _person(world, "unassigned-season")
        seasons.request = cast(Request, SimpleNamespace(
            user=unassigned, auth=SimpleNamespace(token_hash="proof"), query_params={},
        ))
        with pytest.raises(Refusal) as season_denial:
            seasons.get_queryset()
        assert season_denial.value.code == "ACTION_DENIED"
        sku_request = cast(Request, SimpleNamespace(
            user=user, auth=SimpleNamespace(token_hash="proof"),
            query_params={"design": "SHARED-CODE"},
        ))
        with pytest.raises(Refusal) as sku_denial:
            SkuLookupView().get(sku_request)
        assert sku_denial.value.code == "TENANT_SCOPE_UNRESOLVED"
        assert other.sites[0].gstin_id not in StoreSerializer().get_fields()["gstin"].queryset.values_list("pk", flat=True)


def test_shared_season_write_is_blocked_and_size_rules_are_tenant_owned(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    """SO-03: shared season history cannot be changed and stock rules never cross tenants."""
    from stockledger.broken_size import rules_in_force
    from stockledger.broken_size_models import SizeRule
    from stockledger.broken_size_views import GoodsSizeRuleView
    from stockledger.goods_ageing_views import GoodsSeasonEndView

    first, second = worlds
    with tenant_context(first.tenant.pk):
        user, human = _person(first, "site-steward")
        _assign(first, human, "owner", sites=(first.sites[0],), all_brands=True)
        request = cast(Request, SimpleNamespace(
            user=user, auth=SimpleNamespace(token_hash="proof"), data={},
        ))
        SizeRule.objects.create(
            tenant=first.tenant, category="Shirts", category_key="SHIRTS",
            core_sizes=["S", "M"], missing_percent=40, updated_at=timezone.now(),
        )
        with pytest.raises(Refusal) as scoped_denial:
            GoodsSizeRuleView().post(request)
        assert scoped_denial.value.code == "NOT_FOUND"
        with pytest.raises(Refusal) as season_denial:
            GoodsSeasonEndView().post(request)
        assert season_denial.value.code == "TENANT_SCOPE_UNRESOLVED"
    with tenant_context(second.tenant.pk):
        SizeRule.objects.create(
            tenant=second.tenant, category="Shirts", category_key="SHIRTS",
            core_sizes=["XS", "XL"], missing_percent=60, updated_at=timezone.now(),
        )
        assert rules_in_force()["SHIRTS"].core_sizes == ("XS", "XL")
    with tenant_context(first.tenant.pk):
        assert rules_in_force()["SHIRTS"].core_sizes == ("S", "M")


def test_whole_site_ageing_reports_require_all_brand_scope_at_that_site(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stockledger import goods_ageing_views, sor_ageing_views

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "ageing-reader")
        _assign(world, human, "owner", sites=(world.sites[0],), brands=(world.brands[0],))
        def enabled(stores: Iterable[Store], _features: Any) -> list[SimpleNamespace]:
            return [SimpleNamespace(enabled=True) for _ in stores]
        monkeypatch.setattr(goods_ageing_views, "switch_states", enabled)
        monkeypatch.setattr(sor_ageing_views, "switch_states", enabled)
        assert goods_ageing_views.ageing_stores(user) == []
        assert sor_ageing_views.sor_sites(user) == []

        _assign(world, human, "owner", sites=(world.sites[0],), all_brands=True)
        assert [site.pk for site in goods_ageing_views.ageing_stores(user)] == [world.sites[0].pk]
        assert [site.pk for site in sor_ageing_views.sor_sites(user)] == [world.sites[0].pk]


def test_arrival_counter_assignment_needs_full_site_brand_authority(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    from inbound.goods_services import person_can_receive

    world, other = worlds
    with tenant_context(world.tenant.pk):
        _user, human = _person(world, "receiver")
        _assign(world, human, "warehouse", sites=(world.sites[0],), brands=(world.brands[0],))
        assert not person_can_receive(world.tenant.pk, human.pk, world.sites[0].pk)
        assert not person_can_receive(other.tenant.pk, human.pk, world.sites[0].pk)
        _assign(world, human, "warehouse", sites=(world.sites[0],), all_brands=True)
        assert person_can_receive(world.tenant.pk, human.pk, world.sites[0].pk)
        assert not person_can_receive(world.tenant.pk, human.pk, world.sites[1].pk)


def test_export_worker_refuses_revocation_during_production(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SO-03: a queued export is checked again after its bytes are built."""
    from files import goods_exports as exports

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "exporter")
        spec = exports.ExportSpec(
            kind="stock_csv",
            scope={"scope_kind": "tenant", "site_ids": [], "brand_ids": [], "sbu_ids": [], "entity_id": None},
            field_set=(),
        )
        produced = exports.ExportFile(
            filename="stock.csv", media_type="text/csv", data=b"row\n",
            row_count=1, cells=((world.sites[0].pk, world.brands[0].pk),),
        )
        intent = SimpleNamespace(
            pk=uuid.uuid4(), tenant_id=world.tenant.pk, actor_id=human.pk,
            payload={"export_spec": spec.as_payload()}, request_key=uuid.uuid4(),
        )
        allowed = SimpleNamespace(all_actions=lambda: {exports.EXPORT_GRANT})
        revoked = SimpleNamespace(all_actions=lambda: set())
        snapshots = iter((allowed, revoked))
        monkeypatch.setattr(exports, "requester_access", lambda _intent: next(snapshots))
        monkeypatch.setattr(
            exports, "resolve_kind",
            lambda _kind: exports.ExportKind(check=lambda _access, _spec: None,
                                             produce=lambda _access, _spec: produced),
        )
        failures: list[str] = []
        monkeypatch.setattr(exports, "_open_failure", lambda _intent, code: failures.append(code))
        stored: list[bool] = []
        monkeypatch.setattr(exports, "_store", lambda *_args: stored.append(True))
        outcome = exports.run_export(intent)
        assert outcome.state == "failed" and outcome.terminal
        assert outcome.diagnostic == "ACTION_DENIED"
        assert failures == ["ACTION_DENIED"]
        assert not stored


def test_queued_export_refuses_revoked_assignment_before_production(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from files import goods_exports as exports

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        _user, human = _person(world, "queued-export")
        assignment = _assign(world, human, "owner", all_sites=True, all_brands=True)
        spec = exports.ExportSpec(
            kind="stock_csv",
            scope={"scope_kind": "tenant", "site_ids": [], "brand_ids": [], "sbu_ids": [], "entity_id": None},
            field_set=(),
        )
        intent = SimpleNamespace(
            pk=uuid.uuid4(), tenant_id=world.tenant.pk, actor_id=human.pk,
            payload={"export_spec": spec.as_payload()}, request_key=uuid.uuid4(),
        )
        assignment.revoked_at = timezone.now() - timedelta(minutes=1)
        assignment.save(update_fields=["revoked_at"])
        assert not effective_assignments(human.pk)
        produced: list[bool] = []
        monkeypatch.setattr(exports, "resolve_kind", lambda _kind: produced.append(True))
        failures: list[str] = []
        monkeypatch.setattr(exports, "_open_failure", lambda _intent, code: failures.append(code))
        outcome = exports.run_export(intent)
        assert outcome.state == "failed" and outcome.terminal
        assert outcome.diagnostic == "ACTION_DENIED"
        assert failures == ["ACTION_DENIED"]
        assert not produced


def test_export_create_rechecks_authority_before_replayed_job_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from files import goods_exports as exports
    from files import goods_export_views as views

    allowed = False
    access = SimpleNamespace(
        require_action=lambda _action: None,
        require_step_up=lambda: None,
        refresh=lambda: allowed,
    )
    intent_id = uuid.uuid4()
    view = views.GoodsExportCreateView()
    monkeypatch.setattr(view, "access", lambda _request: access)
    monkeypatch.setattr(
        view, "run_command", lambda *_args, **_kwargs:
        SimpleNamespace(resource_id=intent_id, status_code=202),
    )
    monkeypatch.setattr(
        views, "OutboxIntent", SimpleNamespace(objects=SimpleNamespace(
            get=lambda **_kwargs: SimpleNamespace(pk=intent_id),
        )),
    )
    monkeypatch.setattr(
        exports, "resolve_kind", lambda _kind: SimpleNamespace(check=lambda *_: None),
    )
    expected_access = access
    delivered: list[bool] = []

    def response(_intent: Any, *, access: Any) -> dict[str, Any]:
        delivered.append(access is expected_access)
        return {"download_url": None}

    monkeypatch.setattr(exports, "export_job_dto", response)
    request = cast(Request, SimpleNamespace(data={
        "command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
        "kind": "stock_csv", "scope": {"scope_kind": "tenant"},
    }))
    with pytest.raises(Refusal) as denied:
        view.post(request)
    assert denied.value.code == "AUTH_REQUIRED"
    assert not delivered

    allowed = True
    assert view.post(request).data["download_url"] is None
    assert delivered == [True]


def test_evidence_download_refuses_revocation_after_offbox_read(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from files import goods_views
    from core.canonical import sha256_hex

    world, _ = worlds
    data = b"protected file bytes"
    evidence = SimpleNamespace(
        pk=uuid.uuid4(), object_key="proof", sha256=sha256_hex(data),
        size=len(data), media_type="application/pdf", filename="proof.pdf",
    )
    revoked = False

    def read(_key: str) -> bytes:
        nonlocal revoked
        revoked = True
        return data

    access = SimpleNamespace(
        tenant_id=world.tenant.pk, refresh=lambda: not revoked,
        principal=lambda: SimpleNamespace(),
    )
    view = goods_views.EvidenceDownloadView()
    monkeypatch.setattr(view, "access", lambda _request: access)
    monkeypatch.setattr(
        goods_views, "EvidenceObject",
        SimpleNamespace(objects=SimpleNamespace(filter=lambda **_kw: SimpleNamespace(first=lambda: evidence))),
    )
    monkeypatch.setattr(goods_views, "readable_by", lambda _access, _evidence: True)
    monkeypatch.setattr(goods_views, "get_store", lambda: SimpleNamespace(get=read))
    monkeypatch.setattr(goods_views, "execute_command", lambda *_args: None)
    with tenant_context(world.tenant.pk), pytest.raises(Refusal) as denied:
        view.get(cast(Request, SimpleNamespace()), evidence.pk)
    assert denied.value.code == "NOT_FOUND"


def test_report_protected_fields_require_every_store_on_one_assignment(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    """SO-03: an Owner role at A cannot expose cost or targets at store B."""
    from reporting.base import sees_cost, sees_financial_report, sees_targets, viewer_stores
    from reporting.staff_report import may_set_targets, sees_team

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "reporter")
        _assign(world, human, "owner", sites=(world.sites[0],), all_brands=True)
        _assign(world, human, "store_person", sites=(world.sites[1],), all_brands=True)
        assert [site.pk for site in viewer_stores(user)] == [site.pk for site in world.sites]
        assert sees_cost(user, [world.sites[0]])
        assert sees_targets(user, [world.sites[0]])
        assert sees_financial_report(user, [world.sites[0]])
        assert sees_team(user, [world.sites[0]])
        assert may_set_targets(user, [world.sites[0]])
        assert not sees_cost(user, [world.sites[1]])
        assert not sees_targets(user, [world.sites[1]])
        assert not sees_financial_report(user, [world.sites[1]])
        assert not sees_team(user, [world.sites[1]])
        assert not may_set_targets(user, [world.sites[1]])
        assert not sees_cost(user, world.sites)
        assert not sees_targets(user, world.sites)
        assert not sees_financial_report(user, world.sites)
        assert not sees_team(user, list(world.sites))

        _assign(world, human, "owner", sites=(world.sites[1],), all_brands=True)
        assert sees_cost(user, world.sites)
        assert sees_targets(user, world.sites)
        assert sees_financial_report(user, world.sites)
        assert sees_team(user, list(world.sites))


def test_synchronous_export_rechecks_protected_fields_before_delivery(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    """SO-03: rendered XLSX bytes are not returned after a field-policy change."""
    from datetime import date

    from reporting.base import ReportScope, record_export

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "xlsx-requester")
        _assign(world, human, "owner", sites=(world.sites[0],), all_brands=True)
        scope = ReportScope(
            user=user, options=[world.sites[0]], stores=[world.sites[0]],
            date_from=date(2026, 9, 1), date_to=date(2026, 9, 30),
        )
        role = world.roles["owner"]
        role.field_access = ["financial", "customer"]
        role.save(update_fields=["field_access"])
        with pytest.raises(Refusal) as denied:
            record_export(
                user, report="sales", scope=scope, detail={"rows": 1}, contains_cost=True
            )
        assert denied.value.code == "ACTION_DENIED"


def test_event_stream_stops_inside_batch_after_session_revocation(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SO-03: queued SSE items must not continue after the session is revoked."""
    from alerts import goods_views

    world, _ = worlds
    refreshes = 0

    def refresh() -> bool:
        nonlocal refreshes
        refreshes += 1
        return refreshes < 3

    access = SimpleNamespace(
        tenant_id=world.tenant.pk, human_id=uuid.uuid4(), grants=[],
        refresh=refresh, site_reach=lambda _action: set(),
    )
    view = goods_views.EventStreamView()
    monkeypatch.setattr(view, "access", lambda _request: access)
    monkeypatch.setattr(
        goods_views, "_my_notifications",
        lambda _access: [
            SimpleNamespace(pk=uuid.uuid4(), subject_key="one"),
            SimpleNamespace(pk=uuid.uuid4(), subject_key="two"),
        ],
    )
    with tenant_context(world.tenant.pk):
        response = view.get(cast(Request, SimpleNamespace(query_params={})))
        sent = b"".join(cast(Iterable[bytes], response.streaming_content))
    assert b'"subject_id": "one"' in sent
    assert b'"subject_id": "two"' not in sent
    assert refreshes == 3


def test_event_reconnect_refuses_revoked_session(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    from rest_framework.test import APIRequestFactory

    from accounts.sessions import SESSION_COOKIE, issue_session
    from alerts.goods_views import EventStreamView

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "event-reconnect")
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        issued = issue_session(user)
        factory = APIRequestFactory()

        def reconnect() -> Any:
            request = factory.get("/api/goods-v1/events", HTTP_ACCEPT="text/event-stream")
            request.COOKIES[SESSION_COOKIE] = issued.token
            return EventStreamView.as_view()(request)

        before = reconnect()
        assert before.status_code == 200
        bump_security_epoch(human.pk, world.tenant.pk)
        after = reconnect()
        assert after.status_code in (401, 403)


def test_staff_retirement_closes_current_and_future_role_assignments(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retirement may not leave scheduled authority or append a legacy grant."""
    from accounts import goods_admin_services as svc
    from accounts.goods_models import RoleGrant, Staff

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        actor, actor_human = _person(world, "retirement-owner")
        _assign(world, actor_human, "owner", sites=(world.sites[0],), all_brands=True)
        _target, target_human = _person(world, "retirement-target")
        staff = Staff.objects.create(tenant=world.tenant, human=target_human)
        current = _assign(
            world, target_human, "warehouse", sites=(world.sites[0],), all_brands=True,
        )
        future = _assign(
            world, target_human, "warehouse", sites=(world.sites[1],), all_brands=True,
            starts=timedelta(days=3),
        )
        effective_at = timezone.now()
        run = cast(CommandRun, SimpleNamespace(tenant_id=world.tenant.pk, now=effective_at))
        monkeypatch.setattr(svc, "lock_staff", lambda _run, _pk: Staff.objects.get(pk=staff.pk))
        monkeypatch.setattr(svc, "record_version", lambda *_args, **_kwargs: None)

        with pytest.raises(Refusal) as denied:
            svc.retire_staff(
                run, access=_access(actor, actor_human), staff_id=staff.pk,
                expected_revision=1, effective_at=effective_at, reason_code="end_of_service",
            )
        assert denied.value.code == "NOT_FOUND"
        current.refresh_from_db()
        future.refresh_from_db()
        assert current.effective_to is None and future.revoked_at is None
        assert Staff.objects.get(pk=staff.pk).retired_at is None

        _assign(world, actor_human, "owner", sites=(world.sites[1],), all_brands=True)
        svc.retire_staff(
            run, access=_access(actor, actor_human), staff_id=staff.pk,
            expected_revision=1, effective_at=effective_at, reason_code="end_of_service",
        )
        current.refresh_from_db()
        future.refresh_from_db()
        assert current.effective_to == effective_at
        assert future.revoked_at == effective_at
        assert future.effective_from > effective_at
        assert not effective_assignments(target_human.pk)
        assert RoleGrant.objects.filter(human=target_human).count() == 0
        audit = {item["field"]: item["value"] for item in run.audit_after}
        assert str(current.pk) in audit["role_assignments_revoked"]
        assert str(future.pk) in audit["role_assignments_revoked"]


def test_online_sale_replay_refuses_foreign_store_before_revealing_bill(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django.db import IntegrityError

    from sell.services import accept

    world, _ = worlds
    real_accept_new = accept._accept_new
    with tenant_context(world.tenant.pk):
        actor, human = _person(world, "sale-replay")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        existing = SimpleNamespace(store_id=world.sites[1].pk)
        data = {"idempotency_uuid": uuid.uuid4(), "store": world.sites[0].code}
        monkeypatch.setattr(accept, "_find_by_uuid", lambda _uuid: existing)
        monkeypatch.setattr(accept, "_replay_or_refuse", lambda *_: pytest.fail("foreign replay"))
        with pytest.raises(accept.AcceptError) as denied:
            accept.accept_sale(data, actor)
        assert denied.value.code == "SCOPE_DENIED"

        results = iter((None, existing))
        monkeypatch.setattr(accept, "_find_by_uuid", lambda _uuid: next(results))
        monkeypatch.setattr(accept, "_accept_new", lambda *_: (_ for _ in ()).throw(IntegrityError()))
        with pytest.raises(accept.AcceptError) as concurrent:
            accept.accept_sale(data, actor)
        assert concurrent.value.code == "SCOPE_DENIED"

        # A bill may land after the first lookup but before the transaction's
        # own replay check. That branch must compare the store too.
        results = iter((None, existing))
        monkeypatch.setattr(accept, "_find_by_uuid", lambda _uuid: next(results))
        monkeypatch.setattr(accept, "_accept_new", real_accept_new)
        monkeypatch.setattr(accept, "_replay", lambda _sale: pytest.fail("foreign replay"))
        with pytest.raises(accept.AcceptError) as in_transaction:
            accept.accept_sale(data, actor)
        assert in_transaction.value.code == "SCOPE_DENIED"


def test_special_order_transfer_command_keeps_live_session_guard(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    from accounts.sessions import issue_session
    from sell.services.special_orders import _transfer_access

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        actor, human = _person(world, "special-transfer")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        principal = Principal(tenant_id=world.tenant.pk, human_id=human.pk, user_id=actor.pk)
        with pytest.raises(Refusal) as no_session:
            _transfer_access(world.sites[0], actor, principal, None)
        assert no_session.value.code == "SCOPE_DENIED"
        session = issue_session(actor).session
        guarded = _transfer_access(world.sites[0], actor, principal, session)
        assert guarded.guard is not None
        bump_security_epoch(human.pk, world.tenant.pk)
        run = cast(CommandRun, SimpleNamespace(
            claim_rank=lambda _rank: None, authority={},
            spec=SimpleNamespace(action="special_order.transfer"),
        ))
        with pytest.raises(Refusal) as denied:
            guarded.guard(run, False)
        assert denied.value.code == "AUTH_REQUIRED"


def test_dataset_refuses_revocation_after_build(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rest_framework.test import APIRequestFactory, force_authenticate

    from accounts.sessions import issue_session
    from sell import views as sell_views

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "dataset-revoked")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        session = issue_session(user).session

        def build(_store: Store, _since: str) -> dict[str, Any]:
            bump_security_epoch(human.pk, world.tenant.pk)
            return {"stock": [{"barcode": "SECRET"}]}

        monkeypatch.setattr(sell_views, "build_dataset", build)
        monkeypatch.setattr(sell_views, "_with_till_allocation", lambda _store, payload: payload)
        request = APIRequestFactory().get("/api/sell/dataset")
        force_authenticate(request, user=user, token=session)
        response = sell_views.DatasetView.as_view()(request)
        assert response.status_code in (401, 403)
        assert b"SECRET" not in str(response.data).encode()


def test_tenant_wide_settings_reads_need_explicit_all_scope(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    from rest_framework.test import APIRequestFactory, force_authenticate

    from accounts.sessions import issue_session
    from masters.goods_consent_wording_views import GoodsConsentWordingView
    from masters.goods_document_series_views import GoodsDocumentSeriesView
    from masters.goods_tax_settings_views import GoodsTaxSettingsView

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "local-setup-reader")
        _assign(world, human, "owner", sites=(world.sites[0],), all_brands=True)
        session = issue_session(user).session
        for view_type, path in (
            (GoodsConsentWordingView, "/api/goods-v1/masters/consent-wording"),
            (GoodsDocumentSeriesView, "/api/goods-v1/masters/document-series"),
            (GoodsTaxSettingsView, "/api/goods-v1/masters/tax-settings"),
        ):
            request = APIRequestFactory().get(path)
            force_authenticate(request, user=user, token=session)
            response = view_type.as_view()(request)
            assert response.status_code == 403


def test_two_tenants_and_wrong_scope_are_denied(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    """PRD §4.3: an assignment and selected IDs belong to exactly one tenant."""
    first, second = worlds
    with tenant_context(first.tenant.pk):
        user, human = _person(first, "tenant-wall")
        _assign(first, human, "owner", sites=(first.sites[0],), brands=(first.brands[0],))
        access = _access(user, human)
        assert access.can("stock.view", site_id=first.sites[0].pk, brand_id=first.brands[0].pk)
        assert not access.can("stock.view", site_id=first.sites[1].pk, brand_id=first.brands[0].pk)
        assert not access.can("stock.view", site_id=first.sites[0].pk, brand_id=first.brands[1].pk)
        assert not access.can("stock.view", site_id=second.sites[0].pk, brand_id=second.brands[0].pk)
        with pytest.raises(Refusal) as denied:
            access.require("stock.view", site_id=second.sites[0].pk, brand_id=second.brands[0].pk)
        assert denied.value.code == "NOT_FOUND"
        with pytest.raises(ValidationError):
            _assign(first, human, "owner", sites=(second.sites[0],), brands=(first.brands[0],))
    with tenant_context(second.tenant.pk):
        assert effective_assignments(human.pk) == []
        assert effective_grants(human.pk) == []


def test_roles_cannot_exchange_site_brand_or_field_authority(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    """PRD §4.3: action, protected fields and site/brand come from one row."""
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "multi-role")
        _assign(world, human, "owner", sites=(world.sites[0],), brands=(world.brands[0],))
        _assign(world, human, "store_person", sites=(world.sites[1],), brands=(world.brands[1],))
        access = _access(user, human)
        assert access.can("stock.view", site_id=world.sites[0].pk, brand_id=world.brands[0].pk)
        assert access.can("stock.view", site_id=world.sites[1].pk, brand_id=world.brands[1].pk)
        assert not access.can("stock.view", site_id=world.sites[0].pk, brand_id=world.brands[1].pk)
        assert not access.can("stock.view", site_id=world.sites[1].pk, brand_id=world.brands[0].pk)
        assert access.covers_all(
            {"stock.view"}, [(world.sites[0].pk, world.brands[0].pk)], {"cost"}
        )
        assert not access.covers_all(
            {"stock.view"}, [(world.sites[1].pk, world.brands[1].pk)], {"cost"}
        )
        assert not access.can("access.manage", site_id=world.sites[1].pk, brand_id=world.brands[1].pk)
        assert not access.covers_all(
            {"stock.view"}, [(world.sites[0].pk, world.brands[0].pk), (world.sites[1].pk, world.brands[1].pk)], {"cost"}
        )
        with pytest.raises(Refusal):
            access.require_all_actions(
                {"stock.view", "transfer.move"},
                site_id=world.sites[0].pk, brand_id=world.brands[0].pk,
            )


def test_all_includes_future_membership_selected_does_not(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    """PRD §4.3: explicit all follows new masters; selected lists stay fixed."""
    world, foreign = worlds
    with tenant_context(world.tenant.pk):
        all_user, all_human = _person(world, "all")
        selected_user, selected_human = _person(world, "selected")
        _assign(world, all_human, "owner", all_sites=True, all_brands=True)
        _assign(world, selected_human, "owner", sites=(world.sites[0],), brands=(world.brands[0],))
        new_site = Store.objects.create(
            tenant=world.tenant, gstin=world.sites[0].gstin,
            code="new-site", name="New Site",
        )
        new_brand = Brand.objects.create(tenant=world.tenant, code="new-brand", name="New Brand")
        assert _access(all_user, all_human).can("stock.view", site_id=new_site.pk, brand_id=new_brand.pk)
        assert not _access(all_user, all_human).can(
            "stock.view", site_id=foreign.sites[0].pk, brand_id=foreign.brands[0].pk
        )
        assert not _access(selected_user, selected_human).can("stock.view", site_id=new_site.pk, brand_id=new_brand.pk)


def test_expired_future_empty_and_inactive_assignments_deny(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    """PRD §4.3: no implicit authority outside the assignment's active period."""
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "periods")
        _assign(world, human, "owner", all_sites=True, all_brands=True, ends=timedelta(minutes=-1))
        _assign(world, human, "owner", all_sites=True, all_brands=True, starts=timedelta(minutes=1))
        _assign(world, human, "owner", all_sites=False, all_brands=True)
        assert _access(user, human).grants == []
        role = world.roles["owner"]
        active = _assign(world, human, "owner", all_sites=True, all_brands=True)
        assert _access(user, human).grants
        role.is_active = False
        role.save(update_fields=["is_active"])
        assert _access(user, human).grants == []
        assert active.pk is not None


def test_six_role_field_and_step_floors(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    """Appendix B: financial, customer, staff-private and PT cost boundaries."""
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        owner = world.roles["owner"]
        store = world.roles["store_person"]
        warehouse = world.roles["warehouse"]
        brand = world.roles["brand_manager"]
        accounts = world.roles["accounts"]
        admin = world.roles["it_admin"]
        assert {"financial", "employee_private", "customer", "cost", "margin"} <= role_fields(owner)
        assert "customer" in role_fields(store) and "cost" not in role_fields(store)
        assert "cost_own_pt" in role_fields(warehouse)
        assert not {"cost", "margin", "layer_value", "financial"} & role_fields(warehouse)
        assert {"cost", "margin"} <= role_fields(brand)
        assert "customer" not in role_fields(brand)
        assert "billing_identity" in role_fields(accounts)
        assert "customer" not in role_fields(accounts)
        assert role_fields(admin) == frozenset()
        assert "pt.prepare" in role_actions(warehouse)
        assert "pt.prepare" not in role_actions(store)
        assert "pt.approve.receipt" in role_actions(owner)
        assert "pt.approve.receipt" not in role_actions(warehouse)
        owner.permissions_map = {"step_actions": []}
        owner.save(update_fields=["permissions_map"])
        assert "pt.approve.receipt" not in role_actions(owner)
        assert "access.manage" in role_actions(admin)
        assert "receive.arrival" not in role_actions(admin)


def test_security_epoch_revocation_ends_a_live_access_context(worlds: tuple[TenantWorld, TenantWorld]) -> None:
    """PRD §4.3: a session cannot keep its old authority after revocation."""
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "revocation")
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        SecurityGuard.objects.create(tenant=world.tenant, human=human, epoch=1)
        moment = timezone.now()
        session = ServerSession.objects.create(
            tenant=world.tenant, user=user,
            token_hash=uuid.uuid4().hex * 2, csrf_hash=uuid.uuid4().hex * 2,
            issued_at=moment, last_seen_at=moment,
            expires_at=moment + timedelta(hours=1), security_epoch=1,
        )
        request = SimpleNamespace(user=user, auth=session)
        access = resolve_access(request)
        assert access.can("stock.view", site_id=world.sites[0].pk, brand_id=world.brands[0].pk)
        bump_security_epoch(human.pk, world.tenant.pk)
        assert not access.refresh()
