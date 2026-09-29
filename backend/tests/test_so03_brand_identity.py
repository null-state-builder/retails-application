"""SO-03: labels are evidence; only tenant-proven IDs establish brand scope."""

from __future__ import annotations

import uuid
from typing import Any, cast
from types import SimpleNamespace

import pytest
from django.apps import apps
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.sessions import issue_session
from core.tenancy import tenant_context
from masters.brand_identity import RESOURCES, fingerprint, reconciliation_page, resource_rows
from masters.brand_reconciliation_views import BrandReconciliationView
from masters.goods_models import BrandIdentityBinding
from masters.scoping import scope_by_store_and_brand
from stockledger.models import StockOnHand
from test_so03_denials import TenantWorld, _assign, _person, _tenant_world


@pytest.fixture
def world(db: None) -> TenantWorld:
    return _tenant_world("brand-proof")


def stock(world: TenantWorld, index: int = 0, *, brand: int | None = None) -> StockOnHand:
    return StockOnHand.objects.create(
        store=world.sites[index], sku_code=uuid.uuid4().hex,
        brand=world.brands[0].name, brand_ref_id=brand,
        net_qty=3, net_value_paise=72000,
    )


def test_resource_registry_paths_are_real_and_tenant_scoped(world: TenantWorld) -> None:
    with tenant_context(world.tenant.pk):
        for label in RESOURCES:
            assert apps.get_model(label) is not None
            # Query construction resolves every relationship, even with no rows.
            assert list(resource_rows(label, world.tenant.pk)) == []


def test_names_never_authorize_and_scope_tuples_do_not_mix(world: TenantWorld) -> None:
    with tenant_context(world.tenant.pk):
        person, human = _person(world, "brand-reader")
        _assign(world, human, "brand_manager", sites=(world.sites[0],), brands=(world.brands[0],))
        _assign(world, human, "brand_manager", sites=(world.sites[1],), brands=(world.brands[1],))
        unresolved = stock(world)
        allowed = stock(world, brand=world.brands[0].pk)
        wrong_tuple = stock(world, index=1, brand=world.brands[0].pk)
        # Duplicate presentation labels must not merge either assignment.
        world.brands[1].name = world.brands[0].name
        world.brands[1].save(update_fields=["name"])
        def query() -> Any:
            return scope_by_store_and_brand(StockOnHand.objects.all(), person, section="stock", minimum="view")
        assert list(query().values_list("pk", flat=True)) == [allowed.pk]
        assert not query().filter(pk__in=[unresolved.pk, wrong_tuple.pk]).exists()
        # A later rename cannot detach stable ownership or authorize a name match.
        world.brands[0].name = "Renamed presentation"
        world.brands[0].save(update_fields=["name"])
        assert list(query().values_list("pk", flat=True)) == [allowed.pk]
        assert not scope_by_store_and_brand(StockOnHand.objects.all(), person).exists()


def editor(world: TenantWorld) -> tuple[Any, Any]:
    user, human = _person(world, "identity-reviewer")
    _assign(world, human, "it_admin", all_sites=True, all_brands=True)
    user.set_password("proof-password")
    user.save(update_fields=["password"])
    return user, issue_session(user).session


def apply_rows(user: Any, session: Any, rows: list[dict[str, Any]], *, resource: str = "stockledger.stockonhand") -> Any:
    request = APIRequestFactory().post(
        "/api/goods-v1/masters/brand-reconciliation",
        {"command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
         "resource": resource, "rows": rows,
         "current_password": "proof-password"}, format="json",
    )
    force_authenticate(request, user=user, token=session)
    return BrandReconciliationView.as_view()(request)


def test_review_is_explicit_and_preserves_labels_values_and_history(world: TenantWorld) -> None:
    with tenant_context(world.tenant.pk):
        row = stock(world)
        user, session = editor(world)
        page = reconciliation_page("stockledger.stockonhand")
        assert page["items"][0]["brand_id"] is None
        assert page["items"][0]["suggestions"][0]["id"] == world.brands[0].pk
        assert "net_value_paise" not in page["items"][0]
        response = apply_rows(user, session, [{"id": row.pk, "brand_id": world.brands[0].pk, "fingerprint": fingerprint(row)}])
        assert response.status_code == 200, response.data
        row.refresh_from_db()
        assert row.brand_ref_id == world.brands[0].pk
        assert row.brand == world.brands[0].name
        assert (row.net_qty, row.net_value_paise) == (3, 72000)
        binding = BrandIdentityBinding.objects.get(resource_id=row.pk)
        assert binding.actor_id == user.human_id
        assert binding.basis["method"] == "reviewed_rows"
        # A repeat review with the current fingerprint is an unchanged outcome.
        response = apply_rows(user, session, [{"id": row.pk, "brand_id": world.brands[0].pk, "fingerprint": fingerprint(row)}])
        assert response.status_code == 401
        session = issue_session(user).session
        response = apply_rows(user, session, [{"id": row.pk, "brand_id": world.brands[0].pk, "fingerprint": fingerprint(row)}])
        assert response.status_code == 200, response.data
        assert response.data["applied"] == 0
        assert BrandIdentityBinding.objects.filter(resource_id=row.pk).count() == 1


def test_batch_rejects_stale_or_foreign_targets_before_any_write(world: TenantWorld) -> None:
    other = _tenant_world("foreign-brand")
    with tenant_context(world.tenant.pk):
        first, changed = stock(world), stock(world)
        user, session = editor(world)
        batch = [{"id": row.pk, "brand_id": world.brands[0].pk, "fingerprint": fingerprint(row)} for row in (first, changed)]
        changed.net_qty = 8
        changed.save(update_fields=["net_qty"])
        response = apply_rows(user, session, batch)
        assert response.status_code == 409, response.data
        assert not BrandIdentityBinding.objects.exists()
        first.refresh_from_db()
        assert first.brand_ref_id is None
        response = apply_rows(user, session, [{"id": first.pk, "brand_id": other.brands[0].pk, "fingerprint": fingerprint(first)}])
        assert response.status_code == 404, response.data
        assert not BrandIdentityBinding.objects.exists()


def test_foreign_record_with_local_brand_cannot_enter_scope(world: TenantWorld) -> None:
    other = _tenant_world("foreign-site")
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "tenant-owner")
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        stock(other, brand=world.brands[0].pk)
        assert not scope_by_store_and_brand(StockOnHand.objects.all(), user, section="stock", minimum="view").exists()


def test_foreign_existing_brand_link_cannot_be_rebound_by_review(world: TenantWorld) -> None:
    other = _tenant_world("foreign-ref")
    with tenant_context(world.tenant.pk):
        row = stock(world, brand=other.brands[0].pk)
        user, session = editor(world)
        response = apply_rows(user, session, [{"id": row.pk, "brand_id": world.brands[0].pk,
                                              "fingerprint": fingerprint(row)}])
        assert response.status_code == 409, response.data
        assert response.data["code"] == "IDENTITY_CONFLICT"
        row.refresh_from_db()
        assert row.brand_ref_id == other.brands[0].pk
        assert not BrandIdentityBinding.objects.filter(resource_model="stockledger.stockonhand",
                                                       resource_id=row.pk).exists()


def test_printed_sale_keeps_unresolved_identity_and_only_copies_reviewed_tenant_stock(
    world: TenantWorld, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sell.models import SaleLine
    from sell.services.accept import _PreparedLine, _write_line
    from sell.services.resolve import ResolvedPiece

    other = _tenant_world("foreign-sale")
    with tenant_context(world.tenant.pk):
        source = stock(world)
        foreign = stock(other, brand=other.brands[0].pk)
        foreign.sku_code = source.sku_code
        foreign.save(update_fields=["sku_code"])
        payload = {
            "barcode": source.sku_code, "line_no": 1, "direction": SaleLine.Direction.SALE,
            "qty": 1, "mrp_paise": 10000, "disc_paise": 0, "value_paise": 10000,
            "gst_rate": "5", "gst_paise": 476, "offer_evidence": {}, "manual_desc": "",
        }
        line = _PreparedLine(payload=payload, piece=ResolvedPiece(None, "", 0, {}))
        sale = cast(Any, SimpleNamespace(store=world.sites[0]))
        monkeypatch.setattr(SaleLine.objects, "create", lambda **values: values)
        unbound = cast(Any, _write_line(sale, line, None, {}))
        assert unbound["brand_ref_id"] is None
        assert unbound["barcode"] == source.sku_code
        source.brand_ref = world.brands[0]
        source.save(update_fields=["brand_ref"])
        linked = cast(Any, _write_line(sale, line, None, {}))
        assert linked["brand_ref_id"] == world.brands[0].pk


def test_whole_site_alert_cannot_be_mapped_to_one_brand(world: TenantWorld) -> None:
    from alerts.models import Alert, AlertKind

    with tenant_context(world.tenant.pk):
        row = Alert.objects.create(
            kind=AlertKind.COUNT_DUE, kind_label="Count due", title="Count the whole store",
            dedupe_key=uuid.uuid4().hex, store=world.sites[0], brand="",
        )
        user, session = editor(world)
        response = apply_rows(user, session, [{"id": row.pk, "brand_id": world.brands[0].pk,
                                              "fingerprint": fingerprint(row)}], resource="alerts.alert")
        assert response.status_code == 409, response.data
        assert response.data["code"] == "INVALID_SCOPE_ROW"
        row.refresh_from_db()
        assert row.brand_ref_id is None
        assert not BrandIdentityBinding.objects.filter(resource_model="alerts.alert", resource_id=row.pk).exists()
        page = reconciliation_page("alerts.alert")
        assert page["items"][0]["state"] == "whole_site"
        assert page["items"][0]["suggestions"] == []


def test_immutable_ledger_keeps_snapshot_and_appends_source_identity(world: TenantWorld) -> None:
    from stockledger.models import StockLedgerEntry
    from vendors.models import Booking, Vendor
    from masters.models import Season

    with tenant_context(world.tenant.pk):
        user, session = editor(world)
        vendor = Vendor.objects.create(tenant=world.tenant, code="ledger-vendor", name="Ledger vendor")
        season = Season.objects.create(code=f"S{uuid.uuid4().hex[:7]}", name="Proof season")
        booking = Booking.objects.create(
            number=f"BK-{uuid.uuid4().hex[:8]}", vendor=vendor, brand=world.brands[0],
            season=season, destination_store=world.sites[0], created_by=user,
        )
        row = StockLedgerEntry.objects.create(
            store=world.sites[0], gstin=world.sites[0].gstin,
            sku_code=uuid.uuid4().hex, brand="Historical label",
            qty=2, amount=1234, kind=StockLedgerEntry.Kind.PT_INWARD,
            doc_number=f"PT-{uuid.uuid4().hex[:8]}", booking=booking,
        )
        reviewed = fingerprint(row)
        before = (row.brand, row.qty, int(row.amount), row.doc_number)
        response = apply_rows(user, session, [{"id": row.pk, "brand_id": world.brands[0].pk,
                                              "fingerprint": reviewed}], resource="stockledger.stockledgerentry")
        assert response.status_code == 200, response.data
        row.refresh_from_db()
        assert (row.brand, row.qty, int(row.amount), row.doc_number) == before
        assert BrandIdentityBinding.objects.filter(
            tenant=world.tenant, resource_model="stockledger.stockledgerentry",
            resource_id=row.pk, brand=world.brands[0], source_fingerprint=reviewed,
        ).exists()
        booking.brand = world.brands[1]
        booking.save(update_fields=["brand"])
        from masters.brand_identity import identity_id

        assert identity_id(row, world.tenant.pk) is None


@pytest.mark.parametrize("role,shows_value", [
    ("owner", True), ("accounts", True), ("brand_manager", True),
    ("warehouse", False), ("store_person", False), ("it_admin", False),
])
def test_stock_values_follow_action_and_fields_on_one_assignment(
    world: TenantWorld, role: str, shows_value: bool,
) -> None:
    from stockledger.views import StockOnHandView

    with tenant_context(world.tenant.pk):
        stock(world, brand=world.brands[0].pk)
        user, human = _person(world, "stock-reader")
        _assign(world, human, role, sites=(world.sites[0],), brands=(world.brands[0],))
        request = APIRequestFactory().get("/api/stock/on-hand")
        force_authenticate(request, user=user, token=issue_session(user).session)
        response = StockOnHandView.as_view()(request)
        assert response.status_code == 200, response.data
        assert len(response.data["rows"]) == 1
        assert ("net_value_paise" in response.data["rows"][0]) is shows_value
        assert ("value_paise" in response.data["summary"]) is shows_value


def test_cost_assignment_cannot_lend_fields_to_operational_assignment(world: TenantWorld) -> None:
    from stockledger.views import StockOnHandView

    with tenant_context(world.tenant.pk):
        stock(world, brand=world.brands[0].pk)
        user, human = _person(world, "mixed-cost")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        _assign(world, human, "owner", sites=(world.sites[1],), all_brands=True)
        request = APIRequestFactory().get("/api/stock/on-hand?basis=cost")
        force_authenticate(request, user=user, token=issue_session(user).session)
        response = StockOnHandView.as_view()(request)
        assert response.status_code == 403, response.data
        assert response.data["code"] == "FIELD_DENIED"
