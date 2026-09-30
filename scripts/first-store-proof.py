"""Create or run the owned blank-installation browser rehearsal; never reset data.

The normal proof fixture stays intact. The sibling database is created only on
the already identified proof cluster, with a generated, recorded name. Every
subsequent command verifies database/user/version/system identifier before it
runs. Credentials and deployment identity remain in private local files.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid

from proof import (
    DATABASE_USER,
    PORT,
    PROJECT,
    ROOT,
    assert_database,
    proof_config,
    proof_environment,
)

RECORD = ROOT / ".local" / "first-store-proof.json"
PYTHON = ROOT / "backend/.venv/bin/python"


def environment(record: dict[str, str]) -> dict[str, str]:
    result = proof_environment(proof_config(create=False))
    result.update(KDPS_REHEARSAL_DB=record["database"],
                  KDPS_DEPLOYMENT_KEY=record["deployment_key"], SEED_DEMO="0",
                  CSRF_TRUSTED_ORIGINS="http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:5178")
    return result


def connection(database: str):
    import psycopg

    config = proof_config(create=False)
    return psycopg.connect(host="127.0.0.1", port=PORT, user=DATABASE_USER,
                           password=config["POSTGRES_PASSWORD"], dbname=database, connect_timeout=3)


def identity(database: str) -> dict[str, str]:
    with connection(database) as conn:
        row = conn.execute("SELECT current_database(), current_user, current_setting('server_version_num'), system_identifier::text FROM pg_control_system()").fetchone()
    if row is None or row[:3] != (database, DATABASE_USER, "170011"):
        raise RuntimeError("Blank rehearsal database identity mismatch; nothing was run.")
    return {"database": row[0], "user": row[1], "postgresql_version": "17.11", "system_identifier": row[3]}


def verified_record() -> dict[str, str]:
    assert_database()
    if not RECORD.exists():
        raise RuntimeError("No owned blank rehearsal is recorded. Run first-store-proof.py create first.")
    record = json.loads(RECORD.read_text())
    if not isinstance(record, dict) or not str(record.get("database", "")).startswith("kdps_rehearsal_first_store_"):
        raise RuntimeError("Blank rehearsal record is invalid; nothing was run.")
    base = identity("kdps_proof")
    target = identity(record["database"])
    if target["system_identifier"] != base["system_identifier"] or target["system_identifier"] != record["system_identifier"]:
        raise RuntimeError("Blank rehearsal is not on the owned proof cluster; nothing was run.")
    return record


def show(record: dict[str, str]) -> None:
    print(json.dumps({"project": PROJECT, "container": f"{PROJECT}-database-1",
                      "host": "127.0.0.1", "port": PORT, **identity(record["database"])}, sort_keys=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "status", "run"])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "create":
        assert_database()
        if RECORD.exists():
            record = verified_record()
            show(record)
            print("Existing rehearsal preserved. No database or registration was reset.")
            return
        from psycopg import sql

        base = identity("kdps_proof")
        database = "kdps_rehearsal_first_store_" + uuid.uuid4().hex[:12]
        with connection("kdps_proof") as conn:
            conn.autocommit = True
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(DATABASE_USER)))
        record = {"database": database, "system_identifier": base["system_identifier"],
                  "deployment_key": str(uuid.uuid4())}
        descriptor = os.open(RECORD, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(record, output)
        record = verified_record()
        show(record)  # Identity is documented before database-dependent commands.
        env = environment(record)
        subprocess.run([str(PYTHON), "manage.py", "migrate", "--noinput"], cwd=ROOT / "backend", env=env, check=True)
        subprocess.run([str(PYTHON), "manage.py", "initialise_installation"], cwd=ROOT / "backend", env=env, check=True)
        return
    record = verified_record()
    show(record)
    if args.action == "status":
        return
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise RuntimeError("Supply a command after run --.")
    subprocess.run(command, cwd=ROOT, env=environment(record), check=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
