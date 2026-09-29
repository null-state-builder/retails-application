"""New surfaces and weakened retirement decisions must stop inventory refresh."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from scripts.check_so03_consolidation import (
    INVENTORY,
    InventoryError,
    _access_cutover_table,
    _check_protected_boundaries,
    _check_snapshot,
    _classify,
    _retirements,
    _rules,
)


class ConsolidationInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = {
            "id": "P00",
            "owner": "SO-02",
            "default_lifecycle": "canonical",
            "data_ownership": "platform",
            "authorization": "operator",
            "callers": "trace callers",
            "target_implementation": "supported platform endpoint",
            "data_mapping": "no migration",
            "acceptance_evidence": "route test",
            "removal_condition": "platform review",
        }
        self.known = {
            "kind": "api",
            "key": "api:GET:/api/known",
            "source": "backend/known.py",
            "detail": {},
        }
        self.rule = {
            "id": "known-api",
            "kind": "api",
            "key_regex": r"^api:GET:/api/known$",
            "primary_id": "P00",
        }

    def test_new_route_command_and_screen_need_explicit_rules(self) -> None:
        for kind, key, source in (
            ("api", "api:POST:/api/new", "backend/new.py"),
            (
                "command",
                "command:new_worker",
                "backend/management/commands/new_worker.py",
            ),
            ("screen", "screen:/new", "frontend/src/routes.tsx"),
        ):
            with self.subTest(kind=kind):
                new = {"kind": kind, "key": key, "source": source, "detail": {}}
                with self.assertRaisesRegex(InventoryError, "Unclassified"):
                    _classify([self.known, new], [self.rule], {"P00": self.owner})

    def test_added_deleted_or_changed_discovery_fails_check(self) -> None:
        snapshot = {"checksum": "old", "records": [self.known]}
        manifest = {"discovered": snapshot}
        _check_snapshot(manifest, copy.deepcopy(snapshot))
        for changed in (
            {"checksum": "new", "records": [self.known]},
            {"checksum": "old", "records": []},
            {"checksum": "old", "records": [self.known, {"key": "api:GET:/api/new"}]},
        ):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(InventoryError, "Discovered surfaces changed"),
            ):
                _check_snapshot(manifest, changed)

    def test_temporary_api_without_named_consumer_gets_trace_gate(self) -> None:
        rule = {**self.rule, "lifecycle": "temporary_supported_legacy"}
        rows = _classify([self.known], [rule], {"P00": self.owner})

        self.assertEqual(rows[0]["consumer_trace"]["status"], "untraced")
        self.assertIn("SO-02 must name", rows[0]["consumer_trace"]["gate"])

    def test_parameterised_api_links_to_named_frontend_consumer(self) -> None:
        route = {
            "kind": "api",
            "key": "api:GET:/api/items/<int:pk>",
            "source": "backend/items.py",
            "detail": {},
        }
        consumer = {
            "kind": "api_consumer",
            "key": "api_consumer:items:detail",
            "source": "frontend/src/pages/Items.tsx",
            "detail": {"method": "GET", "api_reference": "/api/items/{id}"},
        }
        rules = [
            {
                "id": "route",
                "kind": "api",
                "key_regex": r"^api:GET:/api/items/<int:pk>$",
                "primary_id": "P00",
                "lifecycle": "temporary_supported_legacy",
            },
            {
                "id": "consumer",
                "kind": "api_consumer",
                "key_regex": r"^api_consumer:items:detail$",
                "primary_id": "P00",
            },
        ]
        rows = _classify([route, consumer], rules, {"P00": self.owner})

        self.assertEqual(
            rows[0]["consumer_trace"]["status"], "static_frontend_callsite"
        )
        self.assertEqual(
            rows[0]["consumer_trace"]["named_callsites"], [consumer["key"]]
        )

    def test_entrypoint_trace_links_source_service_model_and_reader(self) -> None:
        raw = [
            {
                "kind": "module",
                "key": "module:items.views",
                "source": "backend/items/views.py",
                "detail": {
                    "imports": ["items.models"],
                    "read_candidates": ["Item.objects.get"],
                    "write_candidates": ["Item.objects.create"],
                },
            },
            {
                "kind": "module",
                "key": "module:items.models",
                "source": "backend/items/models.py",
                "detail": {
                    "imports": [],
                    "read_candidates": [],
                    "write_candidates": [],
                },
            },
            {
                "kind": "model",
                "key": "model:items.Item",
                "source": "backend/items/models.py",
                "detail": {"fields": ["name"], "relations": []},
            },
            {
                "kind": "api",
                "key": "api:GET:/api/items/<int:pk>",
                "source": "backend/items/views.py",
                "detail": {},
            },
            {
                "kind": "screen",
                "key": "screen:/items",
                "source": "frontend/src/routes.tsx",
                "detail": {
                    "path": "/items",
                    "component_source": "frontend/src/pages/Items.tsx",
                },
            },
            {
                "kind": "api_consumer",
                "key": "api_consumer:items:detail",
                "source": "frontend/src/pages/Items.tsx",
                "detail": {"method": "GET", "api_reference": "/api/items/{id}"},
            },
            {
                "kind": "subview",
                "key": "subview:/items?view=detail",
                "source": "frontend/src/routes.tsx",
                "detail": {
                    "parent_path": "/items",
                    "component_source": "frontend/src/pages/Items.tsx",
                },
            },
            {
                "kind": "print_output",
                "key": "print_output:items",
                "source": "frontend/src/pages/Items.tsx",
                "detail": {"invocation": "window.print", "data_dependency": "dynamic"},
            },
        ]
        rules = [
            {
                "id": row["key"],
                "kind": row["kind"],
                "key_regex": "^" + __import__("re").escape(row["key"]) + "$",
                "primary_id": "P00",
            }
            for row in raw
        ]
        rows = {row["key"]: row for row in _classify(raw, rules, {"P00": self.owner})}
        api = rows["api:GET:/api/items/<int:pk>"]

        self.assertEqual(api["dependencies"]["source_module"], "module:items.views")
        self.assertIn(
            "module:items.models", api["dependencies"]["local_component_keys"]
        )
        self.assertIn("model:items.Item", api["dependencies"]["candidate_model_keys"])
        self.assertIn("Item.objects.get", api["data_access"]["read_candidates"])
        self.assertIn("Item.objects.create", api["data_access"]["write_candidates"])
        self.assertEqual(
            rows["screen:/items"]["dependencies"]["frontend_api_callsites"],
            ["api_consumer:items:detail"],
        )
        self.assertEqual(
            rows["api_consumer:items:detail"]["dependencies"]["api_targets"],
            ["api:GET:/api/items/<int:pk>"],
        )
        self.assertEqual(
            rows["subview:/items?view=detail"]["dependencies"]["local_component_keys"],
            ["screen:/items"],
        )
        self.assertEqual(
            rows["print_output:items"]["dependencies"]["frontend_api_callsites"],
            ["api_consumer:items:detail"],
        )

    def test_registered_job_uses_callback_module_data_leads(self) -> None:
        raw = [
            {
                "kind": "module",
                "key": "module:items.apps",
                "source": "backend/items/apps.py",
                "detail": {
                    "imports": ["items.worker"],
                    "read_candidates": [],
                    "write_candidates": [],
                },
            },
            {
                "kind": "module",
                "key": "module:items.worker",
                "source": "backend/items/worker.py",
                "detail": {
                    "imports": [],
                    "read_candidates": ["Item.objects.filter"],
                    "write_candidates": ["Item.objects.update"],
                },
            },
            {
                "kind": "job_handler",
                "key": "job_handler:items",
                "source": "backend/items/apps.py",
                "detail": {"callback": "run"},
            },
        ]
        rules = [
            {
                "id": row["key"],
                "kind": row["kind"],
                "key_regex": "^" + __import__("re").escape(row["key"]) + "$",
                "primary_id": "P00",
            }
            for row in raw
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backend/items/apps.py"
            source.parent.mkdir(parents=True)
            source.write_text("from items.worker import run\n")
            with patch("scripts.check_so03_consolidation.ROOT", root):
                rows = {
                    row["key"]: row
                    for row in _classify(raw, rules, {"P00": self.owner})
                }

        job = rows["job_handler:items"]
        self.assertEqual(
            job["dependencies"]["callback_component"], "module:items.worker"
        )
        self.assertEqual(
            job["data_access"]["basis"], "callback_module_static_candidates"
        )
        self.assertIn("Item.objects.filter", job["data_access"]["read_candidates"])
        self.assertIn("Item.objects.update", job["data_access"]["write_candidates"])

    def test_offline_client_modules_link_storage_and_importers(self) -> None:
        raw = [
            {
                "kind": "client_module",
                "key": "client_module:frontend/src/till/db.ts",
                "source": "frontend/src/till/db.ts",
                "detail": {
                    "imports": [],
                    "read_candidates": ["db.items.get("],
                    "write_candidates": ["db.items.put("],
                    "storage_markers": ["Dexie"],
                },
            },
            {
                "kind": "client_module",
                "key": "client_module:frontend/src/till/sync.ts",
                "source": "frontend/src/till/sync.ts",
                "detail": {
                    "imports": ["frontend/src/till/db.ts"],
                    "read_candidates": [],
                    "write_candidates": [],
                    "storage_markers": [],
                },
            },
        ]
        rules = [
            {
                "id": row["key"],
                "kind": row["kind"],
                "key_regex": "^" + __import__("re").escape(row["key"]) + "$",
                "primary_id": "P00",
                "lifecycle": "unresolved",
                "gate": "Offline proof",
            }
            for row in raw
        ]
        rows = {row["key"]: row for row in _classify(raw, rules, {"P00": self.owner})}
        db = rows["client_module:frontend/src/till/db.ts"]
        sync = rows["client_module:frontend/src/till/sync.ts"]

        self.assertEqual(db["data_access"]["basis"], "client_storage_static_candidates")
        self.assertEqual(db["data_access"]["storage_markers"], ["Dexie"])
        self.assertIn("db.items.put(", db["data_access"]["write_candidates"])
        self.assertEqual(
            db["callers"]["static_frontend_importers"],
            ["client_module:frontend/src/till/sync.ts"],
        )
        self.assertEqual(
            sync["dependencies"]["local_component_keys"],
            ["client_module:frontend/src/till/db.ts"],
        )

    def test_owner_and_replacement_gaps_are_rejected(self) -> None:
        required = (
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
        )
        ids = ["A00", "P00", *(f"C{number:02d}" for number in range(1, 21))]
        retirements = [
            dict.fromkeys(required, "proof") | {"id": ident} for ident in ids
        ]
        for row in retirements:
            row["default_lifecycle"] = "canonical"
        manifest: dict[str, Any] = {"schema_version": 1, "retirements": retirements}
        owners = _retirements(manifest)
        for broken in (
            {**self.rule, "primary_id": "C99"},
            {**self.rule, "replacement": ""},
            {**self.rule, "migration_requirements": ""},
            {**self.rule, "acceptance_evidence": ""},
            {**self.rule, "removal_condition": ""},
        ):
            with self.subTest(broken=broken):
                manifest["classification_rules"] = [broken]
                with self.assertRaises(InventoryError):
                    _rules(manifest, owners)
        manifest["retirements"][0]["target_implementation"] = ""
        with self.assertRaisesRegex(InventoryError, "target_implementation"):
            _retirements(manifest)

    def test_access_cutover_table_comes_from_structured_manifest(self) -> None:
        row = {
            "id": "authority",
            "existing_family": "old grant editor",
            "target_and_data_movement": "one policy editor and linked evidence",
            "proof_and_removal": "migrate, test denial, then remove old writer",
        }
        rendered = _access_cutover_table({"access_cutovers": [row]})
        self.assertIn("old grant editor", rendered)
        self.assertIn("one policy editor and linked evidence", rendered)
        with self.assertRaisesRegex(InventoryError, "target_and_data_movement"):
            _access_cutover_table(
                {"access_cutovers": [{**row, "target_and_data_movement": ""}]}
            )

    def test_boundary_regression_is_rejected_before_either_mode_writes(self) -> None:
        # The main command invokes this guard before branching into --check or --write.
        with (
            patch(
                "scripts.check_so03_boundaries.check_boundaries",
                return_value=["retired path"],
            ),
            self.assertRaisesRegex(InventoryError, "Protected boundary regression"),
        ):
            _check_protected_boundaries({"discovered": {"records": []}})

    def test_optional_admin_and_known_duplicate_routes_keep_proof_gates(self) -> None:
        records = json.loads(INVENTORY.read_text())["discovered"]["records"]
        by_key = {row["key"]: row for row in records}
        admin = [
            row
            for row in records
            if row["kind"] == "api"
            and row["key"].split(":", 2)[2].startswith("/admin/")
        ]
        self.assertTrue(admin)
        self.assertTrue(
            all(
                row["primary_id"] == "P00"
                and row["lifecycle"] == "unresolved"
                and row.get("gate")
                for row in admin
            )
        )
        self.assertEqual(
            by_key["screen:/transfer"]["lifecycle"], "temporary_supported_legacy"
        )
        self.assertEqual(by_key["screen:/goods/transfers"]["lifecycle"], "canonical")
        self.assertEqual(
            by_key["model:accounts.RoleGrant"]["lifecycle"],
            "temporary_supported_legacy",
        )


if __name__ == "__main__":
    unittest.main()
