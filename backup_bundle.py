"""Safe, checksummed ZIP bundles for full panel backups and restores."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Tuple


BACKUP_FORMAT = "hosting-control-panel-full-backup"
BACKUP_VERSION = 1
BACKUP_ROOTS = (
    "storage/data",
    "storage/uploads",
    "storage/encfiles",
    "storage/bot_data",
    "storage/tickets",
    "storage/photos",
    "storage/logs",
    "sandbox",
)
MAX_BACKUP_FILES = 20_000
MAX_BACKUP_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_BACKUP_COMPRESSED_BYTES = 20 * 1024 * 1024
SQLITE_FILE_HEADER = b"SQLite format 3\x00"
_SKIP_DIRS = {
    ".deps",
    ".git",
    ".tmp_run",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
}
_SKIP_FILES = {
    "panel.lock",
    "panel.sqlite3-shm",
    "panel.sqlite3-wal",
}


def temporary_directory_near(directory: Path, prefix: str):
    """Create restore staging on the same filesystem as the destination roots."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=directory)


def temporary_file_near(destination: Path, prefix: str, suffix: str):
    """Create a replacement file beside its destination for atomic os.replace."""
    parent = Path(destination).parent
    parent.mkdir(parents=True, exist_ok=True)
    return tempfile.NamedTemporaryFile(
        prefix=prefix,
        suffix=suffix,
        dir=parent,
        delete=False,
    )


def detect_backup_format(data: bytes, filename: str = "") -> str | None:
    """Identify supported backup payloads by content, not just their filename."""
    if data.startswith(b"PK"):
        return "zip"
    if data.startswith(SQLITE_FILE_HEADER):
        return "sqlite"
    if Path(filename).suffix.lower() == ".json" or data.lstrip().startswith(b"{"):
        return "json"
    return None


def _is_allowed_path(name: str) -> bool:
    return any(name.startswith(root + "/") for root in BACKUP_ROOTS)


def _iter_files(base_dir: Path, database_snapshot: Path) -> List[Tuple[str, Path]]:
    files: List[Tuple[str, Path]] = []
    database_name = "storage/data/panel.sqlite3"

    for root_name in BACKUP_ROOTS:
        root = base_dir / root_name
        if not root.is_dir():
            continue
        for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in _SKIP_DIRS
                and not (current_path / name).is_symlink()
            )
            for filename in sorted(filenames):
                source = current_path / filename
                if filename in _SKIP_FILES or source.is_symlink() or not source.is_file():
                    continue
                name = source.relative_to(base_dir).as_posix()
                if name == database_name:
                    continue
                if _is_allowed_path(name):
                    files.append((name, source))

    if not database_snapshot.is_file():
        raise ValueError("The SQLite database snapshot is missing.")
    files.append((database_name, database_snapshot))
    files.sort(key=lambda item: item[0])
    if len(files) > MAX_BACKUP_FILES:
        raise ValueError("The backup contains too many files.")
    return files


def create_full_backup(
    base_dir: Path,
    destination: Path,
    database_snapshot: Path,
    created_at: str,
) -> Dict[str, Any]:
    """Write a complete, checksummed bundle to destination."""
    base_dir = Path(base_dir).resolve()
    destination = Path(destination)
    file_sources = _iter_files(base_dir, Path(database_snapshot))
    manifest_files: List[Dict[str, Any]] = []
    total_size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(
            destination,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for name, source_path in file_sources:
                if not _is_allowed_path(name):
                    raise ValueError("A file is outside the supported backup roots.")
                digest = hashlib.sha256()
                size = 0
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                with source_path.open("rb") as source, archive.open(info, "w") as target:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        total_size += len(chunk)
                        if total_size > MAX_BACKUP_UNCOMPRESSED_BYTES:
                            raise ValueError("The backup exceeds the 1 GiB size limit.")
                        digest.update(chunk)
                        target.write(chunk)
                manifest_files.append(
                    {"path": name, "size": size, "sha256": digest.hexdigest()}
                )

            manifest = {
                "format": BACKUP_FORMAT,
                "version": BACKUP_VERSION,
                "created_at": created_at,
                "roots": list(BACKUP_ROOTS),
                "files": manifest_files,
            }
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
            )
        if destination.stat().st_size > MAX_BACKUP_COMPRESSED_BYTES:
            raise ValueError("The backup ZIP exceeds the 20 MiB compressed-size limit.")
        return manifest
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _safe_member_name(name: str) -> str:
    if not name or "\\" in name or name.startswith("/"):
        raise ValueError("The archive contains an unsafe path.")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("The archive contains an unsafe path.")
    normalized = path.as_posix()
    if normalized != "manifest.json" and not _is_allowed_path(normalized):
        raise ValueError("The archive contains an unsupported path.")
    return normalized


def extract_full_backup(data: bytes, destination: Path) -> Dict[str, Any]:
    """Validate and extract a full bundle without trusting ZIP paths or sizes."""
    if len(data) > MAX_BACKUP_COMPRESSED_BYTES:
        raise ValueError("The backup file exceeds the 20 MiB compressed-size limit.")

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)

    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_BACKUP_FILES + 1:
                raise ValueError("The archive contains too many entries.")

            by_name: Dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                normalized = _safe_member_name(info.filename.rstrip("/"))
                if info.filename.endswith("/") and normalized == "manifest.json":
                    raise ValueError("The archive has an invalid manifest entry.")
                if normalized in by_name:
                    raise ValueError("The archive contains duplicate paths.")
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise ValueError("The archive contains a symbolic link.")
                by_name[normalized] = info

            manifest_info = by_name.get("manifest.json")
            if manifest_info is None or manifest_info.file_size > 2 * 1024 * 1024:
                raise ValueError("The archive manifest is missing or too large.")
            manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != BACKUP_FORMAT
                or manifest.get("version") != BACKUP_VERSION
                or manifest.get("roots") != list(BACKUP_ROOTS)
                or not isinstance(manifest.get("files"), list)
            ):
                raise ValueError("This is not a supported full database backup.")

            expected: Dict[str, Dict[str, Any]] = {}
            total_size = 0
            for item in manifest["files"]:
                if not isinstance(item, dict):
                    raise ValueError("The archive manifest contains an invalid file entry.")
                name = _safe_member_name(str(item.get("path") or ""))
                size = item.get("size")
                digest = item.get("sha256")
                if (
                    not isinstance(size, int)
                    or size < 0
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(ch not in "0123456789abcdef" for ch in digest.lower())
                    or name in expected
                ):
                    raise ValueError("The archive manifest contains invalid file metadata.")
                total_size += size
                if total_size > MAX_BACKUP_UNCOMPRESSED_BYTES:
                    raise ValueError("The archive exceeds the 1 GiB uncompressed-size limit.")
                expected[name] = item

            actual_names = {
                name for name, info in by_name.items()
                if name != "manifest.json" and not info.is_dir()
            }
            if actual_names != set(expected):
                raise ValueError("The archive file list does not match its manifest.")
            if "storage/data/panel.sqlite3" not in expected:
                raise ValueError("The archive does not contain the SQL database.")

            for root_name in BACKUP_ROOTS:
                (destination / root_name).mkdir(parents=True, exist_ok=True)

            extracted_total = 0
            for name, item in expected.items():
                info = by_name[name]
                if info.file_size != item["size"]:
                    raise ValueError("A file size does not match the manifest.")
                target = destination.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info, "r") as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        extracted_total += len(chunk)
                        if extracted_total > MAX_BACKUP_UNCOMPRESSED_BYTES:
                            raise ValueError("The archive exceeds the size limit.")
                        digest.update(chunk)
                        output.write(chunk)
                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise ValueError(f"Backup integrity check failed for {name}.")
            return manifest
    except (zipfile.BadZipFile, RuntimeError, OSError, json.JSONDecodeError) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise ValueError("The backup ZIP is damaged or could not be read.") from exc
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def replace_backup_roots(base_dir: Path, staged_root: Path) -> None:
    """Replace only managed state roots; roll back all roots if any move fails."""
    base_dir = Path(base_dir).resolve()
    staged_root = Path(staged_root).resolve()
    rollback_root = staged_root / ".rollback"
    rollback_root.mkdir(parents=True, exist_ok=True)
    moved_old: List[str] = []
    installed_new: List[str] = []

    try:
        for root_name in BACKUP_ROOTS:
            source = staged_root / root_name
            if not source.is_dir():
                raise ValueError(f"The backup is missing the {root_name} data root.")

        for root_name in BACKUP_ROOTS:
            target = base_dir / root_name
            old = rollback_root / root_name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                old.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, old)
                moved_old.append(root_name)

        for root_name in BACKUP_ROOTS:
            target = base_dir / root_name
            source = staged_root / root_name
            os.replace(source, target)
            installed_new.append(root_name)
    except Exception:
        for root_name in reversed(installed_new):
            target = base_dir / root_name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink(missing_ok=True)
        for root_name in reversed(moved_old):
            old = rollback_root / root_name
            target = base_dir / root_name
            if old.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, target)
        raise
