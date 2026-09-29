#!/usr/bin/env python3
"""Generate or check the frontend's OpenAPI types from the current Django API.

The check mode never writes a tracked file. Both modes use the locally installed,
locked toolchain, so a backend schema change cannot silently keep old client types.
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
TARGET = FRONTEND / "src/lib/api-schema.ts"
PYTHON = BACKEND / ".venv/bin/python"
OPENAPI_TYPESCRIPT = FRONTEND / "node_modules/.bin/openapi-typescript"


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        raise SystemExit(result.returncode)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("generate", "check"))
    args = parser.parse_args()
    for executable in (PYTHON, OPENAPI_TYPESCRIPT):
        if not executable.is_file():
            raise SystemExit(f"Missing {executable}; run npm run setup first")

    schema = run(
        [str(PYTHON), "manage.py", "spectacular", "--format", "openapi-json"],
        BACKEND,
    )
    try:
        document = json.loads(schema.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(f"Django generated invalid OpenAPI JSON: {error}") from error
    if not isinstance(document.get("paths"), dict) or not document["paths"]:
        raise SystemExit("Django generated an empty OpenAPI path map")
    errors: list[str] = []
    warnings: list[str] = []
    if schema.stderr:
        # drf-spectacular emits view diagnostics with a source location, but
        # enum and operation-id collisions begin with a plain "Warning:".
        diagnostics = schema.stderr.splitlines()
        errors = [line for line in diagnostics if ": Error [" in line]
        warnings = [
            line for line in diagnostics
            if ": Warning [" in line or line.startswith("Warning:")
        ]
        print(
            f"OpenAPI schema diagnostics: {len(errors)} errors, "
            f"{len(warnings)} warnings.",
            file=sys.stderr,
        )
        for line in (errors + warnings)[:10]:
            print(line, file=sys.stderr)
    if errors or warnings:
        raise SystemExit(
            "OpenAPI has unresolved schema diagnostics; correct the affected views "
            "or enum names before accepting the generated client contract"
        )

    with tempfile.TemporaryDirectory(prefix="kdps-openapi-") as temporary:
        schema_file = Path(temporary) / "schema.json"
        schema_file.write_text(schema.stdout, encoding="utf-8")
        generated = run([str(OPENAPI_TYPESCRIPT), str(schema_file)], FRONTEND)
    if generated.stderr:
        print(generated.stderr, file=sys.stderr)
    if not generated.stdout.strip():
        raise SystemExit("openapi-typescript produced no client types")

    if args.mode == "generate":
        TARGET.write_text(generated.stdout, encoding="utf-8")
        print(f"Generated {TARGET.relative_to(ROOT)} from {len(document['paths'])} API paths")
        return
    committed = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
    if committed != generated.stdout:
        difference = difflib.unified_diff(
            committed.splitlines(), generated.stdout.splitlines(),
            fromfile=str(TARGET.relative_to(ROOT)), tofile="Django-generated contract", lineterm="",
        )
        for line in list(difference)[:32]:
            print(line, file=sys.stderr)
        raise SystemExit(
            f"API contract drift in {TARGET.relative_to(ROOT)}; run npm run api:generate "
            "and review the changes"
        )
    print(f"API contract matches {len(document['paths'])} Django paths")


if __name__ == "__main__":
    main()
