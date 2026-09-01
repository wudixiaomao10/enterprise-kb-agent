from __future__ import annotations

from backend.app.models.knowledge import KnowledgeAnswer


def serialize_knowledge_answer(answer: KnowledgeAnswer) -> dict[str, object]:
    return {
        "answer": answer.answer,
        "verified": answer.verified,
        "refusal_reason": answer.refusal_reason,
        "claims": [
            {
                "text": claim.text,
                "citation_chunk_ids": claim.citation_chunk_ids,
                "confidence": round(claim.confidence, 4),
            }
            for claim in answer.claims
        ],
        "citations": [
            {
                "document_id": citation.document_id,
                "version_id": citation.version_id,
                "page": citation.page,
                "chunk_id": citation.chunk_id,
                "title": citation.title,
                "section_path": citation.section_path,
            }
            for citation in answer.citations
        ],
        "evidence": [
            {
                "chunk_id": item.chunk.chunk_id,
                "score": round(item.score, 4),
                "keyword_score": round(item.keyword_score, 4),
                "vector_score": round(item.vector_score, 4),
                "metadata_score": round(item.metadata_score, 4),
                "reranker_score": (
                    round(item.reranker_score, 4)
                    if item.reranker_score is not None
                    else None
                ),
            }
            for item in answer.evidence
        ],
        "verification": (
            {
                "coverage": answer.verification.coverage,
                "support_scores": answer.verification.support_scores,
                "conflicts": answer.verification.conflicts,
                "issues": [
                    {
                        "code": issue.code,
                        "message": issue.message,
                        "claim_index": issue.claim_index,
                        "chunk_ids": list(issue.chunk_ids),
                    }
                    for issue in answer.verification.issues
                ],
            }
            if answer.verification is not None
            else None
        ),
    }


__all__ = ["serialize_knowledge_answer"]
