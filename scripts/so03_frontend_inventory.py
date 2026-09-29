"""Discover frontend consolidation surfaces without assigning retirement owners.

The output is deliberately factual.  The SO-03 classifier joins these records
with backend surfaces and assigns one retirement row; this scanner never treats
a hidden navigation item or a bookmark redirect as a retired implementation.

Run ``python3 scripts/so03_frontend_inventory.py --json`` for the raw records,
or ``--check`` to validate that the route, navigation and bookmark declarations
still fit the shapes this scanner understands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = Path("frontend/src")


class InventoryShapeError(ValueError):
    """A source declaration changed shape and needs an explicit scanner update."""


def _mask(text: str) -> str:
    """Keep code punctuation and offsets while blanking JS comments and strings."""
    chars = list(text)
    i = 0
    state = "code"
    while i < len(chars):
        char = text[i]
        next_char = text[i + 1] if i + 1 < len(chars) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                state = "line"
                chars[i] = chars[i + 1] = " "
                i += 2
                continue
            if char == "/" and next_char == "*":
                state = "block"
                chars[i] = chars[i + 1] = " "
                i += 2
                continue
            if char in ('"', "'", "`"):
                state = char
                chars[i] = " "
        elif state == "line":
            if char == "\n":
                state = "code"
            else:
                chars[i] = " "
        elif state == "block":
            if char == "*" and next_char == "/":
                chars[i] = chars[i + 1] = " "
                i += 2
                state = "code"
                continue
            if char != "\n":
                chars[i] = " "
        else:
            if char == "\\":
                chars[i] = " "
                if i + 1 < len(chars):
                    chars[i + 1] = " " if chars[i + 1] != "\n" else "\n"
                i += 2
                continue
            if char == state:
                state = "code"
            if char != "\n":
                chars[i] = " "
        i += 1
    return "".join(chars)


def _closing(mask: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    for index in range(start, len(mask)):
        if mask[index] == opening:
            depth += 1
        elif mask[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    raise InventoryShapeError(f"Unclosed {opening!r} at offset {start}")


def _declared_array(text: str, declaration: str) -> tuple[str, int]:
    at = text.find(declaration)
    if at < 0:
        raise InventoryShapeError(f"Missing {declaration}")
    mask = _mask(text)
    start = mask.find("[", at + len(declaration))
    if start < 0:
        raise InventoryShapeError(f"Missing array after {declaration}")
    end = _closing(mask, start, "[", "]")
    return text[start + 1 : end], start + 1


def _objects(array: str) -> list[tuple[str, int]]:
    mask = _mask(array)
    rows: list[tuple[str, int]] = []
    cursor = 0
    while True:
        start = mask.find("{", cursor)
        if start < 0:
            return rows
        end = _closing(mask, start, "{", "}")
        rows.append((array[start : end + 1], start))
        cursor = end + 1


def _quoted_property(block: str, name: str) -> str | None:
    found = re.search(rf"\b{re.escape(name)}\s*:\s*([\"'])(.*?)\1", block, re.DOTALL)
    return found.group(2) if found else None


def _property(block: str, name: str) -> str | None:
    found = re.search(rf"\b{re.escape(name)}\s*:\s*([\w.]+)", block)
    return found.group(1) if found else None


def _imports(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in re.finditer(
        r'import\s+(.+?)\s+from\s+["\']([^"\']+)["\']', text, re.DOTALL
    ):
        clause, module = match.groups()
        if not module.startswith("./pages/"):
            continue
        module_path = f"frontend/src/{module.removeprefix('./')}.tsx"
        if clause.startswith("{"):
            names = clause.strip("{} \n").split(",")
            for name in names:
                parts = name.strip().split(" as ")
                if parts[0]:
                    found[parts[-1].strip()] = module_path
        else:
            default = clause.split(",", 1)[0].strip()
            if default:
                found[default] = module_path
            if "{" in clause:
                for name in clause.split("{", 1)[1].split("}", 1)[0].split(","):
                    parts = name.strip().split(" as ")
                    if parts[0]:
                        found[parts[-1].strip()] = module_path
    return found


def _screens(root: Path) -> list[dict[str, Any]]:
    path = root / FRONTEND / "routes.tsx"
    text = path.read_text(encoding="utf-8")
    array, _ = _declared_array(text, "const BUILT: Screen[] =")
    imports = _imports(text)
    records: list[dict[str, Any]] = []
    for block, _ in _objects(array):
        route_id = _quoted_property(block, "id")
        route_path = _quoted_property(block, "path")
        if route_id is None or route_path is None:
            raise InventoryShapeError("A built route lacks a literal id or path")
        element = re.search(
            r"\belement\s*:\s*(?:withTill\s*\(\s*)?<([A-Za-z][\w]*)", block
        )
        component = element.group(1) if element else None
        records.append(
            {
                "kind": "screen",
                "key": f"screen:{route_path}",
                "source": "frontend/src/routes.tsx",
                "detail": {
                    "path": route_path,
                    "route_id": route_id,
                    "lifecycle_hint": "built",
                    "component": component,
                    "component_source": imports.get(component or ""),
                    "room": _quoted_property(block, "room"),
                },
            }
        )
    if not records:
        raise InventoryShapeError("No built routes found")
    app_text = (root / FRONTEND / "App.tsx").read_text(encoding="utf-8")
    app_imports = _imports(app_text)
    for route_path, component in re.findall(
        r'<Route\s+path="([^"]+)"\s+element=\{<([A-Za-z][\w]*)', app_text
    ):
        if route_path == "*":
            continue
        records.append(
            {
                "kind": "screen",
                "key": f"screen:{route_path}",
                "source": "frontend/src/App.tsx",
                "detail": {
                    "path": route_path,
                    "route_id": f"app:{route_path}",
                    "lifecycle_hint": "standalone",
                    "component": component,
                    "component_source": app_imports.get(component),
                },
            }
        )
    return records


def _navigation_and_bookmarks(root: Path) -> list[dict[str, Any]]:
    path = root / FRONTEND / "shell/navConfig.ts"
    text = path.read_text(encoding="utf-8")
    sections, _ = _declared_array(text, "export const SECTIONS: NavSectionDef[] =")
    records: list[dict[str, Any]] = []
    for section_block, _ in _objects(sections):
        section = _quoted_property(section_block, "code")
        if not section:
            raise InventoryShapeError("A navigation section lacks a literal code")
        items_at = section_block.find("items:")
        if items_at < 0:
            raise InventoryShapeError(f"Navigation section {section} has no items")
        mask = _mask(section_block)
        start = mask.find("[", items_at)
        end = _closing(mask, start, "[", "]")
        for item, _ in _objects(section_block[start + 1 : end]):
            target = _quoted_property(item, "to")
            if not target:
                raise InventoryShapeError(
                    f"Navigation item in {section} lacks a literal target"
                )
            detail = {
                "section": section,
                "target": target,
                "label": _quoted_property(item, "label"),
                "lifecycle_hint": "planned"
                if _property(item, "planned") == "true"
                else "built",
                "hidden": _property(item, "hidden") == "true",
                "action": _property(item, "action") == "true",
                "deep_link": _property(item, "deepLink") == "true",
                "min_capability": _quoted_property(item, "minCapability"),
                "display_action": _quoted_property(item, "displayAction")
                or _property(item, "displayAction"),
            }
            records.append(
                {
                    "kind": "navigation",
                    "key": f"navigation:{section}:{target}",
                    "source": "frontend/src/shell/navConfig.ts",
                    "detail": detail,
                }
            )
            if detail["lifecycle_hint"] == "planned" and not detail["deep_link"]:
                route_path = target.split("?", 1)[0]
                records.append(
                    {
                        "kind": "screen",
                        "key": f"screen:{route_path}",
                        "source": "frontend/src/shell/navConfig.ts",
                        "detail": {
                            "path": route_path,
                            "route_id": f"planned:{route_path}",
                            "lifecycle_hint": "planned",
                            "component": "PlannedPage",
                        },
                    }
                )
    if not any(record["kind"] == "navigation" for record in records):
        raise InventoryShapeError("No navigation items found")
    mask = _mask(text)
    for match in re.finditer(
        r"export const (\w+_(?:FOLD|STRIP)):\s*Nav(?:Fold|Strip)Def\s*=\s*\{", mask
    ):
        name = match.group(1)
        start = mask.find("{", match.start())
        block = text[start : _closing(mask, start, "{", "}") + 1]
        tabs_at = block.find("tabs:")
        tabs: list[str] = []
        if tabs_at >= 0:
            block_mask = _mask(block)
            tab_start = block_mask.find("[", tabs_at)
            tab_end = _closing(block_mask, tab_start, "[", "]")
            tab_block = block[tab_start + 1 : tab_end]
            if name.endswith("_FOLD"):
                tabs = re.findall(r'\bentry:\s*"([^"]+)"', tab_block)
            else:
                tabs = re.findall(r'"(/[^"]+)"', tab_block)
        target = _quoted_property(block, "to")
        section = _quoted_property(block, "section")
        records.append(
            {
                "kind": "navigation",
                "key": f"navigation:{'fold' if target else 'strip'}:{target or name}",
                "source": "frontend/src/shell/navConfig.ts",
                "detail": {
                    "layout": name,
                    "target": target,
                    "section": section,
                    "tabs": tabs,
                    "lifecycle_hint": "layout",
                },
            }
        )
    redirects, _ = _declared_array(
        text, "const LEGACY_PREFIXES: [from: string, to: string][] ="
    )
    grouped: dict[str, list[str]] = {}
    for old, target in re.findall(r'\[\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\]', redirects):
        grouped.setdefault(old, []).append(target)
    if not grouped:
        raise InventoryShapeError("No bookmark redirects found")
    for old, targets in grouped.items():
        if len(set(targets)) != 1:
            raise InventoryShapeError(
                f"Conflicting bookmark targets for {old}: {targets}"
            )
        records.append(
            {
                "kind": "bookmark",
                "key": f"bookmark:{old}",
                "source": "frontend/src/shell/navConfig.ts",
                "detail": {
                    "from": old,
                    "to": targets[0],
                    "lifecycle_hint": "redirect",
                    "declarations": len(targets),
                },
            }
        )
    return records


def _subviews(root: Path, screens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find query-selected components rendered inside mounted route pages."""
    records: list[dict[str, Any]] = []
    for screen in screens:
        component_source = screen["detail"].get("component_source")
        if not component_source:
            continue
        path = root / component_source
        text = path.read_text(encoding="utf-8")
        if not re.search(r'\bview\s*=\s*params\.get\(["\']view["\']\)', text):
            continue
        imported: dict[str, str] = {}
        for match in re.finditer(
            r'import\s+\{([^}]+)\}\s+from\s+["\']([^"\']+)["\']', text, re.DOTALL
        ):
            names, module = match.groups()
            if not module.startswith("."):
                continue
            source = (path.parent / module).with_suffix(".tsx")
            for name in names.split(","):
                local = name.strip().split(" as ")[-1]
                imported[local] = source.relative_to(root).as_posix()
        for value, component in re.findall(
            r'\{\s*view\s*===\s*["\']([^"\']+)["\']\s*&&\s*<([A-Za-z][\w]*)',
            text,
        ):
            parent = screen["detail"]["path"]
            records.append(
                {
                    "kind": "subview",
                    "key": f"subview:{parent}?view={value}",
                    "source": component_source,
                    "detail": {
                        "parent_path": parent,
                        "query": {"view": value},
                        "component": component,
                        "component_source": imported.get(component, component_source),
                        "lifecycle_hint": "rendered_subview",
                    },
                }
            )
    return records


_CALL = re.compile(
    r"\b(?:(?P<client>api|typedApi|authApi|axios)\."
    r"(?P<method>get|post|put|patch|delete|request|me|login|logout|csrf|stepUp|changePassword)"
    r"|(?P<function>fetch|EventSource|useList|useDoc|useGoodsFetch|useAllPages|"
    r"useResourceList|useResourceDoc|useResourceListRaw))\b"
)


def _call_argument(text: str, mask: str, after_name: int) -> str | None:
    cursor = after_name
    while cursor < len(mask) and mask[cursor].isspace():
        cursor += 1
    if cursor < len(mask) and mask[cursor] == "<":
        cursor = _closing(mask, cursor, "<", ">") + 1
    while cursor < len(mask) and mask[cursor].isspace():
        cursor += 1
    if cursor >= len(mask) or mask[cursor] != "(":
        return None
    start = cursor + 1
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    for index in range(start, len(mask)):
        char = mask[index]
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char in (",", ")"):
            return text[start:index].strip()
    raise InventoryShapeError(f"Unclosed call at offset {after_name}")


def _constants(text: str) -> dict[str, str]:
    return dict(
        re.findall(
            r'\bconst\s+([A-Z][A-Z_\d]+)\s*=\s*(["\'][^"\'\n]*["\']|`[^`\n]*`)', text
        )
    )


def _conditional_url_constants(text: str) -> dict[str, str]:
    """Resolve one local `condition ? URL : empty` declaration without running JS."""
    declarations = re.findall(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=", text)
    counts = {name: declarations.count(name) for name in set(declarations)}
    found: dict[str, str] = {}
    pattern = re.compile(
        r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*[^;\n]*?\?\s*"
        r"(`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+')\s*:\s*(?:\"\"|'')\s*;"
    )
    for match in pattern.finditer(text):
        name, value = match.groups()
        if counts[name] == 1:
            found[name] = value
    return found


def _file_constants(
    path: Path, root: Path, visited: set[Path] | None = None
) -> dict[str, str]:
    """Resolve literal named imports, without executing frontend application code."""
    visited = set() if visited is None else visited
    if path in visited:
        return {}
    visited.add(path)
    text = path.read_text(encoding="utf-8")
    found = _constants(text)
    for match in re.finditer(
        r'import\s+\{([^}]+)\}\s+from\s+["\']([^"\']+)["\']', text, re.DOTALL
    ):
        names, module = match.groups()
        if not module.startswith("."):
            continue
        base = (path.parent / module).resolve()
        candidate = next(
            (
                p
                for p in (base.with_suffix(".ts"), base.with_suffix(".tsx"))
                if p.is_file()
            ),
            None,
        )
        if candidate is None or not candidate.is_relative_to(root):
            continue
        exported = _file_constants(candidate, root, visited)
        for name in names.split(","):
            parts = name.strip().split(" as ")
            if parts[0] in exported:
                found[parts[-1].strip()] = exported[parts[0]]
    visited.remove(path)
    return found


def _api_reference(argument: str, constants: dict[str, str]) -> tuple[str | None, str]:
    expression = argument.strip()
    if expression.startswith("withQuery("):
        first = _call_argument(expression, _mask(expression), len("withQuery"))
        if first is not None:
            expression = first
    conditional = re.fullmatch(
        r'.*\?\s*(`[^`]+`|["\'][^"\']+["\'])\s*:\s*null', expression, re.DOTALL
    )
    if conditional:
        expression = conditional.group(1)
    if expression in constants:
        expression = constants[expression]
    if expression.startswith("apiUrl(") and expression.endswith(")"):
        expression = expression[len("apiUrl(") : -1].strip()
    if (
        len(expression) >= 2
        and expression[0] in ('"', "'")
        and expression[-1] == expression[0]
    ):
        value = expression[1:-1]
        return (value if value.startswith("/") else None), "literal_or_constant"
    if expression.startswith("`") and expression.endswith("`"):
        value = expression[1:-1]
        # The axios instance's baseURL is `${BASE}/api`; direct anchors use it
        # instead of `api.get`, but still target the same mounted API.
        if value.startswith("${api.defaults.baseURL}/"):
            value = value[len("${api.defaults.baseURL}") :]
        elif value.startswith("${BASE}/api/"):
            value = value[len("${BASE}") :]
        for identifier in re.findall(r"\$\{([^}]+)\}", value):
            replacement = constants.get(identifier)
            if replacement and replacement[0] in ('"', "'", "`"):
                replacement = replacement[1:-1]
            else:
                replacement = "{" + identifier + "}"
            value = value.replace("${" + identifier + "}", replacement)
        return (value if value.startswith("/") else None), "template"
    return None, "dynamic"


def _api_path(reference: str | None) -> str | None:
    if reference and reference.startswith("/") and not reference.startswith("/api/"):
        return "/api" + reference
    return reference


def _in_comment(text: str, position: int) -> bool:
    """Ignore direct browser calls appearing only in source comments."""
    if text.rfind("/*", 0, position) > text.rfind("*/", 0, position):
        return True
    line = text[text.rfind("\n", 0, position) + 1 : position]
    return "//" in line


def _imported_url_function(
    expression: str,
    source_text: str,
    source_path: Path,
    root: Path,
) -> tuple[str | None, str]:
    """Read a named URL helper's literal return for a window-navigation call."""
    call = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*\(.*\)", expression, re.DOTALL)
    if call is None:
        return None, "dynamic"
    name = call.group(1)
    for imported in re.finditer(
        r'import\s+\{([^}]+)\}\s+from\s+["\']([^"\']+)["\']',
        source_text,
        re.DOTALL,
    ):
        names, module = imported.groups()
        imported_names = {part.strip().split(" as ")[-1] for part in names.split(",")}
        if name not in imported_names or not module.startswith("."):
            continue
        base = (source_path.parent / module).resolve()
        target = next(
            (
                p
                for p in (base.with_suffix(".ts"), base.with_suffix(".tsx"))
                if p.is_file()
            ),
            None,
        )
        if target is None or not target.is_relative_to(root):
            continue
        text = target.read_text(encoding="utf-8")
        signature = re.search(rf"\bexport\s+function\s+{re.escape(name)}\s*\(", text)
        if signature is None:
            continue
        mask = _mask(text)
        body_start = mask.find("{", signature.end())
        if body_start < 0:
            continue
        body = text[body_start + 1 : _closing(mask, body_start, "{", "}")]
        returned = re.search(r"\breturn\s+(`[^`]+`|\"[^\"]+\"|'[^']+')\s*;", body)
        if returned:
            return _api_reference(returned.group(1), {})
    return None, "dynamic"


def _direct_url_consumers(root: Path) -> list[dict[str, Any]]:
    """Capture browser URL delivery that never calls the axios API client."""
    records: list[dict[str, Any]] = []
    for path in sorted((root / FRONTEND).rglob("*")):
        if path.suffix not in (".ts", ".tsx") or path.name.endswith(".test.ts"):
            continue
        source = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        mask = _mask(text)
        constants = _file_constants(path, root)
        constants.update(_conditional_url_constants(text))
        nested_ranges: list[tuple[int, int]] = []
        seen: dict[tuple[str, str], int] = {}
        # JSX prose may contain an apostrophe, which the lightweight JS mask
        # cannot distinguish from a string delimiter. Scan the raw JSX for
        # browser links; only the balanced expression is used as evidence.
        for match in re.finditer(r"\bhref\s*=\s*\{", text):
            if _in_comment(text, match.start()):
                continue
            start = text.find("{", match.start())
            end = _closing(text, start, "{", "}")
            nested_ranges.append((start, end))
            expression = text[start + 1 : end].strip()
            reference, resolution = _api_reference(expression, constants)
            is_api = (
                "apiUrl(" in expression
                or "api.defaults.baseURL" in expression
                or bool(reference and reference.startswith("/api/"))
            )
            invocation = "href"
            identity = (invocation, expression)
            seen[identity] = seen.get(identity, 0) + 1
            digest = hashlib.sha256(
                f"{invocation}\0{expression}\0{seen[identity]}".encode()
            ).hexdigest()[:12]
            detail = {
                "invocation": invocation,
                "method": "GET",
                "argument": " ".join(expression.split()),
                "api_reference": _api_path(reference) if is_api else None,
                "resolution": resolution if is_api else "dynamic",
                "delivery": "browser_link",
            }
            records.append(
                {
                    "kind": "api_consumer" if is_api else "navigation",
                    "key": f"{'api_consumer' if is_api else 'navigation'}:{source}:{digest}",
                    "source": source,
                    "detail": detail,
                }
            )
        for match in re.finditer(r"\bnew\s+EventSource\s*\(", mask):
            start = mask.find("(", match.start())
            nested_ranges.append((start, _closing(mask, start, "(", ")")))
        for match in re.finditer(r"\bwindow\.open\b", text):
            if _in_comment(text, match.start()):
                continue
            window_argument = _call_argument(text, text, match.end())
            if window_argument is None:
                continue
            reference, resolution = _imported_url_function(
                window_argument, text, path, root
            )
            identity = ("window.open", window_argument)
            seen[identity] = seen.get(identity, 0) + 1
            digest = hashlib.sha256(
                f"window.open\0{window_argument}\0{seen[identity]}".encode()
            ).hexdigest()[:12]
            records.append(
                {
                    "kind": "navigation",
                    "key": f"navigation:window.open:{source}:{digest}",
                    "source": source,
                    "detail": {
                        "invocation": "window.open",
                        "target": reference,
                        "argument": " ".join(window_argument.split()),
                        "resolution": resolution,
                        "lifecycle_hint": "indirect_window",
                    },
                }
            )
            records.append(
                {
                    "kind": "display_window",
                    "key": f"display_window:{source}:{digest}",
                    "source": source,
                    "detail": {
                        "invocation": "window.open",
                        "target": reference,
                        "argument": " ".join(window_argument.split()),
                        "resolution": resolution,
                        "data_dependency": "dynamic",
                    },
                }
            )
        for match in re.finditer(r"\bapiUrl\b", text):
            if any(start <= match.start() <= end for start, end in nested_ranges):
                continue
            if _in_comment(text, match.start()):
                continue
            if re.search(
                r"\bfunction\s+$", text[max(0, match.start() - 20) : match.start()]
            ):
                continue
            url_argument = _call_argument(text, text, match.end())
            if url_argument is None:
                continue
            reference, resolution = _api_reference(url_argument, constants)
            identity = ("apiUrl", url_argument)
            seen[identity] = seen.get(identity, 0) + 1
            digest = hashlib.sha256(
                f"apiUrl\0{url_argument}\0{seen[identity]}".encode()
            ).hexdigest()[:12]
            records.append(
                {
                    "kind": "api_consumer",
                    "key": f"api_consumer:{source}:{digest}",
                    "source": source,
                    "detail": {
                        "invocation": "apiUrl",
                        "method": "GET",
                        "argument": " ".join(url_argument.split()),
                        "api_reference": _api_path(reference),
                        "resolution": resolution,
                        "delivery": "browser_url",
                    },
                }
            )
    return records


def _print_consumers(root: Path) -> list[dict[str, Any]]:
    """Record browser print sinks whose rendered data needs field projection."""
    records: list[dict[str, Any]] = []
    pattern = re.compile(r"\b(window\.print|browserPrintAdapter\.print)\b")
    for path in sorted((root / FRONTEND).rglob("*")):
        if path.suffix not in (".ts", ".tsx") or path.name.endswith(".test.ts"):
            continue
        source = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        seen: dict[tuple[str, str], int] = {}
        for match in pattern.finditer(text):
            if _in_comment(text, match.start()):
                continue
            argument = _call_argument(text, text, match.end())
            if argument is None:
                continue
            invocation = match.group(1)
            identity = (invocation, argument)
            seen[identity] = seen.get(identity, 0) + 1
            digest = hashlib.sha256(
                f"{invocation}\0{argument}\0{seen[identity]}".encode()
            ).hexdigest()[:12]
            records.append(
                {
                    "kind": "print_output",
                    "key": f"print_output:{source}:{digest}",
                    "source": source,
                    "detail": {
                        "invocation": invocation,
                        "argument": " ".join(argument.split()),
                        "data_dependency": "dynamic",
                        "delivery": "browser_print",
                    },
                }
            )
    return records


def _api_consumers(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / FRONTEND).rglob("*")):
        if path.suffix not in (".ts", ".tsx") or path.name.endswith(".test.ts"):
            continue
        source = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        mask = _mask(text)
        constants = _file_constants(path, root)
        constants.update(_conditional_url_constants(text))
        seen: dict[tuple[str, str], int] = {}
        for match in _CALL.finditer(mask):
            invocation = match.group(0)
            if match.group("function") and re.search(
                r"\bfunction\s+$", mask[max(0, match.start() - 20) : match.start()]
            ):
                continue
            argument = _call_argument(text, mask, match.end())
            if argument is None:
                continue
            method = match.group("method") or (
                "GET"
                if invocation.startswith("use")
                else "subscribe"
                if invocation == "EventSource"
                else "fetch"
            )
            reference, resolution = _api_reference(argument, constants)
            reference = _api_path(reference)
            identity = (invocation, argument)
            ordinal = seen.get(identity, 0) + 1
            seen[identity] = ordinal
            digest = hashlib.sha256(
                f"{invocation}\0{argument}\0{ordinal}".encode()
            ).hexdigest()[:12]
            records.append(
                {
                    "kind": "api_consumer",
                    "key": f"api_consumer:{source}:{digest}",
                    "source": source,
                    "detail": {
                        "invocation": invocation,
                        "method": method.upper(),
                        "argument": " ".join(argument.split()),
                        "api_reference": reference,
                        "resolution": resolution,
                    },
                }
            )
    return records


def _client_modules(root: Path) -> list[dict[str, Any]]:
    """Inventory offline till and PWA modules whose data outlives a request."""
    records: list[dict[str, Any]] = []
    for family in ("till", "pwa"):
        for path in sorted((root / FRONTEND / family).rglob("*")):
            if path.suffix not in (".ts", ".tsx") or ".test." in path.name:
                continue
            source = path.relative_to(root).as_posix()
            contents = path.read_text(encoding="utf-8")
            masked = _mask(contents)
            imports: list[str] = []
            for module in re.findall(r'\bfrom\s+["\'](\.[^"\']+)["\']', contents):
                base = (path.parent / module).resolve()
                candidate = next(
                    (
                        item
                        for item in (
                            base.with_suffix(".ts"),
                            base.with_suffix(".tsx"),
                            base / "index.ts",
                            base / "index.tsx",
                        )
                        if item.is_file()
                    ),
                    None,
                )
                if candidate is not None and candidate.is_relative_to(root / FRONTEND):
                    imports.append(candidate.relative_to(root).as_posix())
            reads = re.findall(
                r"\b[A-Za-z_][\w.]*\.(?:get|where|filter|count|toArray|first|last)\s*\(",
                masked,
            )
            writes = re.findall(
                r"\b[A-Za-z_][\w.]*\.(?:add|put|bulkPut|delete|clear|update|transaction)\s*\(",
                masked,
            )
            records.append(
                {
                    "kind": "client_module",
                    "key": f"client_module:{source}",
                    "source": source,
                    "detail": {
                        "family": family,
                        "imports": sorted(set(imports)),
                        "read_candidates": sorted(set(reads)),
                        "write_candidates": sorted(set(writes)),
                        "storage_markers": sorted(
                            marker
                            for marker in (
                                "Dexie",
                                "indexedDB",
                                "localStorage",
                                "sessionStorage",
                                "serviceWorker",
                                "caches",
                            )
                            if marker in contents
                        ),
                    },
                }
            )
    return records


def discover_frontend(root: Path = ROOT) -> list[dict[str, Any]]:
    """Return stable raw records for routes, nav, bookmarks and API call sites."""
    screens = _screens(root)
    records = [
        *screens,
        *_subviews(root, screens),
        *_navigation_and_bookmarks(root),
        *_api_consumers(root),
        *_direct_url_consumers(root),
        *_print_consumers(root),
        *_client_modules(root),
    ]
    keys = [record["key"] for record in records]
    if len(keys) != len(set(keys)):
        duplicates = sorted(key for key in set(keys) if keys.count(key) > 1)
        raise InventoryShapeError(f"Duplicate frontend surface keys: {duplicates[:5]}")
    return sorted(records, key=lambda record: (record["kind"], record["key"]))


def check_frontend(records: list[dict[str, Any]]) -> list[str]:
    """Flag nav/bookmark targets with no declared built or planned route."""
    paths = {
        record["detail"]["path"] for record in records if record["kind"] == "screen"
    }
    findings: list[str] = []
    for record in records:
        if (
            record["kind"] == "navigation"
            and record["detail"].get("target")
            and not record["detail"].get("deep_link")
        ):
            path = record["detail"]["target"].split("?", 1)[0]
            if path not in paths:
                findings.append(f"{record['key']} has no declared screen")
        if record["kind"] == "bookmark":
            path = record["detail"]["to"].split("?", 1)[0]
            if path not in paths:
                findings.append(f"{record['key']} targets no declared screen: {path}")
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="write raw records to stdout"
    )
    parser.add_argument(
        "--check", action="store_true", help="validate declaration coverage"
    )
    args = parser.parse_args()
    records = discover_frontend()
    findings = check_frontend(records)
    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        counts = {
            kind: sum(row["kind"] == kind for row in records)
            for kind in (
                "screen",
                "subview",
                "navigation",
                "bookmark",
                "api_consumer",
                "print_output",
                "display_window",
                "client_module",
            )
        }
        print("Frontend consolidation surfaces:", counts)
        for finding in findings:
            print("UNMATCHED:", finding)
    if args.check and findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
