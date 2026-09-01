from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.retrieval.providers import load_dotenv_if_available
from backend.app.storage.object_store import create_object_storage


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate local document versions to configured S3 storage"
    )
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument(
        "--delete-local-after-verify",
        action="store_true",
        help="Delete local source only after S3 upload, read-back, and DB update",
    )
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("Refusing to migrate without --confirm")

    load_dotenv_if_available()
    storage = create_object_storage()
    primary = getattr(storage, "primary", storage)
    if primary.name != "s3":
        raise SystemExit("Configured primary object storage is not S3")

    import psycopg

    dsn = os.environ["KNOWLEDGE_DATABASE_URL"]
    with psycopg.connect(dsn) as connection:
        versions = connection.execute(
            """
            SELECT version_id, storage_uri, content_hash
            FROM document_versions
            WHERE storage_uri NOT LIKE 's3://%'
            ORDER BY created_at
            """
        ).fetchall()

    migrated = 0
    retained_local = 0
    for version_id, old_uri, expected_hash in versions:
        raw_bytes = storage.read(old_uri)
        actual_hash = hashlib.sha256(raw_bytes).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Source hash mismatch for {version_id}: {actual_hash} != {expected_hash}"
            )
        new_uri = primary.put(Path(old_uri).name, raw_bytes)
        try:
            uploaded_hash = hashlib.sha256(primary.read(new_uri)).hexdigest()
            if uploaded_hash != expected_hash:
                raise RuntimeError(f"S3 read-back hash mismatch for {version_id}")
            with psycopg.connect(dsn) as connection:
                updated = connection.execute(
                    """
                    UPDATE document_versions
                    SET storage_uri = %s
                    WHERE version_id = %s AND storage_uri = %s
                    """,
                    (new_uri, version_id, old_uri),
                ).rowcount
                if updated != 1:
                    raise RuntimeError(f"Concurrent storage URI change for {version_id}")
        except Exception:
            primary.delete(new_uri)
            raise
        migrated += 1
        if args.delete_local_after_verify:
            storage.local.delete(old_uri)
        else:
            retained_local += 1

    print(
        {
            "status": "ok",
            "migrated_versions": migrated,
            "retained_local_backups": retained_local,
            "deleted_local_sources": migrated - retained_local,
        }
    )


if __name__ == "__main__":
    main()
