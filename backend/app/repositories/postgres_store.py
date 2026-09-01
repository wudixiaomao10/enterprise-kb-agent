from __future__ import annotations

import hashlib
import json
import re
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


class EmbeddingDimensionMismatch(RuntimeError):
    pass


class PostgresKnowledgeStore(KnowledgeStore):
    """PostgreSQL + pgvector store with ACL-first hybrid search."""

    def __init__(
        self,
        dsn: str,
        *,
        vector_dimensions: int = 1536,
        initialize_schema: bool = False,
    ) -> None:
        self.dsn = dsn
        self.vector_dimensions = vector_dimensions
        if initialize_schema:
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
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO documents (
                        document_id, title, source_type, owner_id, department_id,
                        acl_json, created_at, metadata_json
                    )
                    VALUES (
                        %(document_id)s, %(title)s, %(source_type)s, %(owner_id)s,
                        %(department_id)s, %(acl_json)s::jsonb, %(created_at)s,
                        %(metadata_json)s::jsonb
                    )
                    """,
                    {
                        "document_id": document.document_id,
                        "title": document.title,
                        "source_type": document.source_type.value,
                        "owner_id": document.owner_id,
                        "department_id": document.department_id,
                        "acl_json": encode_acl(document.acl),
                        "created_at": document.created_at,
                        "metadata_json": json.dumps(
                            document.metadata,
                            ensure_ascii=False,
                        ),
                    },
                )
                cur.execute(
                    """
                    INSERT INTO document_versions (
                        version_id, document_id, version_number, storage_uri,
                        content_hash, is_current, created_at
                    )
                    VALUES (
                        %(version_id)s, %(document_id)s, %(version_number)s,
                        %(storage_uri)s, %(content_hash)s, %(is_current)s,
                        %(created_at)s
                    )
                    """,
                    {
                        "version_id": version.version_id,
                        "document_id": version.document_id,
                        "version_number": version.version_number,
                        "storage_uri": version.storage_uri,
                        "content_hash": version.content_hash,
                        "is_current": version.is_current,
                        "created_at": version.created_at,
                    },
                )
        return document, version

    def add_chunks(self, chunks: list[DocumentChunk]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO document_chunks (
                        chunk_id, document_id, version_id, page, section_path,
                        content, content_hash, acl_json, embedding, metadata_json
                    )
                    VALUES (
                        %(chunk_id)s, %(document_id)s, %(version_id)s, %(page)s,
                        %(section_path)s, %(content)s, %(content_hash)s,
                        %(acl_json)s::jsonb, %(embedding)s::vector,
                        %(metadata_json)s::jsonb
                    )
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        content = EXCLUDED.content,
                        content_hash = EXCLUDED.content_hash,
                        acl_json = EXCLUDED.acl_json,
                        embedding = EXCLUDED.embedding,
                        metadata_json = EXCLUDED.metadata_json
                    """,
                    [
                        {
                            "chunk_id": chunk.chunk_id,
                            "document_id": chunk.document_id,
                            "version_id": chunk.version_id,
                            "page": chunk.page,
                            "section_path": chunk.section_path,
                            "content": chunk.content,
                            "content_hash": chunk.content_hash,
                            "acl_json": encode_acl(chunk.acl),
                            "embedding": vector_literal(chunk.embedding),
                            "metadata_json": json.dumps(
                                chunk.metadata,
                                ensure_ascii=False,
                            ),
                        }
                        for chunk in chunks
                    ],
                )

    def replace_chunks_for_version(
        self,
        version_id: str,
        chunks: list[DocumentChunk],
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM document_chunks WHERE version_id = %(version_id)s",
                    {"version_id": version_id},
                )
                cur.executemany(
                    """
                    INSERT INTO document_chunks (
                        chunk_id, document_id, version_id, page, section_path,
                        content, content_hash, acl_json, embedding, metadata_json
                    )
                    VALUES (
                        %(chunk_id)s, %(document_id)s, %(version_id)s, %(page)s,
                        %(section_path)s, %(content)s, %(content_hash)s,
                        %(acl_json)s::jsonb, %(embedding)s::vector,
                        %(metadata_json)s::jsonb
                    )
                    """,
                    [
                        {
                            "chunk_id": chunk.chunk_id,
                            "document_id": chunk.document_id,
                            "version_id": chunk.version_id,
                            "page": chunk.page,
                            "section_path": chunk.section_path,
                            "content": chunk.content,
                            "content_hash": chunk.content_hash,
                            "acl_json": encode_acl(chunk.acl),
                            "embedding": vector_literal(chunk.embedding),
                            "metadata_json": json.dumps(
                                chunk.metadata,
                                ensure_ascii=False,
                            ),
                        }
                        for chunk in chunks
                    ],
                )

    def list_documents(self) -> list[Document]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM documents ORDER BY created_at")
                return [document_from_row(row) for row in cur.fetchall()]

    def list_accessible_documents(self, subject: SubjectScope) -> list[Document]:
        sql, params = build_document_acl_query(subject)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [document_from_row(row) for row in cur.fetchall()]

    def list_accessible_chunks(self, subject: SubjectScope) -> list[DocumentChunk]:
        sql, params = build_accessible_chunks_query(subject)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [chunk_from_row(row) for row in cur.fetchall()]

    def hybrid_search(
        self,
        *,
        query: str,
        query_embedding: list[float],
        subject: SubjectScope,
        limit: int,
        min_score: float,
    ) -> list[Evidence] | None:
        sql, params = build_hybrid_search_query(
            query=query,
            query_embedding=query_embedding,
            subject=subject,
            limit=limit,
            min_score=min_score,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        evidence: list[Evidence] = []
        for row in rows:
            chunk = chunk_from_row(row)
            citation = Citation(
                document_id=chunk.document_id,
                version_id=chunk.version_id,
                page=chunk.page,
                chunk_id=chunk.chunk_id,
                title=row["title"],
                section_path=chunk.section_path,
            )
            evidence.append(
                Evidence(
                    chunk=chunk,
                    citation=citation,
                    score=float(row["score"]),
                    keyword_score=float(row["keyword_score"]),
                    vector_score=float(row["vector_score"]),
                    metadata_score=float(row["metadata_score"]),
                )
            )
        return evidence

    def get_chunk(self, chunk_id: str) -> DocumentChunk | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM document_chunks WHERE chunk_id = %(chunk_id)s",
                    {"chunk_id": chunk_id},
                )
                row = cur.fetchone()
        return chunk_from_row(row) if row else None

    def get_accessible_chunk(
        self,
        chunk_id: str,
        subject: SubjectScope,
    ) -> DocumentChunk | None:
        acl_predicate, params = build_acl_predicate(subject)
        params["chunk_id"] = chunk_id
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT c.*
                    FROM document_chunks c
                    JOIN documents d ON d.document_id = c.document_id
                    WHERE c.chunk_id = %(chunk_id)s
                      AND ({acl_predicate})
                    """,
                    params,
                )
                row = cur.fetchone()
        return chunk_from_row(row) if row else None

    def get_document(self, document_id: str) -> Document | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM documents WHERE document_id = %(document_id)s",
                    {"document_id": document_id},
                )
                row = cur.fetchone()
        return document_from_row(row) if row else None

    def get_accessible_document(
        self,
        document_id: str,
        subject: SubjectScope,
    ) -> Document | None:
        acl_predicate, params = build_acl_predicate(subject, chunk_alias=None)
        params["document_id"] = document_id
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT d.*
                    FROM documents d
                    WHERE d.document_id = %(document_id)s
                      AND ({acl_predicate})
                    """,
                    params,
                )
                row = cur.fetchone()
        return document_from_row(row) if row else None

    def get_current_version(self, document_id: str) -> DocumentVersion | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM document_versions
                    WHERE document_id = %(document_id)s AND is_current = true
                    ORDER BY version_number DESC
                    LIMIT 1
                    """,
                    {"document_id": document_id},
                )
                row = cur.fetchone()
        return version_from_row(row) if row else None

    def list_accessible_document_versions(
        self,
        document_id: str,
        subject: SubjectScope,
    ) -> list[DocumentVersion]:
        acl_predicate, params = build_acl_predicate(subject, chunk_alias=None)
        params["document_id"] = document_id
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT v.*
                    FROM document_versions v
                    JOIN documents d ON d.document_id = v.document_id
                    WHERE v.document_id = %(document_id)s
                      AND ({acl_predicate})
                    ORDER BY v.version_number DESC
                    """,
                    params,
                )
                rows = cur.fetchall()
        return [version_from_row(row) for row in rows]

    def citation_for_chunk(self, chunk: DocumentChunk) -> Citation:
        document = self.get_document(chunk.document_id)
        if document is None:
            raise KeyError(f"Document not found for chunk: {chunk.chunk_id}")
        return Citation(
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            page=chunk.page,
            chunk_id=chunk.chunk_id,
            title=document.title,
            section_path=chunk.section_path,
        )

    def get_embedding_dimensions(self) -> int | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT format_type(attribute.atttypid, attribute.atttypmod) AS data_type
                    FROM pg_attribute attribute
                    JOIN pg_class relation ON relation.oid = attribute.attrelid
                    JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = current_schema()
                      AND relation.relname = 'document_chunks'
                      AND attribute.attname = 'embedding'
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                    """
                )
                row = cur.fetchone()
        if not row:
            return None
        matched = re.fullmatch(r"vector\((\d+)\)", str(row["data_type"]))
        return int(matched.group(1)) if matched else None

    def migrate_embedding_dimensions(
        self,
        dimensions: int,
        *,
        confirm_clear_vectors: bool = False,
    ) -> dict[str, int | str]:
        if dimensions <= 0:
            raise ValueError("Embedding dimensions must be positive")
        current = self.get_embedding_dimensions()
        if current == dimensions:
            self.vector_dimensions = dimensions
            return {
                "status": "unchanged",
                "previous_dimensions": current,
                "dimensions": dimensions,
                "cleared_vectors": 0,
            }
        if not confirm_clear_vectors:
            raise RuntimeError(
                "Changing pgvector dimensions clears derived vectors. "
                "Pass confirm_clear_vectors=True and immediately run a full reindex."
            )

        embedding_type = f"vector({int(dimensions)})"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS count FROM document_chunks WHERE embedding IS NOT NULL"
                )
                row = cur.fetchone()
                cleared_vectors = int(row["count"])
                cur.execute("DROP INDEX IF EXISTS idx_chunks_embedding")
                cur.execute(
                    f"""
                    ALTER TABLE document_chunks
                    ALTER COLUMN embedding TYPE {embedding_type}
                    USING NULL::{embedding_type}
                    """
                )
                cur.execute(
                    """
                    UPDATE document_chunks
                    SET metadata_json = metadata_json
                        - 'embedding_provider'
                        - 'embedding_dimensions'
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX idx_chunks_embedding
                    ON document_chunks USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 100)
                    """
                )
        self.vector_dimensions = dimensions
        return {
            "status": "migrated",
            "previous_dimensions": current or 0,
            "dimensions": dimensions,
            "cleared_vectors": cleared_vectors,
        }

    def _connect(self):
        from backend.app.database import get_postgres_pool

        return get_postgres_pool(self.dsn).connection()

    def _init_schema(self) -> None:
        from backend.app.database import ensure_database_schema

        ensure_database_schema(self.dsn, self.vector_dimensions)
        self.validate_schema()

    def validate_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        actual_dimensions = self.get_embedding_dimensions()
        if actual_dimensions != self.vector_dimensions:
            raise EmbeddingDimensionMismatch(
                "PostgreSQL document_chunks.embedding is "
                f"vector({actual_dimensions}), but the configured provider emits "
                f"{self.vector_dimensions} dimensions. Run the controlled embedding "
                "dimension migration and full reindex before starting the API."
            )


def build_schema_sql(vector_dimensions: int) -> str:
    embedding_type = f"VECTOR({vector_dimensions})"
    return f"""
    CREATE TABLE IF NOT EXISTS documents (
        document_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        source_type TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        department_id TEXT,
        acl_json JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        metadata_json JSONB NOT NULL DEFAULT '{{}}'::jsonb
    );

    CREATE TABLE IF NOT EXISTS document_versions (
        version_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents(document_id),
        version_number INTEGER NOT NULL,
        storage_uri TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        is_current BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (document_id, version_number)
    );

    CREATE TABLE IF NOT EXISTS document_chunks (
        chunk_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES documents(document_id),
        version_id TEXT NOT NULL REFERENCES document_versions(version_id),
        page INTEGER NOT NULL,
        section_path TEXT NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        acl_json JSONB NOT NULL,
        embedding {embedding_type},
        search_vector TSVECTOR GENERATED ALWAYS AS (
            setweight(to_tsvector('simple', coalesce(section_path, '')), 'A') ||
            setweight(to_tsvector('simple', coalesce(content, '')), 'B')
        ) STORED,
        metadata_json JSONB NOT NULL DEFAULT '{{}}'::jsonb
    );

    CREATE TABLE IF NOT EXISTS queries (
        query_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        department_ids TEXT[] NOT NULL DEFAULT '{{}}',
        question TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS answers (
        answer_id TEXT PRIMARY KEY,
        query_id TEXT NOT NULL REFERENCES queries(query_id),
        answer TEXT NOT NULL,
        verified BOOLEAN NOT NULL,
        refusal_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS citations (
        citation_id TEXT PRIMARY KEY,
        answer_id TEXT NOT NULL REFERENCES answers(answer_id),
        document_id TEXT NOT NULL REFERENCES documents(document_id),
        version_id TEXT NOT NULL REFERENCES document_versions(version_id),
        chunk_id TEXT NOT NULL REFERENCES document_chunks(chunk_id),
        page INTEGER NOT NULL,
        section_path TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_documents_department
        ON documents(department_id);
    CREATE INDEX IF NOT EXISTS idx_documents_acl
        ON documents USING gin(acl_json);
    CREATE INDEX IF NOT EXISTS idx_chunks_document
        ON document_chunks(document_id);
    CREATE INDEX IF NOT EXISTS idx_chunks_version
        ON document_chunks(version_id);
    CREATE INDEX IF NOT EXISTS idx_chunks_acl
        ON document_chunks USING gin(acl_json);
    CREATE INDEX IF NOT EXISTS idx_chunks_search_vector
        ON document_chunks USING gin(search_vector);
    CREATE INDEX IF NOT EXISTS idx_chunks_embedding
        ON document_chunks USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100);
    """


def build_hybrid_search_query(
    *,
    query: str,
    query_embedding: list[float],
    subject: SubjectScope,
    limit: int,
    min_score: float,
) -> tuple[str, dict[str, Any]]:
    acl_predicate, acl_params = build_acl_predicate(subject)
    sql = f"""
    WITH accessible_chunks AS (
        SELECT
            c.chunk_id,
            c.document_id,
            c.version_id,
            c.page,
            c.section_path,
            c.content,
            c.content_hash,
            c.acl_json,
            c.embedding,
            c.search_vector,
            c.metadata_json,
            d.title,
            ts_rank_cd(c.search_vector, plainto_tsquery('simple', %(query)s)) AS keyword_score,
            GREATEST(0.0, 1.0 - (c.embedding <=> %(embedding)s::vector)) AS vector_score,
            CASE
                WHEN c.section_path ILIKE %(section_like)s THEN 1.0
                ELSE 0.0
            END AS metadata_score
        FROM document_chunks c
        JOIN documents d ON d.document_id = c.document_id
        JOIN document_versions v ON v.version_id = c.version_id
        WHERE v.is_current = true
          AND c.embedding IS NOT NULL
          AND ({acl_predicate})
    ),
    scored AS (
        SELECT
            *,
            (
                0.45 * keyword_score +
                0.45 * vector_score +
                0.10 * metadata_score
            ) AS score
        FROM accessible_chunks
        WHERE search_vector @@ plainto_tsquery('simple', %(query)s)
           OR vector_score > 0
           OR metadata_score > 0
    )
    SELECT *
    FROM scored
    WHERE score >= %(min_score)s
    ORDER BY score DESC
    LIMIT %(limit)s
    """
    params = {
        "query": query,
        "embedding": vector_literal(query_embedding),
        "section_like": f"%{escape_like(query)}%",
        "limit": limit,
        "min_score": min_score,
        **acl_params,
    }
    return sql, params


def build_document_acl_query(subject: SubjectScope) -> tuple[str, dict[str, Any]]:
    acl_predicate, acl_params = build_acl_predicate(subject, chunk_alias=None)
    return (
        f"SELECT * FROM documents d WHERE {acl_predicate} ORDER BY created_at",
        acl_params,
    )


def build_accessible_chunks_query(subject: SubjectScope) -> tuple[str, dict[str, Any]]:
    acl_predicate, acl_params = build_acl_predicate(subject)
    return (
        f"""
        SELECT c.*
        FROM document_chunks c
        JOIN documents d ON d.document_id = c.document_id
        JOIN document_versions v ON v.version_id = c.version_id
        WHERE v.is_current = true AND ({acl_predicate})
        ORDER BY c.document_id, c.page, c.chunk_id
        """,
        acl_params,
    )


def build_acl_predicate(
    subject: SubjectScope,
    *,
    chunk_alias: str | None = "c",
    document_alias: str = "d",
) -> tuple[str, dict[str, Any]]:
    if "admin" in subject.role_ids:
        return "TRUE", {}
    params: dict[str, Any] = {
        "acl_user_id": subject.user_id,
        "acl_department_ids": list(subject.department_ids),
        "acl_role_ids": list(subject.role_ids),
    }
    predicates = [jsonb_acl_predicate(f"{document_alias}.acl_json", "doc_acl")]
    if chunk_alias:
        predicates.insert(0, jsonb_acl_predicate(f"{chunk_alias}.acl_json", "chunk_acl"))
    return " OR ".join(f"({predicate})" for predicate in predicates), params


def jsonb_acl_predicate(column: str, alias: str) -> str:
    return f"""
    EXISTS (
        SELECT 1
        FROM jsonb_array_elements({column}) AS {alias}(entry)
        WHERE ({alias}.entry->>'permission') IN ('read', 'admin')
          AND (
              ({alias}.entry->>'subject_type' = 'public')
              OR (
                  {alias}.entry->>'subject_type' = 'user'
                  AND {alias}.entry->>'subject_id' = %(acl_user_id)s
              )
              OR (
                  {alias}.entry->>'subject_type' = 'department'
                  AND {alias}.entry->>'subject_id' = ANY(%(acl_department_ids)s)
              )
              OR (
                  {alias}.entry->>'subject_type' = 'role'
                  AND {alias}.entry->>'subject_id' = ANY(%(acl_role_ids)s)
              )
          )
    )
    """


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.12g}" for value in values) + "]"


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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


def decode_acl(raw: str | list[dict[str, Any]]) -> list[ACLEntry]:
    data = json.loads(raw) if isinstance(raw, str) else raw
    return [
        ACLEntry(
            subject_type=SubjectType(item["subject_type"]),
            subject_id=item["subject_id"],
            permission=Permission(item.get("permission", Permission.READ.value)),
        )
        for item in data
    ]


def document_from_row(row: dict[str, Any]) -> Document:
    return Document(
        document_id=row["document_id"],
        title=row["title"],
        source_type=SourceType(row["source_type"]),
        owner_id=row["owner_id"],
        department_id=row["department_id"],
        acl=decode_acl(row["acl_json"]),
        created_at=parse_datetime(row["created_at"]),
        metadata=decode_json(row["metadata_json"]),
    )


def version_from_row(row: dict[str, Any]) -> DocumentVersion:
    return DocumentVersion(
        version_id=row["version_id"],
        document_id=row["document_id"],
        version_number=row["version_number"],
        storage_uri=row["storage_uri"],
        content_hash=row["content_hash"],
        is_current=bool(row["is_current"]),
        created_at=parse_datetime(row["created_at"]),
    )


def chunk_from_row(row: dict[str, Any]) -> DocumentChunk:
    embedding = row.get("embedding")
    if isinstance(embedding, str):
        embedding = [float(item) for item in embedding.strip("[]").split(",") if item]
    return DocumentChunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        version_id=row["version_id"],
        page=row["page"],
        section_path=row["section_path"],
        content=row["content"],
        content_hash=row["content_hash"],
        acl=decode_acl(row["acl_json"]),
        embedding=embedding or [],
        metadata=decode_json(row["metadata_json"]),
    )


def decode_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    return json.loads(value)


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
