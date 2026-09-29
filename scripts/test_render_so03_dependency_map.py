"""The generated map must expose writer candidates and unresolved call edges."""

from __future__ import annotations

import unittest

from scripts.render_so03_dependency_map import render_map


class DependencyMapTests(unittest.TestCase):
    def test_writer_and_dynamic_consumer_are_visible_with_owner(self) -> None:
        ids = ["A00", "P00", *(f"C{n:02d}" for n in range(1, 21))]
        manifest = {
            "retirements": [
                {
                    "id": owner_id,
                    "owner": "SO-04",
                    "target_implementation": "target",
                    "removal_condition": "gate",
                }
                for owner_id in ids
            ],
            "discovered": {
                "records": [
                    {
                        "kind": "module",
                        "key": "module:masters.writer",
                        "source": "backend/masters/writer.py",
                        "primary_id": "C01",
                        "lifecycle": "temporary_supported_legacy",
                        "authorization": "tenant and scope",
                        "callers": {},
                        "detail": {
                            "tags": ["service"],
                            "imports": [],
                            "write_candidates": ["Master.objects.create"],
                        },
                    },
                    {
                        "kind": "api_consumer",
                        "key": "api_consumer:unknown",
                        "source": "frontend/src/pages/Masters.tsx",
                        "primary_id": "C01",
                        "lifecycle": "unresolved",
                        "authorization": "tenant and scope",
                        "callers": {},
                        "detail": {"api_reference": None, "argument": "makeUrl(id)"},
                    },
                ]
            },
        }

        rendered = render_map(manifest)

        self.assertIn("## C01 — SO-04", rendered)
        self.assertIn("backend/masters/writer.py", rendered)
        self.assertIn("Master.objects.create", rendered)
        self.assertIn("api_consumer:unknown", rendered)
        self.assertIn("makeUrl(id)", rendered)
        self.assertIn("not proof that all writes are business writes", rendered)

    def test_each_entrypoint_has_an_authority_and_static_dependency_chain(self) -> None:
        ids = ["A00", "P00", *(f"C{n:02d}" for n in range(1, 21))]
        manifest = {
            "retirements": [
                {
                    "id": owner_id,
                    "owner": "SO-04",
                    "target_implementation": "target",
                    "removal_condition": "gate",
                }
                for owner_id in ids
            ],
            "discovered": {
                "records": [
                    {
                        "kind": "api",
                        "key": "api:POST:/api/masters/items/",
                        "source": "backend/masters/views.py",
                        "primary_id": "C01",
                        "lifecycle": "temporary_supported_legacy",
                        "authorization": "tenant and site scope",
                        "detail": {},
                        "callers": {
                            "frontend_api_callsites": ["api_consumer:item-post"]
                        },
                        "dependencies": {
                            "source_module": "module:masters.views",
                            "local_component_keys": [
                                "module:masters.services",
                                "model:masters.Item",
                            ],
                            "runtime_edges": ["indirect calls need tracing"],
                        },
                        "data_access": {
                            "read_candidates": ["Item.objects.get"],
                            "write_candidates": ["Item.objects.create"],
                            "completeness": "static candidates only",
                        },
                    },
                    {
                        "kind": "screen",
                        "key": "screen:/setup/items",
                        "source": "frontend/src/routes.tsx",
                        "primary_id": "C01",
                        "lifecycle": "canonical",
                        "authorization": "tenant and site scope",
                        "detail": {"component_source": "frontend/src/pages/Items.tsx"},
                        "callers": {},
                        "dependencies": {
                            "frontend_api_callsites": ["api_consumer:item-post"],
                            "local_component_keys": [],
                            "runtime_edges": [],
                        },
                        "data_access": {
                            "read_candidates": [],
                            "write_candidates": [],
                            "completeness": "frontend API candidates",
                        },
                    },
                    {
                        "kind": "api_consumer",
                        "key": "api_consumer:item-post",
                        "source": "frontend/src/pages/Items.tsx",
                        "primary_id": "C01",
                        "lifecycle": "canonical",
                        "authorization": "tenant and site scope",
                        "detail": {"api_reference": "/api/masters/items/"},
                        "callers": {},
                        "dependencies": {
                            "api_targets": ["api:POST:/api/masters/items/"]
                        },
                    },
                    {
                        "kind": "print_output",
                        "key": "print_output:items:receipt",
                        "source": "frontend/src/pages/Items.tsx",
                        "primary_id": "C01",
                        "lifecycle": "canonical",
                        "authorization": "project printable fields",
                        "detail": {"invocation": "window.print"},
                        "callers": {},
                        "dependencies": {
                            "runtime_edges": "print projection needs tracing"
                        },
                        "data_access": {
                            "read_candidates": [],
                            "write_candidates": [],
                            "completeness": "dynamic output",
                        },
                    },
                    {
                        "kind": "client_module",
                        "key": "client_module:frontend/src/till/db.ts",
                        "source": "frontend/src/till/db.ts",
                        "primary_id": "C01",
                        "lifecycle": "canonical",
                        "authorization": "scoped offline lease",
                        "detail": {
                            "imports": [],
                            "read_candidates": ["db.bills.get("],
                            "write_candidates": ["db.bills.put("],
                        },
                        "callers": {},
                        "dependencies": {},
                        "data_access": {},
                    },
                ]
            },
        }

        rendered = render_map(manifest)

        self.assertIn("### Entry-point dependency traces", rendered)
        self.assertIn("`screen:/setup/items`", rendered)
        self.assertIn("`api:POST:/api/masters/items/`", rendered)
        self.assertIn("`module:masters.services`", rendered)
        self.assertIn("`model:masters.Item`", rendered)
        self.assertIn("`api_consumer:item-post`", rendered)
        self.assertIn("R:1 / W:1", rendered)
        self.assertIn("`print_output:items:receipt`", rendered)
        self.assertIn("`client_module:frontend/src/till/db.ts`", rendered)
        self.assertIn("db.bills.put(", rendered)


if __name__ == "__main__":
    unittest.main()
