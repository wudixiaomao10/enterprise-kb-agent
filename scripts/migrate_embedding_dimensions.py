from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.ingestion.service import DocumentIngestionService
from backend.app.repositories.postgres_store import PostgresKnowledgeStore
from backend.app.retrieval.providers import create_embedding_provider, load_dotenv_if_available


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate pgvector dimensions and rebuild all document vectors"
    )
    parser.add_argument(
        "--confirm-clear-vectors",
        action="store_true",
        help="Confirm clearing derived vectors before the full reindex",
    )
    args = parser.parse_args()
    if not args.confirm_clear_vectors:
        raise RuntimeError("Pass --confirm-clear-vectors to perform the migration")

    load_dotenv_if_available()
    dsn = os.getenv("KNOWLEDGE_DATABASE_URL")
    if not dsn:
        raise RuntimeError("KNOWLEDGE_DATABASE_URL is required")

    provider = create_embedding_provider()
    probe = provider.probe()
    store = PostgresKnowledgeStore(
        dsn,
        vector_dimensions=provider.dimensions,
        initialize_schema=False,
    )
    migration = store.migrate_embedding_dimensions(
        provider.dimensions,
        confirm_clear_vectors=True,
    )
    ingestion = DocumentIngestionService(
        store,
        Path(os.getenv("KNOWLEDGE_STORAGE_DIR", ".codex-tmp/knowledge-documents")),
        embedding_provider=provider,
    )
    reindex = ingestion.reindex_all()
    print(
        json.dumps(
            {
                "embedding_probe": {
                    "provider": probe.provider,
                    "model": probe.model,
                    "dimensions": probe.dimensions,
                    "elapsed_ms": probe.elapsed_ms,
                },
                "migration": migration,
                "reindex": reindex,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
