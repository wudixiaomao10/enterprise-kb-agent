from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from typing import Any, Protocol

from backend.app.jobs.models import IndexJob, IndexJobStatus


class IndexJobRepository(Protocol):
    def create_job(
        self,
        *,
        document_id: str,
        version_id: str,
        requested_by: str,
    ) -> IndexJob:
        ...

    def get_job(self, job_id: str) -> IndexJob | None:
        ...

    def list_jobs(self, limit: int = 100) -> list[IndexJob]:
        ...

    def update_job(
        self,
        job_id: str,
        *,
        status: IndexJobStatus,
        progress: int | None = None,
        error_message: str | None = None,
        result: dict[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> IndexJob | None:
        ...

    def cancel_job(self, job_id: str) -> IndexJob | None:
        ...


class InMemoryIndexJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, IndexJob] = {}
        self._lock = threading.Lock()

    def create_job(
        self,
        *,
        document_id: str,
        version_id: str,
        requested_by: str,
    ) -> IndexJob:
        job = IndexJob(
            job_id=f"job_{uuid.uuid4().hex[:16]}",
            document_id=document_id,
            version_id=version_id,
            requested_by=requested_by,
        )
        with self._lock:
            self.jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str) -> IndexJob | None:
        return self.jobs.get(job_id)

    def list_jobs(self, limit: int = 100) -> list[IndexJob]:
        return sorted(
            self.jobs.values(),
            key=lambda job: job.created_at,
            reverse=True,
        )[:limit]

    def update_job(
        self,
        job_id: str,
        *,
        status: IndexJobStatus,
        progress: int | None = None,
        error_message: str | None = None,
        result: dict[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> IndexJob | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            job.status = status
            if progress is not None:
                job.progress = max(0, min(100, progress))
            job.error_message = error_message
            if result is not None:
                job.result = result
            if increment_attempts:
                job.attempts += 1
            job.updated_at = datetime.now(job.updated_at.tzinfo)
            return job

    def cancel_job(self, job_id: str) -> IndexJob | None:
        job = self.get_job(job_id)
        if job is None or job.status in {IndexJobStatus.COMPLETED, IndexJobStatus.FAILED}:
            return job
        return self.update_job(
            job_id,
            status=IndexJobStatus.CANCELLED,
            progress=job.progress,
        )


class PostgresIndexJobRepository:
    def __init__(self, dsn: str, initialize_schema: bool = False) -> None:
        self.dsn = dsn
        if initialize_schema:
            self._init_schema()

    def create_job(
        self,
        *,
        document_id: str,
        version_id: str,
        requested_by: str,
    ) -> IndexJob:
        job = IndexJob(
            job_id=f"job_{uuid.uuid4().hex[:16]}",
            document_id=document_id,
            version_id=version_id,
            requested_by=requested_by,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO indexing_jobs (
                        job_id, document_id, version_id, requested_by, status,
                        progress, attempts, result_json, created_at, updated_at
                    ) VALUES (
                        %(job_id)s, %(document_id)s, %(version_id)s, %(requested_by)s,
                        %(status)s, %(progress)s, %(attempts)s, %(result)s::jsonb,
                        %(created_at)s, %(updated_at)s
                    )
                    """,
                    job_params(job),
                )
        return job

    def get_job(self, job_id: str) -> IndexJob | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM indexing_jobs WHERE job_id = %(job_id)s",
                    {"job_id": job_id},
                )
                row = cur.fetchone()
        return job_from_row(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[IndexJob]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM indexing_jobs
                    ORDER BY created_at DESC
                    LIMIT %(limit)s
                    """,
                    {"limit": max(1, min(limit, 500))},
                )
                rows = cur.fetchall()
        return [job_from_row(row) for row in rows]

    def update_job(
        self,
        job_id: str,
        *,
        status: IndexJobStatus,
        progress: int | None = None,
        error_message: str | None = None,
        result: dict[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> IndexJob | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE indexing_jobs
                    SET status = %(status)s,
                        progress = COALESCE(%(progress)s, progress),
                        error_message = %(error_message)s,
                        result_json = COALESCE(%(result)s::jsonb, result_json),
                        attempts = attempts + %(attempt_delta)s,
                        updated_at = now()
                    WHERE job_id = %(job_id)s
                    RETURNING *
                    """,
                    {
                        "job_id": job_id,
                        "status": status.value,
                        "progress": progress,
                        "error_message": error_message,
                        "result": (
                            json.dumps(result, ensure_ascii=False)
                            if result is not None
                            else None
                        ),
                        "attempt_delta": 1 if increment_attempts else 0,
                    },
                )
                row = cur.fetchone()
        return job_from_row(row) if row else None

    def cancel_job(self, job_id: str) -> IndexJob | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE indexing_jobs
                    SET status = 'cancelled', updated_at = now()
                    WHERE job_id = %(job_id)s
                      AND status IN ('queued', 'running')
                    RETURNING *
                    """,
                    {"job_id": job_id},
                )
                row = cur.fetchone()
        return job_from_row(row) if row else self.get_job(job_id)

    def _connect(self):
        from backend.app.database import get_postgres_pool

        return get_postgres_pool(self.dsn).connection()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(build_job_schema_sql())


def build_job_schema_sql() -> str:
    return """
    CREATE TABLE IF NOT EXISTS indexing_jobs (
        job_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents(document_id),
        version_id TEXT NOT NULL REFERENCES document_versions(version_id),
        requested_by TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
        ),
        progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
        attempts INTEGER NOT NULL DEFAULT 0,
        error_message TEXT,
        result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_indexing_jobs_status_created
        ON indexing_jobs(status, created_at);
    CREATE INDEX IF NOT EXISTS idx_indexing_jobs_document
        ON indexing_jobs(document_id, created_at);
    """


def job_params(job: IndexJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "document_id": job.document_id,
        "version_id": job.version_id,
        "requested_by": job.requested_by,
        "status": job.status.value,
        "progress": job.progress,
        "attempts": job.attempts,
        "result": json.dumps(job.result, ensure_ascii=False),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def job_from_row(row: dict[str, Any]) -> IndexJob:
    result = row.get("result_json") or {}
    if isinstance(result, str):
        result = json.loads(result)
    return IndexJob(
        job_id=row["job_id"],
        document_id=row["document_id"],
        version_id=row["version_id"],
        requested_by=row["requested_by"],
        status=IndexJobStatus(row["status"]),
        progress=int(row["progress"]),
        attempts=int(row["attempts"]),
        error_message=row.get("error_message"),
        result=result,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
