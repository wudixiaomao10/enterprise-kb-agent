from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any


def backup_local_objects(source_root: Path, destination_root: Path) -> list[dict[str, Any]]:
    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    if destination_root == source_root or destination_root.is_relative_to(source_root):
        raise ValueError("Backup destination cannot be inside the object storage root")
    destination_root.mkdir(parents=True, exist_ok=True)
    if not source_root.exists():
        return []

    records: list[dict[str, Any]] = []
    for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
        relative = source.relative_to(source_root)
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        records.append(
            {
                "relative_path": relative.as_posix(),
                "size": source.stat().st_size,
                "sha256": sha256_file(destination),
            }
        )
    return records


def restore_local_objects(
    source_root: Path,
    destination_root: Path,
    records: list[dict[str, Any]],
) -> int:
    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    restored = 0
    for record in records:
        relative = safe_relative_path(str(record["relative_path"]))
        source = (source_root / relative).resolve()
        destination = (destination_root / relative).resolve()
        if not source.is_relative_to(source_root) or not destination.is_relative_to(destination_root):
            raise ValueError("Backup object path escapes its configured root")
        if not source.is_file():
            raise FileNotFoundError(source)
        if sha256_file(source) != str(record["sha256"]):
            raise ValueError(f"Backup checksum mismatch: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.restore")
        shutil.copy2(source, temporary)
        if sha256_file(temporary) != str(record["sha256"]):
            temporary.unlink(missing_ok=True)
            raise ValueError(f"Restored object checksum mismatch: {relative}")
        temporary.replace(destination)
        restored += 1
    return restored


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative_path(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe backup object path: {value}")
    return relative


__all__ = [
    "backup_local_objects",
    "restore_local_objects",
    "safe_relative_path",
    "sha256_file",
]
