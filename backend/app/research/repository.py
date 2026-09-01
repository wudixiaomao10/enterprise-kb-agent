from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from typing import Any, Protocol

from backend.app.models.knowledge import SubjectScope
from backend.app.research.models import ResearchJob, ResearchJobStatus


class ResearchJobRepository(Protocol):
    def create_job(
        self,
        *,
        question: str,
        requested_by: str,
        subject: SubjectScope,
        identity_issuer: str | None = None,
        identity_subject: str | None = None,
        max_rounds: int,
        per_query_limit: int,
    ) -> ResearchJob: ...

    def get_job(self, job_id: str) -> ResearchJob | None: ...

    def list_jobs(self, limit: int = 100) -> list[ResearchJob]: ...

    def update_job(
        self,
        job_id: str,
        *,
        status: ResearchJobStatus,
        stage: str | None = None,
        progress: int | None = None,
        error_message: str | None = None,
        result: dict[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> ResearchJob | None: ...

    def cancel_job(self, job_id: str) -> ResearchJob | None: ...


class InMemoryResearchJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, ResearchJob] = {}
        self._lock = threading.Lock()

    def create_job(
        self,
        *,
        question: str,
        requested_by: str,
        subject: SubjectScope,
        identity_issuer: str | None = None,
        identity_subject: str | None = None,
        max_rounds: int,
        per_query_limit: int,
    ) -> ResearchJob:
        job = ResearchJob(
            job_id=f"research_{uuid.uuid4().hex[:16]}",
            question=question,
            requested_by=requested_by,
            subject=subject,
            identity_issuer=identity_issuer,
            identity_subject=identity_subject,
            max_rounds=max_rounds,
            per_query_limit=per_query_limit,
        )
        with self._lock:
            self.jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str) -> ResearchJob | None:
        return self.jobs.get(job_id)

    def list_jobs(self, limit: int = 100) -> list[ResearchJob]:
        return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)[:limit]

    def update_job(
        self,
        job_id: str,
        *,
        status: ResearchJobStatus,
        stage: str | None = None,
        progress: int | None = None,
        error_message: str | None = None,
        result: dict[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> ResearchJob | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            job.status = status
            if stage is not None:
                job.stage = stage
            if progress is not None:
                job.progress = max(0, min(100, progress))
            job.error_message = error_message
            if result is not None:
                job.result = result
            if increment_attempts:
                job.attempts += 1
            job.updated_at = datetime.now(job.updated_at.tzinfo)
            return job

    def cancel_job(self, job_id: str) -> ResearchJob | None:
        job = self.get_job(job_id)
        if job is None or job.status in {ResearchJobStatus.COMPLETED, ResearchJobStatus.FAILED}:
            return job
        return self.update_job(
            job_id,
            status=ResearchJobStatus.CANCELLED,
            stage="cancelled",
            progress=job.progress,
        )


class PostgresResearchJobRepository:
    def __init__(self, dsn: str, initialize_schema: bool = False) -> None:
        self.dsn = dsn
        if initialize_schema:
            self._init_schema()

    def create_job(
        self,
        *,
        question: str,
        requested_by: str,
        subject: SubjectScope,
        identity_issuer: str | None = None,
        identity_subject: str | None = None,
        max_rounds: int,
        per_query_limit: int,
    ) -> ResearchJob:
        job = ResearchJob(
            job_id=f"research_{uuid.uuid4().hex[:16]}",
            question=question,
            requested_by=requested_by,
            subject=subject,
            identity_issuer=identity_issuer,
            identity_subject=identity_subject,
            max_rounds=max_rounds,
            per_query_limit=per_query_limit,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO research_jobs (
                        job_id, question, requested_by, subject_json,
                        identity_issuer, identity_subject, max_rounds,
                        per_query_limit, status, stage, progress, attempts,
                        error_message, result_json, created_at, updated_at
                    ) VALUES (
                        %(job_id)s, %(question)s, %(requested_by)s, %(subject)s::jsonb,
                        %(identity_issuer)s, %(identity_subject)s, %(max_rounds)s,
                        %(per_query_limit)s, %(status)s, %(stage)s,
                        %(progress)s, %(attempts)s, %(error_message)s,
                        %(result)s::jsonb,
                        %(created_at)s, %(updated_at)s
                    )
                    """,
                    research_job_params(job),
                )
        return job

    def get_job(self, job_id: str) -> ResearchJob | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM research_jobs WHERE job_id = %(job_id)s", {"job_id": job_id})
                row = cur.fetchone()
        return research_job_from_row(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[ResearchJob]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM research_jobs ORDER BY created_at DESC LIMIT %(limit)s",
                    {"limit": max(1, min(limit, 500))},
                )
                rows = cur.fetchall()
        return [research_job_from_row(row) for row in rows]

    def update_job(
        self,
        job_id: str,
        *,
        status: ResearchJobStatus,
        stage: str | None = None,
        progress: int | None = None,
        error_message: str | None = None,
        result: dict[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> ResearchJob | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE research_jobs
                    SET status = %(status)s,
                        stage = COALESCE(%(stage)s, stage),
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
                        "stage": stage,
                        "progress": progress,
                        "error_message": error_message,
                        "result": json.dumps(result, ensure_ascii=False) if result is not None else None,
                        "attempt_delta": 1 if increment_attempts else 0,
                    },
                )
                row = cur.fetchone()
        return research_job_from_row(row) if row else None

    def cancel_job(self, job_id: str) -> ResearchJob | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE research_jobs
                    SET status = 'cancelled', stage = 'cancelled', updated_at = now()
                    WHERE job_id = %(job_id)s AND status IN ('queued', 'running')
                    RETURNING *
                    """,
                    {"job_id": job_id},
                )
                row = cur.fetchone()
        return research_job_from_row(row) if row else self.get_job(job_id)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(build_research_job_schema_sql())

    def _connect(self):
        from backend.app.database import get_postgres_pool

        return get_postgres_pool(self.dsn).connection()


def build_research_job_schema_sql() -> str:
    return """
    CREATE TABLE IF NOT EXISTS research_jobs (
        job_id TEXT PRIMARY KEY,
        question TEXT NOT NULL,
        requested_by TEXT NOT NULL,
        subject_json JSONB NOT NULL,
        identity_issuer TEXT,
        identity_subject TEXT,
        max_rounds INTEGER NOT NULL,
        per_query_limit INTEGER NOT NULL,
        status TEXT NOT NULL,
        stage TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        error_message TEXT,
        result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_research_jobs_status_created
        ON research_jobs(status, created_at);
    CREATE INDEX IF NOT EXISTS idx_research_jobs_requester_created
        ON research_jobs(requested_by, created_at DESC);
    ALTER TABLE research_jobs
        ADD COLUMN IF NOT EXISTS identity_issuer TEXT;
    ALTER TABLE research_jobs
        ADD COLUMN IF NOT EXISTS identity_subject TEXT;
    """


def research_job_params(job: ResearchJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "question": job.question,
        "requested_by": job.requested_by,
        "identity_issuer": job.identity_issuer,
        "identity_subject": job.identity_subject,
        "subject": json.dumps(
            {
                "user_id": job.subject.user_id,
                "department_ids": list(job.subject.department_ids),
                "role_ids": list(job.subject.role_ids),
            },
            ensure_ascii=False,
        ),
        "max_rounds": job.max_rounds,
        "per_query_limit": job.per_query_limit,
        "status": job.status.value,
        "stage": job.stage,
        "progress": job.progress,
        "attempts": job.attempts,
        "error_message": job.error_message,
        "result": json.dumps(job.result, ensure_ascii=False),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def research_job_from_row(row: dict[str, Any]) -> ResearchJob:
    subject = row.get("subject_json") or {}
    result = row.get("result_json") or {}
    if isinstance(subject, str):
        subject = json.loads(subject)
    if isinstance(result, str):
        result = json.loads(result)
    return ResearchJob(
        job_id=row["job_id"],
        question=row["question"],
        requested_by=row["requested_by"],
        subject=SubjectScope(
            user_id=subject["user_id"],
            department_ids=tuple(subject.get("department_ids", [])),
            role_ids=tuple(subject.get("role_ids", [])),
        ),
        identity_issuer=row.get("identity_issuer"),
        identity_subject=row.get("identity_subject"),
        max_rounds=int(row["max_rounds"]),
        per_query_limit=int(row["per_query_limit"]),
        status=ResearchJobStatus(row["status"]),
        stage=row["stage"],
        progress=int(row["progress"]),
        attempts=int(row["attempts"]),
        error_message=row.get("error_message"),
        result=result,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = [
    "InMemoryResearchJobRepository",
    "PostgresResearchJobRepository",
    "ResearchJobRepository",
]
