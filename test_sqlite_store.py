import json
import tempfile
import unittest
from pathlib import Path

from sqlite_store import SQLitePanelStore


class SQLitePanelStoreTests(unittest.TestCase):
    def test_migrates_json_and_preserves_collections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "storage" / "data"
            data_dir.mkdir(parents=True)
            database = data_dir / "panel.sqlite3"
            legacy = data_dir / "panel_db.json"
            expected = {
                "users": {"123": {"name": "Owner"}},
                "bots": {"abc123": {"owner": 123, "status": "running"}},
                "audit": [{"action": "created"}],
                "metrics_total": 7,
            }
            legacy.write_text(json.dumps(expected), encoding="utf-8")

            store = SQLitePanelStore(database, legacy)
            self.assertEqual(store.load(), expected)
            self.assertFalse(legacy.exists())
            self.assertEqual(
                len(list((root / "storage" / "backups").glob("panel_db_pre_sqlite_*.json"))),
                1,
            )

            expected["users"]["123"]["name"] = "Updated"
            store.save(expected)
            store.close()

            reopened = SQLitePanelStore(database, legacy)
            self.assertEqual(reopened.load(), expected)
            reopened.close()

    def test_invalid_legacy_json_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "storage" / "data"
            data_dir.mkdir(parents=True)
            database = data_dir / "panel.sqlite3"
            legacy = data_dir / "panel_db.json"
            legacy.write_text("{not json", encoding="utf-8")

            store = SQLitePanelStore(database, legacy)
            with self.assertRaises(RuntimeError):
                store.load()
            self.assertTrue(legacy.exists())
            store.close()

    def test_sql_snapshot_is_standalone_and_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "panel.sqlite3"
            store = SQLitePanelStore(database, root / "missing.json")
            expected = {
                "users": {"123": {"name": "Owner"}},
                "bots": {},
                "audit": [],
            }
            store.save(expected)
            snapshot = root / "backup.sqlite3"
            store.backup_to(snapshot)
            store.close()

            SQLitePanelStore.validate_snapshot(snapshot)
            self.assertEqual(SQLitePanelStore.read_snapshot(snapshot), expected)


if __name__ == "__main__":
    unittest.main()
