from __future__ import annotations

from backend.app.agent.citation_binder import CitationBinder, render_cited_answer
from backend.app.agent.claims import (
    ClaimGenerator,
    ExtractiveClaimGenerator,
    build_claim_text,
)
from backend.app.agent.verifier import EvidenceVerifier
from backend.app.models.knowledge import (
    Evidence,
    KnowledgeAnswer,
    SubjectScope,
    VerificationResult,
)
from backend.app.observability import observed_span
from backend.app.repositories.base import KnowledgeStore
from backend.app.retrieval.hybrid import HybridRetriever
from backend.app.retrieval.providers import EmbeddingProvider, LocalHashEmbeddingProvider
from backend.app.retrieval.rerankers import LexicalReranker, Reranker
from backend.app.security.content import contains_prompt_injection


class KnowledgeQAService:
    def __init__(
        self,
        store: KnowledgeStore,
        embedding_provider: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
        claim_generator: ClaimGenerator | None = None,
    ) -> None:
        self.store = store
        self.embedding_provider = embedding_provider or LocalHashEmbeddingProvider()
        self.reranker = reranker or LexicalReranker()
        self.claim_generator = claim_generator or ExtractiveClaimGenerator()
        self.retriever = HybridRetriever(
            store,
            self.embedding_provider,
            reranker=self.reranker,
        )
        self.binder = CitationBinder(store)
        self.verifier = EvidenceVerifier(store, self.embedding_provider)

    def answer(self, question: str, subject: SubjectScope, limit: int = 5) -> KnowledgeAnswer:
        with observed_span(
            "knowledge.answer",
            attributes={"qa.limit": limit},
            stage="qa",
        ):
            evidence = self.retriever.search(question, subject, limit=limit)
            return self.answer_from_evidence(question, subject, evidence)

    def answer_from_evidence(
        self,
        question: str,
        subject: SubjectScope,
        evidence: list[Evidence],
    ) -> KnowledgeAnswer:
        evidence = [
            item for item in evidence if not contains_prompt_injection(item.chunk.content)
        ]
        if not evidence:
            return KnowledgeAnswer(
                answer="当前知识库中没有找到你有权限访问的充分依据。",
                claims=[],
                citations=[],
                evidence=[],
                verified=False,
                refusal_reason="no_accessible_evidence",
            )

        with observed_span("knowledge.claim_generation", stage="claim_generation"):
            draft = self.claim_generator.generate(question, evidence)
        with observed_span("knowledge.citation_binding", stage="citation_binding"):
            binding = self.binder.bind(draft.claims, evidence, subject)
        if not binding.valid:
            report = VerificationResult(
                verified=False,
                coverage=0.0,
                support_scores=[],
                conflicts=[],
                issues=binding.issues,
            )
            return refused_answer(
                evidence=evidence,
                report=report,
                reason=(binding.issues[0].code if binding.issues else "no_bound_claims"),
            )

        with observed_span("knowledge.evidence_verification", stage="verification"):
            report = self.verifier.verify_report(binding.claims, subject)
        if not report.verified:
            return refused_answer(
                evidence=evidence,
                report=report,
                reason=(report.issues[0].code if report.issues else "verification_failed"),
            )

        answer = render_cited_answer(
            "根据你有权限访问且通过验证的资料：",
            binding.claims,
            binding.citations,
        )
        return KnowledgeAnswer(
            answer=answer,
            claims=binding.claims,
            citations=binding.citations,
            evidence=evidence,
            verified=True,
            verification=report,
        )


def refused_answer(
    *,
    evidence,
    report: VerificationResult,
    reason: str,
) -> KnowledgeAnswer:
    return KnowledgeAnswer(
        answer="当前知识库中没有找到可验证且引用有效的充分依据。",
        claims=[],
        citations=[],
        evidence=evidence,
        verified=False,
        refusal_reason=reason,
        verification=report,
    )


__all__ = ["KnowledgeQAService", "build_claim_text"]
