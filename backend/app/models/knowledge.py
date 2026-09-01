from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SubjectType(str, Enum):
    USER = "user"
    DEPARTMENT = "department"
    ROLE = "role"
    PUBLIC = "public"


class Permission(str, Enum):
    READ = "read"
    ADMIN = "admin"


class SourceType(str, Enum):
    PDF = "pdf"
    WORD = "word"
    MARKDOWN = "markdown"
    TEXT = "text"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ACLEntry:
    subject_type: SubjectType
    subject_id: str
    permission: Permission = Permission.READ


@dataclass(frozen=True)
class SubjectScope:
    user_id: str
    department_ids: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()


@dataclass
class Document:
    document_id: str
    title: str
    source_type: SourceType
    owner_id: str
    department_id: str | None
    acl: list[ACLEntry]
    created_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentVersion:
    version_id: str
    document_id: str
    version_number: int
    storage_uri: str
    content_hash: str
    is_current: bool = True
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class ParsedBlock:
    text: str
    page: int
    section_path: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentChunk:
    chunk_id: str
    document_id: str
    version_id: str
    page: int
    section_path: str
    content: str
    content_hash: str
    acl: list[ACLEntry]
    embedding: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Citation:
    document_id: str
    version_id: str
    page: int
    chunk_id: str
    title: str
    section_path: str


@dataclass
class Evidence:
    chunk: DocumentChunk
    citation: Citation
    score: float
    keyword_score: float
    vector_score: float
    metadata_score: float
    reranker_score: float | None = None


@dataclass
class Claim:
    text: str
    citation_chunk_ids: list[str]
    confidence: float = 1.0


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    message: str
    claim_index: int | None = None
    chunk_ids: tuple[str, ...] = ()


@dataclass
class VerificationResult:
    verified: bool
    coverage: float
    support_scores: list[float]
    conflicts: list[str]
    issues: list[VerificationIssue]

    @property
    def reason(self) -> str | None:
        return self.issues[0].message if self.issues else None


@dataclass
class KnowledgeAnswer:
    answer: str
    claims: list[Claim]
    citations: list[Citation]
    evidence: list[Evidence]
    verified: bool
    refusal_reason: str | None = None
    verification: VerificationResult | None = None
