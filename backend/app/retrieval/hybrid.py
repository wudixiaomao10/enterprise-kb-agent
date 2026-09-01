from __future__ import annotations

from collections import Counter

from backend.app.models.knowledge import Evidence, SubjectScope
from backend.app.observability import observed_span
from backend.app.repositories.base import KnowledgeStore
from backend.app.retrieval.embeddings import cosine_similarity, tokenize
from backend.app.retrieval.providers import EmbeddingProvider, LocalHashEmbeddingProvider
from backend.app.retrieval.rerankers import LexicalReranker, Reranker


class HybridRetriever:
    """ACL-first hybrid retriever for the MVP."""

    def __init__(
        self,
        store: KnowledgeStore,
        embedding_provider: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
        candidate_multiplier: int = 4,
        max_candidates: int = 40,
    ) -> None:
        self.store = store
        self.embedding_provider = embedding_provider or LocalHashEmbeddingProvider()
        self.reranker = reranker or LexicalReranker()
        self.candidate_multiplier = max(candidate_multiplier, 1)
        self.max_candidates = max(max_candidates, 1)

    def search(
        self,
        query: str,
        subject: SubjectScope,
        limit: int = 8,
        min_score: float = 0.05,
    ) -> list[Evidence]:
        with observed_span(
            "retrieval.search",
            attributes={
                "retrieval.limit": limit,
                "retrieval.reranker": self.reranker.name,
            },
            stage="retrieval",
        ):
            return self._search(query, subject, limit=limit, min_score=min_score)

    def _search(
        self,
        query: str,
        subject: SubjectScope,
        *,
        limit: int,
        min_score: float,
    ) -> list[Evidence]:
        query_embedding = self.embedding_provider.embed_query(query)
        candidate_limit = min(
            self.max_candidates,
            max(limit, limit * self.candidate_multiplier),
        )
        with observed_span("retrieval.store_search", stage="retrieval.store"):
            delegated = self.store.hybrid_search(
                query=query,
                query_embedding=query_embedding,
                subject=subject,
                limit=candidate_limit,
                min_score=min_score,
            )
        if delegated is not None:
            with observed_span("retrieval.rerank", stage="rerank"):
                return self.reranker.rerank(query, delegated, limit)

        allowed_chunks = self.store.list_accessible_chunks(subject)
        if not allowed_chunks:
            return []

        query_terms = Counter(tokenize(query))
        scored: list[Evidence] = []
        for chunk in allowed_chunks:
            keyword = keyword_score(query_terms, chunk.content)
            vector = cosine_similarity(query_embedding, chunk.embedding)
            metadata = metadata_score(query_terms, chunk.section_path)
            score = (0.45 * keyword) + (0.45 * vector) + (0.10 * metadata)
            if score < min_score:
                continue
            scored.append(
                Evidence(
                    chunk=chunk,
                    citation=self.store.citation_for_chunk(chunk),
                    score=score,
                    keyword_score=keyword,
                    vector_score=vector,
                    metadata_score=metadata,
                )
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        with observed_span("retrieval.rerank", stage="rerank"):
            return self.reranker.rerank(query, scored[:candidate_limit], limit)


def keyword_score(query_terms: Counter[str], text: str) -> float:
    if not query_terms:
        return 0.0
    text_terms = Counter(tokenize(text))
    if not text_terms:
        return 0.0
    overlap = sum(min(count, text_terms.get(term, 0)) for term, count in query_terms.items())
    return overlap / max(sum(query_terms.values()), 1)


def metadata_score(query_terms: Counter[str], section_path: str) -> float:
    if not query_terms or not section_path:
        return 0.0
    section_terms = set(tokenize(section_path))
    matched = sum(1 for term in query_terms if term in section_terms)
    return matched / max(len(query_terms), 1)
