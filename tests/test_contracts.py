import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from menu_review import (
    CURRENT_SCHEMA_VERSION,
    load_claim,
    load_record,
)
from menu_review.contracts import migrate_envelope


class ContractTest(unittest.TestCase):
    def test_example_uses_current_contract(self):
        item = load_record(Path(__file__).parents[1] / "fixtures" / "menu_claim.json")
        self.assertEqual(item.domain, "menu_review")
        self.assertGreater(item.revision, 0)

    def test_v1_envelope_fields_preserved(self):
        # v1 六字段的含义与取值在 v2 样例中保持不变。
        item = load_record(Path(__file__).parents[1] / "fixtures" / "menu_claim.json")
        self.assertEqual(item.record_id, "sample-010")
        self.assertEqual(item.occurred_at, "2026-09-20T09:00:00+08:00")
        self.assertEqual(item.revision, 1)
        self.assertEqual(item.source, "业务样例")
        self.assertEqual(item.schema_version, CURRENT_SCHEMA_VERSION)

    def test_v1_to_v2_migration_keeps_envelope_and_nulls_menu(self):
        v1 = {
            "schema_version": 1,
            "record_id": "old-1",
            "domain": "menu_review",
            "occurred_at": "2026-01-01T00:00:00+08:00",
            "revision": 7,
            "source": "legacy",
        }
        migrated = migrate_envelope(v1)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["record_id"], "old-1")
        self.assertEqual(migrated["revision"], 7)
        self.assertIsNone(migrated["menu"])

    def test_load_v1_file_gives_null_menu(self):
        v1 = {
            "schema_version": 1,
            "record_id": "old-1",
            "domain": "menu_review",
            "occurred_at": "2026-01-01T00:00:00+08:00",
            "revision": 1,
            "source": "legacy",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claim.json"
            path.write_text(json.dumps(v1), encoding="utf-8")
            envelope, menu = load_claim(path)
        self.assertEqual(envelope.record_id, "old-1")
        self.assertIsNone(menu)

    def test_unknown_schema_rejected(self):
        with self.assertRaises(ValueError):
            migrate_envelope({"schema_version": 99})


if __name__ == "__main__":
    unittest.main()
