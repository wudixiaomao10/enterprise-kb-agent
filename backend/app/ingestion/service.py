from __future__ import annotations

import uuid
from pathlib import Path

from backend.app.ingestion.chunking import build_chunks
from backend.app.ingestion.parser import detect_source_type, parse_document
from backend.app.models.knowledge import ACLEntry
from backend.app.observability import observed_span
from backend.app.repositories.base import KnowledgeStore
from backend.app.retrieval.providers import EmbeddingProvider, LocalHashEmbeddingProvider
from backend.app.security.content import (
    UploadSecurityError,
    quarantine_upload,
    validate_upload,
)
from backend.app.storage.object_store import (
    LocalObjectStorage,
    ObjectStorage,
    ObjectStorageNotFound,
    safe_filename,
)


class DocumentIngestionService:
    def __init__(
        self,
        store: KnowledgeStore,
        storage_dir: Path,
        embedding_provider: EmbeddingProvider | None = None,
        object_storage: ObjectStorage | None = None,
    ) -> None:
        self.store = store
        self.storage_dir = storage_dir
        self.object_storage = object_storage or LocalObjectStorage(storage_dir)
        self.embedding_provider = embedding_provider or LocalHashEmbeddingProvider()

    def ingest(
        self,
        *,
        filename: str,
        raw_bytes: bytes,
        title: str | None,
        owner_id: str,
        department_id: str | None,
        acl: list[ACLEntry],
    ) -> dict[str, object]:
        registration = self.register_document(
            filename=filename,
            raw_bytes=raw_bytes,
            title=title,
            owner_id=owner_id,
            department_id=department_id,
            acl=acl,
        )
        result = self.reindex_document(str(registration["document_id"]))
        return {
            **registration,
            **result,
        }

    def register_document(
        self,
        *,
        filename: str,
        raw_bytes: bytes,
        title: str | None,
        owner_id: str,
        department_id: str | None,
        acl: list[ACLEntry],
    ) -> dict[str, object]:
        if not raw_bytes:
            raise ValueError("Document cannot be empty")
        try:
            security_report = validate_upload(filename, raw_bytes)
        except UploadSecurityError as error:
            if error.status == "quarantined":
                try:
                    quarantine_upload(filename, raw_bytes, error.code)
                except OSError as quarantine_error:
                    raise ValueError(
                        "Upload blocked: quarantine storage unavailable"
                    ) from quarantine_error
                raise ValueError(
                    f"Upload quarantined for security review: {error.code}"
                ) from error
            raise
        safe_name = safe_filename(filename)
        storage_uri = self.object_storage.put(safe_name, raw_bytes)
        try:
            document, version = self.store.create_document(
                title=title or Path(safe_name).stem,
                source_type=detect_source_type(safe_name),
                owner_id=owner_id,
                department_id=department_id,
                acl=acl,
                storage_uri=storage_uri,
                raw_bytes=raw_bytes,
            )
        except Exception:
            self.object_storage.delete(storage_uri)
            raise
        return {
            "document_id": document.document_id,
            "version_id": version.version_id,
            "status": "registered",
            "chunk_count": 0,
            "storage_uri": storage_uri,
            "security_scan_status": security_report.virus_scan_status,
        }

    def reindex_document(self, document_id: str) -> dict[str, object]:
        document = self.store.get_document(document_id)
        if document is None:
            return {
                "document_id": document_id,
                "status": "not_found",
                "chunk_count": 0,
            }

        version = self.store.get_current_version(document_id)
        if version is None:
            return {
                "document_id": document_id,
                "status": "no_current_version",
                "chunk_count": 0,
            }

        try:
            source_context = self.object_storage.materialize(version.storage_uri)
            with source_context as source_path:
                raw_bytes = source_path.read_bytes()
                filename = str(document.metadata.get("filename") or source_path.name)
                with observed_span(
                    "ingestion.parse",
                    attributes={"document.source_type": document.source_type.value},
                    stage="parse",
                ):
                    blocks = parse_document(filename, raw_bytes, source_path=source_path)
        except ObjectStorageNotFound:
            return {
                "document_id": document_id,
                "version_id": version.version_id,
                "status": "source_missing",
                "storage_uri": version.storage_uri,
                "chunk_count": 0,
            }

        with observed_span(
            "ingestion.chunk_and_embed",
            attributes={"ingestion.block_count": len(blocks)},
            stage="chunking",
        ):
            chunks = build_chunks(
                document_id=document.document_id,
                version_id=version.version_id,
                blocks=blocks,
                acl=document.acl,
                embedding_provider=self.embedding_provider,
            )
        self.store.replace_chunks_for_version(version.version_id, chunks)
        return {
            "document_id": document.document_id,
            "version_id": version.version_id,
            "status": "reindexed",
            "chunk_count": len(chunks),
            "embedding_provider": self.embedding_provider.name,
            "embedding_dimensions": self.embedding_provider.dimensions,
            "parser": chunks[0].metadata.get("parser") if chunks else None,
        }

    def reindex_all(self) -> dict[str, object]:
        results = [
            self.reindex_document(document.document_id)
            for document in self.store.list_documents()
        ]
        return {
            "status": "completed",
            "document_count": len(results),
            "chunk_count": sum(int(item.get("chunk_count", 0)) for item in results),
            "results": results,
        }
