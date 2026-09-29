#!/usr/bin/env python3
"""Run verification only against SO-02's disposable PostgreSQL 17.11 stack."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent.parent
PROOF_ENV = ROOT / ".local" / "proof-postgres.env"
COMPOSE = ROOT / "compose.proof.yaml"
DATABASE_NAME = "kdps_proof"
DATABASE_USER = "kdps_proof"
TEST_DATABASE_NAME = "kdps_proof_test"
PORT = 55433
COMPOSE_COMMAND = ["docker", "compose", "-f", str(COMPOSE)]


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"{name} is required for proof verification; install it first.")


def values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def proof_config(*, create: bool) -> dict[str, str]:
    if not PROOF_ENV.exists():
        if not create:
            raise RuntimeError("Proof credentials are absent. Run `python3 scripts/proof.py up` first.")
        PROOF_ENV.parent.mkdir(exist_ok=True)
        password = secrets.token_hex(24)
        with PROOF_ENV.open("x") as stream:
            stream.write(
                f"POSTGRES_USER={DATABASE_USER}\n"
                f"POSTGRES_DB={DATABASE_NAME}\n"
                f"POSTGRES_PASSWORD={password}\n"
            )
        PROOF_ENV.chmod(0o600)
    config = values(PROOF_ENV)
    if (
        config.get("POSTGRES_USER") != DATABASE_USER
        or config.get("POSTGRES_DB") != DATABASE_NAME
        or not config.get("POSTGRES_PASSWORD")
    ):
        raise RuntimeError("Proof database settings do not match the isolated target; nothing was run.")
    return config


def proof_environment(config: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    password = quote(config["POSTGRES_PASSWORD"], safe="")
    inherited_python_path = env.get("PYTHONPATH", "")
    env.update(
        KDPS_PROOF_MODE="1",
        DATABASE_URL=f"postgresql://{DATABASE_USER}:{password}@127.0.0.1:{PORT}/{DATABASE_NAME}",
        KDPS_TEST_DB_NAME=TEST_DATABASE_NAME,
        DJANGO_DEBUG="1",
        DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost",
        KDPS_COOKIE_SECURE="0",
        CSRF_TRUSTED_ORIGINS="http://127.0.0.1:5173,http://localhost:5173",
        SEED_DEMO="1",
        SEED_CREDENTIALS_PATH=str(ROOT / ".local" / "proof-test-credentials.md"),
        PYTHONPATH=str(ROOT / "backend") + (os.pathsep + inherited_python_path if inherited_python_path else ""),
    )
    env.pop("KDPS_REHEARSAL_DB", None)
    return env


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*COMPOSE_COMMAND, *args], cwd=ROOT, text=True, check=check,
    )


def assert_database() -> None:
    """Check the server identity before any migration, seed or application test."""
    result = subprocess.run(
        [
            *COMPOSE_COMMAND, "exec", "-T", "database", "psql", "-X", "-A", "-t",
            "-U", DATABASE_USER, "-d", DATABASE_NAME, "-c",
            "SELECT current_setting('server_version_num'), current_database(), current_user",
        ],
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("The isolated proof database is unavailable; run `python3 scripts/proof.py up`.")
    fields = result.stdout.strip().split("|")
    if len(fields) != 3 or fields[0] != "170011" or fields[1:] != [DATABASE_NAME, DATABASE_USER]:
        raise RuntimeError("Proof database identity/version mismatch; verification refused.")


def run(command: list[str]) -> None:
    cwd = ROOT
    if len(command) >= 2 and command[0] == "--cwd":
        location = command[1]
        if location not in {"backend", "frontend"}:
            raise RuntimeError("Proof run --cwd accepts only backend or frontend.")
        cwd = ROOT / location
        command = command[2:]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise RuntimeError("Provide a command after `run --`.")
    config = proof_config(create=False)
    assert_database()
    subprocess.run(command, cwd=cwd, env=proof_environment(config), check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["up", "prepare", "run", "down", "status"])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    require_tool("docker")
    if args.action == "up":
        proof_config(create=True)
        compose("up", "-d", "--wait", "database")
        assert_database()
        print("Proof PostgreSQL 17.11 is ready on the isolated kdps_proof target.")
    elif args.action == "prepare":
        run([str(ROOT / "backend/.venv/bin/python"), "backend/manage.py", "migrate", "--noinput"])
        run([str(ROOT / "backend/.venv/bin/python"), "backend/manage.py", "seed_foundation"])
    elif args.action == "run":
        run(args.command)
    elif args.action == "status":
        proof_config(create=False)
        assert_database()
        print("Proof PostgreSQL 17.11 is ready.")
    else:
        # This project name and volume belong only to compose.proof.yaml.
        compose("down", "--volumes", "--remove-orphans")
        print("Proof database and its disposable volume removed.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Proof verification stopped: {error}", file=sys.stderr)
        raise SystemExit(1) from error
