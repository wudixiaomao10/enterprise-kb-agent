from __future__ import annotations

from dataclasses import dataclass

from backend.app.models.knowledge import (
    Citation,
    Claim,
    Evidence,
    SubjectScope,
    VerificationIssue,
)
from backend.app.repositories.base import KnowledgeStore
from backend.app.security.acl import can_access_chunk


@dataclass(frozen=True)
class CitationBindingResult:
    claims: list[Claim]
    citations: list[Citation]
    issues: list[VerificationIssue]

    @property
    def valid(self) -> bool:
        return not self.issues and bool(self.claims)


class CitationBinder:
    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def bind(
        self,
        claims: list[Claim],
        evidence: list[Evidence],
        subject: SubjectScope,
    ) -> CitationBindingResult:
        retrieved = {item.chunk.chunk_id: item for item in evidence}
        citations: list[Citation] = []
        issues: list[VerificationIssue] = []

        for claim_index, claim in enumerate(claims):
            if not claim.citation_chunk_ids:
                issues.append(
                    VerificationIssue(
                        code="citation_missing",
                        message=f"结论缺少引用: {claim.text}",
                        claim_index=claim_index,
                    )
                )
                continue
            for chunk_id in claim.citation_chunk_ids:
                item = retrieved.get(chunk_id)
                if item is None:
                    issues.append(
                        VerificationIssue(
                            code="citation_not_retrieved",
                            message=f"引用不在本次检索证据中: {chunk_id}",
                            claim_index=claim_index,
                            chunk_ids=(chunk_id,),
                        )
                    )
                    continue
                chunk = self.store.get_chunk(chunk_id)
                document = self.store.get_document(item.chunk.document_id)
                current_version = self.store.get_current_version(item.chunk.document_id)
                if chunk is None or document is None:
                    issues.append(
                        VerificationIssue(
                            code="citation_not_found",
                            message=f"引用不存在: {chunk_id}",
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
                citation = self.store.citation_for_chunk(chunk)
                if citation not in citations:
                    citations.append(citation)

        return CitationBindingResult(
            claims=claims,
            citations=citations,
            issues=issues,
        )


def render_cited_answer(
    summary: str,
    claims: list[Claim],
    citations: list[Citation],
) -> str:
    citation_numbers = {
        citation.chunk_id: index
        for index, citation in enumerate(citations, start=1)
    }
    lines = [summary]
    for index, claim in enumerate(claims, start=1):
        markers = "".join(
            f"[{citation_numbers[chunk_id]}]"
            for chunk_id in claim.citation_chunk_ids
            if chunk_id in citation_numbers
        )
        lines.append(f"{index}. {claim.text} {markers}".rstrip())
    lines.append("")
    for index, citation in enumerate(citations, start=1):
        lines.append(
            f"[{index}] {citation.title}，version={citation.version_id}，"
            f"第 {citation.page} 页，chunk={citation.chunk_id}"
        )
    return "\n".join(lines)
