"""Boundary policy must reject new old-authority edges even during refresh."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.check_so03_boundaries import check_boundaries, derive_legacy_allowlist


class BoundaryTests(unittest.TestCase):
    def test_new_legacy_import_and_retired_source_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backend" / "example.py"
            source.parent.mkdir()
            source.write_text("from accounts.permissions import user_can\n")
            manifest = {
                "protected_boundaries": {
                    "legacy_permission_modules": ["accounts.permissions"],
                    "legacy_permission_symbols": ["RoleGrant"],
                    "legacy_permission_allowlist": [],
                    "retired_modules": ["accounts.goods_urls"],
                    "retired_paths": ["frontend/src/pages/AccessMatrix.tsx"],
                    "retired_api_prefixes": ["/api/goods-v1/auth/admin/"],
                },
                "discovered": {
                    "records": [
                        {
                            "kind": "module",
                            "key": "module:example",
                            "source": "backend/example.py",
                            "detail": {
                                "imports": [
                                    "accounts.permissions",
                                    "accounts.goods_urls",
                                ]
                            },
                        },
                        {
                            "kind": "api",
                            "key": "api:POST:/api/goods-v1/auth/admin/grants/",
                            "source": "backend/example.py",
                            "detail": {},
                        },
                    ]
                },
            }
            (root / "frontend/src/pages").mkdir(parents=True)
            (root / "frontend/src/pages/AccessMatrix.tsx").write_text(
                "export default null;"
            )

            findings = check_boundaries(manifest, root)

            self.assertTrue(
                any("New legacy permission dependency" in item for item in findings)
            )
            self.assertTrue(
                any("Retired module dependency" in item for item in findings)
            )
            self.assertTrue(
                any("Retired source path reintroduced" in item for item in findings)
            )
            self.assertTrue(
                any("Retired API reintroduced" in item for item in findings)
            )
            manifest["protected_boundaries"]["legacy_permission_allowlist"] = (
                derive_legacy_allowlist(manifest, root)
            )
            self.assertFalse(
                any(
                    "New legacy permission dependency" in item
                    for item in check_boundaries(manifest, root)
                )
            )
            manifest["protected_boundaries"]["reference_review_required"] = True
            self.assertTrue(any(
                "needs disposition" in item for item in check_boundaries(manifest, root)
            ))
            manifest["protected_boundaries"]["legacy_permission_allowlist"][0].update(
                disposition="assignment_backed_adapter",
                trace_status="needs_resource_trace",
                reviewed_chain="Example permission import requires resource proof.",
            )
            self.assertFalse(any(
                "needs disposition" in item for item in check_boundaries(manifest, root)
            ))
            source.write_text(
                source.read_text()
                + "from accounts.permissions import require_section\n"
            )
            self.assertTrue(
                any(
                    "Expanded legacy permission dependency" in item
                    for item in check_boundaries(manifest, root)
                )
            )

    def test_aliased_rolegrant_import_and_legacy_user_role_read_are_frozen(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "backend" / "example.py"
            source.parent.mkdir()
            source.write_text(
                "from accounts.goods_models import RoleGrant as RG\n"
                "def allowed(user, request): return (user.role is not None and "
                "request.user.scope_type and user.is_superuser and RG)\n"
            )
            manifest = {
                "protected_boundaries": {
                    "legacy_permission_modules": ["accounts.permissions"],
                    "legacy_permission_symbols": ["RoleGrant"],
                    "legacy_permission_allowlist": [],
                    "retired_modules": [],
                    "retired_paths": [],
                    "retired_api_prefixes": [],
                },
                "discovered": {
                    "records": [
                        {
                            "kind": "module",
                            "key": "module:example",
                            "source": "backend/example.py",
                            "detail": {"imports": ["accounts.goods_models"]},
                        },
                    ]
                },
            }
            found = derive_legacy_allowlist(manifest, root)
            references = {row["reference"] for row in found}
            self.assertIn("symbol_import:RoleGrant", references)
            self.assertIn("legacy_field:user.role", references)
            self.assertIn("legacy_field:user.scope_type", references)
            self.assertIn("legacy_field:user.is_superuser", references)
            self.assertTrue(
                any(
                    "New legacy permission dependency" in item
                    for item in check_boundaries(manifest, root)
                )
            )


if __name__ == "__main__":
    unittest.main()
