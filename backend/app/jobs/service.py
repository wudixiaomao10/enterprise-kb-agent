from __future__ import annotations

import os
from typing import Protocol

from backend.app.ingestion.service import DocumentIngestionService
from backend.app.jobs.dlq import DeadLetterQueue, InMemoryDeadLetterQueue
from backend.app.jobs.models import IndexJob, IndexJobStatus
from backend.app.jobs.repository import IndexJobRepository
from backend.app.observability import correlation_context, observed_span


class JobDispatcher(Protocol):
    name: str

    def dispatch(self, job_id: str) -> None:
        ...


class DramatiqJobDispatcher:
    name = "dramatiq-redis"

    def dispatch(self, job_id: str) -> None:
        from backend.app.jobs.tasks import process_index_job

        process_index_job.send(job_id)


class DeferredInlineDispatcher:
    name = "inline"

    def __init__(self) -> None:
        self.service: IndexJobService | None = None

    def dispatch(self, job_id: str) -> None:
        if self.service is None:
            raise RuntimeError("Inline dispatcher is not bound to a job service")
        self.service.execute(job_id)


class IndexJobService:
    def __init__(
        self,
        repository: IndexJobRepository,
        ingestion: DocumentIngestionService,
        dispatcher: JobDispatcher,
        dlq: DeadLetterQueue | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self.repository = repository
        self.ingestion = ingestion
        self.dispatcher = dispatcher
        self.dlq = dlq or InMemoryDeadLetterQueue()
        self.max_attempts = configured_max_attempts(max_attempts, default=4)
        if isinstance(dispatcher, DeferredInlineDispatcher):
            dispatcher.service = self

    def submit(
        self,
        *,
        document_id: str,
        version_id: str,
        requested_by: str,
    ) -> IndexJob:
        job = self.repository.create_job(
            document_id=document_id,
            version_id=version_id,
            requested_by=requested_by,
        )
        try:
            self.dispatcher.dispatch(job.job_id)
        except Exception as error:
            self.repository.update_job(
                job.job_id,
                status=IndexJobStatus.FAILED,
                error_message=f"Queue dispatch failed: {error}",
            )
            raise
        return self.repository.get_job(job.job_id) or job

    def retry(self, job_id: str) -> IndexJob | None:
        job = self.repository.get_job(job_id)
        if job is None:
            return None
        if job.status not in {IndexJobStatus.FAILED, IndexJobStatus.CANCELLED}:
            raise ValueError("Only failed or cancelled jobs can be retried")
        queued = self.repository.update_job(
            job_id,
            status=IndexJobStatus.QUEUED,
            progress=0,
            error_message=None,
            result={},
        )
        self.dispatcher.dispatch(job_id)
        return queued

    def cancel(self, job_id: str) -> IndexJob | None:
        return self.repository.cancel_job(job_id)

    def execute(self, job_id: str) -> IndexJob | None:
        with correlation_context(job_id=job_id):
            with observed_span(
                "index.job",
                attributes={"job.id": job_id},
                stage="index_job",
            ):
                return self._execute(job_id)

    def _execute(self, job_id: str) -> IndexJob | None:
        job = self.repository.get_job(job_id)
        if job is None:
            return None
        if job.status in {IndexJobStatus.CANCELLED, IndexJobStatus.COMPLETED}:
            return job
        self.repository.update_job(
            job_id,
            status=IndexJobStatus.RUNNING,
            progress=5,
            error_message=None,
            increment_attempts=True,
        )
        try:
            result = self.ingestion.reindex_document(job.document_id)
            if result.get("status") != "reindexed":
                raise RuntimeError(f"Indexing did not complete: {result}")
            latest = self.repository.get_job(job_id)
            if latest is not None and latest.status == IndexJobStatus.CANCELLED:
                return latest
            return self.repository.update_job(
                job_id,
                status=IndexJobStatus.COMPLETED,
                progress=100,
                result=result,
            )
        except Exception as error:
            latest = self.repository.get_job(job_id)
            if latest is not None and latest.status == IndexJobStatus.CANCELLED:
                return latest
            failed = self.repository.update_job(
                job_id,
                status=IndexJobStatus.FAILED,
                error_message=str(error)[:4000],
            )
            if failed is not None and failed.attempts >= self.max_attempts:
                self.dlq.enqueue(
                    job_type="indexing",
                    job_id=failed.job_id,
                    payload={
                        "job_id": failed.job_id,
                        "document_id": failed.document_id,
                        "version_id": failed.version_id,
                        "requested_by": failed.requested_by,
                    },
                    error_type=type(error).__name__,
                    error_message=str(error),
                    attempts=failed.attempts,
                )
            raise

    def replay_dead_letter(self, dlq_id: str) -> IndexJob | None:
        entry = self.dlq.get(dlq_id)
        if entry is None or entry.status != "pending":
            raise ValueError("Pending indexing dead-letter entry not found")
        if entry.job_type != "indexing":
            raise ValueError("Dead-letter entry does not belong to indexing jobs")
        job = self.retry(entry.job_id)
        self.dlq.mark_replayed(dlq_id)
        return job

    def discard_dead_letter(self, dlq_id: str):
        entry = self.dlq.get(dlq_id)
        if entry is None or entry.status != "pending":
            raise ValueError("Pending indexing dead-letter entry not found")
        if entry.job_type != "indexing":
            raise ValueError("Dead-letter entry does not belong to indexing jobs")
        return self.dlq.discard(dlq_id)


def create_job_dispatcher() -> JobDispatcher:
    mode = os.getenv("KNOWLEDGE_JOB_MODE", "inline").strip().lower()
    if mode == "inline":
        return DeferredInlineDispatcher()
    if mode in {"dramatiq", "redis"}:
        return DramatiqJobDispatcher()
    raise RuntimeError("Unsupported KNOWLEDGE_JOB_MODE. Use inline or dramatiq.")


def configured_max_attempts(value: int | None, *, default: int) -> int:
    if value is not None:
        return max(1, value)
    try:
        return max(1, int(os.getenv("KNOWLEDGE_JOB_MAX_ATTEMPTS", str(default))))
    except ValueError:
        return default
