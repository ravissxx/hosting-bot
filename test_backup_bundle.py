import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from backup_bundle import (
    BACKUP_ROOTS,
    create_full_backup,
    detect_backup_format,
    extract_full_backup,
    replace_backup_roots,
    temporary_directory_near,
    temporary_file_near,
)
from sqlite_store import SQLitePanelStore


class BackupBundleTests(unittest.TestCase):
    def test_restore_staging_uses_destination_filesystem(self):
        default_temp_device = Path(tempfile.gettempdir()).stat().st_dev
        shm_root = Path("/dev/shm")
        destination_root = (
            shm_root
            if shm_root.is_dir() and shm_root.stat().st_dev != default_temp_device
            else Path(tempfile.gettempdir())
        )
        with tempfile.TemporaryDirectory(dir=destination_root) as temporary:
            destination = Path(temporary) / "panel" / "storage" / "data" / "panel.sqlite3"
            destination.parent.mkdir(parents=True)
            destination_device = destination.parent.stat().st_dev

            with temporary_directory_near(destination.parent.parent.parent, "restore-test-") as stage:
                self.assertEqual(Path(stage).stat().st_dev, destination_device)

            with temporary_file_near(destination, "import-test-", ".sqlite3") as staged_file:
                self.assertEqual(Path(staged_file.name).stat().st_dev, destination_device)
                staged_file.write(b"test")
                staged_file.flush()
                os.replace(staged_file.name, destination)

            self.assertEqual(destination.read_bytes(), b"test")

    def test_detects_supported_backup_formats_from_content(self):
        self.assertEqual(detect_backup_format(b"PK\x03\x04rest", "backup.bin"), "zip")
        self.assertEqual(
            detect_backup_format(b"SQLite format 3\x00rest", "database.backup"),
            "sqlite",
        )
        self.assertEqual(detect_backup_format(b'{"users":{},"bots":{}}', "old.dat"), "json")
        self.assertIsNone(detect_backup_format(b"not a backup", "unknown.bin"))

    def _build_bundle(self, root: Path):
        base = root / "panel"
        for name in BACKUP_ROOTS:
            (base / name).mkdir(parents=True, exist_ok=True)
        (base / "sandbox" / "project").mkdir(parents=True, exist_ok=True)
        (base / "storage" / "data" / "panel_settings.json").write_text(
            '{"theme":"dark"}', encoding="utf-8"
        )
        (base / "storage" / "uploads" / "source.zip").write_bytes(b"bot upload")
        (base / "sandbox" / "project" / "main.py").write_text(
            "print('bot')", encoding="utf-8"
        )
        (base / "sandbox" / "project" / "node_modules").mkdir()
        (base / "sandbox" / "project" / "node_modules" / "cache.js").write_text(
            "not backed up", encoding="utf-8"
        )

        store = SQLitePanelStore(
            base / "storage" / "data" / "panel.sqlite3",
            base / "storage" / "data" / "missing.json",
        )
        store.save(
            {
                "users": {"123": {"name": "Owner"}},
                "bots": {"abc123": {"owner": 123, "status": "running"}},
            }
        )
        snapshot = root / "snapshot.sqlite3"
        store.backup_to(snapshot)
        store.close()

        archive_path = root / "full-backup.zip"
        manifest = create_full_backup(base, archive_path, snapshot, "2026-10-12T00:00:00Z")
        return base, archive_path, manifest

    def test_full_bundle_round_trip_and_managed_root_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base, archive_path, manifest = self._build_bundle(root)
            self.assertIn("storage/data/panel.sqlite3", {item["path"] for item in manifest["files"]})
            self.assertNotIn(
                "sandbox/project/node_modules/cache.js",
                {item["path"] for item in manifest["files"]},
            )

            staged = root / "staged"
            extracted_manifest = extract_full_backup(archive_path.read_bytes(), staged)
            self.assertEqual(len(extracted_manifest["files"]), len(manifest["files"]))
            self.assertTrue((staged / "storage/data/panel.sqlite3").is_file())
            self.assertTrue((staged / "sandbox/project/main.py").is_file())
            self.assertFalse((staged / "sandbox/project/node_modules").exists())

            (base / "storage" / "uploads" / "old.txt").write_text(
                "remove me", encoding="utf-8"
            )
            replace_backup_roots(base, staged)
            self.assertTrue((base / "storage" / "uploads" / "source.zip").is_file())
            self.assertFalse((base / "storage" / "uploads" / "old.txt").exists())
            restored = SQLitePanelStore.read_snapshot(
                base / "storage" / "data" / "panel.sqlite3"
            )
            self.assertEqual(restored["users"]["123"]["name"], "Owner")

    def test_rejects_path_traversal_archive_entries(self):
        manifest = {
            "format": "hosting-control-panel-full-backup",
            "version": 1,
            "created_at": "now",
            "roots": list(BACKUP_ROOTS),
            "files": [],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../outside.txt", "unsafe")
            archive.writestr("manifest.json", json.dumps(manifest))

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "staged"
            with self.assertRaises(ValueError):
                extract_full_backup(buffer.getvalue(), destination)
            self.assertFalse(destination.exists())

    def test_rejects_file_that_does_not_match_manifest_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, archive_path, _ = self._build_bundle(root)
            tampered = io.BytesIO()
            with zipfile.ZipFile(archive_path, "r") as source:
                with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as target:
                    for info in source.infolist():
                        payload = source.read(info.filename)
                        if info.filename == "storage/uploads/source.zip":
                            payload = b"evil upload"
                        target.writestr(info.filename, payload)

            with tempfile.TemporaryDirectory() as extract_dir:
                destination = Path(extract_dir) / "staged"
                with self.assertRaises(ValueError):
                    extract_full_backup(tampered.getvalue(), destination)
                self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
