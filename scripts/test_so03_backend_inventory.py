"""Backend discovery must include non-HTTP writers and optional entry points."""

from __future__ import annotations

import unittest

from scripts.so03_backend_inventory import discover_backend


class BackendInventoryTests(unittest.TestCase):
    def test_mounted_and_background_surfaces_are_discovered(self) -> None:
        records = discover_backend()
        by_key = {record["key"]: record for record in records}

        self.assertEqual(len(by_key), len(records))
        self.assertIn("api:ANY:/admin/", by_key)
        self.assertIn("command:run_goods_worker", by_key)
        self.assertIn("scheduled_job:inventory_report_refresh", by_key)
        self.assertIn("job_handler:export", by_key)
        self.assertIn("middleware:masters.tenancy_middleware.TenantMiddleware", by_key)
        self.assertIn("middleware:masters.unit_context.ActiveContextMiddleware", by_key)
        self.assertLess(
            by_key["middleware:masters.tenancy_middleware.TenantMiddleware"]["detail"][
                "position"
            ],
            by_key["middleware:masters.unit_context.ActiveContextMiddleware"]["detail"][
                "position"
            ],
        )
        self.assertTrue(any(row["kind"] == "signal_receiver" for row in records))
        self.assertIn("model:accounts.RoleAssignment", by_key)
        self.assertIn(
            "migration:accounts.migrations.0022_roleassignment_revoked_at", by_key
        )


if __name__ == "__main__":
    unittest.main()
