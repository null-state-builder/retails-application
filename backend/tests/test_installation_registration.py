"""Real blank installation genesis, tested only through the owned proof wrapper."""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from django.contrib.auth.hashers import check_password
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from rest_framework.test import APIClient

from accounts.authentication import enforce_password_change_restriction
from accounts.change_password import change_own_password
from accounts.goods_models import RoleAssignment
from accounts.goods_setup import bootstrap_deployment
from accounts.models import LoginAttempt, Role, User
from accounts.principal import AccessContext, effective_grants
from accounts.registration_models import InstallationRegistration
from accounts.registration_serializers import RegistrationEditInput, RegistrationInput
from accounts.registration_services import (
    confirm_registration,
    public_state,
    stage_registration,
)
from accounts.unified_policy import ACTION_LEVELS, WORKFLOW_TARGET_KEY, initial_step_actions
from core.kernel_models import AuditEvent, CommandOutcome
from core.refusals import Refusal
from core.tenancy import tenant_context
from masters.goods_models import ConfigVersion, MasterVersion, SiteGuard, Tenant
from masters.models import Season, Store

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def allow_disposable_test_flush(django_db_blocker: Any) -> None:
    """Use the documented harness-only hatch on the named disposable test DB.

    This session setting exists solely for TransactionTestCase's teardown.
    Django closes that connection after each test; no runtime or migration
    setting, ordinary write guard, or retained database is changed.
    """
    with django_db_blocker.unblock():
        if (
            os.environ.get("KDPS_PROOF_MODE") != "1"
            or connection.settings_dict["NAME"] != "kdps_proof_test"
        ):
            raise AssertionError(
                "Transactional registration tests require the disposable proof test database."
            )
        with connection.cursor() as cursor:
            cursor.execute("SET kdps.allow_truncate = 'on'")


@pytest.fixture
def proposal(settings: Any) -> dict[str, Any]:
    settings.KDPS_DEPLOYMENT_KEY = str(uuid.uuid4())
    # Fast hashing is confined to disposable proof-test data. The production
    # service always uses Django's configured password hasher.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    payload = {
        "command_id": uuid.uuid4(),
        "company": {
            "code": "ALPHA",
            "name": "Alpha proof company",
            "legal_name": "Alpha Proof Private Limited",
            "pan": "ABCDE1234F",
            "gstin": "29ABCDE1234F1Z5",
            "state_code": "29",
            "state_name": "Karnataka",
            "billing_address": "Proof-only billing address",
        },
        "store": {
            "code": "FIRST",
            "name": "First proof shop",
            "city": "Bengaluru",
            "address": "Proof-only store address",
            "setup_kind": "existing",
            "source_system": "Prior software",
        },
        "owner": {
            "name": "First Owner",
            "email": "owner@alpha.example.test",
            "staff_code": "OWNER",
            "temporary_password": uuid.uuid4().hex,
        },
        "admin": {
            "name": "Separate Admin",
            "email": "admin@alpha.example.test",
            "staff_code": "ADMIN",
            "temporary_password": uuid.uuid4().hex,
        },
        "proposed_team": [
            {
                "name": "Proposed Manager",
                "email": "manager@alpha.example.test",
                "staff_code": "MANAGER",
                "role_code": "store_person",
            }
        ],
    }
    parsed = RegistrationInput(data=payload)
    if not parsed.is_valid():
        raise AssertionError("The non-sensitive proof registration fixture is invalid.")
    return dict(parsed.validated_data)


def credentials(
    proposal: dict[str, Any], person: str, summary_hash: str | None = None
) -> dict[str, Any]:
    data = {
        "email": proposal[person]["email"],
        "temporary_password": proposal[person]["temporary_password"],
    }
    if summary_hash is not None:
        data.update(summary_hash=summary_hash, acknowledged=True)
    return data


def register(proposal: dict[str, Any]) -> dict[str, Any]:
    staged = stage_registration(proposal)
    confirm_registration(credentials(proposal, "owner", staged["summary_hash"]))
    return confirm_registration(credentials(proposal, "admin", staged["summary_hash"]))


def assert_no_credentials(value: Any, proposal: dict[str, Any]) -> None:
    encoded = json.dumps(value, default=str)
    for person in ("owner", "admin"):
        if proposal[person]["temporary_password"] in encoded:
            raise AssertionError(
                "Credentials appeared in registration output or evidence."
            )
    if "password_hash" in encoded or '"temporary_password"' in encoded:
        raise AssertionError(
            "Private password fields appeared in registration output or evidence."
        )


def test_staging_is_private_and_creates_no_tenant_or_authority(
    proposal: dict[str, Any],
) -> None:
    staged = stage_registration(proposal)
    row = InstallationRegistration.objects.get()
    assert staged["state"] == "awaiting_confirmation"
    assert (
        Tenant.objects.count()
        == User.objects.count()
        == RoleAssignment.objects.count()
        == 0
    )
    assert check_password(
        proposal["owner"]["temporary_password"], row.owner_password_hash
    )
    assert check_password(
        proposal["admin"]["temporary_password"], row.admin_password_hash
    )
    assert_no_credentials(staged, proposal)
    assert_no_credentials(row.summary, proposal)
    assert public_state()["pending_confirmation"] is True
    assert stage_registration(proposal) == staged
    changed = copy.deepcopy(proposal)
    changed["store"]["name"] = "Changed proof store"
    with pytest.raises(Refusal, match="different information") as refusal:
        stage_registration(changed)
    assert refusal.value.code == "COMMAND_CONFLICT"


@pytest.mark.parametrize("password", ["1", "1234", "password", "owner", " "])
def test_temporary_password_has_no_strength_requirements(
    proposal: dict[str, Any], password: str,
) -> None:
    proposal["owner"]["temporary_password"] = password
    serializer = RegistrationInput(data=proposal)
    assert serializer.is_valid(), serializer.errors


def test_empty_and_shared_temporary_passwords_remain_invalid(
    proposal: dict[str, Any],
) -> None:
    proposal["owner"]["temporary_password"] = ""
    serializer = RegistrationInput(data=proposal)
    assert not serializer.is_valid()
    assert "temporary_password" in serializer.errors["owner"]
    proposal["owner"]["temporary_password"] = "1"
    proposal["admin"]["temporary_password"] = "1"
    assert not RegistrationInput(data=proposal).is_valid()


def test_basic_temporary_passwords_support_joint_confirmation(
    proposal: dict[str, Any],
) -> None:
    proposal["owner"]["temporary_password"] = "1"
    proposal["admin"]["temporary_password"] = "2"
    serializer = RegistrationInput(data=proposal)
    serializer.is_valid(raise_exception=True)
    result = register(serializer.validated_data)
    assert result["state"] == "registered"
    with tenant_context(Tenant.objects.get().pk):
        for who in ("owner", "admin"):
            user = User.objects.get(email=proposal[who]["email"])
            assert user.check_password(proposal[who]["temporary_password"])
            assert user.must_change_password
            with pytest.raises(Refusal) as refusal:
                change_own_password(
                    user=user, session=None,
                    current_password=proposal[who]["temporary_password"],
                    new_password="password",
                )
            assert refusal.value.code == "INVALID_REQUEST"
            user.refresh_from_db()
            assert user.must_change_password
            assert user.check_password(proposal[who]["temporary_password"])


def test_joint_confirmation_requires_both_individual_credentials_and_exact_summary(
    proposal: dict[str, Any],
) -> None:
    staged = stage_registration(proposal)
    inspected = confirm_registration(credentials(proposal, "owner"))
    assert inspected["confirming_role"] == "owner"
    assert inspected["confirmed"] == {"owner": False, "admin": False}
    invalid = credentials(proposal, "admin", staged["summary_hash"])
    invalid["temporary_password"] = uuid.uuid4().hex
    with pytest.raises(Refusal) as refusal:
        confirm_registration(invalid)
    assert refusal.value.code == "INVALID_CREDENTIALS"
    assert LoginAttempt.objects.filter(failures=1).exists()
    stale = credentials(proposal, "owner", "0" * 64)
    with pytest.raises(Refusal) as refusal:
        confirm_registration(stale)
    assert refusal.value.code == "REVISION_SUPERSEDED"
    missing_ack = credentials(proposal, "owner", staged["summary_hash"])
    missing_ack.pop("acknowledged")
    with pytest.raises(Refusal) as refusal:
        confirm_registration(missing_ack)
    assert refusal.value.code == "INVALID_REQUEST"
    owner = confirm_registration(credentials(proposal, "owner", staged["summary_hash"]))
    assert owner["confirmed"] == {"owner": True, "admin": False}
    assert (
        confirm_registration(credentials(proposal, "owner", staged["summary_hash"]))
        == owner
    )
    assert Tenant.objects.count() == 0
    row = InstallationRegistration.objects.get()
    assert len(row.confirmation_history) == 1


def test_atomic_joint_claim_has_complete_versions_and_only_two_restricted_logins(
    proposal: dict[str, Any],
) -> None:
    result = register(proposal)
    tenant = Tenant.objects.get()
    row = InstallationRegistration.objects.get()
    assert result["state"] == "registered"
    assert tenant.synthetic is False
    assert row.owner_password_hash == row.admin_password_hash == ""
    assert row.tenant_id == tenant.pk
    assert User.objects.count() == 2
    assert User.objects.filter(email=proposal["proposed_team"][0]["email"]).count() == 0
    with tenant_context(tenant.pk):
        roles = list(Role.objects.filter(tenant=tenant))
        assert {role.code for role in roles} == {
            "owner",
            "it_admin",
            "warehouse",
            "store_person",
            "brand_manager",
            "accounts",
        }
        for role in roles:
            confirmed_policy = result["summary"]["initial_access"]["policy_baseline"]["roles"][role.code]
            assert role.section_access == confirmed_policy["section_access"]
            assert role.field_access == confirmed_policy["field_access"]
            assert role.permissions_map["step_actions"] == initial_step_actions(
                role.code
            )
            version = MasterVersion.objects.get(
                tenant=tenant, kind="role", target_key=role.code
            )
            assert version.payload["role_policy"][
                "step_actions"
            ] == initial_step_actions(role.code)
        assert (
            MasterVersion.objects.filter(
                tenant=tenant, kind="tenant", target_key=WORKFLOW_TARGET_KEY
            ).count()
            == 1
        )
        assert ConfigVersion.objects.filter(tenant=tenant).count() == 0
        owner, admin = [
            User.objects.get(email=proposal[who]["email"]) for who in ("owner", "admin")
        ]
        assert owner.human_id != admin.human_id
        for who, user, code in (
            ("owner", owner, "owner"),
            ("admin", admin, "it_admin"),
        ):
            assert user.must_change_password
            assert user.role_id is None
            assert user.check_password(proposal[who]["temporary_password"])
            assigned = RoleAssignment.objects.get(tenant=tenant, human_id=user.human_id)
            assert assigned.role.code == code
            assert assigned.all_sites and assigned.all_brands
            access = AccessContext(
                user=user,
                tenant_id=tenant.pk,
                human_id=user.human_id,
                session=None,
                grants=effective_grants(user.human_id),
            )
            assert access.can("access.manage")
            if code == "it_admin":
                assert not any(
                    "cost" in grant.fields or "margin" in grant.fields
                    for grant in access.grants
                )
        guard = SiteGuard.objects.get(tenant=tenant, site_id=result["first_store_id"])
        assert guard.lifecycle == "planned" and guard.selling_mode == "online_alpha"
        assert (
            not guard.goods_ready
            and not guard.sell_ready
            and not guard.opening_setup_ready
        )
        assert Store.objects.filter(tenant=tenant).count() == 1
        event = AuditEvent.objects.get(tenant=tenant, action="deployment.register")
        genesis = event.authority["joint_genesis"]
        assert genesis["summary_hash"] == result["summary_hash"]
        assert {
            entry["role"]
            for entry in genesis["confirmations"]
            if entry["event"] == "confirmed"
        } == {"owner", "admin"}
        assert CommandOutcome.objects.filter(tenant=tenant).count() == 1
        assert_no_credentials(event.authority, proposal)
        assert_no_credentials(event.after, proposal)
        assert_no_credentials(
            list(CommandOutcome.objects.filter(tenant=tenant).values("result")),
            proposal,
        )
    assert_no_credentials(result, proposal)
    assert public_state()["available"] is False


def test_completed_retry_is_safe_and_subsequent_company_signup_is_closed(
    proposal: dict[str, Any],
) -> None:
    result = register(proposal)
    assert stage_registration(proposal) == {"state": "registered", "login_url": "/login"}
    receipt = confirm_registration(
        credentials(proposal, "admin", result["summary_hash"])
    )
    assert receipt == {"state": "registered", "login_url": "/login"}
    second = copy.deepcopy(proposal)
    second["command_id"] = uuid.uuid4()
    with pytest.raises(Refusal) as refusal:
        stage_registration(second)
    assert refusal.value.code == "REGISTRATION_CLOSED"
    assert Tenant.objects.count() == 1
    assert User.objects.count() == 2


def test_existing_or_partial_tenant_closes_signup(proposal: dict[str, Any]) -> None:
    Tenant.objects.create(
        code="PARTIAL",
        name="Interrupted old bootstrap",
        deployment_key=uuid.uuid4(),
        timezone="Asia/Kolkata",
        currency="INR",
        locale="en-IN",
    )
    assert public_state()["available"] is False
    with pytest.raises(Refusal) as refusal:
        stage_registration(proposal)
    assert refusal.value.code == "REGISTRATION_CLOSED"
    assert InstallationRegistration.objects.count() == 0
    assert User.objects.count() == 0


def test_failed_bootstrap_rolls_back_every_business_row_and_can_retry(
    proposal: dict[str, Any], monkeypatch: Any
) -> None:
    import accounts.registration_services as services

    staged = stage_registration(proposal)
    confirm_registration(credentials(proposal, "owner", staged["summary_hash"]))
    from accounts.goods_setup import create_person as actual
    calls = 0

    def fail_second_person(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Refusal("STATE_CONFLICT", "Proof bootstrap interruption.")
        return actual(*args, **kwargs)

    monkeypatch.setattr(services, "create_person", fail_second_person)
    with pytest.raises(Refusal, match="Proof bootstrap interruption"):
        confirm_registration(credentials(proposal, "admin", staged["summary_hash"]))
    assert (
        Tenant.objects.count()
        == User.objects.count()
        == RoleAssignment.objects.count()
        == 0
    )
    row = InstallationRegistration.objects.get()
    assert row.owner_confirmed_at is not None and row.admin_confirmed_at is None
    assert row.completed_at is None and row.tenant_id is None
    monkeypatch.setattr(services, "create_person", actual)
    assert (
        confirm_registration(credentials(proposal, "admin", staged["summary_hash"]))[
            "state"
        ]
        == "registered"
    )
    assert Tenant.objects.count() == 1 and User.objects.count() == 2


def test_owner_revision_clears_both_signatures_and_stale_summary_cannot_claim(
    proposal: dict[str, Any],
) -> None:
    staged = stage_registration(proposal)
    confirm_registration(credentials(proposal, "admin", staged["summary_hash"]))
    edited = copy.deepcopy(proposal)
    edited["command_id"] = uuid.uuid4()
    edited["store"]["name"] = "Corrected first shop"
    edited["current_owner_password"] = proposal["owner"]["temporary_password"]
    revised = stage_registration(edited, edit=True)
    assert revised["revision"] == 2 and revised["confirmed"] == {
        "owner": False,
        "admin": False,
    }
    assert revised["summary_hash"] != staged["summary_hash"]
    with pytest.raises(Refusal) as refusal:
        confirm_registration(credentials(proposal, "owner", staged["summary_hash"]))
    assert refusal.value.code == "REVISION_SUPERSEDED"
    assert Tenant.objects.count() == 0
    confirm_registration(credentials(edited, "owner", revised["summary_hash"]))
    assert (
        confirm_registration(credentials(edited, "admin", revised["summary_hash"]))[
            "state"
        ]
        == "registered"
    )
    row = InstallationRegistration.objects.get()
    assert len([event for event in row.confirmation_history if event["event"] != "staff_codes_reserved"]) == 4
    assert row.summary["store"]["name"] == "Corrected first shop"


def test_owner_revision_can_change_one_password_and_keep_the_other(
    proposal: dict[str, Any],
) -> None:
    stage_registration(proposal)
    edited = copy.deepcopy(proposal)
    edited["command_id"] = str(uuid.uuid4())
    edited["current_owner_password"] = proposal["owner"]["temporary_password"]
    edited["owner"]["temporary_password"] = "new-owner-secret"
    edited["admin"]["temporary_password"] = ""
    parsed = RegistrationEditInput(data=edited)
    assert parsed.is_valid(), parsed.errors
    assert "temporary_password" not in parsed.validated_data["admin"]
    stage_registration(dict(parsed.validated_data), edit=True)
    row = InstallationRegistration.objects.get()
    assert check_password("new-owner-secret", row.owner_password_hash)
    assert check_password(proposal["admin"]["temporary_password"], row.admin_password_hash)
    # The new Owner password is the one that confirms; the old one no longer works.
    assert confirm_registration(credentials(edited, "owner"))["confirming_role"] == "owner"
    with pytest.raises(Refusal) as old:
        confirm_registration(credentials(proposal, "owner"))
    assert old.value.code == "INVALID_CREDENTIALS"
    # A kept Admin password still cannot be reused for the Owner.
    clash = copy.deepcopy(edited)
    clash["command_id"] = str(uuid.uuid4())
    clash["current_owner_password"] = "new-owner-secret"
    clash["owner"]["temporary_password"] = proposal["admin"]["temporary_password"]
    parsed = RegistrationEditInput(data=clash)
    assert parsed.is_valid(), parsed.errors
    with pytest.raises(Refusal) as refusal:
        stage_registration(dict(parsed.validated_data), edit=True)
    assert refusal.value.code == "INVALID_REQUEST"
    assert check_password("new-owner-secret", InstallationRegistration.objects.get().owner_password_hash)


def test_wrong_owner_cannot_revise_pending_genesis(proposal: dict[str, Any]) -> None:
    stage_registration(proposal)
    edited = copy.deepcopy(proposal)
    edited["command_id"] = uuid.uuid4()
    edited["current_owner_password"] = proposal["admin"]["temporary_password"]
    with pytest.raises(Refusal) as refusal:
        stage_registration(edited, edit=True)
    assert refusal.value.code == "INVALID_CREDENTIALS"
    assert InstallationRegistration.objects.get().revision == 1
    assert Tenant.objects.count() == 0


def test_pending_registration_has_bounded_lockout(proposal: dict[str, Any]) -> None:
    stage_registration(proposal)
    bad = credentials(proposal, "owner")
    bad["temporary_password"] = uuid.uuid4().hex
    for _ in range(10):
        with pytest.raises(Refusal) as refusal:
            confirm_registration(bad)
        assert refusal.value.code == "INVALID_CREDENTIALS"
    with pytest.raises(Refusal) as refusal:
        confirm_registration(credentials(proposal, "owner"))
    assert refusal.value.code == "LOGIN_LOCKED"
    assert Tenant.objects.count() == 0


def test_simultaneous_final_confirmations_create_exactly_one_company(
    proposal: dict[str, Any],
) -> None:
    staged = stage_registration(proposal)
    confirm_registration(credentials(proposal, "owner", staged["summary_hash"]))
    barrier = threading.Barrier(2)

    def claim() -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return str(
                confirm_registration(
                    credentials(proposal, "admin", staged["summary_hash"])
                )["state"]
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    assert results == ["registered", "registered"]
    tenant = Tenant.objects.get()
    assert User.objects.count() == 2
    with tenant_context(tenant.pk):
        assert RoleAssignment.objects.filter(tenant=tenant).count() == 2
        assert CommandOutcome.objects.filter(tenant=tenant).count() == 1
        assert (
            AuditEvent.objects.filter(
                tenant=tenant, action="deployment.register"
            ).count()
            == 1
        )


def test_simultaneous_different_initial_proposals_have_one_pending_winner(
    proposal: dict[str, Any],
) -> None:
    second = copy.deepcopy(proposal)
    second["command_id"] = uuid.uuid4()
    second["company"]["name"] = "Other attempted company"
    barrier = threading.Barrier(2)

    def stage(payload: dict[str, Any]) -> str:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return str(stage_registration(payload)["state"])
            except Refusal as refusal:
                return refusal.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(stage, [proposal, second]))
    assert sorted(results) == ["REGISTRATION_PENDING", "awaiting_confirmation"]
    assert InstallationRegistration.objects.count() == 1
    assert Tenant.objects.count() == 0


def test_public_registration_writes_require_csrf_and_refuse_bad_origin(
    proposal: dict[str, Any],
) -> None:
    client = APIClient()
    payload = json.loads(json.dumps(proposal, default=str))
    response = client.post("/api/auth/registration", payload, format="json")
    assert response.status_code == 403 and response.data["code"] == "CSRF_FAILED"
    client.cookies["kdps_csrf"] = "proof-csrf"
    response = client.post(
        "/api/auth/registration",
        payload,
        format="json",
        HTTP_X_CSRF_TOKEN="proof-csrf",
        HTTP_ORIGIN="https://untrusted.example.test",
    )
    assert response.status_code == 403 and response.data["code"] == "CSRF_FAILED"
    response = client.post(
        "/api/auth/registration", payload, format="json", HTTP_X_CSRF_TOKEN="proof-csrf"
    )
    assert response.status_code == 202
    assert response["Cache-Control"] == "no-store, private"
    assert_no_credentials(response.data, proposal)


def test_initial_people_cannot_do_business_before_replacing_temporary_password(
    proposal: dict[str, Any],
) -> None:
    from types import SimpleNamespace

    register(proposal)
    for user in User.objects.all():
        with pytest.raises(Refusal) as refusal:
            enforce_password_change_restriction(
                SimpleNamespace(path="/api/auth/admin/registration"), user
            )
        assert refusal.value.code == "PASSWORD_CHANGE_REQUIRED"
        enforce_password_change_restriction(
            SimpleNamespace(path="/api/auth/change-password"), user
        )


def test_setup_summary_does_not_advertise_ready_from_a_stale_selling_flag(
    proposal: dict[str, Any],
) -> None:
    from accounts.sessions import issue_session

    result = register(proposal)
    tenant = Tenant.objects.get()
    with tenant_context(tenant.pk):
        owner = User.objects.get(email=proposal["owner"]["email"])
        initial = issue_session(owner)
        client = APIClient()
        client.cookies["kdps_session"] = initial.token
        client.cookies["kdps_csrf"] = initial.csrf_token
        client.credentials(HTTP_X_CSRF_TOKEN=initial.csrf_token)
        changed = client.post("/api/auth/change-password", {"current_password": proposal["owner"]["temporary_password"], "new_password": uuid.uuid4().hex}, format="json")
        assert changed.status_code == 200
        owner.refresh_from_db()
        fresh = issue_session(owner)
        client.cookies["kdps_session"] = fresh.token
        client.cookies["kdps_csrf"] = fresh.csrf_token
        client.credentials(HTTP_X_CSRF_TOKEN=fresh.csrf_token)
        # Simulate a once-approved flag retained while required setup is
        # absent. Its presence alone is never current readiness evidence.
        guard = SiteGuard.objects.get(site_id=result["first_store_id"])
        guard.sell_ready = True
        guard.save(update_fields=["sell_ready"])
        response = client.get("/api/auth/admin/registration")
        assert response.status_code == 200 and response.data["setup_complete"] is False
        assert response["Cache-Control"] == "no-store, private"


def test_synthetic_seed_cannot_change_real_registered_company(
    proposal: dict[str, Any],
) -> None:
    register(proposal)
    with pytest.raises(CommandError, match="fixture seeding is refused"):
        call_command("seed_foundation")
    assert Tenant.objects.count() == 1 and User.objects.count() == 2


def test_new_real_cli_bootstrap_cannot_bypass_joint_initial_confirmation(
    proposal: dict[str, Any],
) -> None:
    with pytest.raises(Refusal) as refusal:
        bootstrap_deployment(
            deployment_key=uuid.uuid4(),
            code="CLI",
            name="No bypass",
            timezone_name="Asia/Kolkata",
            currency="INR",
            locale="en-IN",
            synthetic=False,
            admin_email=proposal["owner"]["email"],
            admin_name="Owner",
            admin_staff_code="OWNER",
            admin_password=proposal["owner"]["temporary_password"],
        )
    assert refusal.value.code == "ACTION_DENIED"
    assert Tenant.objects.count() == User.objects.count() == 0


def test_long_individual_emails_with_shared_prefix_do_not_collide(
    proposal: dict[str, Any],
) -> None:
    common = "a" * 60
    proposal["owner"]["email"] = f"{common}@owner.example.test"
    proposal["admin"]["email"] = f"{common}@admin.example.test"
    result = register(proposal)
    assert result["state"] == "registered"
    assert User.objects.values("username").distinct().count() == 2


def test_duplicate_identity_and_invalid_registration_input_are_refused(
    proposal: dict[str, Any],
) -> None:
    duplicated = copy.deepcopy(proposal)
    duplicated["admin"]["email"] = duplicated["owner"]["email"]
    assert not RegistrationInput(data=duplicated).is_valid()
    wrong_registration = copy.deepcopy(proposal)
    wrong_registration["company"]["state_code"] = "27"
    assert not RegistrationInput(data=wrong_registration).is_valid()
    unknown_field = copy.deepcopy(proposal)
    unknown_field["admin"]["all_sites"] = True
    assert not RegistrationInput(data=unknown_field).is_valid()
    too_many = copy.deepcopy(proposal)
    too_many["proposed_team"] = [
        {"name": f"Proof proposed {index}", "email": f"proof-{index}@alpha.example.test", "staff_code": f"TEAM{index}", "role_code": "store_person"}
        for index in range(51)
    ]
    assert not RegistrationInput(data=too_many).is_valid()
    too_many["proposed_team"] = too_many["proposed_team"][:50]
    assert RegistrationInput(data=too_many).is_valid()


def test_action_upgrade_after_confirmation_requires_revised_joint_baseline(
    proposal: dict[str, Any],
    monkeypatch: Any,
) -> None:

    staged = stage_registration(proposal)
    confirm_registration(credentials(proposal, "owner", staged["summary_hash"]))
    # Change an actual source of the baseline, so the revised tenant must
    # publish exactly the newly confirmed requirements rather than a mock DTO.
    monkeypatch.setitem(ACTION_LEVELS, "opening.import.stage", ("receive_goods", "approve"))
    with pytest.raises(Refusal) as refusal:
        confirm_registration(credentials(proposal, "admin", staged["summary_hash"]))
    assert refusal.value.code == "REVISION_SUPERSEDED"
    assert Tenant.objects.count() == 0
    row = InstallationRegistration.objects.get()
    assert row.owner_confirmed_at is not None and row.admin_confirmed_at is None
    revised_input = copy.deepcopy(proposal)
    revised_input.update(command_id=uuid.uuid4(), current_owner_password=proposal["owner"]["temporary_password"])
    revised = stage_registration(revised_input, edit=True)
    assert revised["summary_hash"] != staged["summary_hash"]
    assert revised["confirmed"] == {"owner": False, "admin": False}
    for who in ("owner", "admin"):
        with pytest.raises(Refusal) as old_confirmation:
            confirm_registration(credentials(proposal, who, staged["summary_hash"]))
        assert old_confirmation.value.code == "REVISION_SUPERSEDED"
    assert Tenant.objects.count() == 0
    confirm_registration(credentials(revised_input, "admin", revised["summary_hash"]))
    assert Tenant.objects.count() == 0
    result = confirm_registration(credentials(revised_input, "owner", revised["summary_hash"]))
    assert result["state"] == "registered"
    tenant = Tenant.objects.get()
    with tenant_context(tenant.pk):
        baseline = MasterVersion.objects.get(kind="tenant", target_key=WORKFLOW_TARGET_KEY)
        assert baseline.payload["action_levels"] == revised["summary"]["initial_access"]["policy_baseline"]["action_levels"]
        assert baseline.payload["action_levels"]["opening.import.stage"]["minimum"] == "approve"


def test_automatic_codes_are_allocated_once_and_preserved_on_revision(proposal: dict[str, Any]) -> None:
    for key in ("company", "store"):
        proposal[key].pop("code")
    for person in [proposal["owner"], proposal["admin"], *proposal["proposed_team"]]:
        person.pop("staff_code")
    parsed = RegistrationInput(data=proposal)
    assert parsed.is_valid(), parsed.errors
    staged = stage_registration(dict(parsed.validated_data))
    summary = staged["summary"]
    assert summary["company"]["code"] == "CMP-0001"
    assert summary["store"]["code"] == "STR-0001"
    assert [summary[k]["staff_code"] for k in ("owner", "admin")] == ["EMP-0001", "EMP-0002"]
    assert summary["proposed_team"][0]["staff_code"] == "EMP-0003"
    assert stage_registration(dict(parsed.validated_data)) == staged
    revision = copy.deepcopy(dict(parsed.validated_data))
    revision.update(command_id=uuid.uuid4(), current_owner_password=proposal["owner"]["temporary_password"])
    revision["company"]["name"] = "Revised company"
    revised = stage_registration(revision, edit=True)
    assert revised["summary"]["owner"]["staff_code"] == "EMP-0001"
    assert revised["summary"]["proposed_team"][0]["staff_code"] == "EMP-0003"
    assert revised["summary_hash"] != staged["summary_hash"]
    assert revised["confirmed"] == {"owner": False, "admin": False}


def test_automatic_codes_skip_custom_codes_case_insensitively(proposal: dict[str, Any]) -> None:
    proposal["owner"].pop("staff_code")
    proposal["admin"]["staff_code"] = "emp-0001"
    proposal["proposed_team"][0].pop("staff_code")
    summary = stage_registration(proposal)["summary"]
    assert summary["owner"]["staff_code"] == "EMP-0002"
    assert summary["admin"]["staff_code"] == "emp-0001"
    assert summary["proposed_team"][0]["staff_code"] == "EMP-0003"


def test_public_options_do_not_disclose_pending_people(proposal: dict[str, Any]) -> None:
    stage_registration(proposal)
    state = public_state()
    assert [s["label"] for s in state["options"]["states"]] == ["Jharkhand", "Bihar"]
    assert state["options"]["languages"][0]["value"] == "en-IN"
    assert "summary" not in state
    assert all(p["email"] not in json.dumps(state) for p in [proposal["owner"], proposal["admin"], *proposal["proposed_team"]])


def test_proposed_code_requires_explicit_email_and_stable_claim(proposal: dict[str, Any]) -> None:
    from django.db import transaction
    from accounts.registration_services import claim_signup_code, validate_signup_login

    register(proposal)
    row = InstallationRegistration.objects.get()
    tenant_id = row.tenant_id
    assert tenant_id is not None
    person = proposal["proposed_team"][0]
    human_id = uuid.uuid4()
    with tenant_context(tenant_id), transaction.atomic():
        with pytest.raises(Refusal, match="reserved"):
            claim_signup_code(tenant_id, person["staff_code"], None, human_id)
        with pytest.raises(Refusal, match="reserved"):
            claim_signup_code(tenant_id, person["staff_code"], "wrong@example.test", human_id)
        claim_signup_code(tenant_id, person["staff_code"], person["email"], human_id)
        claim_signup_code(tenant_id, person["staff_code"], person["email"], human_id)
        with pytest.raises(Refusal, match="another signup person"):
            claim_signup_code(tenant_id, person["staff_code"], person["email"], uuid.uuid4())
        with pytest.raises(Refusal, match="email must match"):
            validate_signup_login(tenant_id, human_id, "wrong@example.test")
        validate_signup_login(tenant_id, human_id, person["email"])
        assert User.objects.count() == 2
        assert RoleAssignment.objects.count() == 2
    row.refresh_from_db()
    assert len([e for e in row.confirmation_history if e.get("event") == "staff_code_claimed"]) == 1


def test_genesis_adopts_the_unknown_historical_season_and_tax_rules_may_be_switched_on(
    proposal: dict[str, Any],
) -> None:
    from masters.store_feature_registry import GST_AFTER_DISCOUNT, HSN_ON_EVERY_ITEM
    from masters.store_features import feature, gate_lock

    unknown, _ = Season.objects.get_or_create(
        historical_unknown=True,
        defaults={"code": "UNKNOWN-HIST", "name": "Unknown historical season", "status": "closed"},
    )
    register(proposal)
    tenant = Tenant.objects.get()
    with tenant_context(tenant.pk):
        version = MasterVersion.objects.get(tenant=tenant, kind="season", target_key=str(unknown.pk))
        assert version.revision == 1 and version.payload["historical_unknown"] is True
    # KDPS approved the per-HSN rates (1 October 2026): only those gates closed.
    assert gate_lock(feature("tax-settings"), real=True) is None
    assert gate_lock(feature(HSN_ON_EVERY_ITEM), real=True) is None
    assert gate_lock(feature(GST_AFTER_DISCOUNT), real=True) is not None
