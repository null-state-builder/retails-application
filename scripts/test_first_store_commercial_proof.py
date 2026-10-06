"""Proof ownership failures refuse before touching any commercial state."""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).with_name("first-store-commercial-proof.py")
spec = importlib.util.spec_from_file_location("commercial_proof_test", PATH)
assert spec is not None and spec.loader is not None
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class CommercialOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = {"database": "kdps_rehearsal_first_store_fictional", "system_identifier": "123456"}
        self.actual = (self.record["database"], "kdps_proof", "123456")

    def check(self, actual=None, *, host="127.0.0.1", port="55433", record=None) -> None:
        proof.require_owned_connection(record or self.record, actual or self.actual, host=host, port=port)

    def test_accepts_only_exact_current_owned_identity(self) -> None:
        with patch.dict(os.environ, KDPS_PROOF_MODE="1", KDPS_REHEARSAL_DB=self.record["database"]):
            self.check()
            for actual in (("another_db", "kdps_proof", "123456"),
                           (self.record["database"], "another_user", "123456"),
                           (self.record["database"], "kdps_proof", "654321")):
                with self.subTest(actual=actual), self.assertRaises(RuntimeError):
                    self.check(actual)
            for endpoint in ({"host": "localhost"}, {"port": "5432"}):
                with self.subTest(endpoint=endpoint), self.assertRaises(RuntimeError):
                    self.check(**endpoint)
            with self.assertRaises(RuntimeError):
                self.check(record={"database": "production", "system_identifier": "123456"})

    def test_refuses_missing_or_changed_proof_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            self.check()
        with patch.dict(os.environ, KDPS_PROOF_MODE="1", KDPS_REHEARSAL_DB="another_db"), self.assertRaises(RuntimeError):
            self.check()


if __name__ == "__main__":
    unittest.main()
