from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from backend.app.models.knowledge import utc_now


class IndexJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class IndexJob:
    job_id: str
    document_id: str
    version_id: str
    requested_by: str
    status: IndexJobStatus = IndexJobStatus.QUEUED
    progress: int = 0
    attempts: int = 0
    error_message: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
