"""Rehearse clean and accounts-0021 upgrades on fresh owned proof siblings.

Never resets, drops or reverses a database. Records identity before migrations;
credentials stay in the private proof environment. Run with backend/.venv/python.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from proof import (
    DATABASE_USER,
    PORT,
    PROJECT,
    ROOT,
    assert_database,
    proof_config,
    proof_environment,
)


def source_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted([*(root / "backend").rglob("*.py"), *(root / "scripts").rglob("*.py")]):
        if any(part in {".venv", "__pycache__"} for part in path.parts):
            continue
        raw = path.read_bytes()
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + str(len(raw)).encode() + b"\0" + raw + b"\0")
    return digest.hexdigest()


def child(case: str) -> None:
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.utils import timezone

    expected = os.environ["KDPS_REHEARSAL_DB"]
    assert source_fingerprint(ROOT) == os.environ["KDPS_REHEARSAL_SOURCE_SHA256"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user, current_setting('server_version_num'), system_identifier::text FROM pg_control_system()")
        identity = cursor.fetchone()
    assert identity[:3] == (expected, DATABASE_USER, "170011")
    assert identity[3] == os.environ["KDPS_REHEARSAL_SYSTEM_ID"]
    project = os.environ["KDPS_REHEARSAL_PROJECT"]
    print(json.dumps({"case": case, "project": project, "container": f"{project}-database-1",
                      "host": "127.0.0.1", "port": PORT, "database": expected,
                      "user": DATABASE_USER, "postgresql_version": "17.11",
                      "system_identifier": identity[3],
                      "frozen_source_sha256": os.environ["KDPS_REHEARSAL_SOURCE_SHA256"]}), flush=True)
    source = None
    if case == "upgrade":
        executor = MigrationExecutor(connection)
        targets = [("accounts", "0021_role_field_access_roleassignment")]
        executor.migrate(targets)
        apps = executor.loader.project_state(targets).apps
        tenant = apps.get_model("masters", "Tenant").objects.create(
            code="UPGRADE-PROOF", name="Fictional upgrade proof", deployment_key=uuid.uuid4(),
            timezone="Asia/Kolkata", currency="INR", locale="en-IN", synthetic=True,
        )
        human = apps.get_model("accounts", "HumanIdentity").objects.create(
            tenant_id=tenant.pk, staff_code="UPGRADE", display_name="Fictional upgrade identity",
        )
        role = apps.get_model("accounts", "Role").objects.create(
            tenant_id=tenant.pk, code="owner", name="Owner", section_access={}, field_access=[],
        )
        user = apps.get_model("accounts", "User").objects.create(
            tenant_id=tenant.pk, human_id=human.pk, role_id=role.pk,
            username="fictional-upgrade", scope_type="all", password="!",
        )
        source = (tenant.pk, human.pk, role.pk, user.pk)
        print("accounts0021: historical source identity, login and custom inactive role created.", flush=True)
    call_command("migrate", interactive=False, verbosity=0)
    executor = MigrationExecutor(connection)
    assert not executor.migration_plan(executor.loader.graph.leaf_nodes())
    call_command("migrate", interactive=False, verbosity=0)
    assert not MigrationExecutor(connection).migration_plan(executor.loader.graph.leaf_nodes())
    result: dict[str, object] = {"case": case, "migrations": "current, repeated with no pending operations"}
    if source:
        from accounts.assignment_migration import (
            apply_migration_plan,
            build_migration_plan,
        )
        from accounts.goods_models import HumanIdentity, RoleAssignment
        from accounts.models import Role, User
        from accounts.role_assignments import effective_assignments
        from core.refusals import Refusal
        from core.tenancy import tenant_context
        from masters.goods_models import Tenant

        tenant_id, human_id, role_id, user_id = source
        with tenant_context(tenant_id):
            assert Tenant.objects.filter(pk=tenant_id, code="UPGRADE-PROOF").exists()
            assert HumanIdentity.objects.filter(pk=human_id, tenant_id=tenant_id).exists()
            user = User.objects.get(pk=user_id, human_id=human_id, role_id=role_id)
            role = Role.objects.get(pk=role_id)
            assert role.section_access == {} and role.field_access == []
            preview = build_migration_plan(tenant_id)
            user.scope_type = "brand"
            user.save(update_fields=["scope_type"])
            try:
                apply_migration_plan(preview, apply_policy_defaults=False)
            except Refusal as refused:
                assert refused.code == "STALE_MIGRATION_PLAN"
            else:
                raise AssertionError("Stale source preview unexpectedly applied")
            assert not RoleAssignment.objects.filter(tenant_id=tenant_id).exists()
            user.scope_type = "all"
            user.save(update_fields=["scope_type"])
            first = apply_migration_plan(build_migration_plan(tenant_id), apply_policy_defaults=False)
            repeat = apply_migration_plan(build_migration_plan(tenant_id), apply_policy_defaults=False)
            assert first["created"] == 1 and repeat["created"] == 0 and repeat["unchanged"] == 1
            assignment = RoleAssignment.objects.get(tenant_id=tenant_id, legacy_user_id=user_id)
            assignment.revoked_at = timezone.now()
            assignment.save(update_fields=["revoked_at"])
            replay = apply_migration_plan(build_migration_plan(tenant_id), apply_policy_defaults=False)
            assignment.refresh_from_db()
            assert replay["created"] == 0 and assignment.revoked_at is not None
            assert not effective_assignments(human_id)
            role.refresh_from_db()
            assert role.section_access == {} and role.field_access == []
            result.update(source_ids_preserved=True, custom_inactive_policy_preserved=True,
                          stale_plan="denied without changes", repeat="one linked assignment",
                          revoked_target="never reactivated")
    print(json.dumps(result, sort_keys=True), flush=True)
    # This fixture's direct revocation tests migration replay only. It is not a
    # demonstration of independently reviewed operational compensating rollback.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=["clean", "upgrade"])
    args = parser.parse_args()
    if args.child:
        if os.environ.get("KDPS_PROOF_MODE") != "1" or not os.environ.get("KDPS_REHEARSAL_DB", "").startswith("kdps_rehearsal_migrations_"):
            raise RuntimeError("This child can run only on its owned disposable migration sibling.")
        child(args.child)
        return
    import psycopg
    from psycopg import sql

    assert_database()
    config = proof_config(create=False)
    # Rehearse from an isolated source copy as well as isolated databases.
    # No credentials, virtual environment, raw workbook or runtime state is
    # copied. The interpreter/dependencies remain the owned installed runtime.
    snapshot = ROOT / ".local" / ("first-store-migration-source-" + uuid.uuid4().hex[:12])
    snapshot.mkdir()
    ignore = shutil.ignore_patterns(".venv", "__pycache__", "*.pyc", ".env", ".env.*", "staticfiles", "media", "*.sqlite3")
    for name in ("backend", "scripts"):
        shutil.copytree(ROOT / name, snapshot / name, ignore=ignore)
    source_hash = source_fingerprint(snapshot)
    print(json.dumps({"frozen_source_copy": str(snapshot), "python_sources_sha256": source_hash}), flush=True)
    with psycopg.connect(host="127.0.0.1", port=PORT, user=DATABASE_USER,
                         password=config["POSTGRES_PASSWORD"], dbname="kdps_proof", connect_timeout=3) as conn:
        conn.autocommit = True
        system_id = conn.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()[0]
        for case in ("clean", "upgrade"):
            database = "kdps_rehearsal_migrations_" + uuid.uuid4().hex[:12]
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(DATABASE_USER)))
            env = proof_environment(config)
            env.update(KDPS_REHEARSAL_DB=database, KDPS_REHEARSAL_SYSTEM_ID=system_id,
                       KDPS_DEPLOYMENT_KEY=str(uuid.uuid4()), SEED_DEMO="0",
                       KDPS_REHEARSAL_PROJECT=PROJECT, KDPS_REHEARSAL_SOURCE_SHA256=source_hash,
                       PYTHONPATH=str(snapshot / "backend"))
            subprocess.run([str(ROOT / "backend/.venv/bin/python"), str(snapshot / "scripts" / Path(__file__).name), "--child", case],
                           cwd=snapshot, env=env, check=True)
    fingerprint = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    print(f"Rehearsal helper SHA-256: {fingerprint}; all sibling evidence preserved.")


if __name__ == "__main__":
    main()
