from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.retrieval.embeddings import cosine_similarity
from backend.app.retrieval.providers import (
    LocalHashEmbeddingProvider,
    create_embedding_provider,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the configured embedding API")
    parser.add_argument(
        "--allow-local",
        action="store_true",
        help="Allow the local hash provider instead of requiring a remote provider",
    )
    args = parser.parse_args()

    provider = create_embedding_provider()
    if isinstance(provider, LocalHashEmbeddingProvider) and not args.allow_local:
        raise RuntimeError(
            "Smoke test requires a real provider. Set "
            "KNOWLEDGE_EMBEDDING_PROVIDER=openai or azure-openai."
        )

    probe = provider.probe()
    vectors = provider.embed_texts(
        [
            "员工入职满一年后可以享受年假。",
            "入职一年后，员工享有年度休假。",
            "PostgreSQL 使用 pgvector 保存向量。",
        ]
    )
    related_similarity = cosine_similarity(vectors[0], vectors[1])
    unrelated_similarity = cosine_similarity(vectors[0], vectors[2])
    passed = related_similarity > unrelated_similarity
    report = {
        "status": "passed" if passed else "failed",
        "provider": probe.provider,
        "model": probe.model,
        "dimensions": probe.dimensions,
        "probe_elapsed_ms": probe.elapsed_ms,
        "vector_norm": probe.vector_norm,
        "related_similarity": round(related_similarity, 6),
        "unrelated_similarity": round(unrelated_similarity, 6),
        "semantic_ordering_valid": passed,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
