"""Database roles, row-level security and write grants for goods-v1 (design §4.2, §4.4, §7.1).

Triggers alone cannot prove tenant walls: a superuser skips row-level security.
So the running application does not stay the superuser that migrates the schema.
Each connection opened by a served process (and by the test suite, once its
database exists) switches to ``kdps_app`` - a role with no RLS bypass, no
``TRUNCATE`` and no direct ``INSERT`` into the operational journals. Journal rows
can only be appended through a function owned by ``kdps_ledger``.

``manage.py`` commands (migrate, seeds, drift check) keep the owner role; they
are setup operations, not the served application.
"""

from __future__ import annotations

import os
from typing import Any

from django.db.backends.signals import connection_created

APP_ROLE = "kdps_app"
LEDGER_ROLE = "kdps_ledger"
VERIFIER_ROLE = "kdps_verifier"

#: Environment switch read when a connection opens. The ASGI/WSGI entry points
#: set it; ``manage.py`` does not; pytest turns it on after creating the test DB.
RUNTIME_ROLE_ENV = "KDPS_DB_RUNTIME_ROLE"

_runtime_role_forced: bool | None = None


def runtime_role_enabled() -> bool:
    if _runtime_role_forced is not None:
        return _runtime_role_forced
    return os.environ.get(RUNTIME_ROLE_ENV, "") == "1"


def force_runtime_role(enabled: bool | None) -> None:
    """Test/entry-point override; ``None`` returns control to the environment."""
    global _runtime_role_forced
    _runtime_role_forced = enabled


def _switch_role(sender: Any, connection: Any, **kwargs: Any) -> None:
    if connection.vendor != "postgresql" or not runtime_role_enabled():
        return
    with connection.cursor() as cursor:
        cursor.execute(f"SET ROLE {APP_ROLE}")


def install_connection_hook() -> None:
    connection_created.connect(_switch_role, dispatch_uid="kdps-runtime-role")


def create_roles_sql() -> str:
    """Idempotently create the three roles and the application's base grants."""
    return f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LEDGER_ROLE}') THEN
            CREATE ROLE {LEDGER_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{VERIFIER_ROLE}') THEN
            CREATE ROLE {VERIFIER_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
        END IF;
        EXECUTE format('GRANT {APP_ROLE} TO %I', current_user);
    END
    $$;

    GRANT USAGE ON SCHEMA public TO {APP_ROLE}, {LEDGER_ROLE}, {VERIFIER_ROLE};
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO {VERIFIER_ROLE}, {LEDGER_ROLE};
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE};
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE};
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT ON TABLES TO {VERIFIER_ROLE}, {LEDGER_ROLE};

    -- Legacy append-only ledgers: the trigger already refuses UPDATE/DELETE for
    -- every role; the grant keeps the application role from even holding it.
    DO $$
    DECLARE t record;
    BEGIN
        FOR t IN
            SELECT DISTINCT c.relname
            FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
            WHERE g.tgname LIKE '%\\_forbid\\_update\\_delete' ESCAPE '\\'
        LOOP
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM {APP_ROLE}', t.relname);
        END LOOP;
    END
    $$;

    CREATE OR REPLACE FUNCTION kdps_current_tenant() RETURNS uuid
        LANGUAGE sql STABLE
        AS $fn$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $fn$;
    """


def drop_roles_reverse_sql() -> str:
    return "DROP FUNCTION IF EXISTS kdps_current_tenant();"


def tenant_rls_sql(table: str) -> str:
    """Forced RLS that shows and accepts only the bound tenant's rows."""
    policy = f"{table}_tenant_isolation"[:63]
    return f"""
    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS {policy} ON {table};
    CREATE POLICY {policy} ON {table}
        USING (tenant_id = kdps_current_tenant())
        WITH CHECK (tenant_id = kdps_current_tenant());
    """


def append_only_grants_sql(table: str) -> str:
    """The application role may read and insert protected evidence, never change it."""
    return (
        f"REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM {APP_ROLE};"
        f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE};"
    )


def journal_grants_sql(table: str) -> str:
    """Only the ledger role's append function may insert operational journal rows."""
    return (
        f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table} FROM {APP_ROLE};"
        f"GRANT SELECT ON {table} TO {APP_ROLE};"
        f"GRANT SELECT, INSERT ON {table} TO {LEDGER_ROLE};"
    )
