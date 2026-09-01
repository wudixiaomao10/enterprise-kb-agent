from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.identity.directory import (
    DirectoryMembership,
    DirectorySyncSnapshot,
    DirectoryUnit,
    DirectoryUser,
    PostgresIdentityDirectory,
)
from backend.app.retrieval.providers import load_dotenv_if_available
from backend.app.storage.object_store import create_object_storage


def cleanup_directory_source(dsn: str, source: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection:
        user_ids = [
            row[0]
            for row in connection.execute(
                "SELECT user_id FROM directory_users WHERE source = %s", (source,)
            ).fetchall()
        ]
        if user_ids:
            connection.execute(
                "DELETE FROM directory_user_departments WHERE user_id = ANY(%s)",
                (user_ids,),
            )
            connection.execute(
                "DELETE FROM directory_user_roles WHERE user_id = ANY(%s)",
                (user_ids,),
            )
        connection.execute("DELETE FROM directory_users WHERE source = %s", (source,))
        connection.execute(
            "DELETE FROM directory_departments WHERE source = %s", (source,)
        )
        connection.execute("DELETE FROM directory_roles WHERE source = %s", (source,))
        connection.execute(
            "DELETE FROM directory_sync_runs WHERE source = %s", (source,)
        )


def main() -> None:
    load_dotenv_if_available()
    storage = create_object_storage()
    uri = storage.put("storage-smoke.txt", b"s3 object storage smoke test")
    try:
        if not uri.startswith("s3://"):
            raise RuntimeError(f"Production storage is not S3: {uri}")
        if storage.read(uri) != b"s3 object storage smoke test":
            raise RuntimeError("S3 read did not match the uploaded bytes")
        with storage.materialize(uri) as path:
            if path.read_bytes() != b"s3 object storage smoke test":
                raise RuntimeError("Materialized S3 object did not match")
    finally:
        storage.delete(uri)

    dsn = os.environ["KNOWLEDGE_DATABASE_URL"]
    source = f"smoke-directory-{uuid.uuid4().hex[:8]}"
    subject = f"smoke-subject-{uuid.uuid4().hex[:8]}"
    directory = PostgresIdentityDirectory(dsn)
    try:
        result = directory.sync(
            DirectorySyncSnapshot(
                source=source,
                users=(
                    DirectoryUser(
                        external_id="user-external",
                        user_id=f"user-{uuid.uuid4().hex[:8]}",
                        subject=subject,
                        issuer="smoke-issuer",
                    ),
                ),
                departments=(
                    DirectoryUnit("department-external", "smoke-department", "Smoke"),
                ),
                user_departments=(
                    DirectoryMembership("user-external", "department-external"),
                ),
            )
        )
        identity = directory.resolve_user("smoke-issuer", subject)
        if identity is None or identity.department_ids != ("smoke-department",):
            raise RuntimeError(f"Directory identity resolution failed: {identity}")
        directory.sync(DirectorySyncSnapshot(source=source, users=()))
        if directory.resolve_user("smoke-issuer", subject) is not None:
            raise RuntimeError("Missing directory user was not deactivated")
    finally:
        cleanup_directory_source(dsn, source)

    print(
        {
            "status": "ok",
            "object_storage": storage.name,
            "storage_uri_scheme": "s3",
            "directory": directory.name,
            "sync_run": result.run_id,
            "deactivation_verified": True,
            "artifacts_cleaned": True,
        }
    )


if __name__ == "__main__":
    main()
