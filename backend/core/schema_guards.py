"""Install tenant walls and write protection on every goods-v1 table (design §4.2, §4.4, §5.1).

Rather than trusting each migration author to remember four kinds of SQL, the
guards are derived from the models after every ``migrate``:

* ``UNIQUE(tenant_id, id)`` and a composite, deferred foreign key
  ``(tenant_id, x_id) → parent(tenant_id, id)`` for every reference between
  tenant-owned tables, so a row can never point into another tenant;
* ``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` with a policy on ``kdps_current_tenant()``;
* append-only triggers and grants for evidence, journal grants for ledger rows,
  and the set-once rule for document numbers.

The inherited masters of the change PRD §14.2 (roles, vendors and their brand
links, brands, legal entities, registrations and stores) get the same wall once
their tenant column is non-null, and so does every reference to them from a
tenant-carrying table. A legacy login's scope (its role, entity, stores and
brands) has no tenant column of its own, so a trigger holds it to the login's
tenant instead.

Idempotent: it reads the catalogue once and adds only what is missing, so it can
run after every migrate and every test flush.
"""

from __future__ import annotations

import hashlib
from typing import Any

from django.apps import apps
from django.db import connection, models

from core.dbroles import APP_ROLE, append_only_grants_sql, journal_grants_sql, tenant_rls_sql
from core.goods_base import APPEND_ONLY, JOURNAL, TenantOwned
from core.ledger import append_only_sql, truncate_guard_sql

#: Every inherited parent the approved repair contract names (change PRD §14.2),
#: plus the vendor-brand link whose two ends must share a tenant.
INHERITED_MASTERS: tuple[str, ...] = (
    "accounts.Role",
    "vendors.Vendor",
    "vendors.VendorBrand",
    "masters.Brand",
    "masters.LegalEntity",
    "masters.Gstin",
    "masters.Store",
)

#: A legacy login's operating scope: (table, column, parent table) checked by trigger.
LOGIN_SCOPE_LINKS: tuple[tuple[str, str, str], ...] = (
    ("accounts_user_stores", "store_id", "masters_store"),
    ("accounts_user_brands", "brand_id", "masters_brand"),
)
#: Versioned so ``migrate`` replaces triggers installed by an earlier guard body.
LOGIN_SCOPE_TRIGGER = "kdps_login_scope_v2"
LEGACY_LOGIN_SCOPE_TRIGGER = "kdps_login_scope_same_tenant"


def _short(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]  # noqa: S324 - a name, not a secret


def goods_models() -> list[type[models.Model]]:
    return [
        model
        for model in apps.get_models()
        if issubclass(model, TenantOwned) and not model._meta.abstract and model._meta.managed
    ]


def inherited_models() -> list[type[models.Model]]:
    return [apps.get_model(label) for label in INHERITED_MASTERS]


class _Catalogue:
    def __init__(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT conname FROM pg_constraint")
            self.constraints = {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
            self.triggers = {row[0] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace"
            )
            self.rls = {row[0]: (bool(row[1]), bool(row[2])) for row in cursor.fetchall()}
            cursor.execute("SELECT policyname FROM pg_policies WHERE schemaname = 'public'")
            self.policies = {row[0] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT table_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'tenant_id'"
            )
            self.tenant_nullable = {row[0]: row[1] == "YES" for row in cursor.fetchall()}

    def tenant_enforced(self, table: str) -> bool:
        return self.tenant_nullable.get(table) is False


def _walled(catalogue: _Catalogue) -> list[type[models.Model]]:
    """Goods tables, plus inherited masters whose ownership is already non-null.

    An inherited master still in its migration window (column missing or still
    nullable) is left alone: walling it then would hide the rows being backfilled.
    ``unguarded_tables`` reports it.
    """
    walled = [m for m in goods_models() if m._meta.db_table in catalogue.rls]
    walled += [m for m in inherited_models() if catalogue.tenant_enforced(m._meta.db_table)]
    return walled


#: The document-identity guard's body. Only ``official_number`` may be set, once;
#: and a booking saved with no destination site (GSA-T05) may have ``site_id``
#: set once from empty - never cleared, moved, or set on any other purpose,
#: whose site the table's check constraint already requires.
IDENTITY_GUARD_FUNCTION_SQL = """
    CREATE OR REPLACE FUNCTION kdps_identity_number_once() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'document identities are never deleted (%)', TG_TABLE_NAME;
        END IF;
        IF OLD.official_number IS NOT NULL
           AND NEW.official_number IS DISTINCT FROM OLD.official_number THEN
            RAISE EXCEPTION 'an official number is set once and never changed';
        END IF;
        IF NEW.site_id IS DISTINCT FROM OLD.site_id
           AND (OLD.site_id IS NOT NULL OR NEW.site_id IS NULL OR OLD.purpose <> 'booking') THEN
            RAISE EXCEPTION 'a booking''s site is set once, from none, and never changed';
        END IF;
        IF (to_jsonb(NEW) - 'official_number' - 'site_id')
           IS DISTINCT FROM (to_jsonb(OLD) - 'official_number' - 'site_id') THEN
            RAISE EXCEPTION 'document identity fields are immutable';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
"""


def identity_guard_sql(table: str) -> str:
    return f"""
    {IDENTITY_GUARD_FUNCTION_SQL}
    CREATE TRIGGER {table}_identity_guard
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION kdps_identity_number_once();
    REVOKE DELETE, TRUNCATE ON {table} FROM {APP_ROLE};
    """


def login_scope_guard_sql() -> str:
    """A legacy login may only be scoped with records of its own tenant.

    The expected tenant is the login's own, or the connection's trusted tenant for
    a login that has none yet; with neither, the scope is refused. Lookups run as
    the caller, so a record hidden by row-level security reads as absent, and a
    login that changes tenant must still own every store and brand it holds.
    Each function pins ``search_path`` (``pg_temp`` last) and names its tables
    by schema, so a session's temporary table cannot stand in for a master.
    """
    links = "\n".join(
        f"""
    DROP TRIGGER IF EXISTS {table}_{LEGACY_LOGIN_SCOPE_TRIGGER} ON {table};
    DROP TRIGGER IF EXISTS {table}_{LOGIN_SCOPE_TRIGGER} ON {table};
    CREATE TRIGGER {table}_{LOGIN_SCOPE_TRIGGER}
        BEFORE INSERT OR UPDATE ON {table}
        FOR EACH ROW EXECUTE FUNCTION kdps_login_link_same_tenant('{parent}', '{column}');"""
        for table, column, parent in LOGIN_SCOPE_LINKS
    )
    held = " UNION ALL ".join(
        f"SELECT p.id, p.tenant_id FROM public.{table} l "
        f"LEFT JOIN public.{parent} p ON p.id = l.{column} WHERE l.user_id = NEW.id"
        for table, column, parent in LOGIN_SCOPE_LINKS
    )
    return f"""
    CREATE OR REPLACE FUNCTION kdps_login_scope_tenant(login_tenant uuid, target uuid)
    RETURNS void
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        expected uuid := COALESCE(login_tenant, public.kdps_current_tenant());
    BEGIN
        IF expected IS NULL OR target IS NULL OR target <> expected THEN
            RAISE EXCEPTION 'a login can only be scoped with records of its own tenant'
                USING ERRCODE = '23514';
        END IF;
    END;
    $$ LANGUAGE plpgsql;

    CREATE OR REPLACE FUNCTION kdps_login_same_tenant() RETURNS trigger
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        target uuid;
        held record;
        retenanted boolean := TG_OP = 'UPDATE' AND NEW.tenant_id IS DISTINCT FROM OLD.tenant_id;
    BEGIN
        IF NEW.role_id IS NOT NULL
           AND (TG_OP = 'INSERT' OR retenanted OR NEW.role_id IS DISTINCT FROM OLD.role_id) THEN
            SELECT tenant_id INTO target FROM public.accounts_role WHERE id = NEW.role_id;
            PERFORM public.kdps_login_scope_tenant(NEW.tenant_id, target);
        END IF;
        IF NEW.entity_id IS NOT NULL
           AND (TG_OP = 'INSERT' OR retenanted OR NEW.entity_id IS DISTINCT FROM OLD.entity_id) THEN
            SELECT tenant_id INTO target FROM public.masters_legalentity WHERE id = NEW.entity_id;
            PERFORM public.kdps_login_scope_tenant(NEW.tenant_id, target);
        END IF;
        IF retenanted THEN
            -- A hidden (other-tenant) store or brand reads as NULL and is refused.
            FOR held IN {held} LOOP
                PERFORM public.kdps_login_scope_tenant(NEW.tenant_id, held.tenant_id);
            END LOOP;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE OR REPLACE FUNCTION kdps_login_link_same_tenant() RETURNS trigger
    SET search_path = pg_catalog, public, pg_temp
    AS $$
    DECLARE
        target uuid;
        login_tenant uuid;
    BEGIN
        SELECT tenant_id INTO login_tenant FROM public.accounts_user WHERE id = NEW.user_id;
        EXECUTE format('SELECT tenant_id FROM public.%I WHERE id = $1', TG_ARGV[0])
            INTO target
            USING (to_jsonb(NEW) ->> TG_ARGV[1])::bigint;
        PERFORM public.kdps_login_scope_tenant(login_tenant, target);
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS accounts_user_{LEGACY_LOGIN_SCOPE_TRIGGER} ON accounts_user;
    DROP TRIGGER IF EXISTS accounts_user_{LOGIN_SCOPE_TRIGGER} ON accounts_user;
    CREATE TRIGGER accounts_user_{LOGIN_SCOPE_TRIGGER}
        BEFORE INSERT OR UPDATE ON accounts_user
        FOR EACH ROW EXECUTE FUNCTION kdps_login_same_tenant();
    {links}
    """


def drop_tenant_wall_sql(*tables: str) -> str:
    """Take an inherited master's wall down again, for a migration's reverse step.

    Removing the tenant column needs the composite keys that use it, the policy
    and the login-scope triggers gone first; ``migrate`` reinstalls them all.
    """
    parts = [
        f"""
    DO $$
    DECLARE c record;
    BEGIN
        FOR c IN
            SELECT conname, conrelid::regclass AS rel FROM pg_constraint
            WHERE conname LIKE 'kg\\_fk\\_%' ESCAPE '\\'
              AND (confrelid = '{table}'::regclass OR conrelid = '{table}'::regclass)
        LOOP
            EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', c.rel, c.conname);
        END LOOP;
    END
    $$;
    ALTER TABLE {table} DROP CONSTRAINT IF EXISTS kg_uq_{_short(table)};
    DROP POLICY IF EXISTS {f"{table}_tenant_isolation"[:63]} ON {table};
    ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
    """
        for table in tables
    ]
    for trigger in (LOGIN_SCOPE_TRIGGER, LEGACY_LOGIN_SCOPE_TRIGGER):
        parts.append(
            f"DROP TRIGGER IF EXISTS accounts_user_{trigger} ON accounts_user;"
            + "".join(
                f"DROP TRIGGER IF EXISTS {table}_{trigger} ON {table};"
                for table, _column, _parent in LOGIN_SCOPE_LINKS
            )
        )
    return "\n".join(parts)


def _tenant_references(model: type[models.Model], walled: set[type[models.Model]]) -> list[Any]:
    return [
        field
        for field in model._meta.concrete_fields
        if isinstance(field, models.ForeignKey)
        and field.name != "tenant"
        and field.related_model in walled
    ]


def _login_scope_ready(catalogue: _Catalogue) -> bool:
    return all(
        catalogue.tenant_enforced(table) for table in ("accounts_role", "masters_store")
    ) and ("accounts_user" in catalogue.rls)


def _login_scope_triggers() -> list[str]:
    return [f"accounts_user_{LOGIN_SCOPE_TRIGGER}"] + [
        f"{table}_{LOGIN_SCOPE_TRIGGER}" for table, _column, _parent in LOGIN_SCOPE_LINKS
    ]


def install_goods_schema_guards() -> list[str]:
    """Add whatever guards are missing; return the statements it ran."""
    catalogue = _Catalogue()
    targets = _walled(catalogue)
    if not targets:
        return []
    walled = set(targets)
    statements: list[str] = []

    for model in targets:
        table = model._meta.db_table
        unique_name = f"kg_uq_{_short(table)}"
        if unique_name not in catalogue.constraints:
            statements.append(
                f"ALTER TABLE {table} ADD CONSTRAINT {unique_name} UNIQUE (tenant_id, id)"
            )

    for model in targets:
        table = model._meta.db_table
        for field in _tenant_references(model, walled):
            fk_name = f"kg_fk_{_short(table, field.column)}"
            if fk_name in catalogue.constraints:
                continue
            statements.append(
                f"ALTER TABLE {table} ADD CONSTRAINT {fk_name} "
                f"FOREIGN KEY (tenant_id, {field.column}) "
                f"REFERENCES {field.related_model._meta.db_table} (tenant_id, id) "
                "DEFERRABLE INITIALLY DEFERRED"
            )

    for model in targets:
        table = model._meta.db_table
        enabled, forced = catalogue.rls[table]
        policy = f"{table}_tenant_isolation"[:63]
        if not (enabled and forced and policy in catalogue.policies):
            statements.append(tenant_rls_sql(table))
        protection = getattr(model, "PROTECTION", "projection")
        if (
            protection in (APPEND_ONLY, JOURNAL)
            and f"{table}_forbid_update_delete" not in catalogue.triggers
        ):
            statements.append(append_only_sql(table))
            statements.append(truncate_guard_sql(table))
            statements.append(
                journal_grants_sql(table)
                if protection == JOURNAL
                else append_only_grants_sql(table)
            )
        if protection == "identity" and f"{table}_identity_guard" not in catalogue.triggers:
            statements.append(identity_guard_sql(table))
        extra = getattr(model, "extra_guard_sql", None)
        if callable(extra):
            marker, sql = extra()
            if marker not in catalogue.triggers and marker not in catalogue.constraints:
                statements.append(sql)

    if _login_scope_ready(catalogue) and not set(_login_scope_triggers()) <= catalogue.triggers:
        statements.append(login_scope_guard_sql())

    if statements:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
    return statements


def unguarded_tables() -> list[str]:
    """Tables that should carry guards but do not (used by the protection tests)."""
    catalogue = _Catalogue()
    missing: list[str] = []
    for model in inherited_models():
        table = model._meta.db_table
        if not catalogue.tenant_enforced(table):
            missing.append(f"{table}: non-null tenant")
    walled = set(goods_models()) | set(inherited_models())
    for model in sorted(walled, key=lambda m: m._meta.db_table):
        table = model._meta.db_table
        enabled, forced = catalogue.rls.get(table, (False, False))
        if not (enabled and forced) or f"{table}_tenant_isolation"[:63] not in catalogue.policies:
            missing.append(f"{table}: rls")
        if f"kg_uq_{_short(table)}" not in catalogue.constraints:
            missing.append(f"{table}: tenant unique")
        for field in _tenant_references(model, walled):
            if f"kg_fk_{_short(table, field.column)}" not in catalogue.constraints:
                missing.append(f"{table}.{field.column}: same-tenant reference")
        protection = getattr(model, "PROTECTION", "projection")
        if (
            protection in (APPEND_ONLY, JOURNAL)
            and f"{table}_forbid_update_delete" not in catalogue.triggers
        ):
            missing.append(f"{table}: append-only")
    for trigger in _login_scope_triggers():
        if trigger not in catalogue.triggers:
            missing.append(f"{trigger}: login scope")
    return missing
