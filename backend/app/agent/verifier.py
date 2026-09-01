from __future__ import annotations

import os
import re

from backend.app.models.knowledge import (
    Claim,
    SubjectScope,
    VerificationIssue,
    VerificationResult,
)
from backend.app.repositories.base import KnowledgeStore
from backend.app.retrieval.embeddings import cosine_similarity, tokenize
from backend.app.retrieval.providers import EmbeddingProvider, LocalHashEmbeddingProvider
from backend.app.security.acl import can_access_chunk
from backend.app.security.content import contains_prompt_injection


NEGATION_TERMS = ("不", "无", "未", "禁止", "不得", "不能", "never", "not", "no ")


class EvidenceVerifier:
    def __init__(
        self,
        store: KnowledgeStore,
        embedding_provider: EmbeddingProvider | None = None,
        min_support_score: float | None = None,
        min_coverage: float | None = None,
    ) -> None:
        self.store = store
        self.embedding_provider = embedding_provider or LocalHashEmbeddingProvider()
        self.min_support_score = (
            min_support_score
            if min_support_score is not None
            else float(os.getenv("KNOWLEDGE_VERIFIER_MIN_SUPPORT", "0.25"))
        )
        self.min_coverage = (
            min_coverage
            if min_coverage is not None
            else float(os.getenv("KNOWLEDGE_VERIFIER_MIN_COVERAGE", "1.0"))
        )

    def verify(
        self,
        claims: list[Claim],
        subject: SubjectScope,
    ) -> tuple[bool, str | None]:
        report = self.verify_report(claims, subject)
        return report.verified, report.reason

    def verify_report(
        self,
        claims: list[Claim],
        subject: SubjectScope,
    ) -> VerificationResult:
        if not claims:
            issue = VerificationIssue(
                code="claims_missing",
                message="没有可验证结论",
            )
            return VerificationResult(False, 0.0, [], [], [issue])

        issues: list[VerificationIssue] = []
        support_scores: list[float] = []
        conflicts: list[str] = []
        supported_claims = 0

        for claim_index, claim in enumerate(claims):
            chunks = []
            if contains_prompt_injection(claim.text):
                issues.append(
                    VerificationIssue(
                        code="prompt_injection_output",
                        message=f"结论包含疑似 Prompt Injection 内容: {claim.text}",
                        claim_index=claim_index,
                    )
                )
                support_scores.append(0.0)
                continue
            if not claim.citation_chunk_ids:
                issues.append(
                    VerificationIssue(
                        code="citation_missing",
                        message=f"结论缺少引用: {claim.text}",
                        claim_index=claim_index,
                    )
                )
                support_scores.append(0.0)
                continue

            for chunk_id in claim.citation_chunk_ids:
                chunk = self.store.get_chunk(chunk_id)
                if chunk is None:
                    issues.append(
                        VerificationIssue(
                            code="citation_not_found",
                            message=f"引用不存在: {chunk_id}",
                            claim_index=claim_index,
                            chunk_ids=(chunk_id,),
                        )
                    )
                    continue
                document = self.store.get_document(chunk.document_id)
                if document is None:
                    issues.append(
                        VerificationIssue(
                            code="document_not_found",
                            message=f"引用文档不存在: {chunk_id}",
                            claim_index=claim_index,
                            chunk_ids=(chunk_id,),
                        )
                    )
                    continue
                if not can_access_chunk(subject, chunk, document):
                    issues.append(
                        VerificationIssue(
                            code="citation_forbidden",
                            message=f"引用不可访问: {chunk_id}",
                            claim_index=claim_index,
                            chunk_ids=(chunk_id,),
                        )
                    )
                    continue
                current_version = self.store.get_current_version(chunk.document_id)
                if current_version is None or current_version.version_id != chunk.version_id:
                    issues.append(
                        VerificationIssue(
                            code="citation_stale",
                            message=f"引用版本已失效: {chunk_id}",
                            claim_index=claim_index,
                            chunk_ids=(chunk_id,),
                        )
                    )
                    continue
                if len(chunk.embedding) != self.embedding_provider.dimensions:
                    issues.append(
                        VerificationIssue(
                            code="embedding_dimension_mismatch",
                            message=f"引用向量维度与当前 provider 不一致: {chunk_id}",
                            claim_index=claim_index,
                            chunk_ids=(chunk_id,),
                        )
                    )
                    continue
                chunks.append(chunk)

            if not chunks:
                support_scores.append(0.0)
                continue

            claim_vector = self.embedding_provider.embed_query(claim.text)
            semantic_score = max(
                cosine_similarity(claim_vector, chunk.embedding) for chunk in chunks
            )
            lexical_score = max(
                lexical_support_score(claim.text, chunk.content) for chunk in chunks
            )
            support_score = max(0.0, min(1.0, max(semantic_score, lexical_score)))
            support_scores.append(round(support_score, 6))
            if support_score >= self.min_support_score:
                supported_claims += 1
            else:
                issues.append(
                    VerificationIssue(
                        code="claim_not_supported",
                        message=f"引用内容不足以支持结论: {claim.text}",
                        claim_index=claim_index,
                        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
                    )
                )

            claim_conflicts = detect_conflicts(claim, [chunk.content for chunk in chunks])
            for conflict in claim_conflicts:
                conflicts.append(conflict)
                issues.append(
                    VerificationIssue(
                        code="evidence_conflict",
                        message=conflict,
                        claim_index=claim_index,
                        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
                    )
                )

        coverage = supported_claims / len(claims)
        if coverage < self.min_coverage:
            issues.append(
                VerificationIssue(
                    code="insufficient_coverage",
                    message=(
                        f"证据覆盖度不足: {coverage:.2f}，"
                        f"要求至少 {self.min_coverage:.2f}"
                    ),
                )
            )
        return VerificationResult(
            verified=not issues,
            coverage=round(coverage, 6),
            support_scores=support_scores,
            conflicts=conflicts,
            issues=dedupe_issues(issues),
        )


def lexical_support_score(claim: str, evidence: str) -> float:
    claim_terms = set(tokenize(claim))
    if not claim_terms:
        return 0.0
    evidence_terms = set(tokenize(evidence))
    return len(claim_terms & evidence_terms) / len(claim_terms)


def detect_conflicts(claim: Claim, evidence_texts: list[str]) -> list[str]:
    if len(evidence_texts) < 2:
        return []
    conflicts: list[str] = []
    polarities = {contains_negation(text) for text in evidence_texts}
    if len(polarities) > 1:
        conflicts.append(f"引用证据存在肯定/否定冲突: {claim.text}")

    number_sets = [set(re.findall(r"\d+(?:\.\d+)?", text)) for text in evidence_texts]
    non_empty_numbers = [numbers for numbers in number_sets if numbers]
    if len(non_empty_numbers) >= 2:
        shared = set.intersection(*non_empty_numbers)
        if not shared:
            conflicts.append(f"引用证据存在数值冲突: {claim.text}")
    return conflicts


def contains_negation(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in NEGATION_TERMS)


def dedupe_issues(issues: list[VerificationIssue]) -> list[VerificationIssue]:
    result: list[VerificationIssue] = []
    seen: set[tuple[str, int | None, tuple[str, ...]]] = set()
    for issue in issues:
        key = (issue.code, issue.claim_index, issue.chunk_ids)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
