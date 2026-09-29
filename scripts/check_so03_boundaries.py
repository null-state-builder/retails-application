"""Guard retired surfaces and freeze surviving legacy permission dependencies.

The reviewed allowlist is intentionally not generated during `--write`: adding
a legacy dependency must first have an explicit owner and retirement decision.
Existing edges may be removed, and the allowlist must then contract with them.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _sources(manifest: dict[str, Any]) -> list[str]:
    return sorted(
        {
            row["source"]
            for row in manifest["discovered"]["records"]
            if row["kind"] in {"module", "operational_file"}
            and row["source"].endswith(".py")
            and not row["source"].startswith("external:")
        }
    )


def _import_targets(source: str, node: ast.ImportFrom) -> set[str]:
    if node.level:
        parts = list(Path(source).with_suffix("").parts)
        if parts and parts[0] == "backend":
            parts = parts[1:]
        parent = parts[: -node.level]
        if not parent:
            return set()
        base = ".".join([*parent, *([node.module] if node.module else [])])
    else:
        base = node.module or ""
    return {base, *(base + "." + alias.name for alias in node.names)}


def _is_user_reference(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id == "user"
        or isinstance(node, ast.Attribute)
        and node.attr == "user"
        and isinstance(node.value, ast.Name)
        and node.value.id == "request"
    )


def derive_legacy_allowlist(
    manifest: dict[str, Any], root: Path = ROOT
) -> list[dict[str, Any]]:
    """Report current legacy edges for a *one-time human-reviewed* baseline."""
    policy = manifest["protected_boundaries"]
    legacy_modules = set(policy["legacy_permission_modules"])
    legacy_symbols = set(policy["legacy_permission_symbols"])
    found: Counter[tuple[str, str]] = Counter()
    for source in _sources(manifest):
        tree = ast.parse((root / source).read_text(encoding="utf-8"), filename=source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in legacy_modules:
                        found[(source, f"import:{alias.name}")] += 1
            elif isinstance(node, ast.ImportFrom):
                for target in _import_targets(source, node) & legacy_modules:
                    found[(source, f"import:{target}")] += 1
                for alias in node.names:
                    # A renamed import still introduces the protected source,
                    # even if no later Name node spells the old identifier.
                    if alias.name in legacy_symbols:
                        found[(source, f"symbol_import:{alias.name}")] += 1
            elif (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in legacy_symbols
            ):
                found[(source, f"symbol:{node.id}")] += 1
            elif isinstance(node, ast.Attribute) and node.attr in legacy_symbols:
                found[(source, f"symbol_attribute:{node.attr}")] += 1
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in {"role", "scope_type", "is_superuser"}
                and _is_user_reference(node.value)
            ):
                # These old User fields can be read for migration evidence, but
                # may not become new application authority.
                found[(source, f"legacy_field:user.{node.attr}")] += 1
    return [
        {"source": source, "reference": reference, "occurrences": count}
        for (source, reference), count in sorted(found.items())
    ]


def check_boundaries(manifest: dict[str, Any], root: Path = ROOT) -> list[str]:
    """Return structural regressions; both checker modes must reject them."""
    policy = manifest.get("protected_boundaries")
    if not isinstance(policy, dict):
        return ["protected_boundaries policy is missing"]
    required = (
        "legacy_permission_modules",
        "legacy_permission_symbols",
        "legacy_permission_allowlist",
        "retired_modules",
        "retired_paths",
        "retired_api_prefixes",
    )
    missing = [field for field in required if not isinstance(policy.get(field), list)]
    if missing:
        return [f"protected_boundaries needs list fields: {', '.join(missing)}"]
    for field in required:
        if field == "legacy_permission_allowlist":
            if any(
                not isinstance(row, dict)
                or not isinstance(row.get("source"), str)
                or not isinstance(row.get("reference"), str)
                or not isinstance(row.get("occurrences"), int)
                or isinstance(row.get("occurrences"), bool)
                or row["occurrences"] < 1
                for row in policy[field]
            ):
                return [
                    "legacy_permission_allowlist needs source/reference/occurrences entries"
                ]
        elif any(not isinstance(item, str) or not item for item in policy[field]):
            return [f"protected_boundaries.{field} needs non-empty strings"]
    if policy.get("reference_review_required"):
        dispositions = {
            "historical_evidence", "unmounted_legacy", "assignment_backed_adapter",
            "disabled_legacy_callback", "active_legacy_authority",
        }
        for row in policy["legacy_permission_allowlist"]:
            if (
                row.get("disposition") not in dispositions
                or row.get("trace_status") not in {"verified", "needs_resource_trace"}
                or not isinstance(row.get("reviewed_chain"), str)
                or not row["reviewed_chain"].strip()
            ):
                return [
                    "Every frozen permission edge needs disposition, trace_status and reviewed_chain"
                ]
    records = manifest.get("discovered", {}).get("records", [])
    findings: list[str] = []

    actual = {
        (row["source"], row["reference"]): row["occurrences"]
        for row in derive_legacy_allowlist(manifest, root)
    }
    allowed = {
        (row["source"], row["reference"]): row["occurrences"]
        for row in policy["legacy_permission_allowlist"]
    }
    for source, reference in sorted(actual.keys() - allowed.keys()):
        findings.append(f"New legacy permission dependency: {source} → {reference}")
    for source, reference in sorted(allowed.keys() - actual.keys()):
        findings.append(
            f"Stale legacy permission exception: {source} → {reference}; remove it"
        )
    for source, reference in sorted(actual.keys() & allowed.keys()):
        if actual[(source, reference)] > allowed[(source, reference)]:
            findings.append(
                f"Expanded legacy permission dependency: {source} → {reference} "
                f"({allowed[(source, reference)]} → {actual[(source, reference)]} occurrences)"
            )
        elif actual[(source, reference)] < allowed[(source, reference)]:
            findings.append(
                f"Contracted legacy permission dependency: {source} → {reference}; "
                "lower its reviewed allowance"
            )

    retired_modules = set(policy["retired_modules"])
    for row in records:
        if row["kind"] not in {"module", "operational_file"}:
            continue
        for imported in row["detail"].get("imports", []):
            if imported in retired_modules or any(
                imported.startswith(name + ".") for name in retired_modules
            ):
                findings.append(
                    f"Retired module dependency: {row['source']} → {imported}"
                )
    for source in sorted(set(policy["retired_paths"])):
        if (root / source).exists():
            findings.append(f"Retired source path reintroduced: {source}")
    for row in records:
        if row["kind"] != "api":
            continue
        route = row["key"].split(":", 2)[2]
        for prefix in policy["retired_api_prefixes"]:
            if route.startswith(prefix):
                findings.append(f"Retired API reintroduced: {row['key']}")
    return sorted(set(findings))
