"""Own a separate synthetic sibling for transfer dispatch and destination acceptance browser proof.

Clone only the verified ALPHA rehearsal. Existing siblings and credentials are
preserved; every run rechecks the database/user/cluster identity before work.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / ".local/first-store-transfer-proof.json"
CREDENTIALS = ROOT / ".local/first-store-transfer-credentials.json"


def helper() -> Any:
    spec = importlib.util.spec_from_file_location("transfer_proof", ROOT / "scripts/first-store-proof.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The owned proof verifier is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_write(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="transfer-proof-", dir=path.parent)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
    os.replace(temporary, path)


def verified(proof: Any) -> dict[str, str]:
    base = proof.verified_record()
    if not RECORD.exists() or stat.S_IMODE(RECORD.stat().st_mode) != 0o600:
        raise RuntimeError("The transfer proof identity must be private (0600).")
    record = json.loads(RECORD.read_text())
    if (not record["database"].startswith("kdps_rehearsal_transfer_")
            or record["template_database"] != base["database"]):
        raise RuntimeError("The transfer proof identity is not owned.")
    actual = proof.identity(record["database"])
    if actual["system_identifier"] != base["system_identifier"] or actual["system_identifier"] != record["system_identifier"]:
        raise RuntimeError("The transfer proof cluster changed; nothing was run.")
    with proof.connection(record["database"]) as conn:
        rows = conn.execute("SELECT code, synthetic FROM masters_tenant").fetchall()
    if rows != [("ALPHA", True)]:
        raise RuntimeError("Only the sole synthetic ALPHA proof shop is permitted.")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("action", choices=["create", "status", "run"])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    forwarded_input = None
    if (args.action == "run" and len(command) == 3
            and Path(command[0]).resolve() == (ROOT / "backend/.venv/bin/python").resolve()
            and command[1:] == ["scripts/first-store-transfer-fixture.py", "expire-receiver-session"]):
        # Docker verification subprocesses can consume inherited stdin. Capture
        # only this bounded test-session payload before verification, then give
        # it directly to the exact owned helper; never put a token in argv.
        forwarded_input = sys.stdin.buffer.read(4097)
        if not forwarded_input or len(forwarded_input) > 4096:
            raise RuntimeError("The exact forced-expiry payload must be nonempty and bounded.")
    proof = helper()
    base = proof.verified_record()
    if args.fresh and args.action != "create":
        raise RuntimeError("--fresh is only valid for create.")
    if args.action == "create" and (not RECORD.exists() or args.fresh):
        from psycopg import sql

        source = ROOT / ".local/first-store-browser-credentials.json"
        if stat.S_IMODE(source.stat().st_mode) != 0o600:
            raise RuntimeError("The source fictional credentials are not private.")
        credentials = json.loads(source.read_text())
        if (credentials.get("rehearsal_identity", {}).get("database") != base["database"]
                or credentials.get("rehearsal_identity", {}).get("system_identifier") != base["system_identifier"]
                or credentials.get("browser_bootstrap", {}).get("completed") is not True):
            raise RuntimeError("Complete the exact owned signup/opening browser proof before cloning.")
        with proof.connection(base["database"]) as conn:
            rows = conn.execute("SELECT code, synthetic FROM masters_tenant").fetchall()
            ready = conn.execute("SELECT g.goods_ready, g.sell_ready, g.freeze_id FROM masters_siteguard g JOIN masters_store s ON s.id=g.site_id WHERE s.code='FIRST' AND g.lifecycle='active'").fetchall()
            paused = conn.execute("SELECT EXISTS(SELECT 1 FROM sell_till_pause WHERE resumed_at IS NULL)").fetchone()[0]
        if rows != [("ALPHA", True)] or not ready or paused or any(not goods or not selling or freeze for goods, selling, freeze in ready):
            raise RuntimeError("Clone only the completed synthetic ALPHA shop after count proof finishes.")
        if RECORD.exists():
            previous = verified(proof)
            for path in (RECORD, CREDENTIALS):
                archive = path.with_name(f"{path.stem}-{previous['database']}.json")
                if archive.exists():
                    raise RuntimeError("The previous transfer identity is already archived; preserved.")
                with archive.open("xb") as stream:
                    stream.write(path.read_bytes())
                archive.chmod(0o600)
        database = "kdps_rehearsal_transfer_" + uuid.uuid4().hex[:12]
        proof.show(base)
        with proof.connection("kdps_proof") as conn:
            conn.autocommit = True
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(base["database"]), sql.Identifier("kdps_proof")))
        record = {**base, "database": database, "template_database": base["database"]}
        private_write(RECORD, record)
        credentials["transfer_rehearsal"] = record
        credentials.pop("transfer_fixture", None)
        private_write(CREDENTIALS, credentials)
    record = verified(proof)
    proof.show(record)
    if args.action in ("create", "status"):
        print("Owned synthetic transfer sibling verified; original history preserved.")
        return
    if not command:
        raise RuntimeError("Supply a command after run --.")
    env = proof.environment(record)
    env["CSRF_TRUSTED_ORIGINS"] = "http://127.0.0.1:5184"
    subprocess.run(command, cwd=ROOT, env=env, check=True, input=forwarded_input)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error))
        raise SystemExit(1) from error
