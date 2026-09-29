"""Run the local API, worker and frontend; Ctrl-C stops these processes only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"
PYTHON = ROOT / "backend/.venv/bin/python"
VITE = ROOT / "frontend/node_modules/.bin/vite"
STARTUP_TIMEOUT_SECONDS = 60


def preflight(*, proof: bool) -> None:
    requirements = (PYTHON, VITE) if proof else (PYTHON, VITE, ROOT / "backend/.env")
    if not all(path.exists() for path in requirements):
        raise SystemExit("Local dependencies/configuration are missing; run `npm run setup` first.")
    if not proof:
        subprocess.run(["python3", str(ROOT / "scripts/init-local.py")], cwd=ROOT, check=True)
    for port in (8000, 5173):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise SystemExit(
                    f"Port {port} is occupied; stop that server before starting KDPS."
                ) from error
    if proof:
        from proof import assert_database

        try:
            assert_database()
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        return
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 55432)):
            raise SystemExit(
                "Local database is unavailable; start it with "
                "`docker compose -f compose.local.yaml up -d --wait database`."
            )
    # Port availability alone does not prove this is the development database.
    result = subprocess.run(
        [
            "docker", "compose", "-f", str(ROOT / "compose.local.yaml"),
            "exec", "-T", "database", "psql", "-X", "-A", "-t",
            "-U", "kdps_local", "-d", "kdps_local", "-c",
            "SELECT current_setting('server_version_num'), current_database(), current_user",
        ],
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode or result.stdout.strip().split("|") != ["170011", "kdps_local", "kdps_local"]:
        raise SystemExit("The local PostgreSQL 17.11 container is unavailable or has the wrong identity.")


def ready(url: str, *, api: bool) -> bool:
    try:
        with urlopen(url, timeout=1) as response:
            if response.status != 200:
                return False
            body = response.read()
    except (OSError, URLError):
        return False
    if api:
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return False
        return data.get("status") == "ok" and data.get("service") == "kdps-backend"
    return b"<html" in body.lower()


def interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", action="store_true", help="Use the disposable SO-02 proof database")
    proof = parser.parse_args().proof
    preflight(proof=proof)
    process_environment = None
    if proof:
        from proof import proof_config, proof_environment

        process_environment = proof_environment(proof_config(create=False))
    commands = [
        ("backend", ROOT / "backend", [str(PYTHON), "-m", "uvicorn", "config.asgi:application", "--host", "127.0.0.1", "--port", "8000"]),
        ("worker", ROOT / "backend", [str(PYTHON), "manage.py", "run_goods_worker"]),
        ("frontend", ROOT / "frontend", [str(VITE)]),
    ]
    processes: list[tuple[str, subprocess.Popen[bytes]]] = []
    logs = []
    signal.signal(signal.SIGTERM, interrupt)
    try:
        for name, cwd, command in commands:
            log_path = LOCAL / f"{'proof-' if proof else ''}{name}.log"
            log = log_path.open("a")
            logs.append(log)
            process = subprocess.Popen(
                command, cwd=cwd, env=process_environment, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
            processes.append((name, process))
        print("Waiting for KDPS API and frontend readiness...", flush=True)
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while True:
            for name, process in processes:
                if process.poll() is not None:
                    raise SystemExit(f"{name} exited ({process.returncode}); see {LOCAL / (('proof-' if proof else '') + name + '.log')}")
            if ready("http://127.0.0.1:8000/api/health", api=True) and ready(
                "http://127.0.0.1:5173/", api=False
            ):
                break
            if time.monotonic() >= deadline:
                raise SystemExit(f"Startup timed out after {STARTUP_TIMEOUT_SECONDS}s; inspect logs in {LOCAL}.")
            time.sleep(0.5)
        print("KDPS ready at http://127.0.0.1:5173", flush=True)
        print(f"Logs: {LOCAL}\nCtrl-C stops the API, worker and frontend; database data is retained.", flush=True)
        while True:
            for name, process in processes:
                if process.poll() is not None:
                    raise SystemExit(f"{name} exited ({process.returncode}); see {LOCAL / (('proof-' if proof else '') + name + '.log')}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping local application processes.", flush=True)
    finally:
        for _, process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for _, process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
