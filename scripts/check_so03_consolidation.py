#!/usr/bin/env python3
"""Reconcile SO-03's retirement decisions with discovered code surfaces.

The JSON inventory is the machine-readable ledger. Discovery scripts report
facts only; ordered, explicit rules assign one primary retirement owner. The
register's generated tables are a readable projection of that same ledger.

`--check` is read-only and fails on new, removed, changed or unclassified
surfaces. `--write` deliberately refreshes the factual snapshot after an owner
has reviewed its classification rule and retirement evidence.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "docs/planning/so-03-consolidation-inventory.json"
REGISTER = ROOT / "docs/planning/so-03-consolidation-register.md"
DEPENDENCY_MAP = ROOT / "docs/planning/so-03-dependency-map.md"
ACCESS_BEGIN = "<!-- BEGIN GENERATED SO-03 ACCESS CUTOVER -->"
ACCESS_END = "<!-- END GENERATED SO-03 ACCESS CUTOVER -->"
RETIREMENTS_BEGIN = "<!-- BEGIN GENERATED SO-03 RETIREMENTS -->"
RETIREMENTS_END = "<!-- END GENERATED SO-03 RETIREMENTS -->"
COVERAGE_BEGIN = "<!-- BEGIN GENERATED SO-03 COVERAGE -->"
COVERAGE_END = "<!-- END GENERATED SO-03 COVERAGE -->"
LIFECYCLES = frozenset(
    {
        "canonical",
        "temporary_supported_legacy",
        "historical_read_only",
        "retirement_candidate",
        "unresolved",
    }
)
ACCESS_ENTRY_KINDS = frozenset({
    "api", "screen", "command", "scheduled_job", "job_handler",
    "signal_receiver", "middleware", "operational_file", "subview",
    "print_output", "display_window", "navigation", "bookmark",
})
REQUIRED_RAW = ("kind", "key", "source", "detail")
REQUIRED_RULE = ("id", "kind", "key_regex", "primary_id")


class InventoryError(ValueError):
    """An inventory decision or discovered surface needs human review."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _retirements(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if manifest.get("schema_version") != 1:
        raise InventoryError("Expected SO-03 inventory schema_version 1")
    rows = manifest.get("retirements")
    if not isinstance(rows, list):
        raise InventoryError("retirements must be a list")
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise InventoryError("Every retirement needs a string id")
        ident = row["id"]
        if ident in by_id:
            raise InventoryError(f"Duplicate retirement {ident}")
        for field in (
            "owner",
            "existing_inventory",
            "target_implementation",
            "data_mapping",
            "acceptance_evidence",
            "removal_condition",
            "default_lifecycle",
            "data_ownership",
            "authorization",
            "callers",
        ):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise InventoryError(f"{ident} needs {field}")
        if row["default_lifecycle"] not in LIFECYCLES:
            raise InventoryError(
                f"{ident}: unknown default_lifecycle {row['default_lifecycle']}"
            )
        by_id[ident] = row
    expected = {f"C{n:02d}" for n in range(1, 21)} | {"A00", "P00"}
    if set(by_id) != expected:
        raise InventoryError(
            f"Retirement IDs must be A00, P00 and C01-C20; got {sorted(by_id)}"
        )
    return by_id


def _raw_records() -> list[dict[str, Any]]:
    # Import by path so the checker also works from any current directory.
    sys.path.insert(0, str(ROOT))
    from scripts.so03_backend_inventory import discover_backend
    from scripts.so03_frontend_inventory import discover_frontend

    raw = [*discover_backend(ROOT), *discover_frontend(ROOT)]
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict) or any(field not in row for field in REQUIRED_RAW):
            raise InventoryError(f"Discovery record lacks {REQUIRED_RAW}: {row!r}")
        if any(
            not isinstance(row[field], str) or not row[field]
            for field in ("kind", "key", "source")
        ):
            raise InventoryError(
                f"Discovery record needs stable kind, key, source: {row!r}"
            )
        if not isinstance(row["detail"], dict):
            raise InventoryError(f"Discovery detail must be an object: {row['key']}")
        if row["key"] in seen:
            raise InventoryError(f"Duplicate discovered key {row['key']}")
        seen.add(row["key"])
        normalized.append({field: row[field] for field in REQUIRED_RAW})
    return sorted(normalized, key=lambda row: (row["kind"], row["key"]))


def _rules(
    manifest: dict[str, Any], owners: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rules = manifest.get("classification_rules")
    if not isinstance(rules, list):
        raise InventoryError("classification_rules must be a list")
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise InventoryError("Each classification rule must be an object")
        for field in REQUIRED_RULE:
            if not isinstance(rule.get(field), str) or not rule[field].strip():
                raise InventoryError(f"Rule {rule.get('id')!r} needs {field}")
        ident = rule["id"]
        if ident in seen:
            raise InventoryError(f"Duplicate classification rule {ident}")
        seen.add(ident)
        if rule["primary_id"] not in owners:
            raise InventoryError(f"{ident}: unknown primary_id {rule['primary_id']}")
        lifecycle = rule.get(
            "lifecycle", owners[rule["primary_id"]]["default_lifecycle"]
        )
        if lifecycle not in LIFECYCLES:
            raise InventoryError(f"{ident}: unknown lifecycle {lifecycle}")
        if lifecycle == "unresolved" and not rule.get("gate"):
            raise InventoryError(f"{ident}: unresolved surface needs a named gate")
        related = rule.get("related_ids", [])
        if not isinstance(related, list) or any(item not in owners for item in related):
            raise InventoryError(f"{ident}: invalid related_ids")
        if rule["primary_id"] in related:
            raise InventoryError(f"{ident}: primary_id must not repeat in related_ids")
        review = rule.get("access_contract")
        if review is not None:
            if not isinstance(review, dict) or review.get("trace_status") not in {"pending", "verified", "blocked"}:
                raise InventoryError(f"{ident}: invalid access_contract trace_status")
            if review["trace_status"] == "verified" and any(
                not review.get(field) for field in (
                    "actor_type", "required_action", "scope_source", "protected_fields",
                    "delivery_recheck", "named_tests", "reviewed_chain",
                )
            ):
                raise InventoryError(f"{ident}: verified access_contract needs a complete tested chain")
        for field in (
            "data_ownership",
            "authorization",
            "callers",
            "replacement",
            "migration_requirements",
            "acceptance_evidence",
            "removal_condition",
        ):
            if field in rule and (
                not isinstance(rule[field], str) or not rule[field].strip()
            ):
                raise InventoryError(f"{ident}: {field} override must be nonempty text")
        if "gate" in rule and (
            not isinstance(rule["gate"], str) or not rule["gate"].strip()
        ):
            raise InventoryError(f"{ident}: gate must be nonempty text")
        priority = rule.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise InventoryError(f"{ident}: priority must be an integer")
        for field in ("key_regex", "source_regex", "detail_reference_regex"):
            if field in rule:
                try:
                    re.compile(rule[field])
                except re.error as exc:
                    raise InventoryError(f"{ident}: invalid {field}: {exc}") from exc
    return rules


def _classify(
    raw: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    owners: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    classified: list[dict[str, Any]] = []
    hits: Counter[str] = Counter()
    indexed_rules: dict[
        str,
        list[tuple[dict[str, Any], re.Pattern[str], re.Pattern[str], re.Pattern[str]]],
    ] = {}
    for rule in rules:
        indexed_rules.setdefault(rule["kind"], []).append(
            (
                rule,
                re.compile(rule["key_regex"]),
                re.compile(rule.get("source_regex", ".*")),
                re.compile(rule.get("detail_reference_regex", ".*")),
            )
        )
    modules = {
        row["key"].removeprefix("module:"): row
        for row in raw
        if row["kind"] == "module"
    }
    module_by_source = {row["source"]: row for row in modules.values()}
    client_modules = {
        row["source"]: row for row in raw if row["kind"] == "client_module"
    }
    model_keys_by_source: dict[str, list[str]] = {}
    for row in raw:
        if row["kind"] == "model":
            model_keys_by_source.setdefault(row["source"], []).append(row["key"])
    static_importers: dict[str, set[str]] = {}
    for row in raw:
        if row["kind"] != "module":
            continue
        for imported in row["detail"].get("imports", []):
            static_importers.setdefault(imported, set()).add(row["key"])
    static_client_importers: dict[str, set[str]] = {}
    for row in client_modules.values():
        for imported in row["detail"].get("imports", []):
            static_client_importers.setdefault(imported, set()).add(row["key"])
    api_consumers = [row for row in raw if row["kind"] == "api_consumer"]
    api_rows = [row for row in raw if row["kind"] == "api"]
    screen_by_path = {
        row["detail"].get("path"): row["key"]
        for row in raw
        if row["kind"] == "screen" and row["detail"].get("path")
    }
    consumers_by_source: dict[str, list[str]] = {}
    for consumer in api_consumers:
        consumers_by_source.setdefault(consumer["source"], []).append(consumer["key"])
    callback_imports: dict[str, dict[str, str]] = {}
    for source in {
        row["source"] for row in raw if row["kind"] in {"scheduled_job", "job_handler"}
    }:
        tree = ast.parse((ROOT / source).read_text(encoding="utf-8"), filename=source)
        bindings: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            for alias in node.names:
                imported_target = f"{node.module}.{alias.name}"
                if imported_target in modules:
                    bindings[alias.asname or alias.name] = imported_target
                elif node.module in modules:
                    bindings[alias.asname or alias.name] = node.module
        callback_imports[source] = bindings

    def callback_module(row: dict[str, Any]) -> dict[str, Any] | None:
        if row["kind"] not in {"scheduled_job", "job_handler"}:
            return None
        callback = row["detail"].get("callback")
        if not isinstance(callback, str):
            return None
        binding = callback_imports.get(row["source"], {}).get(callback.split(".", 1)[0])
        return modules.get(binding) if binding else None

    def matches_consumer(api_key: str, consumer: dict[str, Any]) -> bool:
        method, route = api_key.removeprefix("api:").split(":", 1)
        if "(?P<" in route:
            return False
        detail = consumer["detail"]
        reference = detail.get("api_reference")
        allowed_methods = {method, "FETCH", "REQUEST"}
        if method == "GET":
            allowed_methods.add("SUBSCRIBE")
        if (
            not isinstance(reference, str)
            or detail.get("method") not in allowed_methods
        ):
            return False
        route_pattern = re.escape(route)
        route_pattern = re.sub(r"<path:[^>]+>", ".+", route_pattern)
        route_pattern = re.sub(r"<[^>]+>", "[^/]+", route_pattern)
        sample = re.sub(r"\{[^}]+\}", "x", reference.split("?", 1)[0])
        return re.fullmatch(route_pattern, sample) is not None

    api_to_consumers: dict[str, list[str]] = {}
    consumer_to_apis: dict[str, list[str]] = {}
    for api in api_rows:
        matched = [
            consumer["key"]
            for consumer in api_consumers
            if matches_consumer(api["key"], consumer)
        ]
        api_to_consumers[api["key"]] = matched
        for consumer_key in matched:
            consumer_to_apis.setdefault(consumer_key, []).append(api["key"])

    def dependency_trace(
        row: dict[str, Any], source_module: dict[str, Any] | None
    ) -> dict[str, Any]:
        detail = row["detail"]
        module_detail = source_module["detail"] if source_module else {}
        static_imports = module_detail.get("imports", [])
        local_components = [
            modules[name]["key"] for name in static_imports if name in modules
        ]
        if row["kind"] == "module":
            static_imports = detail.get("imports", [])
            local_components = [
                modules[name]["key"] for name in static_imports if name in modules
            ]
        if row["kind"] == "client_module":
            static_imports = detail.get("imports", [])
            local_components = [
                client_modules[name]["key"]
                for name in static_imports
                if name in client_modules
            ]
        if row["kind"] in {"navigation", "bookmark", "display_window", "subview"}:
            route_target = (
                detail.get("to")
                if row["kind"] == "bookmark"
                else detail.get("parent_path")
                if row["kind"] == "subview"
                else detail.get("target")
            )
            if isinstance(route_target, str):
                screen_key = screen_by_path.get(route_target.split("?", 1)[0])
                if screen_key:
                    local_components.append(screen_key)
        candidate_model_keys = sorted(
            {
                model_key
                for component in local_components
                if component.startswith("module:")
                for model_key in model_keys_by_source.get(
                    modules[component.removeprefix("module:")]["source"], []
                )
            }
        )
        callback_source = callback_module(row)
        return {
            "basis": "source_module_static"
            if source_module
            else (
                "client_module_static"
                if row["kind"] == "client_module"
                else "frontend_declaration"
                if row["kind"]
                in {
                    "screen",
                    "subview",
                    "navigation",
                    "bookmark",
                    "display_window",
                    "print_output",
                    "api_consumer",
                }
                else "declared_metadata_only"
            ),
            "source_module": source_module["key"] if source_module else None,
            "callback_component": callback_source["key"] if callback_source else None,
            "static_imports": static_imports,
            "local_component_keys": sorted(set(local_components)),
            "candidate_model_keys": candidate_model_keys,
            "frontend_api_callsites": (
                api_to_consumers.get(row["key"], [])
                if row["kind"] == "api"
                else consumers_by_source.get(detail.get("component_source"), [])
                if row["kind"] in {"screen", "subview"}
                else consumers_by_source.get(row["source"], [])
                if row["kind"] in {"print_output", "display_window"}
                else []
            ),
            "api_targets": consumer_to_apis.get(row["key"], [])
            if row["kind"] == "api_consumer"
            else [],
            "model_relations": detail.get("relations", [])
            if row["kind"] == "model"
            else [],
            "migration_dependencies": detail.get("dependencies", [])
            if row["kind"] == "migration"
            else [],
            "runtime_edges": "unknown; static discovery is not execution tracing",
        }

    def data_access(
        row: dict[str, Any], source_module: dict[str, Any] | None
    ) -> dict[str, Any]:
        detail = row["detail"]
        callback_source = callback_module(row)
        candidate_detail = (
            detail
            if row["kind"] in {"module", "command", "client_module"}
            else (
                callback_source["detail"]
                if callback_source
                else source_module["detail"]
                if source_module
                else {}
            )
        )
        return {
            "basis": "callback_module_static_candidates"
            if callback_source
            else "client_storage_static_candidates"
            if row["kind"] == "client_module"
            else "source_file_static_candidates"
            if candidate_detail
            else (
                "schema_metadata"
                if row["kind"] in {"model", "migration"}
                else "unknown"
            ),
            "read_candidates": candidate_detail.get("read_candidates", []),
            "write_candidates": candidate_detail.get("write_candidates", []),
            "model_fields": detail.get("fields", []) if row["kind"] == "model" else [],
            "schema_operations": detail.get("operations", [])
            if row["kind"] == "migration"
            else [],
            "storage_markers": detail.get("storage_markers", [])
            if row["kind"] == "client_module"
            else [],
            "completeness": "static leads only; route-specific and dynamic data access unverified",
        }

    for row in raw:
        reference = str(row["detail"].get("api_reference") or "")
        matches = [
            rule
            for rule, key_pattern, source_pattern, reference_pattern in (
                *indexed_rules.get(row["kind"], []),
                *indexed_rules.get("*", []),
            )
            if key_pattern.search(row["key"])
            and source_pattern.search(row["source"])
            and reference_pattern.search(reference)
        ]
        if not matches:
            raise InventoryError(
                f"Unclassified {row['kind']} surface: {row['key']} ({row['source']})"
            )
        priority = max(rule.get("priority", 0) for rule in matches)
        winners = [rule for rule in matches if rule.get("priority", 0) == priority]
        if len(winners) != 1:
            raise InventoryError(
                f"Conflicting equal-priority rules for {row['key']}: "
                + ", ".join(rule["id"] for rule in winners)
            )
        rule = winners[0]
        hits[rule["id"]] += 1
        target = owners[rule["primary_id"]]
        source_module = module_by_source.get(row["source"])
        module = source_module["key"].removeprefix("module:") if source_module else None
        lifecycle = rule.get("lifecycle", target["default_lifecycle"])
        consumers = api_to_consumers.get(row["key"], []) if row["kind"] == "api" else []
        classified.append(
            {
                **row,
                "primary_id": target["id"],
                "owner": target["owner"],
                "related_ids": rule.get("related_ids", []),
                "lifecycle": lifecycle,
                "data_ownership": rule.get("data_ownership", target["data_ownership"]),
                "authorization": rule.get("authorization", target["authorization"]),
                "callers": {
                    "declared": rule.get("callers", target["callers"]),
                    "static_backend_importers": sorted(
                        static_importers.get(module or "", set())
                    ),
                    "static_frontend_importers": sorted(
                        static_client_importers.get(row["source"], set())
                    ),
                    "frontend_api_callsites": consumers,
                },
                "dependencies": dependency_trace(row, source_module),
                "data_access": data_access(row, source_module),
                **({"access_contract": rule.get("access_contract", {
                    "trace_status": "pending",
                    "actor_type": "unreviewed",
                    "required_action": "unreviewed",
                    "scope_source": "unreviewed",
                    "protected_fields": "unreviewed",
                    "delivery_recheck": "unreviewed",
                    "named_tests": [],
                    "reviewed_chain": "Static candidates do not establish runtime access coverage.",
                })} if row["kind"] in ACCESS_ENTRY_KINDS else {}),
                **(
                    {
                        "consumer_trace": {
                            "status": "static_frontend_callsite"
                            if consumers
                            else "untraced",
                            **(
                                {"named_callsites": consumers}
                                if consumers
                                else {
                                    "gate": f"{target['owner']} must name the runtime or historical consumer "
                                    "and prove its replacement before retirement.",
                                }
                            ),
                        }
                    }
                    if row["kind"] == "api"
                    and lifecycle == "temporary_supported_legacy"
                    else {}
                ),
                "replacement": rule.get("replacement", target["target_implementation"]),
                "migration_requirements": rule.get(
                    "migration_requirements", target["data_mapping"]
                ),
                "acceptance_evidence": rule.get(
                    "acceptance_evidence", target["acceptance_evidence"]
                ),
                "removal_condition": rule.get(
                    "removal_condition", target["removal_condition"]
                ),
                **({"gate": rule["gate"]} if lifecycle == "unresolved" else {}),
                "rule_id": rule["id"],
            }
        )
    unused = sorted(rule["id"] for rule in rules if hits[rule["id"]] == 0)
    if unused:
        raise InventoryError(
            f"Classification rules matched no surfaces: {', '.join(unused)}"
        )
    return classified


def _discovered(manifest: dict[str, Any]) -> dict[str, Any]:
    owners = _retirements(manifest)
    rules = _rules(manifest, owners)
    raw = _raw_records()
    records = _classify(raw, rules, owners)
    checksum = hashlib.sha256(_canonical(raw).encode()).hexdigest()
    return {"checksum": checksum, "records": records}


def _retirement_table(manifest: dict[str, Any]) -> str:
    owners = _retirements(manifest)
    lines = [
        "| ID / owner | Mounted API, model and service families present today | Routed screens and target implementation | Data mapping, acceptance evidence and retirement item |",
        "| --- | --- | --- | --- |",
    ]
    for number in range(1, 21):
        row = owners[f"C{number:02d}"]
        handoff = row.get("rendered_handoff") or " ".join(
            (row["data_mapping"], row["acceptance_evidence"], row["removal_condition"])
        )
        lines.append(
            f"| {row['id']} · {row['owner']} | {row['existing_inventory']} | "
            f"{row['target_implementation']} | {handoff} |"
        )
    return "\n".join(lines)


def _access_cutover_table(manifest: dict[str, Any]) -> str:
    rows = manifest.get("access_cutovers")
    if not isinstance(rows, list) or not rows:
        raise InventoryError("access_cutovers needs structured A00 decisions")
    seen: set[str] = set()
    lines = [
        "| Existing family and duplicate | Retained target and data movement | Required proof and removal condition |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        if not isinstance(row, dict):
            raise InventoryError("Every access cutover must be an object")
        for field in (
            "id",
            "existing_family",
            "target_and_data_movement",
            "proof_and_removal",
        ):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise InventoryError(f"Access cutover {row.get('id')!r} needs {field}")
        if row["id"] in seen:
            raise InventoryError(f"Duplicate access cutover {row['id']}")
        seen.add(row["id"])
        if any(
            "|" in row[field] or "\n" in row[field]
            for field in (
                "existing_family",
                "target_and_data_movement",
                "proof_and_removal",
            )
        ):
            raise InventoryError(
                f"Access cutover {row['id']} contains a table delimiter"
            )
        lines.append(
            f"| {row['existing_family']} | {row['target_and_data_movement']} | "
            f"{row['proof_and_removal']} |"
        )
    return "\n".join(lines)


def _coverage_table(manifest: dict[str, Any]) -> str:
    records = manifest["discovered"]["records"]
    counts = Counter((row["primary_id"], row["kind"]) for row in records)
    owners = _retirements(manifest)
    lines = [
        "The [machine-readable inventory](so-03-consolidation-inventory.json) records every discovered entrypoint and component, its primary retirement owner, lifecycle, data ownership, authorization, callers, replacement, evidence and removal condition. Its structural checksum is `"
        + manifest["discovered"]["checksum"]
        + "`. Temporary APIs without a statically named frontend consumer carry an explicit trace gate in the inventory. The counts below are generated from that same snapshot; they are coverage, not completion proof.",
        "",
        "| Retirement item / owner | Discovered surfaces by kind | Unresolved | Untraced temporary APIs |",
        "| --- | --- | ---: | ---: |",
    ]
    for ident in ["A00", "P00", *(f"C{number:02d}" for number in range(1, 21))]:
        kinds = sorted(
            (kind, count)
            for (owner_id, kind), count in counts.items()
            if owner_id == ident
        )
        summary = (
            ", ".join(f"{kind}: {count}" for kind, count in kinds) or "None discovered"
        )
        unresolved = sum(
            row["primary_id"] == ident and row["lifecycle"] == "unresolved"
            for row in records
        )
        untraced = sum(
            row["primary_id"] == ident
            and row.get("consumer_trace", {}).get("status") == "untraced"
            for row in records
        )
        lines.append(
            f"| {ident} · {owners[ident]['owner']} | {summary} | {unresolved} | {untraced} |"
        )
    lines.extend(["", "Unresolved surfaces stay gated under their named owner:", ""])
    pending = [row for row in records if row["lifecycle"] == "unresolved"]
    if not pending:
        lines.append("None in this snapshot.")
    else:
        lines.extend(["| Surface | Owner | Gate |", "| --- | --- | --- |"])
        for row in pending:
            lines.append(f"| `{row['key']}` | {row['owner']} | {row['gate']} |")
    return "\n".join(lines)


def _replace(text: str, begin: str, end: str, content: str) -> str:
    if text.count(begin) != 1 or text.count(end) != 1:
        raise InventoryError(f"Register needs exactly one {begin} / {end} marker pair")
    start = text.index(begin) + len(begin)
    stop = text.index(end)
    if stop < start:
        raise InventoryError(f"Register marker order is wrong for {begin}")
    return text[:start] + "\n" + content.rstrip() + "\n" + text[stop:]


def _render_register(text: str, manifest: dict[str, Any]) -> str:
    text = _replace(text, ACCESS_BEGIN, ACCESS_END, _access_cutover_table(manifest))
    text = _replace(
        text, RETIREMENTS_BEGIN, RETIREMENTS_END, _retirement_table(manifest)
    )
    return _replace(text, COVERAGE_BEGIN, COVERAGE_END, _coverage_table(manifest))


def _check_snapshot(manifest: dict[str, Any], actual: dict[str, Any]) -> None:
    if manifest.get("discovered") != actual:
        raise InventoryError(
            "Discovered surfaces changed; review rules and run --write"
        )


def _check_protected_boundaries(manifest: dict[str, Any]) -> None:
    from scripts.check_so03_boundaries import check_boundaries

    findings = check_boundaries(manifest)
    if findings:
        raise InventoryError("Protected boundary regression:\n" + "\n".join(findings))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="fail on discovery or register drift"
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="refresh reviewed inventory snapshot and register",
    )
    args = parser.parse_args()
    try:
        manifest = json.loads(INVENTORY.read_text())
        actual = _discovered(manifest)
        proposed = {**manifest, "discovered": actual}
        _check_protected_boundaries(proposed)
        expected_register = _render_register(REGISTER.read_text(), proposed)
        from scripts.render_so03_dependency_map import render_map

        expected_map = render_map(proposed)
        if args.check:
            _check_snapshot(manifest, actual)
            if REGISTER.read_text() != expected_register:
                raise InventoryError("Human-readable register drifted; run --write")
            if (
                not DEPENDENCY_MAP.exists()
                or DEPENDENCY_MAP.read_text() != expected_map
            ):
                raise InventoryError("Dependency map drifted; run --write")
            print(
                f"SO-03 inventory matches {len(actual['records'])} discovered surfaces"
            )
            return 0
        INVENTORY.write_text(json.dumps(proposed, ensure_ascii=False, indent=2) + "\n")
        REGISTER.write_text(expected_register)
        DEPENDENCY_MAP.write_text(expected_map)
        print(
            f"Wrote SO-03 inventory and register for {len(actual['records'])} surfaces"
        )
        return 0
    except (InventoryError, ValueError, OSError) as exc:
        print(f"SO-03 inventory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
