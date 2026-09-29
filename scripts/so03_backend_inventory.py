"""Discover backend entry points and code families for the SO-03 inventory.

This scanner records source facts only. The consolidation manifest owns workflow
classification and retirement decisions; neither a module name nor a URL prefix
can decide those safely. Run ``--json`` to inspect the deterministic records.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
APP_PACKAGES = frozenset(
    path.name
    for path in BACKEND.iterdir()
    if path.is_dir() and (path / "__init__.py").is_file()
)
WRITE_METHODS = frozenset(
    {
        "save",
        "delete",
        "create",
        "bulk_create",
        "bulk_update",
        "update",
        "update_or_create",
        "get_or_create",
        "add",
        "remove",
        "clear",
        "set",
        "execute_command",
        "run_command",
        "record",
        "post",
        "allocate",
    }
)
READ_METHODS = frozenset(
    {
        "get",
        "filter",
        "exclude",
        "all",
        "first",
        "last",
        "values",
        "values_list",
        "aggregate",
        "annotate",
        "count",
        "exists",
        "select_related",
        "prefetch_related",
        "select_for_update",
        "raw",
    }
)
AUTH_IDENTIFIERS = frozenset(
    {
        "resolve_access",
        "require_section",
        "user_can",
        "user_can_at",
        "user_may_act",
        "role_capability",
        "effective_grants",
        "effective_assignments",
        "has_effective_role",
        "check_object_permissions",
    }
)
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _relative(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(ROOT)
        if ".venv" in relative.parts or "site-packages" in relative.parts:
            return "external:" + path.as_posix()
        return relative.as_posix()
    except ValueError:
        return "external:" + path.as_posix()


def _expr(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except (TypeError, ValueError):
        return ""


def _imports(tree: ast.Module, module: str | None = None) -> list[str]:
    def is_local_module(name: str) -> bool:
        stem = BACKEND.joinpath(*name.split("."))
        return stem.with_suffix(".py").is_file() or (stem / "__init__.py").is_file()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in APP_PACKAGES:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and module:
                parent = module.split(".")[: -node.level]
                if not parent:
                    continue
                if node.module:
                    target = ".".join([*parent, node.module])
                    if target.split(".", 1)[0] in APP_PACKAGES:
                        found.add(target)
                        for alias in node.names:
                            submodule = target + "." + alias.name
                            if is_local_module(submodule):
                                found.add(submodule)
                else:
                    for alias in node.names:
                        target = ".".join([*parent, alias.name])
                        if target.split(".", 1)[0] in APP_PACKAGES and is_local_module(
                            target
                        ):
                            found.add(target)
            elif node.module and node.module.split(".", 1)[0] in APP_PACKAGES:
                found.add(node.module)
                for alias in node.names:
                    submodule = node.module + "." + alias.name
                    if is_local_module(submodule):
                        found.add(submodule)
    return sorted(found)


def _write_candidates(tree: ast.Module) -> list[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in WRITE_METHODS:
            continue
        target = _expr(node.func)
        # These are candidates for review, not proof that a path is active or
        # that a particular model is written at runtime.
        found.add(target)
    return sorted(found)


def _read_candidates(tree: ast.Module) -> list[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in READ_METHODS
        ):
            found.add(_expr(node.func))
    return sorted(found)


def _authorization_candidates(tree: ast.Module) -> list[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else (node.func.id if isinstance(node.func, ast.Name) else "")
        )
        if name in AUTH_IDENTIFIERS:
            found.add(_expr(node.func))
    return sorted(found)


def _module_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    backend = root / "backend"
    for path in sorted(backend.rglob("*.py")):
        parts = path.relative_to(backend).parts
        if parts[0] not in APP_PACKAGES and parts[0] not in {"manage.py", "server.py"}:
            continue
        if "__pycache__" in parts or "tests" in parts or path.name == "__init__.py":
            continue
        source = path.relative_to(root).as_posix()
        module = ".".join(path.relative_to(backend).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=source)
        imports = _imports(tree, module)
        writes = _write_candidates(tree)
        reads = _read_candidates(tree)
        authority = _authorization_candidates(tree)
        if "migrations" in parts:
            dependencies: list[str] = []
            operations: list[str] = []
            for node in tree.body:
                if not isinstance(node, ast.ClassDef) or node.name != "Migration":
                    continue
                for item in node.body:
                    if not isinstance(item, ast.Assign):
                        continue
                    names = [
                        target.id
                        for target in item.targets
                        if isinstance(target, ast.Name)
                    ]
                    if "dependencies" in names and isinstance(
                        item.value, (ast.List, ast.Tuple)
                    ):
                        dependencies = sorted(_expr(value) for value in item.value.elts)
                    if "operations" in names and isinstance(
                        item.value, (ast.List, ast.Tuple)
                    ):
                        operations = [
                            _expr(value.func)
                            for value in item.value.elts
                            if isinstance(value, ast.Call)
                        ]
            records.append(
                {
                    "kind": "migration",
                    "key": f"migration:{module}",
                    "source": source,
                    "detail": {"dependencies": dependencies, "operations": operations},
                }
            )
            continue

        tags: list[str] = []
        if "management" in parts and "commands" in parts:
            tags.append("management")
        if "seed" in path.stem or "demo" in path.stem:
            tags.append("seed_or_demo")
        if "import" in path.stem:
            tags.append("import")
        if "export" in path.stem:
            tags.append("export")
        if "print" in path.stem:
            tags.append("print")
        if "event" in path.stem or "signal" in path.stem or "outbox" in path.stem:
            tags.append("event")
        if (
            "worker" in path.stem
            or "checks" in path.stem
            or path.stem.startswith("refresh_")
        ):
            tags.append("background_candidate")
        if "services" in parts or "service" in path.stem or path.stem == "posting":
            tags.append("service")
        if parts[0] == "config" or "settings" in path.stem:
            tags.append("configuration")
        records.append(
            {
                "kind": "module",
                "key": f"module:{module}",
                "source": source,
                "detail": {
                    "imports": imports,
                    "read_candidates": reads,
                    "write_candidates": writes,
                    "authorization_candidates": authority,
                    "tags": sorted(tags),
                },
            }
        )

        if (
            "management" in parts
            and "commands" in parts
            and any(
                isinstance(node, ast.ClassDef) and node.name == "Command"
                for node in tree.body
            )
        ):
            records.append(
                {
                    "kind": "command",
                    "key": f"command:{path.stem}",
                    "source": source,
                    "detail": {
                        "module": module,
                        "read_candidates": reads,
                        "write_candidates": writes,
                        "authorization_candidates": authority,
                    },
                }
            )

        # These registrations are executable entry points even though their
        # callbacks are reached through the outbox/clock rather than a URL.
        for call_node in ast.walk(tree):
            if not isinstance(call_node, ast.Call) or not isinstance(
                call_node.func, ast.Name
            ):
                continue
            kind = {
                "register_scheduled": "scheduled_job",
                "register_job_handler": "job_handler",
            }.get(call_node.func.id)
            if kind is None:
                continue
            if (
                not call_node.args
                or not isinstance(call_node.args[0], ast.Constant)
                or not isinstance(call_node.args[0].value, str)
            ):
                raise ValueError(
                    f"Dynamic {call_node.func.id} registration in {source}:{call_node.lineno}"
                )
            name = call_node.args[0].value
            records.append(
                {
                    "kind": kind,
                    "key": f"{kind}:{name}",
                    "source": source,
                    "detail": {
                        "registration_module": module,
                        "callback": _expr(call_node.args[1])
                        if len(call_node.args) > 1
                        else "",
                        "line": call_node.lineno,
                    },
                }
            )

        # Signal receivers also run outside the normal request path. Keep the
        # sender and signal expression as source evidence for their owner.
        for function_node in ast.walk(tree):
            if not isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for ordinal, decorator in enumerate(function_node.decorator_list, 1):
                if not isinstance(decorator, ast.Call) or not isinstance(
                    decorator.func, ast.Name
                ):
                    continue
                if decorator.func.id != "receiver":
                    continue
                records.append(
                    {
                        "kind": "signal_receiver",
                        "key": f"signal_receiver:{module}.{function_node.name}:{ordinal}",
                        "source": source,
                        "detail": {
                            "callback": function_node.name,
                            "signal": _expr(decorator.args[0])
                            if decorator.args
                            else "",
                            "sender": next(
                                (
                                    _expr(keyword.value)
                                    for keyword in decorator.keywords
                                    if keyword.arg == "sender"
                                ),
                                "",
                            ),
                            "line": function_node.lineno,
                        },
                    }
                )

    return records


def _operational_records(root: Path) -> list[dict[str, Any]]:
    """Inventory repository launch, proof and CI entry points outside Django."""
    paths = [
        *sorted((root / "scripts").glob("*.py")),
        *sorted((root / "scripts").glob("*.sh")),
        *sorted((root / "frontend" / "scripts").glob("*")),
        *sorted((root / ".github" / "workflows").glob("*.yml")),
        *sorted(root.glob("compose*.yaml")),
        root / "package.json",
        root / "frontend" / "package.json",
        root / "frontend" / "index.html",
        root / "frontend" / "eslint.config.js",
        root / "frontend" / "playwright.config.ts",
        root / "frontend" / "tsconfig.json",
        root / "frontend" / "vite.config.ts",
        root / "frontend" / "vitest.config.ts",
        root / "backend" / "pyproject.toml",
    ]
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        source = path.relative_to(root).as_posix()
        detail: dict[str, Any] = {"format": path.suffix}
        if path.suffix == ".py":
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=source)
            detail.update(
                imports=_imports(tree),
                read_candidates=_read_candidates(tree),
                write_candidates=_write_candidates(tree),
                authorization_candidates=_authorization_candidates(tree),
            )
        records.append(
            {
                "kind": "operational_file",
                "key": f"operational_file:{source}",
                "source": source,
                "detail": detail,
            }
        )
    return records


def _django_records(root: Path) -> list[dict[str, Any]]:
    # Force the optional administrative mount on for inventory, regardless of
    # local DEBUG. This affects only this scanner process.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["ENABLE_DJANGO_ADMIN"] = "1"
    sys.path.insert(0, str(root / "backend"))
    import django
    from django.apps import apps
    from django.conf import settings
    from django.urls import URLPattern, URLResolver, get_resolver

    django.setup()
    records: list[dict[str, Any]] = []

    # Middleware runs ahead of every routed request and may establish tenant
    # or scope context. Keep each configured class as an execution entrypoint,
    # including third-party layers, so a changed stack cannot escape review.
    for position, dotted in enumerate(settings.MIDDLEWARE):
        module_name = dotted.rsplit(".", 1)[0]
        local_path = root / "backend" / Path(*module_name.split(".")).with_suffix(".py")
        source = (
            _relative(local_path)
            if local_path.is_file()
            else "backend/config/settings.py"
        )
        records.append(
            {
                "kind": "middleware",
                "key": f"middleware:{dotted}",
                "source": source,
                "detail": {
                    "callable": dotted,
                    "module": module_name,
                    "position": position,
                },
            }
        )

    def visit(prefix: str, patterns: list[Any]) -> None:
        for pattern in patterns:
            route = prefix + str(pattern.pattern)
            if isinstance(pattern, URLResolver):
                visit(route, list(pattern.url_patterns))
                continue
            if not isinstance(pattern, URLPattern):
                continue
            callback = pattern.callback
            cls = getattr(callback, "cls", None) or getattr(
                callback, "view_class", None
            )
            handler = cls or callback
            handler_name = f"{handler.__module__}.{handler.__qualname__}"
            source_path = inspect.getsourcefile(handler)
            source = (
                _relative(Path(source_path))
                if source_path
                else "backend/config/urls.py"
            )
            if source.startswith("external:"):
                source = "backend/config/urls.py"
            actions = getattr(callback, "actions", None)
            if isinstance(actions, dict):
                methods = sorted(
                    {
                        method.upper()
                        for method in actions
                        if method.upper() in HTTP_METHODS
                    }
                )
            elif cls is not None:
                methods = [
                    method
                    for method in HTTP_METHODS
                    if method.lower() in getattr(cls, "http_method_names", ())
                    and callable(getattr(cls, method.lower(), None))
                ]
            else:
                declared = getattr(callback, "methods", None)
                methods = (
                    sorted({str(method).upper() for method in declared})
                    if declared
                    else ["ANY"]
                )
            if not methods:
                methods = ["ANY"]
            for method in methods:
                records.append(
                    {
                        "kind": "api",
                        "key": f"api:{method}:/{route}",
                        "source": source,
                        "detail": {
                            "handler": handler_name,
                            "route_name": pattern.name or "",
                            "dynamic_permissions": bool(
                                cls and "get_permissions" in cls.__dict__
                            ),
                        },
                    }
                )

    visit("", list(get_resolver().url_patterns))
    for model in apps.get_models():
        fields = [field for field in model._meta.get_fields() if field.concrete]
        relations: list[str] = []
        for field in fields:
            related_meta = (
                getattr(field.related_model, "_meta", None)
                if field.is_relation
                else None
            )
            if related_meta is not None:
                relations.append(f"{field.name}:{related_meta.label}")
        model_path = inspect.getsourcefile(model)
        source = (
            _relative(Path(model_path))
            if model_path
            else f"external:{model.__module__}"
        )
        if source.startswith("external:"):
            source = f"external:{model.__module__}"
        records.append(
            {
                "kind": "model",
                "key": f"model:{model._meta.label}",
                "source": source,
                "detail": {
                    "fields": sorted(field.name for field in fields),
                    "relations": sorted(relations),
                    "tenant_marker": any(field.name == "tenant" for field in fields),
                },
            }
        )
    return records


def discover_backend(root: Path = ROOT) -> list[dict[str, Any]]:
    """Return source facts with stable keys; refuse duplicate discovered keys."""
    records = _module_records(root) + _operational_records(root) + _django_records(root)
    records.sort(key=lambda row: (row["kind"], row["key"], row["source"]))
    seen: dict[str, str] = {}
    for record in records:
        key = record["key"]
        if key in seen:
            raise ValueError(
                f"Duplicate inventory key {key}: {seen[key]} and {record['source']}"
            )
        seen[key] = record["source"]
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="Print the discovered records as JSON"
    )
    args = parser.parse_args()
    records = discover_backend()
    if args.json:
        json.dump(records, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        counts: dict[str, int] = {}
        for row in records:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
