from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import nullcontext
from typing import Protocol

from backend.app.agent.qa import KnowledgeQAService
from backend.app.agent.serialization import serialize_knowledge_answer
from backend.app.identity.directory import IdentityDirectory
from backend.app.jobs.dlq import DeadLetterQueue, InMemoryDeadLetterQueue
from backend.app.jobs.service import configured_max_attempts
from backend.app.models.knowledge import SubjectScope
from backend.app.observability import correlation_context, observed_span
from backend.app.research.graph import LongRunningResearchAgent, ResearchCancelled
from backend.app.research.models import ResearchJob, ResearchJobStatus
from backend.app.research.planner import ResearchPlanner
from backend.app.research.repository import ResearchJobRepository


class ResearchDispatcher(Protocol):
    name: str

    def dispatch(self, job_id: str) -> None: ...


class DramatiqResearchDispatcher:
    name = "dramatiq"

    def dispatch(self, job_id: str) -> None:
        from backend.app.jobs.tasks import process_research_job

        process_research_job.send(job_id)


class InlineResearchDispatcher:
    name = "inline"

    def __init__(self) -> None:
        self.service: ResearchJobService | None = None

    def dispatch(self, job_id: str) -> None:
        if self.service is None:
            raise RuntimeError("Inline research dispatcher is not bound")
        self.service.execute(job_id)


class DeferredResearchDispatcher:
    name = "deferred"

    def dispatch(self, job_id: str) -> None:
        return None


class ResearchAuthorizationRevoked(ResearchCancelled):
    pass


ResearchSubjectResolver = Callable[[ResearchJob], SubjectScope]


class ResearchCheckpointerFactory(Protocol):
    def open(self): ...

    def delete_thread(self, thread_id: str) -> None: ...


class NoopResearchCheckpointerFactory:
    def open(self):
        return nullcontext(None)

    def delete_thread(self, thread_id: str) -> None:
        return None


class DirectoryResearchSubjectResolver:
    def __init__(self, directory: IdentityDirectory) -> None:
        self.directory = directory

    def __call__(self, job: ResearchJob) -> SubjectScope:
        if not job.identity_issuer or not job.identity_subject:
            raise ResearchAuthorizationRevoked(
                "research job has no resolvable identity subject"
            )
        identity = self.directory.resolve_user(
            issuer=job.identity_issuer,
            subject=job.identity_subject,
        )
        if identity is None:
            raise ResearchAuthorizationRevoked(
                "requesting user is no longer active in the identity directory"
            )
        if identity.user_id != job.requested_by:
            raise ResearchAuthorizationRevoked(
                "requesting user no longer matches the submitted identity"
            )
        return SubjectScope(
            user_id=identity.user_id,
            department_ids=identity.department_ids,
            role_ids=identity.role_ids,
        )


class ResearchJobService:
    def __init__(
        self,
        repository: ResearchJobRepository,
        qa: KnowledgeQAService,
        planner: ResearchPlanner,
        dispatcher: ResearchDispatcher,
        subject_resolver: ResearchSubjectResolver | None = None,
        checkpointer_factory: ResearchCheckpointerFactory | None = None,
        dlq: DeadLetterQueue | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self.repository = repository
        self.qa = qa
        self.planner = planner
        self.dispatcher = dispatcher
        self.subject_resolver = subject_resolver or (lambda job: job.subject)
        self.checkpointer_factory = checkpointer_factory or NoopResearchCheckpointerFactory()
        self.dlq = dlq or InMemoryDeadLetterQueue()
        self.max_attempts = configured_max_attempts(max_attempts, default=3)
        if isinstance(dispatcher, InlineResearchDispatcher):
            dispatcher.service = self

    def submit(
        self,
        *,
        question: str,
        requested_by: str,
        subject: SubjectScope,
        identity_issuer: str | None = None,
        identity_subject: str | None = None,
        max_rounds: int = 3,
        per_query_limit: int = 5,
    ) -> ResearchJob:
        job = self.repository.create_job(
            question=question,
            requested_by=requested_by,
            subject=subject,
            identity_issuer=identity_issuer,
            identity_subject=identity_subject,
            max_rounds=max(1, min(max_rounds, 5)),
            per_query_limit=max(2, min(per_query_limit, 10)),
        )
        try:
            self.dispatcher.dispatch(job.job_id)
        except Exception as error:
            self.repository.update_job(
                job.job_id,
                status=ResearchJobStatus.FAILED,
                stage="queue_failed",
                error_message=str(error),
            )
            raise
        return self.repository.get_job(job.job_id) or job

    def execute(self, job_id: str) -> ResearchJob | None:
        with correlation_context(job_id=job_id, run_id=job_id):
            with observed_span(
                "research.job",
                attributes={"job.id": job_id, "run.id": job_id},
                stage="research_job",
            ):
                return self._execute(job_id)

    def _execute(self, job_id: str) -> ResearchJob | None:
        job = self.repository.get_job(job_id)
        if job is None or job.status in {ResearchJobStatus.CANCELLED, ResearchJobStatus.COMPLETED}:
            return job
        try:
            subject = self.subject_resolver(job)
        except ResearchAuthorizationRevoked as error:
            return self._cancel_for_revoked_authorization(job_id, str(error))
        self.repository.update_job(
            job_id,
            status=ResearchJobStatus.RUNNING,
            stage="starting",
            progress=3,
            increment_attempts=True,
        )

        def update_progress(stage: str, progress: int) -> None:
            self.repository.update_job(
                job_id,
                status=ResearchJobStatus.RUNNING,
                stage=stage,
                progress=progress,
            )

        def is_cancelled() -> bool:
            current = self.repository.get_job(job_id)
            return current is None or current.status == ResearchJobStatus.CANCELLED

        def refresh_subject() -> SubjectScope:
            current = self.repository.get_job(job_id)
            if current is None:
                raise ResearchCancelled("research job no longer exists")
            return self.subject_resolver(current)

        try:
            with self.checkpointer_factory.open() as checkpointer:
                agent = LongRunningResearchAgent(
                    self.qa,
                    self.planner,
                    progress_callback=update_progress,
                    cancel_check=is_cancelled,
                    subject_refresh=refresh_subject,
                    checkpointer=checkpointer,
                )
                state = agent.run(
                    question=job.question,
                    subject=subject,
                    thread_id=job.job_id,
                    max_rounds=job.max_rounds,
                    per_query_limit=job.per_query_limit,
                )
            if is_cancelled():
                return self.repository.get_job(job_id)
            assessment = state.get("assessment")
            result = {
                "answer": serialize_knowledge_answer(state["answer"]),
                "research": {
                    "subquestions": state.get("subquestions", []),
                    "attempted_queries": state.get("attempted_queries", []),
                    "rounds": state.get("round", 0),
                    "coverage": assessment.coverage if assessment else 0.0,
                    "coverage_gaps": list(assessment.gaps) if assessment else [],
                    "conflicts": list(assessment.conflicts) if assessment else [],
                    "evidence_count": len(state.get("evidence", [])),
                },
            }
            updated = self.repository.update_job(
                job_id,
                status=ResearchJobStatus.COMPLETED,
                stage="completed",
                progress=100,
                result=result,
            )
            self.checkpointer_factory.delete_thread(job.job_id)
            return updated
        except ResearchAuthorizationRevoked as error:
            return self._cancel_for_revoked_authorization(job_id, str(error))
        except ResearchCancelled:
            updated = self.repository.cancel_job(job_id)
            self.checkpointer_factory.delete_thread(job_id)
            return updated
        except Exception as error:
            failed = self.repository.update_job(
                job_id,
                status=ResearchJobStatus.FAILED,
                stage="failed",
                error_message=str(error),
            )
            if failed is not None and failed.attempts >= self.max_attempts:
                self.dlq.enqueue(
                    job_type="research",
                    job_id=failed.job_id,
                    payload={
                        "job_id": failed.job_id,
                        "requested_by": failed.requested_by,
                    },
                    error_type=type(error).__name__,
                    error_message=str(error),
                    attempts=failed.attempts,
                )
            raise

    def cancel(self, job_id: str) -> ResearchJob | None:
        return self.repository.cancel_job(job_id)

    def replay_dead_letter(self, dlq_id: str) -> ResearchJob | None:
        entry = self.dlq.get(dlq_id)
        if entry is None or entry.status != "pending":
            raise ValueError("Pending research dead-letter entry not found")
        if entry.job_type != "research":
            raise ValueError("Dead-letter entry does not belong to research jobs")
        job = self.repository.get_job(entry.job_id)
        if job is None:
            raise ValueError("Research job not found for dead-letter entry")
        queued = self.repository.update_job(
            job.job_id,
            status=ResearchJobStatus.QUEUED,
            stage="queued",
            progress=0,
            error_message=None,
            result={},
        )
        if queued is None:
            raise ValueError("Research job could not be requeued")
        self.dispatcher.dispatch(job.job_id)
        self.dlq.mark_replayed(dlq_id)
        return self.repository.get_job(job.job_id) or queued

    def discard_dead_letter(self, dlq_id: str):
        entry = self.dlq.get(dlq_id)
        if entry is None or entry.status != "pending":
            raise ValueError("Pending research dead-letter entry not found")
        if entry.job_type != "research":
            raise ValueError("Dead-letter entry does not belong to research jobs")
        return self.dlq.discard(dlq_id)

    def _cancel_for_revoked_authorization(
        self,
        job_id: str,
        message: str,
    ) -> ResearchJob | None:
        updated = self.repository.update_job(
            job_id,
            status=ResearchJobStatus.CANCELLED,
            stage="authorization_revoked",
            progress=100,
            error_message=message,
            result={},
        )
        self.checkpointer_factory.delete_thread(job_id)
        return updated


__all__ = [
    "DeferredResearchDispatcher",
    "DirectoryResearchSubjectResolver",
    "DramatiqResearchDispatcher",
    "InlineResearchDispatcher",
    "ResearchAuthorizationRevoked",
    "ResearchJobService",
]
