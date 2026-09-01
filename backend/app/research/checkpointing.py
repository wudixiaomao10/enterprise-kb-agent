from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator


class PostgresResearchCheckpointerFactory:
    def __init__(
        self,
        dsn: str,
        *,
        setup: bool = True,
        delete_on_terminal: bool = True,
    ) -> None:
        self.dsn = dsn
        self.setup = setup
        self.delete_on_terminal = delete_on_terminal

    @contextmanager
    def open(self) -> Iterator[object]:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as error:  # pragma: no cover
            raise RuntimeError(
                "Install langgraph-checkpoint-postgres to enable research checkpoints"
            ) from error

        serde = build_checkpoint_serde()
        from backend.app.database import get_postgres_pool

        # Repositories use dict rows, while PostgresSaver expects its default tuple rows.
        with get_postgres_pool(self.dsn, row_factory=None).connection() as connection:
            connection.autocommit = True
            checkpointer = PostgresSaver(connection, serde=serde)
            if self.setup:
                checkpointer.setup()
            yield checkpointer

    def delete_thread(self, thread_id: str) -> None:
        if not self.delete_on_terminal:
            return
        try:
            with self.open() as checkpointer:
                delete = getattr(checkpointer, "delete_thread", None)
                if delete is not None:
                    delete(thread_id[:255])
        except Exception:
            return


def build_checkpoint_serde():
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        from backend.app.models.knowledge import (
            ACLEntry,
            Citation,
            Claim,
            DocumentChunk,
            Evidence,
            KnowledgeAnswer,
            Permission,
            SourceType,
            SubjectScope,
            SubjectType,
            VerificationIssue,
            VerificationResult,
        )
        from backend.app.research.models import ResearchAssessment
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("LangGraph checkpoint serializer dependencies are missing") from error

    serde = JsonPlusSerializer(
        allowed_msgpack_modules=(
            ACLEntry,
            Citation,
            Claim,
            DocumentChunk,
            Evidence,
            KnowledgeAnswer,
            Permission,
            ResearchAssessment,
            SourceType,
            SubjectScope,
            SubjectType,
            VerificationIssue,
            VerificationResult,
        )
    )
    encryption_key = os.getenv("KNOWLEDGE_RESEARCH_CHECKPOINT_ENCRYPTION_KEY", "")
    if not encryption_key:
        return serde
    try:
        from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
    except ImportError as error:  # pragma: no cover
        raise RuntimeError(
            "Install the LangGraph encrypted serializer dependencies "
            "to enable encrypted research checkpoints"
        ) from error

    key = encryption_key.encode()
    if len(key) not in {16, 24, 32}:
        raise RuntimeError(
            "KNOWLEDGE_RESEARCH_CHECKPOINT_ENCRYPTION_KEY must be 16, 24, or 32 bytes"
        )
    return EncryptedSerializer.from_pycryptodome_aes(serde=serde, key=key)


def research_checkpoints_enabled(default: bool) -> bool:
    raw = os.getenv("KNOWLEDGE_RESEARCH_CHECKPOINTS")
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "PostgresResearchCheckpointerFactory",
    "build_checkpoint_serde",
    "research_checkpoints_enabled",
]
