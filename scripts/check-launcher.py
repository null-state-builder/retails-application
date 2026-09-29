"""Exercise proof-backed launcher refusal, readiness, cleanup and restart."""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from proof import COMPOSE_COMMAND, PROJECT

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = [sys.executable, str(ROOT / "scripts/dev-local.py"), "--proof"]


def listening(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_ready(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"Launcher exited before readiness ({process.returncode}): {output}")
        if listening(8000) and listening(5173):
            try:
                with urlopen("http://127.0.0.1:8000/api/health", timeout=1) as api:
                    api_ready = api.status == 200 and b"kdps-backend" in api.read()
                with urlopen("http://127.0.0.1:5173/", timeout=1) as page:
                    page_ready = page.status == 200 and b"<html" in page.read().lower()
                if api_ready and page_ready:
                    return
            except (OSError, URLError):
                pass
        time.sleep(0.5)
    raise RuntimeError("Launcher did not serve API and frontend before timeout.")


def stopped() -> bool:
    return not listening(8000) and not listening(5173)


def missing_prerequisite() -> None:
    """A missing application runtime is refused before any child starts."""
    spec = importlib.util.spec_from_file_location("kdps_dev_launcher", ROOT / "scripts/dev-local.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not import the launcher for its preflight check.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PYTHON = ROOT / ".local" / "missing-launcher-python"
    if module.PYTHON.exists():
        raise RuntimeError("Missing-prerequisite fixture unexpectedly exists.")
    try:
        module.preflight(proof=True)
    except SystemExit as error:
        if "npm run setup" not in str(error):
            raise RuntimeError(f"Missing prerequisite gave no setup instruction: {error}") from error
    else:
        raise RuntimeError("Launcher accepted a missing backend runtime.")


def proof_project_isolation() -> None:
    """An inherited Compose project name must not select the working database."""
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = "kdps-local"
    result = subprocess.run(
        [*COMPOSE_COMMAND, "config", "--format", "json"],
        cwd=ROOT, text=True, capture_output=True, env=env, check=True,
    )
    actual = json.loads(result.stdout)["name"]
    if actual != PROJECT:
        raise RuntimeError(f"Proof Compose project changed under inherited environment: {actual}")


def unavailable_database() -> None:
    """Point this one launcher probe at a nonexistent proof project."""
    code = """
import importlib.util
from pathlib import Path
import sys
root, fixture = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(root / 'scripts'))
import proof
proof.COMPOSE_COMMAND = ['docker', 'compose', '-p', fixture, '-f', str(proof.COMPOSE)]
spec = importlib.util.spec_from_file_location('kdps_dev_launcher', root / 'scripts/dev-local.py')
if spec is None or spec.loader is None:
    raise RuntimeError('Could not import the launcher')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.argv = [str(root / 'scripts/dev-local.py'), '--proof']
module.main()
"""
    fixture = "kdps-proof-unavailable-" + secrets.token_hex(6)
    refusal = subprocess.run(
        [sys.executable, "-c", code, str(ROOT), fixture],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    if (
        refusal.returncode == 0
        or "isolated proof database is unavailable" not in refusal.stdout + refusal.stderr
    ):
        raise RuntimeError(
            "Launcher failed to refuse an unavailable proof database: "
            + refusal.stdout + refusal.stderr
        )


def failed_child() -> None:
    """A worker crash must stop the API and frontend processes this launch owned."""
    with tempfile.TemporaryDirectory(prefix="launcher-failure-", dir=ROOT / ".local") as tmp:
        shim = Path(tmp) / "python"
        real_python = ROOT / "backend/.venv/bin/python"
        shim.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = "manage.py" ]; then exit 37; fi\n'
            f'exec "{real_python}" "$@"\n'
        )
        shim.chmod(0o700)
        code = """
import importlib.util
from pathlib import Path
import sys
root, shim = map(Path, sys.argv[1:3])
sys.path.insert(0, str(root / 'scripts'))
spec = importlib.util.spec_from_file_location('kdps_dev_launcher', root / 'scripts/dev-local.py')
if spec is None or spec.loader is None:
    raise RuntimeError('Could not import the launcher')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.PYTHON = shim
sys.argv = [str(root / 'scripts/dev-local.py'), '--proof']
module.main()
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(ROOT), str(shim)],
            cwd=ROOT, text=True, capture_output=True, timeout=75, check=False,
        )
        if result.returncode == 0 or "worker exited (37)" not in result.stdout + result.stderr:
            raise RuntimeError("Launcher did not report its failed worker: " + result.stdout + result.stderr)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not stopped():
        time.sleep(0.2)
    if not stopped():
        raise RuntimeError("API or frontend remained after a worker failure.")


def cycle() -> None:
    process = subprocess.Popen(
        LAUNCHER, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        wait_ready(process)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            output, _ = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            raise RuntimeError("Launcher did not stop its application processes within 20s")
        if process.returncode != 0:
            raise RuntimeError(f"Launcher failed during shutdown ({process.returncode}): {output}")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not stopped():
        time.sleep(0.2)
    if not stopped():
        raise RuntimeError("API or frontend port remained occupied after launcher shutdown.")


if not stopped():
    raise SystemExit("Launcher smoke requires free API/frontend ports 8000 and 5173.")

missing_prerequisite()
proof_project_isolation()
unavailable_database()

with socket.socket() as occupied:
    occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    occupied.bind(("127.0.0.1", 8000))
    occupied.listen()
    refusal = subprocess.run(LAUNCHER, cwd=ROOT, text=True, capture_output=True, check=False)
    if refusal.returncode == 0 or "Port 8000 is occupied" not in refusal.stdout + refusal.stderr:
        raise SystemExit("Launcher failed to refuse an occupied API port.")

failed_child()
cycle()
cycle()
print(
    "Launcher refused a missing runtime, unavailable proof database and occupied port; "
    "cleaned up after a worker crash; reached readiness, stopped and restarted cleanly."
)
