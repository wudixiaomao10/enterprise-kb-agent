from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from backend.app.models.knowledge import utc_now


DLQ_STATUSES = {"pending", "replayed", "discarded"}


@dataclass
class DeadLetterEntry:
    dlq_id: str
    job_type: str
    job_id: str
    payload: dict[str, Any]
    error_type: str
    error_message: str | None
    attempts: int
    status: str = "pending"
    created_at: datetime = field(default_factory=utc_now)
    replayed_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)


class DeadLetterQueue(Protocol):
    def enqueue(
        self,
        *,
        job_type: str,
        job_id: str,
        payload: dict[str, Any],
        error_type: str,
        error_message: str | None,
        attempts: int,
    ) -> DeadLetterEntry: ...

    def get(self, dlq_id: str) -> DeadLetterEntry | None: ...

    def list(self, *, status: str = "pending", limit: int = 100) -> list[DeadLetterEntry]: ...

    def mark_replayed(self, dlq_id: str) -> DeadLetterEntry | None: ...

    def discard(self, dlq_id: str) -> DeadLetterEntry | None: ...


class InMemoryDeadLetterQueue:
    def __init__(self) -> None:
        self.entries: dict[str, DeadLetterEntry] = {}
        self._lock = threading.Lock()

    def enqueue(
        self,
        *,
        job_type: str,
        job_id: str,
        payload: dict[str, Any],
        error_type: str,
        error_message: str | None,
        attempts: int,
    ) -> DeadLetterEntry:
        validate_job_type(job_type)
        now = utc_now()
        with self._lock:
            existing = next(
                (
                    item
                    for item in self.entries.values()
                    if item.job_type == job_type
                    and item.job_id == job_id
                    and item.status == "pending"
                ),
                None,
            )
            if existing is not None:
                updated = replace(
                    existing,
                    payload=dict(payload),
                    error_type=error_type,
                    error_message=truncate_error(error_message),
                    attempts=max(0, attempts),
                    updated_at=now,
                )
                self.entries[existing.dlq_id] = updated
                return updated
            entry = DeadLetterEntry(
                dlq_id=f"dlq_{uuid.uuid4().hex[:16]}",
                job_type=job_type,
                job_id=job_id,
                payload=dict(payload),
                error_type=error_type,
                error_message=truncate_error(error_message),
                attempts=max(0, attempts),
                created_at=now,
                updated_at=now,
            )
            self.entries[entry.dlq_id] = entry
            return entry

    def get(self, dlq_id: str) -> DeadLetterEntry | None:
        with self._lock:
            return self.entries.get(dlq_id)

    def list(self, *, status: str = "pending", limit: int = 100) -> list[DeadLetterEntry]:
        validate_status(status)
        with self._lock:
            items = [item for item in self.entries.values() if item.status == status]
            return sorted(items, key=lambda item: item.created_at, reverse=True)[
                : max(1, min(limit, 500))
            ]

    def mark_replayed(self, dlq_id: str) -> DeadLetterEntry | None:
        return self._mark(dlq_id, "replayed", utc_now())

    def discard(self, dlq_id: str) -> DeadLetterEntry | None:
        return self._mark(dlq_id, "discarded", None)

    def _mark(
        self,
        dlq_id: str,
        status: str,
        replayed_at: datetime | None,
    ) -> DeadLetterEntry | None:
        with self._lock:
            entry = self.entries.get(dlq_id)
            if entry is None or entry.status != "pending":
                return entry
            updated = replace(
                entry,
                status=status,
                replayed_at=replayed_at,
                updated_at=utc_now(),
            )
            self.entries[dlq_id] = updated
            return updated


class PostgresDeadLetterQueue:
    def __init__(self, dsn: str, initialize_schema: bool = False) -> None:
        self.dsn = dsn
        if initialize_schema:
            self._init_schema()

    def enqueue(
        self,
        *,
        job_type: str,
        job_id: str,
        payload: dict[str, Any],
        error_type: str,
        error_message: str | None,
        attempts: int,
    ) -> DeadLetterEntry:
        validate_job_type(job_type)
        with self._connect() as connection:
            entry = connection.execute(
                """
                INSERT INTO job_dead_letters (
                    dlq_id, job_type, job_id, payload_json, error_type,
                    error_message, attempts, status, created_at, updated_at
                ) VALUES (
                    %(dlq_id)s, %(job_type)s, %(job_id)s, %(payload)s::jsonb,
                    %(error_type)s, %(error_message)s, %(attempts)s,
                    'pending', now(), now()
                )
                ON CONFLICT (job_type, job_id) WHERE status = 'pending'
                DO UPDATE SET
                    payload_json = EXCLUDED.payload_json,
                    error_type = EXCLUDED.error_type,
                    error_message = EXCLUDED.error_message,
                    attempts = EXCLUDED.attempts,
                    updated_at = now()
                RETURNING *
                """,
                {
                    "dlq_id": f"dlq_{uuid.uuid4().hex[:16]}",
                    "job_type": job_type,
                    "job_id": job_id,
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "error_type": error_type,
                    "error_message": truncate_error(error_message),
                    "attempts": max(0, attempts),
                },
            ).fetchone()
        return entry_from_row(entry)

    def get(self, dlq_id: str) -> DeadLetterEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM job_dead_letters WHERE dlq_id = %s",
                (dlq_id,),
            ).fetchone()
        return entry_from_row(row) if row else None

    def list(self, *, status: str = "pending", limit: int = 100) -> list[DeadLetterEntry]:
        validate_status(status)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_dead_letters
                WHERE status = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (status, max(1, min(limit, 500))),
            ).fetchall()
        return [entry_from_row(row) for row in rows]

    def mark_replayed(self, dlq_id: str) -> DeadLetterEntry | None:
        return self._mark(dlq_id, "replayed", utc_now())

    def discard(self, dlq_id: str) -> DeadLetterEntry | None:
        return self._mark(dlq_id, "discarded", None)

    def _mark(
        self,
        dlq_id: str,
        status: str,
        replayed_at: datetime | None,
    ) -> DeadLetterEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE job_dead_letters
                SET status = %s, replayed_at = %s, updated_at = now()
                WHERE dlq_id = %s AND status = 'pending'
                RETURNING *
                """,
                (status, replayed_at, dlq_id),
            ).fetchone()
        return entry_from_row(row) if row else self.get(dlq_id)

    def _connect(self):
        from backend.app.database import get_postgres_pool

        return get_postgres_pool(self.dsn).connection()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(build_dlq_schema_sql())


def build_dlq_schema_sql() -> str:
    return """
    CREATE TABLE IF NOT EXISTS job_dead_letters (
        dlq_id TEXT PRIMARY KEY,
        job_type TEXT NOT NULL CHECK (job_type IN ('indexing', 'research')),
        job_id TEXT NOT NULL,
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error_type TEXT NOT NULL,
        error_message TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL CHECK (status IN ('pending', 'replayed', 'discarded')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        replayed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX IF NOT EXISTS uq_job_dead_letters_pending
        ON job_dead_letters(job_type, job_id) WHERE status = 'pending';
    CREATE INDEX IF NOT EXISTS idx_job_dead_letters_status_created
        ON job_dead_letters(status, created_at);
    """


def entry_from_row(row: dict[str, Any]) -> DeadLetterEntry:
    payload = row.get("payload_json") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return DeadLetterEntry(
        dlq_id=row["dlq_id"],
        job_type=row["job_type"],
        job_id=row["job_id"],
        payload=dict(payload),
        error_type=row["error_type"],
        error_message=row.get("error_message"),
        attempts=int(row["attempts"]),
        status=row["status"],
        created_at=row["created_at"],
        replayed_at=row.get("replayed_at"),
        updated_at=row["updated_at"],
    )


def validate_job_type(job_type: str) -> None:
    if job_type not in {"indexing", "research"}:
        raise ValueError("job_type must be indexing or research")


def validate_status(status: str) -> None:
    if status not in DLQ_STATUSES:
        raise ValueError(f"Unsupported dead-letter status: {status}")


def truncate_error(message: str | None) -> str | None:
    return message[:4000] if message else None


__all__ = [
    "DeadLetterEntry",
    "DeadLetterQueue",
    "InMemoryDeadLetterQueue",
    "PostgresDeadLetterQueue",
    "build_dlq_schema_sql",
]
