from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.app.models.knowledge import (
    ACLEntry,
    Citation,
    Document,
    DocumentChunk,
    DocumentVersion,
    Evidence,
    Permission,
    SourceType,
    SubjectScope,
    SubjectType,
)
from backend.app.repositories.base import KnowledgeStore
from backend.app.security.acl import can_access_chunk, can_access_document


class SQLiteKnowledgeStore(KnowledgeStore):
    """Durable development store with the same boundaries as the future PG store."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO documents (
                    document_id, title, source_type, owner_id, department_id,
                    acl_json, created_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.document_id,
                    document.title,
                    document.source_type.value,
                    document.owner_id,
                    document.department_id,
                    encode_acl(document.acl),
                    document.created_at.isoformat(),
                    json.dumps(document.metadata, ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                INSERT INTO document_versions (
                    version_id, document_id, version_number, storage_uri,
                    content_hash, is_current, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.version_id,
                    version.document_id,
                    version.version_number,
                    version.storage_uri,
                    version.content_hash,
                    1 if version.is_current else 0,
                    version.created_at.isoformat(),
                ),
            )
        return document, version

    def add_chunks(self, chunks: list[DocumentChunk]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO document_chunks (
                    chunk_id, document_id, version_id, page, section_path,
                    content, content_hash, acl_json, embedding_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.version_id,
                        chunk.page,
                        chunk.section_path,
                        chunk.content,
                        chunk.content_hash,
                        encode_acl(chunk.acl),
                        json.dumps(chunk.embedding),
                        json.dumps(chunk.metadata, ensure_ascii=False),
                    )
                    for chunk in chunks
                ],
            )

    def replace_chunks_for_version(
        self,
        version_id: str,
        chunks: list[DocumentChunk],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM document_chunks WHERE version_id = ?",
                (version_id,),
            )
            conn.executemany(
                """
                INSERT INTO document_chunks (
                    chunk_id, document_id, version_id, page, section_path,
                    content, content_hash, acl_json, embedding_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.version_id,
                        chunk.page,
                        chunk.section_path,
                        chunk.content,
                        chunk.content_hash,
                        encode_acl(chunk.acl),
                        json.dumps(chunk.embedding),
                        json.dumps(chunk.metadata, ensure_ascii=False),
                    )
                    for chunk in chunks
                ],
            )

    def list_documents(self) -> list[Document]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM documents ORDER BY created_at").fetchall()
        return [document_from_row(row) for row in rows]

    def list_accessible_documents(self, subject: SubjectScope) -> list[Document]:
        return [
            document
            for document in self.list_documents()
            if can_access_document(subject, document)
        ]

    def list_accessible_chunks(self, subject: SubjectScope) -> list[DocumentChunk]:
        documents = {item.document_id: item for item in self.list_documents()}
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM document_chunks").fetchall()
        chunks = [chunk_from_row(row) for row in rows]
        return [
            chunk
            for chunk in chunks
            if chunk.document_id in documents
            and can_access_chunk(subject, chunk, documents[chunk.document_id])
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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM document_chunks WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()
        return chunk_from_row(row) if row else None

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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        return document_from_row(row) if row else None

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
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM document_versions
                WHERE document_id = ? AND is_current = 1
                ORDER BY version_number DESC
                LIMIT 1
                """,
                (document_id,),
            ).fetchone()
        return version_from_row(row) if row else None

    def list_accessible_document_versions(
        self,
        document_id: str,
        subject: SubjectScope,
    ) -> list[DocumentVersion]:
        if self.get_accessible_document(document_id, subject) is None:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM document_versions
                WHERE document_id = ?
                ORDER BY version_number DESC
                """,
                (document_id,),
            ).fetchall()
        return [version_from_row(row) for row in rows]

    def citation_for_chunk(self, chunk: DocumentChunk) -> Citation:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE document_id = ?",
                (chunk.document_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Document not found for chunk: {chunk.chunk_id}")
        document = document_from_row(row)
        return Citation(
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            page=chunk.page,
            chunk_id=chunk.chunk_id,
            title=document.title,
            section_path=chunk.section_path,
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    department_id TEXT,
                    acl_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS document_versions (
                    version_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(document_id),
                    version_number INTEGER NOT NULL,
                    storage_uri TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    is_current INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS document_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(document_id),
                    version_id TEXT NOT NULL REFERENCES document_versions(version_id),
                    page INTEGER NOT NULL,
                    section_path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    acl_json TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_documents_department
                    ON documents(department_id);
                CREATE INDEX IF NOT EXISTS idx_versions_document_current
                    ON document_versions(document_id, is_current);
                CREATE INDEX IF NOT EXISTS idx_chunks_document
                    ON document_chunks(document_id);
                CREATE INDEX IF NOT EXISTS idx_chunks_version
                    ON document_chunks(version_id);
                """
            )


def encode_acl(acl: list[ACLEntry]) -> str:
    return json.dumps(
        [
            {
                "subject_type": entry.subject_type.value,
                "subject_id": entry.subject_id,
                "permission": entry.permission.value,
            }
            for entry in acl
        ],
        ensure_ascii=False,
    )


def decode_acl(raw: str) -> list[ACLEntry]:
    data = json.loads(raw)
    return [
        ACLEntry(
            subject_type=SubjectType(item["subject_type"]),
            subject_id=item["subject_id"],
            permission=Permission(item.get("permission", Permission.READ.value)),
        )
        for item in data
    ]


def document_from_row(row: sqlite3.Row) -> Document:
    return Document(
        document_id=row["document_id"],
        title=row["title"],
        source_type=SourceType(row["source_type"]),
        owner_id=row["owner_id"],
        department_id=row["department_id"],
        acl=decode_acl(row["acl_json"]),
        created_at=parse_datetime(row["created_at"]),
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def version_from_row(row: sqlite3.Row) -> DocumentVersion:
    return DocumentVersion(
        version_id=row["version_id"],
        document_id=row["document_id"],
        version_number=row["version_number"],
        storage_uri=row["storage_uri"],
        content_hash=row["content_hash"],
        is_current=bool(row["is_current"]),
        created_at=parse_datetime(row["created_at"]),
    )


def chunk_from_row(row: sqlite3.Row) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        version_id=row["version_id"],
        page=row["page"],
        section_path=row["section_path"],
        content=row["content"],
        content_hash=row["content_hash"],
        acl=decode_acl(row["acl_json"]),
        embedding=json.loads(row["embedding_json"]),
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
