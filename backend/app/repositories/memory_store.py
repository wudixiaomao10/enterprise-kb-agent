from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from pathlib import Path

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
from backend.app.repositories.base import KnowledgeStore
from backend.app.security.acl import can_access_chunk, can_access_document


class InMemoryKnowledgeStore(KnowledgeStore):
    """Development store that preserves the production data boundaries."""

    def __init__(self) -> None:
        self.documents: dict[str, Document] = {}
        self.versions: dict[str, DocumentVersion] = {}
        self.chunks: dict[str, DocumentChunk] = {}
        self._document_versions: dict[str, list[str]] = defaultdict(list)

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
        document_id = f"doc_{uuid.uuid4().hex[:12]}"
        version_id = f"ver_{uuid.uuid4().hex[:12]}"
        document = Document(
            document_id=document_id,
            title=title,
            source_type=source_type,
            owner_id=owner_id,
            department_id=department_id,
            acl=acl,
            metadata={"filename": Path(storage_uri).name},
        )
        version = DocumentVersion(
            version_id=version_id,
            document_id=document_id,
            version_number=1,
            storage_uri=storage_uri,
            content_hash=hashlib.sha256(raw_bytes).hexdigest(),
        )
        self.documents[document_id] = document
        self.versions[version_id] = version
        self._document_versions[document_id].append(version_id)
        return document, version

    def add_chunks(self, chunks: list[DocumentChunk]) -> None:
        for chunk in chunks:
            self.chunks[chunk.chunk_id] = chunk

    def replace_chunks_for_version(
        self,
        version_id: str,
        chunks: list[DocumentChunk],
    ) -> None:
        self.chunks = {
            chunk_id: chunk
            for chunk_id, chunk in self.chunks.items()
            if chunk.version_id != version_id
        }
        self.add_chunks(chunks)

    def list_documents(self) -> list[Document]:
        return list(self.documents.values())

    def list_accessible_documents(self, subject) -> list[Document]:
        return [
            document
            for document in self.documents.values()
            if can_access_document(subject, document)
        ]

    def list_accessible_chunks(self, subject) -> list[DocumentChunk]:
        return [
            chunk
            for chunk in self.chunks.values()
            if can_access_chunk(subject, chunk, self.documents[chunk.document_id])
        ]

    def hybrid_search(
        self,
        *,
        query: str,
        query_embedding: list[float],
        subject: SubjectScope,
        limit: int,
        min_score: float,
    ) -> list[Evidence] | None:
        return None

    def get_chunk(self, chunk_id: str) -> DocumentChunk | None:
        return self.chunks.get(chunk_id)

    def get_accessible_chunk(
        self,
        chunk_id: str,
        subject: SubjectScope,
    ) -> DocumentChunk | None:
        chunk = self.get_chunk(chunk_id)
        if chunk is None:
            return None
        document = self.get_document(chunk.document_id)
        if document is None or not can_access_chunk(subject, chunk, document):
            return None
        return chunk

    def get_document(self, document_id: str) -> Document | None:
        return self.documents.get(document_id)

    def get_accessible_document(
        self,
        document_id: str,
        subject: SubjectScope,
    ) -> Document | None:
        document = self.get_document(document_id)
        if document is None or not can_access_document(subject, document):
            return None
        return document

    def get_current_version(self, document_id: str) -> DocumentVersion | None:
        version_ids = self._document_versions.get(document_id, [])
        for version_id in reversed(version_ids):
            version = self.versions[version_id]
            if version.is_current:
                return version
        return None

    def list_accessible_document_versions(
        self,
        document_id: str,
        subject: SubjectScope,
    ) -> list[DocumentVersion]:
        if self.get_accessible_document(document_id, subject) is None:
            return []
        return [
            self.versions[version_id]
            for version_id in reversed(self._document_versions.get(document_id, []))
        ]

    def citation_for_chunk(self, chunk: DocumentChunk) -> Citation:
        document = self.documents[chunk.document_id]
        return Citation(
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            page=chunk.page,
            chunk_id=chunk.chunk_id,
            title=document.title,
            section_path=chunk.section_path,
        )
