"""Focused proof that consolidation discovery follows the mounted frontend."""

from __future__ import annotations

import unittest

from scripts.so03_frontend_inventory import check_frontend, discover_frontend


class FrontendInventoryTests(unittest.TestCase):
    def test_canonical_and_legacy_journeys_are_both_discovered(self) -> None:
        records = discover_frontend()
        by_key = {record["key"]: record for record in records}

        self.assertFalse(check_frontend(records))
        self.assertEqual(
            by_key["screen:/setup/people-access"]["detail"]["component"],
            "PeopleAccessPage",
        )
        self.assertEqual(
            by_key["bookmark:/setup/users"]["detail"]["to"],
            "/setup/people-access?panel=people",
        )
        self.assertIn("screen:/goods/transfers/:id", by_key)
        self.assertIn("screen:/transfer/:id", by_key)
        self.assertTrue(
            any(
                row["kind"] == "api_consumer"
                and row["source"] == "frontend/src/pages/OutboundTransfers.tsx"
                and (row["detail"]["api_reference"] or "").startswith("/api/outbound/")
                for row in records
            )
        )
        self.assertTrue(
            any(
                row["kind"] == "api_consumer"
                and row["source"] == "frontend/src/pages/Transfers.tsx"
                and (row["detail"]["api_reference"] or "").startswith(
                    "/api/goods-v1/outbound/"
                )
                for row in records
            )
        )
        self.assertTrue(
            any(
                row["kind"] == "api_consumer"
                and row["detail"]["invocation"] == "EventSource"
                and row["detail"]["api_reference"] == "/api/goods-v1/events"
                for row in records
            )
        )
        self.assertTrue(
            any(
                row["kind"] == "api_consumer"
                and row["source"] == "frontend/src/pages/UnifiedAccess.tsx"
                and row["detail"]["method"] == "PUT"
                and row["detail"]["api_reference"]
                == "/api/auth/admin/users/{encodeURIComponent(userId)}/assignments"
                for row in records
            )
        )

    def test_keys_are_unique_and_independent_of_line_numbers(self) -> None:
        records = discover_frontend()
        keys = [record["key"] for record in records]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(
            all(record["source"].startswith("frontend/src/") for record in records)
        )
        self.assertEqual(records, discover_frontend())

    def test_direct_browser_outputs_and_configuration_subviews_are_discovered(
        self,
    ) -> None:
        records = discover_frontend()
        by_key = {record["key"]: record for record in records}
        links = [
            row
            for row in records
            if row["kind"] == "api_consumer"
            and row["detail"].get("invocation") == "href"
        ]
        self.assertEqual(len(links), 7)
        self.assertTrue(all(row["detail"]["method"] == "GET" for row in links))
        self.assertEqual(
            {
                row["detail"]["api_reference"]
                for row in links
                if row["detail"]["api_reference"]
            },
            {
                "/api/goods-v1/files/{id}/download",
                "/api/ptmapper/files/{documentId}/export",
                "/api/ptmapper/files/{documentId}/export.xlsx",
                "/api/store/checklists/ticks/{done.id}/photo",
            },
        )
        dynamic_links = [row for row in links if row["detail"]["api_reference"] is None]
        self.assertEqual(len(dynamic_links), 3)
        self.assertTrue(
            all(row["detail"]["resolution"] == "dynamic" for row in dynamic_links)
        )
        self.assertEqual(
            by_key["subview:/setup/configuration?view=wizard"]["detail"][
                "component_source"
            ],
            "frontend/src/pages/ProfileWizard.tsx",
        )
        self.assertEqual(
            by_key["subview:/setup/configuration?view=approvals"]["detail"][
                "component"
            ],
            "ApprovalsPanel",
        )
        print_rows = [row for row in records if row["kind"] == "print_output"]
        self.assertEqual(len(print_rows), 10)
        self.assertTrue(
            all(row["detail"]["data_dependency"] == "dynamic" for row in print_rows)
        )
        display_rows = [row for row in records if row["kind"] == "display_window"]
        self.assertEqual(len(display_rows), 1)
        self.assertEqual(
            display_rows[0]["detail"]["target"].split("?", 1)[0], "/sell/display"
        )
        self.assertFalse(
            any(
                row["kind"] == "api_consumer"
                and row["source"] == "frontend/src/pages/sell/Billing.tsx"
                and row["detail"].get("invocation") == "window.open"
                for row in records
            )
        )

    def test_offline_till_and_pwa_modules_have_owners_in_the_inventory(self) -> None:
        records = discover_frontend()
        by_key = {record["key"]: record for record in records}
        db = by_key["client_module:frontend/src/till/db.ts"]
        self.assertEqual(db["detail"]["family"], "till")
        self.assertIn("Dexie", db["detail"]["storage_markers"])
        self.assertIn("frontend/src/till/types.ts", db["detail"]["imports"])
        self.assertIn("client_module:frontend/src/till/sync.ts", by_key)
        self.assertIn("client_module:frontend/src/till/authority.ts", by_key)
        self.assertEqual(
            by_key["client_module:frontend/src/pwa/config.ts"]["detail"]["family"],
            "pwa",
        )


if __name__ == "__main__":
    unittest.main()
