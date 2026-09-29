"""Representative PostgreSQL, session and tenant-wall proofs for SO-02."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from rest_framework.test import APIClient

from accounts.goods_models import HumanIdentity, ServerSession
from accounts.models import User
from core.tenancy import tenant_context
from masters.goods_models import Tenant


@pytest.fixture
def tenant(db: None) -> Tenant:
    result, _created = Tenant.objects.get_or_create(
        deployment_key=uuid.UUID(str(settings.KDPS_DEPLOYMENT_KEY)),
        defaults={
            "code": "so02-proof",
            "name": "SO-02 isolated proof",
            "timezone": "Asia/Kolkata",
            "currency": "INR",
            "locale": "en-IN",
            "synthetic": True,
        },
    )
    return result


@contextmanager
def application_role() -> Iterator[None]:
    """Use the same restricted PostgreSQL role that served connections set."""
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE kdps_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def test_restricted_role_is_real_and_has_no_rls_bypass(tenant: Tenant) -> None:
    with application_role(), tenant_context(tenant.id), connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, rolsuper, rolbypassrls, rolcreatedb "
            "FROM pg_roles WHERE rolname = current_user"
        )
        row = cursor.fetchone()
        assert row == ("kdps_app", False, False, False)
        cursor.execute("SELECT current_setting('server_version_num')")
        version = cursor.fetchone()
        assert version is not None and version[0] == "170011"


def test_restricted_role_persists_and_savepoint_rollback_removes_a_write(tenant: Tenant) -> None:
    with application_role(), tenant_context(tenant.id):
        HumanIdentity.objects.create(tenant=tenant, staff_code="PERSIST", display_name="Persists")
        assert HumanIdentity.objects.filter(staff_code="PERSIST").exists()

        with pytest.raises(RuntimeError, match="roll back"):
            with transaction.atomic():
                HumanIdentity.objects.create(
                    tenant=tenant, staff_code="ROLLBACK", display_name="Roll back"
                )
                raise RuntimeError("roll back this transaction")

        assert not HumanIdentity.objects.filter(staff_code="ROLLBACK").exists()
        assert HumanIdentity.objects.filter(staff_code="PERSIST").count() == 1


def test_tenant_wall_hides_and_refuses_cross_tenant_rows(tenant: Tenant) -> None:
    second = Tenant.objects.create(
        code="so02-other",
        name="Another isolated tenant",
        deployment_key=uuid.uuid4(),
        timezone="Asia/Kolkata",
        currency="INR",
        locale="en-IN",
        synthetic=True,
    )
    with tenant_context(tenant.id):
        HumanIdentity.objects.create(tenant=tenant, staff_code="VISIBLE", display_name="Visible")
    with tenant_context(second.id):
        HumanIdentity.objects.create(tenant=second, staff_code="HIDDEN", display_name="Hidden")

    with application_role(), tenant_context(tenant.id):
        assert list(HumanIdentity.objects.values_list("staff_code", flat=True)) == ["VISIBLE"]
        with pytest.raises(DatabaseError):
            with transaction.atomic():
                HumanIdentity.objects.create(
                    tenant=second, staff_code="REFUSED", display_name="Refused"
                )
        assert not HumanIdentity.objects.filter(staff_code="REFUSED").exists()


def test_login_denial_session_success_and_logout_under_restricted_role(tenant: Tenant) -> None:
    with tenant_context(tenant.id):
        person = HumanIdentity.objects.create(
            tenant=tenant, staff_code="LOGIN", display_name="Login proof"
        )
        User.objects.create_user(
            username="so02-login",
            password="SO02-proof-password-unique",
            tenant=tenant,
            human=person,
            email="so02-proof@example.invalid",
        )

    client = APIClient()
    with application_role():
        csrf = client.get("/api/auth/csrf")
        assert csrf.status_code == 200
        token = csrf.json()["csrf_token"]
        denied = client.post(
            "/api/auth/login",
            {"email": "so02-proof@example.invalid", "password": "wrong", "csrf_token": token},
            format="json",
        )
        assert denied.status_code == 401
        assert denied.json()["code"] == "INVALID_CREDENTIALS"
        with tenant_context(tenant.id):
            assert ServerSession.objects.count() == 0

        accepted = client.post(
            "/api/auth/login",
            {
                "email": "so02-proof@example.invalid",
                "password": "SO02-proof-password-unique",
                "csrf_token": token,
            },
            format="json",
        )
        assert accepted.status_code == 200
        assert "kdps_session" in accepted.cookies
        with tenant_context(tenant.id):
            assert ServerSession.objects.count() == 1

        assert client.get("/api/auth/me").status_code == 200
        client.credentials(HTTP_X_CSRF_TOKEN=client.cookies["kdps_csrf"].value)
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/auth/me").status_code == 401
