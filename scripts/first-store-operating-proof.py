"""Clone the identified fictional first-store proof for destructive browser journeys.

The existing proof shop and its history are preserved. Only its positively
identified owned sibling can be the template; no real tenant/database is read.
Credentials stay in private local records and are never emitted.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / ".local/first-store-operating-proof.json"


def helper() -> Any:
    spec = importlib.util.spec_from_file_location("operating_proof", ROOT / "scripts/first-store-proof.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The owned proof verifier is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verified(proof: Any) -> dict[str, str]:
    base = proof.verified_record()
    if stat.S_IMODE(RECORD.stat().st_mode) != 0o600:
        raise RuntimeError("The operating proof identity must be private.")
    record = json.loads(RECORD.read_text())
    if not record["database"].startswith("kdps_rehearsal_operating_") or record["template_database"] != base["database"]:
        raise RuntimeError("The operating proof identity is not owned.")
    actual = proof.identity(record["database"])
    if actual["system_identifier"] != base["system_identifier"] or actual["system_identifier"] != record["system_identifier"]:
        raise RuntimeError("The operating proof cluster changed; nothing was run.")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true", help="Create another owned copy, preserving the previous database and identity record.")
    parser.add_argument("action", choices=["create", "status", "run"])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    proof = helper()
    base = proof.verified_record()
    if args.fresh and args.action != "create":
        raise RuntimeError("--fresh is only valid for create.")
    if args.action == "create" and (not RECORD.exists() or args.fresh):
        from psycopg import sql

        # Verify the source is the sole fictional registration before cloning.
        with proof.connection(base["database"]) as conn:
            rows = conn.execute("SELECT code, synthetic FROM masters_tenant").fetchall()
        if rows != [("ALPHA", True)]:
            raise RuntimeError("Only the sole synthetic ALPHA proof shop may be cloned.")
        database = "kdps_rehearsal_operating_" + uuid.uuid4().hex[:12]
        proof.show(base)  # Source identity documented before copying business data.
        with proof.connection("kdps_proof") as conn:
            conn.autocommit = True
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(base["database"]), sql.Identifier("kdps_proof")))
        record = {**base, "database": database, "template_database": base["database"]}
        if RECORD.exists():
            previous = verified(proof)
            archive = RECORD.with_name(f"first-store-operating-proof-{previous['database']}.json")
            if not archive.exists():
                fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(RECORD.read_bytes())
        fd, temporary = tempfile.mkstemp(prefix="operating-identity-", dir=RECORD.parent)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
        os.replace(temporary, RECORD)
    record = verified(proof)
    proof.show(record)
    if args.action in ("create", "status"):
        print("Owned fictional copy preserved. Existing first-store history was not changed.")
        return
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise RuntimeError("Supply a command after run --.")
    env = proof.environment(record)
    env["CSRF_TRUSTED_ORIGINS"] = "http://127.0.0.1:5180"
    subprocess.run(command, cwd=ROOT, env=env, check=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error))
        raise SystemExit(1) from error
