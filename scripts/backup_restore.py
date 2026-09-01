from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.backup import (
    backup_local_objects,
    restore_local_objects,
    safe_relative_path,
    sha256_file,
)
from backend.app.retrieval.providers import load_dotenv_if_available


MANIFEST_NAME = "manifest.json"
BACKUP_SCHEMA_VERSION = 1


def main() -> int:
    load_dotenv_if_available()
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "backup":
            manifest = create_backup(args)
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        else:
            manifest = restore_backup(args)
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(f"backup/restore failed: {type(error).__name__}: {error}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backup and restore knowledge data")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    add_storage_arguments(backup)
    backup.add_argument("--skip-database", action="store_true")
    backup.add_argument("--skip-objects", action="store_true")

    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup-dir", type=Path, required=True)
    restore.add_argument("--confirm", action="store_true")
    add_storage_arguments(restore)
    restore.add_argument("--skip-database", action="store_true")
    restore.add_argument("--skip-objects", action="store_true")
    return parser


def add_storage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-url",
        default=os.getenv("KNOWLEDGE_DATABASE_URL", ""),
        help="PostgreSQL URL; defaults to KNOWLEDGE_DATABASE_URL",
    )
    parser.add_argument(
        "--storage-mode",
        choices=("local", "s3", "minio"),
        default=os.getenv("KNOWLEDGE_OBJECT_STORAGE", "local"),
    )
    parser.add_argument(
        "--storage-root",
        type=Path,
        default=Path(os.getenv("KNOWLEDGE_STORAGE_DIR", ".codex-tmp/knowledge-documents")),
    )


def create_backup(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = None
    objects = None
    if not args.skip_database:
        database = backup_database(args.database_url, output / "database.dump")
    if not args.skip_objects:
        objects = backup_objects(args, output / "objects")
    manifest = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": database,
        "objects": objects,
    }
    (output / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def restore_backup(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm:
        raise ValueError("Restore is destructive; pass --confirm")
    backup_dir = args.backup_dir.resolve()
    manifest_path = backup_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise ValueError("Unsupported backup manifest version")
    restored = {"database": None, "objects": None}
    if not args.skip_database and manifest.get("database"):
        database_source = backup_child(backup_dir, manifest["database"]["file"])
        restored["database"] = restore_database(
            args.database_url,
            database_source,
            expected_sha256=str(manifest["database"].get("sha256", "")),
        )
    if not args.skip_objects and manifest.get("objects"):
        objects_root = backup_child(backup_dir, manifest["objects"]["root"])
        restored["objects"] = restore_objects(
            args,
            objects_root,
            manifest["objects"],
        )
    return restored


def backup_database(database_url: str, destination: Path) -> dict[str, Any]:
    if not database_url:
        raise ValueError("--database-url or KNOWLEDGE_DATABASE_URL is required")
    pg_dump = require_command("pg_dump")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [pg_dump, "--format=custom", "--file", str(destination), "--dbname", database_url]
    )
    return {
        "format": "custom",
        "file": destination.name,
        "size": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }


def restore_database(
    database_url: str,
    source: Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    if not database_url:
        raise ValueError("--database-url or KNOWLEDGE_DATABASE_URL is required")
    if not source.is_file():
        raise FileNotFoundError(source)
    actual_sha256 = sha256_file(source)
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise ValueError(f"Database backup checksum mismatch: {source}")
    pg_restore = require_command("pg_restore")
    run_command(
        [
            pg_restore,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--dbname",
            database_url,
            str(source),
        ]
    )
    return {"status": "restored", "file": source.name, "sha256": actual_sha256}


def backup_objects(args: argparse.Namespace, destination: Path) -> dict[str, Any]:
    if args.storage_mode == "local":
        records = backup_local_objects(args.storage_root, destination)
        return {"mode": "local", "root": destination.name, "items": records}
    return backup_s3_objects(args, destination)


def restore_objects(
    args: argparse.Namespace,
    source_root: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if metadata.get("mode") == "local":
        restored = restore_local_objects(
            source_root,
            args.storage_root,
            list(metadata.get("items", [])),
        )
        return {"status": "restored", "mode": "local", "count": restored}
    return restore_s3_objects(args, source_root, metadata)


def backup_s3_objects(args: argparse.Namespace, destination: Path) -> dict[str, Any]:
    client, bucket, prefix = s3_client()
    destination.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    prefix_filter = f"{prefix}/" if prefix else ""
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix_filter):
        for item in page.get("Contents", []):
            key = str(item["Key"])
            relative = key[len(prefix_filter):] if prefix_filter else key
            if not relative:
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            response = client.get_object(Bucket=bucket, Key=key)
            body = response["Body"]
            try:
                with target.open("wb") as stream:
                    shutil.copyfileobj(body, stream)
            finally:
                body.close()
            items.append(
                {
                    "key": key,
                    "relative_path": Path(relative).as_posix(),
                    "size": target.stat().st_size,
                    "sha256": sha256_file(target),
                }
            )
    return {"mode": args.storage_mode, "root": destination.name, "bucket": bucket, "items": items}


def restore_s3_objects(
    args: argparse.Namespace,
    source_root: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    client, bucket, _ = s3_client()
    restored = 0
    for item in metadata.get("items", []):
        relative = safe_relative_path(str(item["relative_path"]))
        source = (source_root / relative).resolve()
        if (
            not source.is_relative_to(source_root.resolve())
            or not source.is_file()
            or sha256_file(source) != str(item["sha256"])
        ):
            raise ValueError(f"Backup checksum mismatch: {source}")
        client.upload_file(str(source), bucket, str(item["key"]))
        restored += 1
    return {"status": "restored", "mode": metadata.get("mode"), "count": restored}


def s3_client():
    try:
        import boto3
    except ImportError as error:
        raise RuntimeError("boto3 is required for S3/MinIO backup") from error
    bucket = os.getenv("KNOWLEDGE_S3_BUCKET", "").strip()
    if not bucket:
        raise ValueError("KNOWLEDGE_S3_BUCKET is required for S3/MinIO backup")
    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("KNOWLEDGE_S3_ENDPOINT") or None,
        region_name=os.getenv("KNOWLEDGE_S3_REGION", "us-east-1"),
        aws_access_key_id=os.getenv("KNOWLEDGE_S3_ACCESS_KEY") or None,
        aws_secret_access_key=os.getenv("KNOWLEDGE_S3_SECRET_KEY") or None,
    )
    return client, bucket, os.getenv("KNOWLEDGE_S3_PREFIX", "documents").strip("/")


def require_command(name: str) -> str:
    command = which(name)
    if command is None:
        raise RuntimeError(f"{name} is not installed or not on PATH")
    return command


def backup_child(root: Path, value: object) -> Path:
    relative = safe_relative_path(str(value))
    child = (root / relative).resolve()
    if not child.is_relative_to(root.resolve()):
        raise ValueError(f"Backup manifest path escapes backup root: {value}")
    return child


def run_command(command: list[str]) -> None:
    subprocess.run(command, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
