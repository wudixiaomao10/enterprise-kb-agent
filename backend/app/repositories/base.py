from __future__ import annotations

from typing import Protocol

from backend.app.models.knowledge import (
    ACLEntry,
    Citation,
    Document,
    DocumentChunk,
    DocumentVersion,
    Evidence,
    SourceType,
    SubjectScope,
)


class KnowledgeStore(Protocol):
    def create_document(
        self,
        *,
        title: str,
        source_type: SourceType,
        owner_id: str,
        department_id: str | None,
        acl: list[ACLEntry],
        storage_uri: str,
        raw_bytes: bytes,
    ) -> tuple[Document, DocumentVersion]:
        ...

    def add_chunks(self, chunks: list[DocumentChunk]) -> None:
        ...

    def replace_chunks_for_version(
        self,
        version_id: str,
        chunks: list[DocumentChunk],
    ) -> None:
        ...

    def list_documents(self) -> list[Document]:
        ...

    def list_accessible_documents(self, subject: SubjectScope) -> list[Document]:
        ...

    def list_accessible_chunks(self, subject: SubjectScope) -> list[DocumentChunk]:
        ...

    def hybrid_search(
        self,
        *,
        query: str,
        query_embedding: list[float],
        subject: SubjectScope,
        limit: int,
        min_score: float,
    ) -> list[Evidence] | None:
        ...

    def get_chunk(self, chunk_id: str) -> DocumentChunk | None:
        ...

    def get_accessible_chunk(
        self,
        chunk_id: str,
        subject: SubjectScope,
    ) -> DocumentChunk | None:
        ...

    def get_document(self, document_id: str) -> Document | None:
        ...

    def get_accessible_document(
        self,
        document_id: str,
        subject: SubjectScope,
    ) -> Document | None:
        ...

    def get_current_version(self, document_id: str) -> DocumentVersion | None:
        ...

    def list_accessible_document_versions(
        self,
        document_id: str,
        subject: SubjectScope,
    ) -> list[DocumentVersion]:
        ...

    def citation_for_chunk(self, chunk: DocumentChunk) -> Citation:
        ...
