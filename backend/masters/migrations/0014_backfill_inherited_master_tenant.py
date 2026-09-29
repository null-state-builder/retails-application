# Fix ticket 03 (change PRD §14.2), step 2 of 3: assign every existing inherited
# master to its verified tenant, without touching its identifier or content.
#
# The only trusted owner is the tenant this deployment is bound to (its
# `KDPS_DEPLOYMENT_KEY`). Ownership is never guessed, so the migration refuses -
# and changes nothing - when rows need an owner but:
#
#   * no tenant is bound to the deployment key yet, or
#   * a tenant-carrying record of a *different* tenant already references one of
#     them (the row plainly is not the deployment tenant's), or
#   * a login of a different tenant holds one of them in its store/brand scope, or
#   * two of them would share a business key the tenant keeps unique
#     (a nonblank vendor GSTIN or legal-entity PAN).
#
# It reads goods tables that already have forced row-level security, so the
# migrating role must be a superuser or hold BYPASSRLS; any other role is
# refused by PostgreSQL rather than shown fewer rows.
#
# Safe forward path on a populated database whose deployment is not yet bound:
# `migrate` stops here with the columns added (0013 and its siblings applied) and
# nothing assigned; bind the deployment key to its tenant, then `migrate` again.
# `tests/test_inherited_master_tenant_migration.py` walks this path.

from django.conf import settings
from django.db import migrations

TABLES = (
    "masters_legalentity",
    "masters_gstin",
    "masters_store",
    "masters_brand",
    "vendors_vendor",
    "vendors_vendor_brands",
    "accounts_role",
)

# A legacy login's scope links, which carry the tenant only through the login.
LOGIN_LINKS = (
    ("accounts_user_stores", "store_id", "masters_store"),
    ("accounts_user_brands", "brand_id", "masters_brand"),
)

# Business keys that were not unique before and become unique within a tenant.
NEW_KEYS = (
    ("vendors_vendor", "gstin"),
    ("masters_legalentity", "pan"),
)


class OwnershipUnresolved(RuntimeError):
    """Existing masters cannot be given a verified tenant; nothing was changed."""


def _pending(cursor) -> dict[str, int]:
    counts = {}
    for table in TABLES:
        cursor.execute(f"SELECT count(*) FROM {table} WHERE tenant_id IS NULL")  # noqa: S608
        counts[table] = cursor.fetchone()[0]
    return {table: n for table, n in counts.items() if n}


def _foreign_claims(cursor, tenant_id) -> list[str]:
    """References from records owned by another tenant into still-unowned masters."""
    cursor.execute(
        """
        SELECT child.relname, a.attname, parent.relname
        FROM pg_constraint c
        JOIN pg_class child ON child.oid = c.conrelid
        JOIN pg_class parent ON parent.oid = c.confrelid
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        WHERE c.contype = 'f'
          AND array_length(c.conkey, 1) = 1
          AND parent.relname = ANY(%s)
          AND EXISTS (
              SELECT 1 FROM pg_attribute t
              WHERE t.attrelid = c.conrelid AND t.attname = 'tenant_id' AND NOT t.attisdropped
          )
        """,
        [list(TABLES)],
    )
    claims = []
    for child, column, parent in cursor.fetchall():
        if column == "tenant_id":
            continue
        cursor.execute(
            f"SELECT count(*) FROM {child} x JOIN {parent} p ON p.id = x.{column} "  # noqa: S608
            "WHERE p.tenant_id IS NULL AND x.tenant_id IS NOT NULL AND x.tenant_id <> %s",
            [tenant_id],
        )
        count = cursor.fetchone()[0]
        if count:
            claims.append(f"{child}.{column} → {parent} ({count} rows of another tenant)")
    for link, column, parent in LOGIN_LINKS:
        cursor.execute(
            f"SELECT count(*) FROM {link} l JOIN accounts_user u ON u.id = l.user_id "  # noqa: S608
            f"JOIN {parent} p ON p.id = l.{column} "
            "WHERE p.tenant_id IS NULL AND u.tenant_id IS NOT NULL AND u.tenant_id <> %s",
            [tenant_id],
        )
        count = cursor.fetchone()[0]
        if count:
            claims.append(f"{link}.{column} → {parent} ({count} logins of another tenant)")
    return claims


def _duplicate_keys(cursor, tenant_id) -> list[str]:
    duplicates = []
    for table, column in NEW_KEYS:
        cursor.execute(
            f"SELECT {column}, count(*) FROM {table} "  # noqa: S608
            f"WHERE {column} <> '' AND (tenant_id IS NULL OR tenant_id = %s) "
            f"GROUP BY {column} HAVING count(*) > 1 ORDER BY {column}",
            [tenant_id],
        )
        duplicates += [f"{table}.{column} = {value!r} ({n} rows)" for value, n in cursor.fetchall()]
    return duplicates


def assign_verified_tenant(apps, schema_editor) -> None:
    with schema_editor.connection.cursor() as cursor:
        # A role that row-level security would filter must fail loudly here, not
        # quietly see fewer references than exist.
        cursor.execute("SET LOCAL row_security = off")
        pending = _pending(cursor)
        if not pending:
            return
        rows = ", ".join(f"{table}: {n}" for table, n in pending.items())
        cursor.execute(
            "SELECT id FROM masters_tenant WHERE deployment_key = %s",
            [str(settings.KDPS_DEPLOYMENT_KEY)],
        )
        bound = cursor.fetchone()
        if bound is None:
            raise OwnershipUnresolved(
                f"Existing masters have no tenant ({rows}) and no tenant is bound to "
                f"deployment key {settings.KDPS_DEPLOYMENT_KEY}. Ownership is never guessed: "
                "bind this deployment to its tenant (a Tenant row with this deployment key, "
                "for example through `manage.py bootstrap_deployment`), then run "
                "`manage.py migrate` again."
            )
        tenant_id = bound[0]
        problems = _foreign_claims(cursor, tenant_id) + _duplicate_keys(cursor, tenant_id)
        if problems:
            raise OwnershipUnresolved(
                "Existing masters cannot be assigned to the deployment tenant without "
                "guessing; resolve these first: " + "; ".join(problems)
            )
        for table in pending:
            cursor.execute(
                f"UPDATE {table} SET tenant_id = %s WHERE tenant_id IS NULL",  # noqa: S608
                [tenant_id],
            )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0017_role_tenant_column"),
        ("masters", "0013_inherited_master_tenant_columns"),
        ("vendors", "0008_vendor_tenant_columns"),
    ]

    operations = [
        # Reverse keeps the assignment: the columns themselves go in 0013's reverse.
        migrations.RunPython(assign_verified_tenant, migrations.RunPython.noop),
    ]
