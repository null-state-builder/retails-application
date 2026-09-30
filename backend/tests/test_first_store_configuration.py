"""Disposable-proof regressions for first-store configuration and live delivery.

Configuration fixtures use the canonical schema, immutable evidence recorder and
effective-period services. Requests retain real session guards through delivery.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.goods_models import HumanIdentity, ServerSession
from accounts.models import User
from accounts.sessions import issue_session, revoke_session
from core.canonical import content_hash
from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.kernel_models import SubjectRevision
from core.refusals import Refusal
from core.tenancy import tenant_context
from masters import goods_views
from masters.goods_config import activate, normalise_scope, withdraw
from masters.goods_identity_services import vocabulary_value_id
from masters.goods_models import ConfigDraft, ConfigVersion, MasterVersion, Sbu, SiteGuard
from masters.goods_services import ensure_site_sbus, readiness_dto, start_revision, validate_config_payload
from masters.goods_views import GoodsSiteDetailView, GoodsSiteReadinessView, GoodsTenantView
from tests.test_so03_denials import TenantWorld, _assign, _person
from tests.test_so03_denials import worlds as worlds


def _request(user: User, method: str, body: dict[str, Any] | None = None) -> tuple[Any, ServerSession]:
    session = issue_session(user).session
    session.step_up_at = timezone.now()
    session.save(update_fields=["step_up_at"])
    request = getattr(APIRequestFactory(), method)("/first-store-proof", body or {}, format="json")
    force_authenticate(request, user, session)
    return request, session


def _owner(world: TenantWorld, label: str) -> tuple[User, HumanIdentity]:
    user, human = _person(world, label)
    _assign(world, human, "owner", all_sites=True, all_brands=True)
    return user, human


def _version(
    world: TenantWorld,
    human: HumanIdentity,
    kind: str,
    payload: dict[str, Any],
    *,
    starts: datetime | None = None,
    historical_site_scope: bool = False,
) -> ConfigVersion:
    """Record a schema-valid disposable fixture, never seed or real tenant data.

    The one historical site-scoped business-profile fixture is deliberately
    incompatible with today's tenant-wide rule; its payload is still validated.
    It represents retained pre-cutover evidence that cannot bind the tenant.
    """
    holder: dict[str, ConfigVersion] = {}
    scope = normalise_scope(
        {"scope_kind": "sites", "site_ids": [world.sites[0].pk]}
        if historical_site_scope else {"scope_kind": "tenant"}
    )

    def handler(run: CommandRun) -> CommandResult:
        effective_from = starts or run.now
        scope_key = content_hash(scope)
        validate_config_payload(
            kind, payload, tenant_id=run.tenant_id, scope_key=scope_key,
            as_of=effective_from, scope=None if historical_site_scope else scope,
        )
        draft = ConfigDraft.objects.create(
            tenant_id=run.tenant_id, kind=kind, scope=scope, scope_key=scope_key,
            payload=payload, effective_from=effective_from, maker=human,
            state=ConfigDraft.State.APPROVED,
        )
        version = run.record(ConfigVersion(
            draft=draft, kind=kind,
            version=ConfigVersion.objects.filter(tenant_id=run.tenant_id, kind=kind).count() + 1,
            scope=scope, scope_key=scope_key, payload=payload,
            effective_from=effective_from, approved_by=human,
            source_revision=draft.revision, source_hash=content_hash(payload),
        ))
        activate(run, version, effective_to=None)
        holder["version"] = version
        return CommandResult(resource_type="configuration", resource_id=str(version.pk))

    execute_command(
        Principal(tenant_id=world.tenant.pk, service_code="proof.configuration"),
        CommandSpec(action="proof.configuration", command_id=uuid.uuid4(), business_input={"kind": kind}),
        handler,
    )
    return holder["version"]


def _business_profile(
    world: TenantWorld, human: HumanIdentity, *, starts: datetime | None = None,
    historical_site_scope: bool = False,
) -> ConfigVersion:
    _version(world, human, "vocabulary", {
        "dimension": "size", "values": [{"value_key": "one", "label": "One", "sort_order": 0,
                                           "retired": False}],
        "effective_from": timezone.now().isoformat(),
    })
    identity = _version(world, human, "identity_profile", {
        "family": "proof", "distinguishing_dimensions": ["size"], "size_dimension": "size",
        "allowed_size_values": [str(vocabulary_value_id("size", "one"))],
        "allowed_colour_values": [], "allowed_grade_values": [],
    })
    return _version(world, human, "business_profile", {
        "categories": ["apparel"], "identity_profile_id": str(identity.pk), "expected_skus": 10,
        "brands": 2, "sites": 2, "sbus": 4, "staff": 3, "documents_per_day": 10,
        "evidence_bytes_per_year": 1000, "commercial_labels": ["Proof"],
        "workforce_scope": "Disposable proof team",
        "tills_per_site": [{"site_id": str(world.sites[0].pk), "count": 1}],
        "accounting_interface": "Proof export",
    }, starts=starts, historical_site_scope=historical_site_scope)


def _tenant_body(world: TenantWorld, profile_id: uuid.UUID) -> dict[str, Any]:
    return {
        "command_id": str(uuid.uuid4()), "contract_version": "goods-v1",
        "expected_revision": world.tenant.revision, "code": world.tenant.code,
        "name": "Reviewed proof company", "timezone": "Asia/Kolkata", "currency": "INR",
        "locale": "en-IN", "business_profile_version_id": str(profile_id),
    }


def _tenant_state(world: TenantWorld) -> tuple[Any, ...]:
    world.tenant.refresh_from_db()
    return (
        world.tenant.name, world.tenant.timezone, world.tenant.currency, world.tenant.locale,
        world.tenant.business_profile_version_id, world.tenant.revision,
    )


def test_tenant_binds_current_approved_business_profile_and_replay_does_not_revise(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, human = _owner(world, "profile-owner")
        profile = _business_profile(world, human)
        body = _tenant_body(world, profile.pk)
        request, _ = _request(user, "patch", body)
        response = GoodsTenantView.as_view()(request)
        assert response.status_code == 200
        assert response.data["data"]["business_profile_version_id"] == str(profile.pk)
        assert _tenant_state(world)[-2:] == (profile.pk, 2)
        request, _ = _request(user, "patch", body)
        assert GoodsTenantView.as_view()(request).status_code == 200
        assert _tenant_state(world)[-2:] == (profile.pk, 2)


@pytest.mark.parametrize("invalid", ["foreign", "wrong_kind", "future", "withdrawn", "site_scope", "unapproved"])
def test_tenant_refuses_ineligible_profile_without_mutating_company(
    worlds: tuple[TenantWorld, TenantWorld], invalid: str,
) -> None:
    world, other = worlds
    with tenant_context(world.tenant.pk):
        user, human = _owner(world, f"profile-{invalid}")
    if invalid == "foreign":
        with tenant_context(other.tenant.pk):
            _, other_human = _owner(other, "foreign-profile")
            profile_id = _business_profile(other, other_human).pk
    else:
        with tenant_context(world.tenant.pk):
            if invalid == "wrong_kind":
                profile_id = _version(world, human, "rates", {
                    "transport_pct": "0.00", "pricing_margin_pct": "10.00",
                }).pk
            else:
                version = _business_profile(
                    world, human,
                    starts=timezone.now() + timedelta(days=1) if invalid == "future" else None,
                    historical_site_scope=invalid == "site_scope",
                )
                profile_id = version.pk
                if invalid == "unapproved":
                    draft = ConfigDraft.objects.create(
                        tenant=world.tenant, kind=version.kind, scope=version.scope,
                        scope_key=version.scope_key, payload=version.payload,
                        effective_from=timezone.now(), maker=human,
                    )
                    assert draft.state == ConfigDraft.State.DRAFT
                    profile_id = draft.pk
                if invalid == "withdrawn":
                    def handler(run: CommandRun) -> CommandResult:
                        withdraw(run, version, reason_code="PROOF_INVALID")
                        return CommandResult(resource_type="configuration", resource_id=str(version.pk))

                    execute_command(
                        Principal(tenant_id=world.tenant.pk, service_code="proof.configuration"),
                        CommandSpec(action="proof.withdraw", command_id=uuid.uuid4(), business_input={"version": str(version.pk)}),
                        handler,
                    )
    with tenant_context(world.tenant.pk):
        before = _tenant_state(world)
        request, _ = _request(user, "patch", _tenant_body(world, profile_id))
        response = GoodsTenantView.as_view()(request)
        assert response.status_code == 422 and response.data["code"] == "CONFIG_INVALID"
        assert _tenant_state(world) == before


def test_add_site_brands_is_repeatable_and_keeps_retired_units(
    worlds: tuple[TenantWorld, TenantWorld],
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, _ = _owner(world, "site-brand-owner")
        site = world.sites[0]
        retired_at = timezone.now() - timedelta(days=1)
        retired = Sbu.objects.create(tenant=world.tenant, site=site, brand=world.brands[0],
                                     code="RETAINED", retired_at=retired_at, revision=3)
        start_revision(world.tenant.pk, "master:site", str(site.pk))
        body = {"contract_version": "goods-v1", "brand_ids": [brand.pk for brand in world.brands]}
        for revision in (1, 2):
            request, _ = _request(user, "patch", {**body, "command_id": str(uuid.uuid4()),
                                                  "expected_revision": revision})
            response = GoodsSiteDetailView.as_view()(request, pk=site.pk)
            assert response.status_code == 200
            assert response.data["revision"] == revision + 1
            assert Sbu.objects.filter(site=site).count() == 3
        retired.refresh_from_db()
        assert retired.retired_at == retired_at and retired.revision == 3 and retired.code == "RETAINED"
        ids = set(Sbu.objects.filter(site=site).values_list("pk", flat=True))
        assert ensure_site_sbus(world.tenant.pk, site, [world.brands[0].pk, world.brands[0].pk, world.brands[1].pk]) == []
        assert set(Sbu.objects.filter(site=site).values_list("pk", flat=True)) == ids


@pytest.mark.parametrize("invalid", ["foreign", "inactive"])
def test_add_site_brands_refuses_entire_replacement_before_any_change(
    worlds: tuple[TenantWorld, TenantWorld], invalid: str,
) -> None:
    world, other = worlds
    with tenant_context(world.tenant.pk):
        user, _ = _owner(world, f"site-{invalid}")
        site = world.sites[0]
        start_revision(world.tenant.pk, "master:site", str(site.pk))
        invalid_brand = other.brands[0] if invalid == "foreign" else world.brands[1]
        if invalid == "inactive":
            invalid_brand.is_active = False
            invalid_brand.save(update_fields=["is_active"])
        before = (site.name, Sbu.objects.count(), MasterVersion.objects.count())
        request, _ = _request(user, "patch", {
            "command_id": str(uuid.uuid4()), "contract_version": "goods-v1", "expected_revision": 1,
            "name": "Must not persist", "brand_ids": [world.brands[0].pk, invalid_brand.pk],
        })
        response = GoodsSiteDetailView.as_view()(request, pk=site.pk)
        assert response.status_code == 409 and response.data["code"] == "MASTER_INVALID"
        site.refresh_from_db()
        assert (site.name, Sbu.objects.count(), MasterVersion.objects.count()) == before
        assert SubjectRevision.objects.get(family="master:site", subject_key=str(site.pk)).revision == 1
        with pytest.raises(Refusal) as caught:
            ensure_site_sbus(world.tenant.pk, other.sites[0], [world.brands[0].pk])
        assert caught.value.code == "MASTER_INVALID" and Sbu.objects.count() == before[1]


@pytest.mark.parametrize("boundary", ["logout", "expiry"])
def test_readiness_rechecks_live_session_before_delivering_resource(
    worlds: tuple[TenantWorld, TenantWorld], monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    world, _ = worlds
    with tenant_context(world.tenant.pk):
        user, _ = _owner(world, f"readiness-{boundary}")
        site = world.sites[0]
        SiteGuard.objects.create(tenant=world.tenant, site=site, selling_mode="online_alpha")
        request, _ = _request(user, "get")
        allowed = GoodsSiteReadinessView.as_view()(request, pk=site.pk)
        assert allowed.status_code == 200 and allowed.data["data"]["site_id"] == str(site.pk)
        assert allowed["Cache-Control"] == "no-store, private"
        request, session = _request(user, "get")
        original = readiness_dto

        def delayed_data(*args: Any, **kwargs: Any) -> dict[str, Any]:
            data = original(*args, **kwargs)
            if boundary == "logout":
                revoke_session(session)
            else:
                ServerSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
            return data

        monkeypatch.setattr(goods_views, "readiness_dto", delayed_data)
        denied = GoodsSiteReadinessView.as_view()(request, pk=site.pk)
        assert denied.status_code == 401
        assert denied.data["code"] == ("AUTH_REQUIRED" if boundary == "logout" else "SESSION_EXPIRED")
        assert "data" not in denied.data and "selling_checks" not in denied.data
