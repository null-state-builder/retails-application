#!/usr/bin/env python3
"""Stable SO-02 verification entry points used by local development and CI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
PYTHON = BACKEND / ".venv/bin/python"
PROOF = [sys.executable, str(ROOT / "scripts/proof.py")]


def execute(command: list[str], *, cwd: Path = ROOT, proof: bool = False) -> None:
    if proof:
        cwd_argument = ["--cwd", cwd.name] if cwd in {BACKEND, FRONTEND} else []
        command = [*PROOF, "run", *cwd_argument, "--", *command]
        cwd = ROOT
    print(f"Running: {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def backend_checks() -> None:
    execute([str(PYTHON), "manage.py", "check"], cwd=BACKEND, proof=True)
    execute([str(PYTHON), "manage.py", "makemigrations", "--check", "--dry-run"], cwd=BACKEND, proof=True)
    execute([str(PYTHON), "-m", "ruff", "check", "."], cwd=BACKEND, proof=True)
    execute([str(PYTHON), "-m", "mypy", "."], cwd=BACKEND, proof=True)
    execute([str(BACKEND / ".venv/bin/lint-imports")], cwd=BACKEND, proof=True)
    execute([str(PYTHON), "-m", "pytest", "-q"], cwd=BACKEND, proof=True)


def frontend_checks() -> None:
    for script in ("typecheck", "lint", "format:check", "test"):
        execute(["yarn", script], cwd=FRONTEND)


def api_contract(*, generate: bool) -> None:
    execute(
        [sys.executable, str(ROOT / "scripts/api-contract.py"), "generate" if generate else "check"],
        proof=True,
    )


def build() -> None:
    execute(["yarn", "build"], cwd=FRONTEND)
    execute(["yarn", "check:assets"], cwd=FRONTEND)


def browser() -> None:
    build()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".local" / "playwright-browsers")
    execute(["yarn", "test:browser"], cwd=FRONTEND)


def seed_repeatability() -> None:
    execute([str(PYTHON), str(ROOT / "scripts/check-seed-repeatability.py")], proof=True)


def launcher_smoke() -> None:
    execute([sys.executable, str(ROOT / "scripts/check-launcher.py")])


def run_with_proof(action: str) -> None:
    existing = subprocess.run([*PROOF, "status"], cwd=ROOT, capture_output=True).returncode == 0
    try:
        execute([*PROOF, "up"])
        if action in {"browser", "seed", "launcher", "verify"}:
            execute([*PROOF, "prepare"])
        if action in {"browser", "verify"}:
            execute([str(PYTHON), str(ROOT / "scripts/so03_browser_fixture.py")], proof=True)
        if action in {"seed", "verify"}:
            seed_repeatability()
        if action in {"launcher", "verify"}:
            launcher_smoke()
        if action in {"backend", "verify"}:
            backend_checks()
        if action in {"api-check", "verify"}:
            api_contract(generate=False)
        if action == "api-generate":
            api_contract(generate=True)
        if action == "verify":
            frontend_checks()
        if action in {"browser", "verify"}:
            browser()
    finally:
        if not existing:
            execute([*PROOF, "down"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["backend", "frontend", "api-check", "api-generate", "browser", "seed", "launcher", "verify"]
    )
    action = parser.parse_args().action
    if action == "frontend":
        frontend_checks()
    else:
        run_with_proof(action)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from error
