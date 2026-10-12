"""PostgreSQL persistence for the hosting panel's mapping-shaped data."""

from __future__ import annotations

import copy
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from .sqlite_store import SQLitePanelStore
except ImportError:
    from sqlite_store import SQLitePanelStore


class PostgresPanelStore:
    """Postgres adapter with the same load/save API as SQLitePanelStore.

    PostgreSQL holds the live panel records. Portable owner exports remain
    SQLite snapshots so existing full-backup and migration flows keep working.
    """

    SCHEMA_VERSION = 1

    def __init__(self, database_url: str, path: Path, legacy_json_path: Path) -> None:
        if not database_url.strip():
            raise ValueError("DATABASE_URL is required for PostgreSQL storage.")
        self.database_url = database_url.strip()
        # Retain a local path for legacy JSON archival and portable backups.
        self.path = Path(path)
        self.legacy_json_path = Path(legacy_json_path)
        self._lock = threading.RLock()
        self._conn: Any = None
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_revision: Optional[str] = None

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def _connect_locked(self) -> Any:
        if self._conn is not None:
            return self._conn

        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL storage requires psycopg; install requirements.txt."
            ) from exc

        conn = psycopg.connect(
            self.database_url,
            connect_timeout=10,
            autocommit=True,
            row_factory=dict_row,
        )
        self._conn = conn
        try:
            with conn.transaction():
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS panel_collections (
                        collection TEXT PRIMARY KEY,
                        kind TEXT NOT NULL CHECK (kind IN ('map', 'list', 'scalar'))
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS panel_map_items (
                        collection TEXT NOT NULL,
                        item_key TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (collection, item_key),
                        FOREIGN KEY (collection) REFERENCES panel_collections(collection)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS panel_list_items (
                        collection TEXT NOT NULL,
                        position BIGINT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (collection, position),
                        FOREIGN KEY (collection) REFERENCES panel_collections(collection)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS panel_scalar_values (
                        collection TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY (collection) REFERENCES panel_collections(collection)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS panel_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )

                version_row = conn.execute(
                    "SELECT value FROM panel_meta WHERE key='schema_version'"
                ).fetchone()
                version = int(version_row["value"]) if version_row else 0
                if version > self.SCHEMA_VERSION:
                    raise RuntimeError(
                        f"PostgreSQL schema version {version} is newer than supported "
                        f"version {self.SCHEMA_VERSION}."
                    )
                conn.execute(
                    """
                    INSERT INTO panel_meta(key, value) VALUES ('schema_version', %s)
                    ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
                    """,
                    (str(self.SCHEMA_VERSION),),
                )
                conn.execute(
                    """
                    INSERT INTO panel_meta(key, value) VALUES ('data_revision', '0')
                    ON CONFLICT(key) DO NOTHING
                    """
                )

            self._import_legacy_json_locked(conn)
            return conn
        except Exception:
            conn.close()
            self._conn = None
            raise

    def _replace_data_locked(self, conn: Any, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("Panel database must be a JSON object.")

        with conn.transaction():
            conn.execute("DELETE FROM panel_map_items")
            conn.execute("DELETE FROM panel_list_items")
            conn.execute("DELETE FROM panel_scalar_values")
            conn.execute("DELETE FROM panel_collections")

            for collection, value in data.items():
                name = str(collection)
                if isinstance(value, dict):
                    conn.execute(
                        "INSERT INTO panel_collections(collection, kind) VALUES (%s, 'map')",
                        (name,),
                    )
                    rows = [
                        (name, str(key), self._json(item))
                        for key, item in value.items()
                    ]
                    if rows:
                        with conn.cursor() as cursor:
                            cursor.executemany(
                                "INSERT INTO panel_map_items(collection, item_key, payload_json) "
                                "VALUES (%s, %s, %s)",
                                rows,
                            )
                elif isinstance(value, list):
                    conn.execute(
                        "INSERT INTO panel_collections(collection, kind) VALUES (%s, 'list')",
                        (name,),
                    )
                    rows = [
                        (name, index, self._json(item))
                        for index, item in enumerate(value)
                    ]
                    if rows:
                        with conn.cursor() as cursor:
                            cursor.executemany(
                                "INSERT INTO panel_list_items(collection, position, payload_json) "
                                "VALUES (%s, %s, %s)",
                                rows,
                            )
                else:
                    conn.execute(
                        "INSERT INTO panel_collections(collection, kind) VALUES (%s, 'scalar')",
                        (name,),
                    )
                    conn.execute(
                        "INSERT INTO panel_scalar_values(collection, payload_json) "
                        "VALUES (%s, %s)",
                        (name, self._json(value)),
                    )

            revision_row = conn.execute(
                "SELECT value FROM panel_meta WHERE key='data_revision'"
            ).fetchone()
            revision = int(revision_row["value"]) if revision_row else 0
            conn.execute(
                """
                INSERT INTO panel_meta(key, value) VALUES ('data_revision', %s)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
                """,
                (str(revision + 1),),
            )

    def _import_legacy_json_locked(self, conn: Any) -> None:
        marker = conn.execute(
            "SELECT value FROM panel_meta WHERE key='legacy_json_migrated'"
        ).fetchone()
        if marker:
            return

        collection_count = int(
            conn.execute("SELECT COUNT(*) AS count FROM panel_collections").fetchone()["count"]
        )
        if self.legacy_json_path.exists():
            try:
                legacy = json.loads(
                    self.legacy_json_path.read_text(encoding="utf-8") or "{}"
                )
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

            try:
                backup_dir = self.path.parent.parent / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_path = backup_dir / f"panel_db_pre_postgres_{time.time_ns()}.json"
                shutil.copy2(self.legacy_json_path, backup_path)
                self.legacy_json_path.unlink()
            except OSError:
                pass

        with conn.transaction():
            conn.execute(
                """
                INSERT INTO panel_meta(key, value)
                VALUES ('legacy_json_migrated', '1')
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
                """
            )

    def _read_data_locked(self, conn: Any) -> Dict[str, Any]:
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
        ).fetchall():
            data[row["collection"]][row["item_key"]] = json.loads(row["payload_json"])

        for row in conn.execute(
            "SELECT collection, position, payload_json FROM panel_list_items "
            "ORDER BY collection, position"
        ).fetchall():
            data[row["collection"]].append(json.loads(row["payload_json"]))

        for row in conn.execute(
            "SELECT collection, payload_json FROM panel_scalar_values"
        ).fetchall():
            data[row["collection"]] = json.loads(row["payload_json"])
        return data

    def _revision_locked(self, conn: Any) -> str:
        row = conn.execute(
            "SELECT value FROM panel_meta WHERE key='data_revision'"
        ).fetchone()
        return str(row["value"]) if row else "0"

    def load_readonly(self) -> Dict[str, Any]:
        """Return the cached shared object; callers must not mutate it."""
        with self._lock:
            conn = self._connect_locked()
            with conn.transaction():
                conn.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                revision = self._revision_locked(conn)
                if self._cache is None or revision != self._cache_revision:
                    self._cache = self._read_data_locked(conn)
                    self._cache_revision = revision
            return self._cache

    def load(self) -> Dict[str, Any]:
        """Return a defensive copy suitable for mutation."""
        return copy.deepcopy(self.load_readonly())

    def save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            conn = self._connect_locked()
            self._replace_data_locked(conn, data)
            self._cache = copy.deepcopy(data)
            self._cache_revision = self._revision_locked(conn)

    def backup_to(self, destination: Path) -> Path:
        """Write a portable SQLite snapshot from the current Postgres records."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        snapshot = SQLitePanelStore(
            destination,
            destination.with_name("postgres-snapshot-no-legacy.json"),
        )
        try:
            snapshot.save(self.load())
        finally:
            snapshot.close()
        SQLitePanelStore.validate_snapshot(destination)
        return destination

    def database_size_bytes(self) -> int:
        try:
            with self._lock:
                conn = self._connect_locked()
                row = conn.execute(
                    "SELECT pg_database_size(current_database()) AS size_bytes"
                ).fetchone()
                return int(row["size_bytes"]) if row else 0
        except Exception:
            return 0

    def is_writable(self) -> bool:
        try:
            with self._lock:
                conn = self._connect_locked()
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            self._cache = None
            self._cache_revision = None

    def clear_cache(self) -> None:
        with self._lock:
            self._cache = None
            self._cache_revision = None
