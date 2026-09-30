"""Whole-store cash totals and their request-bound delivery authority."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_models import RoleAssignment, ServerSession
from accounts.sessions import issue_session, revoke_session
from core.commands import database_now
from core.tenancy import tenant_context
from storefront import views
from storefront.cash_summary import build as build_cash_summary
from tests.test_so03_denials import TenantWorld, _assign, _person
from tests.test_so03_denials import worlds as worlds


def _request(user: Any, store: Any) -> tuple[Any, ServerSession]:
    session = issue_session(user).session
    request = APIRequestFactory().get("/api/store/cash-summary", {"store": store.code})
    force_authenticate(request, user, session)
    return request, session


def test_cash_summary_refuses_partial_brand_authority_before_aggregation(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        actor, human = _person(world, "cash-summary-scoped")
        _assign(world, human, "owner", sites=(world.sites[0],), brands=(world.brands[0],))
        # A second broad assignment at a different store cannot lend its scope.
        _assign(world, human, "owner", sites=(world.sites[1],), all_brands=True)

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Denied totals must not be calculated.")

        monkeypatch.setattr(views, "build_cash_summary", forbidden)
        request, _ = _request(actor, world.sites[0])
        response = views.CashSummaryView.as_view()(request)
        assert response.status_code == 403, response.data
        assert response.data["code"] == "SCOPE_DENIED"
        assert "modes" not in response.data


@pytest.mark.parametrize("boundary", ["logout", "expiry", "revoked", "scope", "money_policy"])
def test_cash_summary_rechecks_after_calculating_the_days_totals(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        actor, human = _person(world, "cash-summary-delivery")
        assignment = _assign(world, human, "owner", sites=(world.sites[0],), all_brands=True)
        request, _ = _request(actor, world.sites[0])
        allowed = views.CashSummaryView.as_view()(request)
        assert allowed.status_code == 200, allowed.data
        assert allowed.data["bills"] == 0 and allowed.data["modes"]["cash"] == 0
        assert allowed["Cache-Control"] == "no-store, private"
        request, session = _request(actor, world.sites[0])
        original = build_cash_summary

        def withdraw(*args: Any, **kwargs: Any) -> Any:
            body = original(*args, **kwargs)
            if boundary == "logout":
                revoke_session(session)
            elif boundary == "expiry":
                ServerSession.objects.filter(pk=session.pk).update(
                    expires_at=timezone.now() - timedelta(seconds=1),
                )
            elif boundary == "revoked":
                RoleAssignment.objects.filter(pk=assignment.pk).update(revoked_at=database_now())
            elif boundary == "scope":
                RoleAssignment.objects.filter(pk=assignment.pk).update(site_ids=[world.sites[1].pk])
            else:
                role = world.roles["owner"]
                role.section_access = {**role.section_access, "money": {"capability": "none", "label": "Withdrawn"}}
                role.save(update_fields=["section_access"])
            return body

        monkeypatch.setattr(views, "build_cash_summary", withdraw)
        response = views.CashSummaryView.as_view()(request)
        expected = {"logout": (401, "AUTH_REQUIRED"), "expiry": (401, "SESSION_EXPIRED"),
                    "revoked": (403, "ACTION_DENIED"), "scope": (404, "NOT_FOUND"),
                    "money_policy": (403, "ACTION_DENIED")}
        assert (response.status_code, response.data["code"]) == expected[boundary]
        assert "modes" not in response.data and "bills" not in response.data
