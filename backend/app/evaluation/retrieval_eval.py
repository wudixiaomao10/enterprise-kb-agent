from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.app.agent.qa import KnowledgeQAService
from backend.app.models.knowledge import SubjectScope


@dataclass(frozen=True)
class RetrievalEvalCase:
    id: str
    question: str
    user_id: str
    department_ids: tuple[str, ...]
    expected_document_titles: tuple[str, ...]
    should_refuse: bool = False
    k: int = 5


@dataclass(frozen=True)
class RetrievalEvalResult:
    case_id: str
    passed: bool
    recall_at_k: float
    citation_precision: float
    permission_leak: bool
    returned_titles: tuple[str, ...]
    refusal_reason: str | None


def load_eval_cases(path: Path | str) -> list[RetrievalEvalCase]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        RetrievalEvalCase(
            id=item["id"],
            question=item["question"],
            user_id=item["user_id"],
            department_ids=tuple(item.get("department_ids", [])),
            expected_document_titles=tuple(item.get("expected_document_titles", [])),
            should_refuse=bool(item.get("should_refuse", False)),
            k=int(item.get("k", 5)),
        )
        for item in data
    ]


def run_retrieval_evaluation(
    qa: KnowledgeQAService,
    cases: list[RetrievalEvalCase],
) -> dict[str, Any]:
    results = [evaluate_case(qa, case) for case in cases]
    total = len(results)
    return {
        "case_count": total,
        "passed": sum(1 for result in results if result.passed),
        "recall_at_k": average(result.recall_at_k for result in results),
        "citation_precision": average(result.citation_precision for result in results),
        "permission_leak_rate": average(
            1.0 if result.permission_leak else 0.0 for result in results
        ),
        "results": [
            {
                "case_id": result.case_id,
                "passed": result.passed,
                "recall_at_k": result.recall_at_k,
                "citation_precision": result.citation_precision,
                "permission_leak": result.permission_leak,
                "returned_titles": list(result.returned_titles),
                "refusal_reason": result.refusal_reason,
            }
            for result in results
        ],
    }


def evaluate_case(
    qa: KnowledgeQAService,
    case: RetrievalEvalCase,
) -> RetrievalEvalResult:
    subject = SubjectScope(
        user_id=case.user_id,
        department_ids=case.department_ids,
    )
    answer = qa.answer(case.question, subject, limit=case.k)
    returned_titles = tuple(citation.title for citation in answer.citations)
    expected_titles = set(case.expected_document_titles)
    returned_set = set(returned_titles)

    if case.should_refuse:
        permission_leak = bool(returned_titles)
        passed = not answer.verified and not permission_leak
        return RetrievalEvalResult(
            case_id=case.id,
            passed=passed,
            recall_at_k=1.0 if passed else 0.0,
            citation_precision=1.0 if not returned_titles else 0.0,
            permission_leak=permission_leak,
            returned_titles=returned_titles,
            refusal_reason=answer.refusal_reason,
        )

    matched = len(expected_titles & returned_set)
    recall = matched / max(len(expected_titles), 1)
    precision = matched / max(len(returned_set), 1)
    passed = recall >= 1.0 and precision >= 1.0 and answer.verified
    return RetrievalEvalResult(
        case_id=case.id,
        passed=passed,
        recall_at_k=recall,
        citation_precision=precision,
        permission_leak=False,
        returned_titles=returned_titles,
        refusal_reason=answer.refusal_reason,
    )


def average(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)

