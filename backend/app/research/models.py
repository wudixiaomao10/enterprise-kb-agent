from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from backend.app.models.knowledge import SubjectScope, utc_now


class ResearchJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ResearchJob:
    job_id: str
    question: str
    requested_by: str
    subject: SubjectScope
    identity_issuer: str | None = None
    identity_subject: str | None = None
    max_rounds: int = 3
    per_query_limit: int = 5
    status: ResearchJobStatus = ResearchJobStatus.QUEUED
    stage: str = "queued"
    progress: int = 0
    attempts: int = 0
    error_message: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class ResearchAssessment:
    coverage: float
    covered_questions: tuple[str, ...]
    gaps: tuple[str, ...]
    conflicts: tuple[str, ...]


__all__ = ["ResearchAssessment", "ResearchJob", "ResearchJobStatus"]
