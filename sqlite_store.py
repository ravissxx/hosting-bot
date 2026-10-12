"""SQLite persistence for the hosting panel's existing mapping-shaped data."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


class SQLitePanelStore:
    """Store panel collections as SQL rows without changing callers' data model.

    Mapping collections (users, bots, admins, etc.) are stored one row per
    record. Lists are stored one row per ordered item, and uncommon scalar
    collections are stored separately. JSON is used only for each record's
    nested value, not as the database file format.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path, legacy_json_path: Path) -> None:
        self.path = Path(path)
        self.legacy_json_path = Path(legacy_json_path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_data_version: Optional[int] = None

    def _connect_locked(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn

        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS panel_collections (
                    collection TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('map', 'list', 'scalar'))
                );
                CREATE TABLE IF NOT EXISTS panel_map_items (
                    collection TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (collection, item_key),
                    FOREIGN KEY (collection) REFERENCES panel_collections(collection)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS panel_list_items (
                    collection TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (collection, position),
                    FOREIGN KEY (collection) REFERENCES panel_collections(collection)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS panel_scalar_values (
                    collection TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (collection) REFERENCES panel_collections(collection)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS panel_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than supported "
                    f"version {self.SCHEMA_VERSION}."
                )
            if version == 0:
                conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            self._conn = conn
            self._import_legacy_json_locked(conn)
            return conn
        except Exception:
            conn.close()
            self._conn = None
            raise

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def _replace_data_locked(self, conn: sqlite3.Connection, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("Panel database must be a JSON object.")

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM panel_map_items")
            conn.execute("DELETE FROM panel_list_items")
            conn.execute("DELETE FROM panel_scalar_values")
            conn.execute("DELETE FROM panel_collections")

            for collection, value in data.items():
                name = str(collection)
                if isinstance(value, dict):
                    conn.execute(
                        "INSERT INTO panel_collections(collection, kind) VALUES (?, 'map')",
                        (name,),
                    )
                    conn.executemany(
                        "INSERT INTO panel_map_items(collection, item_key, payload_json) "
                        "VALUES (?, ?, ?)",
                        [
                            (name, str(key), self._json(item))
                            for key, item in value.items()
                        ],
                    )
                elif isinstance(value, list):
                    conn.execute(
                        "INSERT INTO panel_collections(collection, kind) VALUES (?, 'list')",
                        (name,),
                    )
                    conn.executemany(
                        "INSERT INTO panel_list_items(collection, position, payload_json) "
                        "VALUES (?, ?, ?)",
                        [
                            (name, index, self._json(item))
                            for index, item in enumerate(value)
                        ],
                    )
                else:
                    conn.execute(
                        "INSERT INTO panel_collections(collection, kind) VALUES (?, 'scalar')",
                        (name,),
                    )
                    conn.execute(
                        "INSERT INTO panel_scalar_values(collection, payload_json) "
                        "VALUES (?, ?)",
                        (name, self._json(value)),
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _import_legacy_json_locked(self, conn: sqlite3.Connection) -> None:
        marker = conn.execute(
            "SELECT value FROM panel_meta WHERE key='legacy_json_migrated'"
        ).fetchone()
        if marker:
            return

        collection_count = int(
            conn.execute("SELECT COUNT(*) FROM panel_collections").fetchone()[0]
        )
        if self.legacy_json_path.exists():
            try:
                legacy = json.loads(self.legacy_json_path.read_text(encoding="utf-8") or "{}")
            except Exception as exc:
                raise RuntimeError(
                    "The existing JSON database could not be read; it was left untouched."
                ) from exc
            if (
                not isinstance(legacy, dict)
                or not isinstance(legacy.get("users"), dict)
                or not isinstance(legacy.get("bots"), dict)
            ):
                raise RuntimeError(
                    "The existing JSON database has an unexpected structure; "
                    "it was left untouched."
                )

            if collection_count == 0:
                self._replace_data_locked(conn, legacy)

            # Keep a recoverable copy before removing the old active JSON file.
            try:
                backup_dir = self.path.parent.parent / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_path = backup_dir / f"panel_db_pre_sqlite_{time.time_ns()}.json"
                shutil.copy2(self.legacy_json_path, backup_path)
                self.legacy_json_path.unlink()
            except OSError:
                # The SQL transaction has already preserved the data; if the
                # optional archival copy fails, leave the original JSON in place.
                pass

        conn.execute(
            "INSERT OR REPLACE INTO panel_meta(key, value) VALUES "
            "('legacy_json_migrated', '1')"
        )

    def _read_data_locked(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        rows = conn.execute(
            "SELECT collection, kind FROM panel_collections ORDER BY collection"
        ).fetchall()
        for row in rows:
            kind = row["kind"]
            if kind == "map":
                data[row["collection"]] = {}
            elif kind == "list":
                data[row["collection"]] = []
            elif kind == "scalar":
                data[row["collection"]] = None
            else:
                raise RuntimeError(f"Unknown SQL collection type: {kind}")

        for row in conn.execute(
            "SELECT collection, item_key, payload_json FROM panel_map_items "
            "ORDER BY collection, item_key"
        ):
            data[row["collection"]][row["item_key"]] = json.loads(row["payload_json"])

        for row in conn.execute(
            "SELECT collection, position, payload_json FROM panel_list_items "
            "ORDER BY collection, position"
        ):
            data[row["collection"]].append(json.loads(row["payload_json"]))

        for row in conn.execute(
            "SELECT collection, payload_json FROM panel_scalar_values"
        ):
            data[row["collection"]] = json.loads(row["payload_json"])
        return data

    def load_readonly(self) -> Dict[str, Any]:
        """Return the cached shared object; callers must not mutate it."""
        with self._lock:
            conn = self._connect_locked()
            data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
            if self._cache is None or data_version != self._cache_data_version:
                self._cache = self._read_data_locked(conn)
                self._cache_data_version = data_version
            return self._cache

    def load(self) -> Dict[str, Any]:
        """Return a defensive copy suitable for mutation."""
        return copy.deepcopy(self.load_readonly())

    def save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            conn = self._connect_locked()
            self._replace_data_locked(conn, data)
            self._cache = copy.deepcopy(data)
            self._cache_data_version = int(
                conn.execute("PRAGMA data_version").fetchone()[0]
            )

    def backup_to(self, destination: Path) -> Path:
        """Create a consistent standalone SQLite snapshot, including WAL data."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.unlink(missing_ok=True)
        try:
            with self._lock:
                source = self._connect_locked()
                target = sqlite3.connect(str(temporary), timeout=30)
                try:
                    source.backup(target)
                    integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
                    if integrity != "ok":
                        raise RuntimeError("Could not create a consistent SQLite backup.")
                finally:
                    target.close()
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @classmethod
    def validate_snapshot(cls, path: Path) -> None:
        """Reject corrupt, incomplete, or incompatible SQLite backup files."""
        path = Path(path)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("SQLite backup file is missing or empty.")
        uri = path.resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=10)
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                required = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if integrity != "ok":
                    raise ValueError("SQLite backup failed its integrity check.")
                if version != cls.SCHEMA_VERSION:
                    raise ValueError("Unsupported SQLite backup schema version.")
                if not {
                    "panel_collections",
                    "panel_map_items",
                    "panel_list_items",
                    "panel_scalar_values",
                    "panel_meta",
                }.issubset(required):
                    raise ValueError("SQLite backup is missing required tables.")
                for row in conn.execute(
                    "SELECT kind FROM panel_collections"
                ):
                    if row[0] not in {"map", "list", "scalar"}:
                        raise ValueError("SQLite backup contains an invalid collection type.")
                for table in (
                    "panel_map_items",
                    "panel_list_items",
                    "panel_scalar_values",
                ):
                    for row in conn.execute(f"SELECT payload_json FROM {table}"):
                        json.loads(row[0])
                required_maps = {
                    row[0]: row[1]
                    for row in conn.execute(
                        "SELECT collection, kind FROM panel_collections "
                        "WHERE collection IN ('users', 'bots')"
                    )
                }
                if required_maps != {"users": "map", "bots": "map"}:
                    raise ValueError("SQLite backup does not contain users and bots maps.")
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise ValueError("SQLite backup is not a valid panel database.") from exc

    @classmethod
    def read_snapshot(cls, path: Path) -> Dict[str, Any]:
        """Read and validate a standalone SQL backup without changing it."""
        cls.validate_snapshot(path)
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            reader = cls(Path(path), Path(path).with_name("missing-legacy.json"))
            return reader._read_data_locked(conn)
        finally:
            conn.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            self._cache = None
            self._cache_data_version = None

    def clear_cache(self) -> None:
        with self._lock:
            self._cache = None
            self._cache_data_version = None
