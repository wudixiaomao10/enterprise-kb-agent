from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.bootstrap import create_demo_services
from backend.app.models.knowledge import SubjectScope


def main() -> None:
    os.environ["KNOWLEDGE_STORE"] = "memory"
    _, _, qa = create_demo_services()
    answer = qa.answer(
        "企业知识库支持哪些能力，并且引用有什么要求？",
        SubjectScope(user_id="u_sales", department_ids=("sales",)),
        limit=5,
    )
    report = {
        "status": "passed" if answer.verified else "failed",
        "embedding": qa.embedding_provider.name,
        "embedding_dimensions": qa.embedding_provider.dimensions,
        "reranker": qa.reranker.name,
        "claim_generator": qa.claim_generator.name,
        "verified": answer.verified,
        "refusal_reason": answer.refusal_reason,
        "claim_count": len(answer.claims),
        "citation_count": len(answer.citations),
        "coverage": answer.verification.coverage if answer.verification else None,
        "support_scores": (
            answer.verification.support_scores if answer.verification else []
        ),
        "citations": [
            {
                "document_id": citation.document_id,
                "version_id": citation.version_id,
                "page": citation.page,
                "chunk_id": citation.chunk_id,
            }
            for citation in answer.citations
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not answer.verified:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
