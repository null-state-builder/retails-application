"""Render a workflow dependency view from the single SO-03 inventory snapshot.

This is deliberately a *static* map. Imports and call expressions show review
leads, not proof of runtime reachability or that a candidate ORM call writes a
particular business table. The inventory checker owns the file write/check.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

ENTRY_KINDS = frozenset(
    {
        "api",
        "screen",
        "command",
        "scheduled_job",
        "job_handler",
        "signal_receiver",
        "middleware",
        "operational_file",
        "subview",
        "print_output",
        "display_window",
        "navigation",
        "bookmark",
    }
)
JOB_KINDS = frozenset({"command", "scheduled_job", "job_handler", "signal_receiver"})


def _code(value: str) -> str:
    return "`" + value.replace("`", "\\`").replace("|", "\\|") + "`"


def _items(values: list[str], *, empty: str = "None discovered") -> str:
    return ", ".join(_code(value) for value in values) if values else empty


def _namespaces(api_rows: list[dict[str, Any]]) -> str:
    groups: Counter[str] = Counter()
    for row in api_rows:
        route = row["key"].split(":", 2)[2]
        parts = [part for part in route.split("/") if part]
        namespace = "/" + "/".join(parts[:3]) if parts else "/"
        groups[namespace] += 1
    return (
        ", ".join(f"{_code(name)} ({count})" for name, count in sorted(groups.items()))
        or "None discovered"
    )


def _entrypoint_traces(
    entrypoints: list[dict[str, Any]],
    by_key: dict[str, dict[str, Any]],
) -> list[str]:
    """Render every discovered entry point, keeping the static evidence explicit."""
    lines = [
        "### Entry-point dependency traces",
        "",
        (
            "Each row follows entry point → declared authorization → implementation and "
            "static dependencies → data-access candidates → discovered downstream callers. "
            "The full dependency lists are in the machine inventory. A candidate call "
            "does not establish that the entry point executes it."
        ),
        "",
        "| Entry point | Authorization | Access review | Implementation and dependencies | Data-access candidates | Downstream callers / unknown edges |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(entrypoints, key=lambda item: (item["kind"], item["key"])):
        deps = row.get("dependencies", {})
        access = row.get("data_access", {})
        module = deps.get("source_module")
        local_keys = deps.get("local_component_keys", [])
        if row["kind"] in {"screen", "subview"}:
            module = row["detail"].get("component_source") or module
        implementation = _code(module or row["source"])
        callback = deps.get("callback_component")
        if callback:
            implementation += " → callback " + _code(callback)
        if local_keys:
            implementation += " → " + _items(local_keys)
        else:
            implementation += " → no resolved local dependency"
        model_keys = sorted(
            set(deps.get("candidate_model_keys", []))
            | {key for key in local_keys if key.startswith("model:")}
        )
        if model_keys:
            implementation += "; model " + _items(model_keys)

        reads = access.get("read_candidates", [])
        writes = access.get("write_candidates", [])
        operations = access.get("schema_operations", [])
        data = f"R:{len(reads)} / W:{len(writes)}"
        if operations:
            data += f" / schema:{len(operations)}"
        if model_keys:
            data += f" / models:{len(model_keys)}"
        data += "; " + _code(access.get("completeness", "static candidates only"))

        callers = row.get("callers", {})
        frontend = callers.get("frontend_api_callsites", [])
        backends = callers.get("static_backend_importers", [])
        screen_calls = deps.get("frontend_api_callsites", [])
        api_targets: set[str] = set(deps.get("api_targets", []))
        for callsite in screen_calls:
            target = by_key.get(callsite)
            if target:
                api_targets.update(
                    target.get("dependencies", {}).get("api_targets", [])
                )
        downstream = []
        if frontend:
            downstream.append("UI " + _items(frontend))
        if backends:
            downstream.append("backend " + _items(backends))
        if screen_calls:
            downstream.append("calls " + _items(screen_calls))
        if api_targets:
            downstream.append("API " + _items(sorted(api_targets)))
        runtime = deps.get("runtime_edges")
        if runtime:
            downstream.append("runtime " + _code(str(runtime)))
        if not downstream:
            downstream.append("no static caller; runtime trace required")
        authorization = row["authorization"].replace("|", "\\|")
        review = row.get("access_contract", {})
        status = str(review.get("trace_status", "pending"))
        lines.append(
            f"| {_code(row['key'])} | {authorization} | {_code(status)} | "
            f"{implementation} | {data} | {'; '.join(downstream)} |"
        )
    lines.append("")
    return lines


def render_map(manifest: dict[str, Any]) -> str:
    """Return a deterministic, human-readable dependency map; never write files."""
    records: list[dict[str, Any]] = manifest["discovered"]["records"]
    retirements = {row["id"]: row for row in manifest["retirements"]}
    by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_key = {row["key"]: row for row in records}
    module_by_name = {
        row["key"].removeprefix("module:"): row
        for row in records
        if row["kind"] == "module"
    }
    model_sources = {row["source"] for row in records if row["kind"] == "model"}
    for row in records:
        by_owner[row["primary_id"]].append(row)

    lines = [
        "# SO-03 workflow dependency map",
        "",
        (
            "Generated from [the SO-03 machine inventory](so-03-consolidation-inventory.json). "
            "Do not edit this map separately. It traces each retirement item's discovered "
            "entry points through its declared authorization boundary, source imports, "
            "models and downstream readers. All component keys and their individual "
            "classifications are in the machine inventory."
        ),
        "",
        (
            "**Evidence limit:** This map uses mounted Django routes and middleware, declared frontend "
            "routes/calls/output sinks, registered jobs, model metadata and static Python imports. "
            "Authorization text is the required boundary, not proof that every handler "
            "enforces it. An ORM call is a writer candidate until its business data and "
            "runtime path are reviewed. Dynamic imports, constructed URLs, callbacks "
            "and raw SQL need targeted tracing before a workflow cutover; lack of a "
            "static edge never means the component is unused."
        ),
        "",
    ]
    order = ["A00", "P00", *(f"C{n:02d}" for n in range(1, 21))]
    for owner_id in order:
        owner = retirements[owner_id]
        members = sorted(by_owner[owner_id], key=lambda row: (row["kind"], row["key"]))
        entrypoints = [row for row in members if row["kind"] in ENTRY_KINDS]
        apis = [row for row in members if row["kind"] == "api"]
        screens = [row for row in members if row["kind"] == "screen"]
        subviews = [row for row in members if row["kind"] == "subview"]
        outputs = [
            row for row in members if row["kind"] in {"print_output", "display_window"}
        ]
        navigation = [
            row for row in members if row["kind"] in {"navigation", "bookmark"}
        ]
        jobs = [row for row in members if row["kind"] in JOB_KINDS]
        middleware = [row for row in members if row["kind"] == "middleware"]
        consumers = [row for row in members if row["kind"] == "api_consumer"]
        dynamic = [
            row for row in consumers if row["detail"].get("api_reference") is None
        ]
        services = [
            row
            for row in members
            if row["kind"] == "module" and "service" in row["detail"].get("tags", [])
        ]
        client_modules = [row for row in members if row["kind"] == "client_module"]
        models = [row for row in members if row["kind"] == "model"]

        # Every source with a static write candidate appears once. Management
        # commands have both command and module records; retain the command key.
        writers_by_source: dict[str, dict[str, Any]] = {}
        for row in members:
            if row["kind"] not in {
                "module",
                "command",
                "operational_file",
                "client_module",
            }:
                continue
            if not row["detail"].get("write_candidates"):
                continue
            current = writers_by_source.get(row["source"])
            if current is None or row["kind"] == "command":
                writers_by_source[row["source"]] = row
        writers = sorted(writers_by_source.values(), key=lambda row: row["key"])

        # Direct model-module edges are concrete source facts. A service may
        # reach more data indirectly; those edges are intentionally unclaimed.
        model_edges: set[tuple[str, str]] = set()
        for row in members:
            if row["kind"] != "module":
                continue
            for name in row["detail"].get("imports", []):
                target = module_by_name.get(name)
                if target and target["source"] in model_sources:
                    model_edges.add((row["key"], target["key"]))

        # The reverse import graph exposes readers outside a workflow owner.
        downstream: set[tuple[str, str]] = set()
        for row in [*services, *models]:
            for importer_key in row.get("callers", {}).get(
                "static_backend_importers", []
            ):
                importer = by_key.get(importer_key)
                if importer and importer["primary_id"] != owner_id:
                    downstream.add((row["key"], importer_key))

        linked_calls = sum(
            len(row.get("callers", {}).get("frontend_api_callsites", []))
            for row in apis
        )
        declared_auth = sorted({row["authorization"] for row in entrypoints})
        read_count = sum(
            len(row["detail"].get("read_candidates", []))
            for row in members
            if row["kind"] in {"module", "client_module"}
        )
        read_sources = sum(
            bool(row["detail"].get("read_candidates"))
            for row in members
            if row["kind"] in {"module", "client_module"}
        )
        lifecycle = Counter(row["lifecycle"] for row in members)
        lines.extend(
            [
                f"## {owner_id} — {owner['owner']}",
                "",
                f"**Target:** {owner['target_implementation']}",
                "",
                f"**Retirement gate:** {owner['removal_condition']}",
                "",
                "| Boundary | Inventory-derived view |",
                "| --- | --- |",
                (
                    f"| Entry points | {len(entrypoints)} discovered: {len(apis)} HTTP method routes, "
                    f"{len(screens)} screens, {len(subviews)} nested views, "
                    f"{len(outputs)} print/display sinks, {len(navigation)} navigation/bookmarks, "
                    f"{len(jobs)} jobs/commands/signals, "
                    f"{len(middleware)} middleware, "
                    f"{sum(row['kind'] == 'operational_file' for row in entrypoints)} operational files. "
                    f"HTTP namespaces: {_namespaces(apis)}. |"
                ),
                (
                    f"| Authorization | Required boundaries: {'; '.join(declared_auth).rstrip('.') if declared_auth else 'None declared'}. "
                    "Handler-level enforcement requires behaviour and denial proof. |"
                ),
                (
                    f"| Services | {len(services)} tagged service modules: "
                    f"{_items([row['key'] for row in services])}. |"
                ),
                (
                    f"| Offline client modules | {len(client_modules)} till/PWA modules: "
                    f"{_items([row['key'] for row in client_modules])}. "
                    + (
                        "SO-09 owns retained-bill, dataset and sync cutover proof. "
                        if client_modules
                        else ""
                    )
                    + "|"
                ),
                (
                    f"| Data | {len(models)} owned model classes: "
                    f"{_items([row['key'] for row in models])}. "
                    f"{len(model_edges)} direct model-module import edges from owned modules; "
                    f"{read_count} static read-call candidates in {read_sources} backend/client modules "
                    "(individual expressions are in the machine inventory). |"
                ),
                (
                    f"| Downstream readers | {len(consumers)} frontend API callsites; "
                    f"{linked_calls} endpoint/callsite links, {len(dynamic)} dynamic URL references; "
                    f"{len(downstream)} cross-owner static backend import edges. |"
                ),
                "| Lifecycle | "
                + ", ".join(
                    f"{name}: {count}" for name, count in sorted(lifecycle.items())
                )
                + ". |",
                "",
            ]
        )

        lines.extend(_entrypoint_traces(entrypoints, by_key))

        if jobs:
            lines.extend(["Registered jobs, commands and signal handlers:", ""])
            lines.extend(
                f"- {_code(row['key'])} — {_code(row['source'])}" for row in jobs
            )
            lines.append("")
        if model_edges:
            lines.extend(["Direct data-import edges (module → model module):", ""])
            lines.extend(
                f"- {_code(source)} → {_code(target)}"
                for source, target in sorted(model_edges)
            )
            lines.append("")
        if downstream:
            lines.extend(
                [
                    "Cross-owner static downstream readers (owned service/model → importer):",
                    "",
                ]
            )
            lines.extend(
                f"- {_code(source)} → {_code(target)} ({by_key[target]['primary_id']})"
                for source, target in sorted(downstream)
            )
            lines.append("")
        if dynamic:
            lines.extend(
                ["Dynamic frontend API references requiring a consumer trace:", ""]
            )
            lines.extend(
                f"- {_code(row['key'])} — {_code(row['detail'].get('argument', ''))}"
                for row in dynamic
            )
            lines.append("")

        lines.extend(
            [
                f"### {owner_id} static writer candidates",
                "",
                (
                    "These calls need a data-target and runtime-reachability review before "
                    "the retirement gate can close. Every candidate has the owner above; "
                    "this is not proof that all writes are business writes."
                ),
                "",
            ]
        )
        if writers:
            lines.extend(
                ["| Source | Candidate calls | Lifecycle |", "| --- | --- | --- |"]
            )
            for row in writers:
                lines.append(
                    f"| {_code(row['source'])} ({_code(row['key'])}) | "
                    f"{_items(row['detail']['write_candidates'])} | {row['lifecycle']} |"
                )
        else:
            lines.append(
                "No static writer candidate in this item's discovered modules; dynamic and indirect writes still require review."
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
