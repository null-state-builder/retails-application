"""Alert titles obey tenant, stable brand, whole-site and protected-field scope."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.sessions import issue_session
from alerts.models import Alert, AlertKind, AlertSeen, AlertStatus
from alerts.views import AlertHistoryView, AlertInboxView, AlertSeenView
from core.commands import database_now
from core.tenancy import tenant_context
from tests.test_so03_denials import TenantWorld, _assign, _person, _tenant_world


@pytest.fixture
def worlds(db: None) -> tuple[TenantWorld, TenantWorld]:
    return _tenant_world("alert-a"), _tenant_world("alert-b")


def alert(world: TenantWorld, kind: str, *, site: int = 0, brand: int | None = None,
          label: str = "", historical: bool = False) -> Alert:
    return Alert.objects.create(
        kind=kind, kind_label=kind, title=f"Private details {uuid.uuid4().hex}",
        dedupe_key=uuid.uuid4().hex, store=world.sites[site], brand=label,
        brand_ref_id=brand, status=AlertStatus.RESOLVED if historical else AlertStatus.OPEN,
        resolved_at=timezone.now() if historical else None,
    )


def visible(user: Any, *, historical: bool = False) -> set[int]:
    session = issue_session(user).session
    request = APIRequestFactory().get("/api/alerts/history" if historical else "/api/alerts")
    force_authenticate(request, user, session)
    response = (AlertHistoryView if historical else AlertInboxView).as_view()(request)
    assert response.status_code == 200, response.data
    return {row["id"] for row in response.data}


@pytest.mark.parametrize("historical", [False, True])
def test_alert_brand_labels_never_authorize_or_exchange_assignment_scope(
    worlds: tuple[TenantWorld, TenantWorld], historical: bool,
) -> None:
    world, foreign = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "brand-alert-reader")
        _assign(world, human, "brand_manager", sites=(world.sites[0],), brands=(world.brands[0],))
        _assign(world, human, "brand_manager", sites=(world.sites[1],), brands=(world.brands[1],))
        world.brands[1].name = world.brands[0].name
        world.brands[1].save(update_fields=["name"])
        own = alert(world, AlertKind.STOCK_AGEING, brand=world.brands[0].pk,
                    label=world.brands[0].name, historical=historical)
        alert(world, AlertKind.STOCK_AGEING, site=1, brand=world.brands[0].pk,
              label=world.brands[0].name, historical=historical)
        alert(world, AlertKind.STOCK_AGEING, label=world.brands[0].name, historical=historical)
        alert(foreign, AlertKind.STOCK_AGEING, brand=foreign.brands[0].pk,
              label=world.brands[0].name, historical=historical)
        assert visible(user, historical=historical) == {own.pk}
        world.brands[0].name = "Renamed alert brand"
        world.brands[0].save(update_fields=["name"])
        assert visible(user, historical=historical) == {own.pk}


def test_whole_site_alerts_require_all_brands_and_protected_fields_from_same_assignment(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    world, foreign = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "mixed-alert-reader")
        _assign(world, human, "it_admin", sites=(world.sites[0],), all_brands=True)
        _assign(world, human, "owner", sites=(world.sites[1],), all_brands=True)
        operational = alert(world, AlertKind.COUNT_DUE)
        alert(world, AlertKind.CASH_VARIANCE)
        alert(world, AlertKind.RESERVATION_EXPIRY)
        financial = alert(world, AlertKind.CASH_VARIANCE, site=1)
        alert(foreign, AlertKind.COUNT_DUE)
        assert visible(user) == {operational.pk, financial.pk}

        selected, selected_human = _person(world, "selected-alert-reader")
        _assign(world, selected_human, "owner", sites=(world.sites[0],), brands=(world.brands[0],))
        assert visible(selected) == set()

        store_user, store_human = _person(world, "store-alert-reader")
        _assign(world, store_human, "store_person", sites=(world.sites[0],), all_brands=True)
        reservation = Alert.objects.get(kind=AlertKind.RESERVATION_EXPIRY, store=world.sites[0])
        assert visible(store_user) == {operational.pk, reservation.pk}


def test_financial_alert_cannot_use_role_rung_when_workflow_action_is_disabled(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from accounts import unified_policy
    from accounts.unified_policy import ACTION_LEVELS

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "disabled-money-alert")
        _assign(world, human, "owner", all_sites=True, all_brands=True)
        alert(world, AlertKind.SOR_AGEING)
        monkeypatch.setattr(unified_policy, "workflow_levels", lambda _tenant: {
            **ACTION_LEVELS, "section.money.manage": ("money", "none"),
        })
        assert visible(user) == set()


def test_alert_job_only_changes_its_tenant_and_preserves_established_brand_identity(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    from alerts.checks import AlertHit, sync_kind
    from core.refusals import Refusal

    world, foreign = worlds
    with tenant_context(world.tenant.pk):
        other = alert(foreign, AlertKind.RETURN_WINDOW, brand=foreign.brands[0].pk,
                      label=foreign.brands[0].name)
        hit = AlertHit(
            dedupe_key=uuid.uuid4().hex, title="Return to the known brand",
            store_id=world.sites[0].pk, brand=world.brands[0].name,
            object_id=None, due_date=None, threshold_days=7, brand_id=world.brands[0].pk,
        )
        sync_kind(AlertKind.RETURN_WINDOW, "Return window", [hit])
        row = Alert.objects.get(dedupe_key=hit.dedupe_key)
        assert row.brand_ref_id == world.brands[0].pk
        other.refresh_from_db()
        assert other.status == AlertStatus.OPEN
        # Re-running a source without a new mapping must not clear a reviewed ID.
        unlinked_hit = AlertHit(
            dedupe_key=hit.dedupe_key, title=hit.title, store_id=hit.store_id, brand=hit.brand,
            object_id=None, due_date=None, threshold_days=7,
        )
        sync_kind(AlertKind.RETURN_WINDOW, "Return window", [unlinked_hit])
        row.refresh_from_db()
        assert row.brand_ref_id == world.brands[0].pk
        foreign_hit = AlertHit(
            dedupe_key=uuid.uuid4().hex, title="Foreign source", store_id=foreign.sites[0].pk,
            brand=foreign.brands[0].name, object_id=None, due_date=None, threshold_days=7,
            brand_id=foreign.brands[0].pk,
        )
        with pytest.raises(Refusal):
            sync_kind(AlertKind.RETURN_WINDOW, "Return window", [foreign_hit])
        row.refresh_from_db()
        assert row.status == AlertStatus.OPEN
        assert not Alert.objects.filter(dedupe_key=foreign_hit.dedupe_key).exists()


@pytest.mark.parametrize("withdrawal", ["session", "assignment"])
def test_seen_cursor_rolls_back_if_authority_ends_during_write(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch, withdrawal: str,
) -> None:
    from datetime import timedelta

    from accounts.goods_models import RoleAssignment, ServerSession

    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "seen-cursor-reader")
        assignment = _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        session = issue_session(user).session
        request = APIRequestFactory().post("/api/alerts/seen", {}, format="json")
        force_authenticate(request, user, session)
        original = AlertSeen.objects.update_or_create

        def withdraw(**values: Any) -> Any:
            row = original(**values)
            if withdrawal == "session":
                ServerSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
            else:
                RoleAssignment.objects.filter(pk=assignment.pk).update(revoked_at=database_now())
            return row

        monkeypatch.setattr(AlertSeen.objects, "update_or_create", withdraw)
        response = AlertSeenView.as_view()(request)
        assert response.status_code in {401, 403}, response.data
        assert not AlertSeen.objects.filter(user=user).exists()


def test_seen_cursor_requires_a_live_session_and_keeps_other_people_unchanged(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    world, foreign = worlds
    with tenant_context(world.tenant.pk):
        user, human = _person(world, "seen-owner")
        _assign(world, human, "store_person", sites=(world.sites[0],), all_brands=True)
        foreign_user, _ = _person(foreign, "foreign-seen-owner")
        other = AlertSeen.objects.create(user=foreign_user, seen_at=timezone.now())
        request = APIRequestFactory().post("/api/alerts/seen", {}, format="json")
        force_authenticate(request, user)
        response = AlertSeenView.as_view()(request)
        assert response.status_code == 401, response.data
        assert not AlertSeen.objects.filter(user=user).exists()
        request = APIRequestFactory().post("/api/alerts/seen", {}, format="json")
        force_authenticate(request, user, issue_session(user).session)
        response = AlertSeenView.as_view()(request)
        assert response.status_code == 200, response.data
        assert AlertSeen.objects.filter(user=user).exists()
        other.refresh_from_db()
        assert other.seen_at is not None
