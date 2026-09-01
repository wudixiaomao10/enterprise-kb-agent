from __future__ import annotations

import os
from collections.abc import Callable
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.app.agent.qa import KnowledgeQAService
from backend.app.models.knowledge import Evidence, KnowledgeAnswer, SubjectScope
from backend.app.observability import observed_span
from backend.app.research.models import ResearchAssessment
from backend.app.research.planner import ResearchPlanner


ProgressCallback = Callable[[str, int], None]
CancelCheck = Callable[[], bool]
SubjectRefresh = Callable[[], SubjectScope]


class ResearchCancelled(RuntimeError):
    pass


class ResearchState(TypedDict, total=False):
    question: str
    subject: SubjectScope
    max_rounds: int
    per_query_limit: int
    round: int
    subquestions: list[str]
    pending_queries: list[str]
    attempted_queries: list[str]
    query_hits: dict[str, int]
    evidence: list[Evidence]
    assessment: ResearchAssessment
    answer: KnowledgeAnswer


class LongRunningResearchAgent:
    def __init__(
        self,
        qa: KnowledgeQAService,
        planner: ResearchPlanner,
        *,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
        subject_refresh: SubjectRefresh | None = None,
        checkpointer: object | None = None,
    ) -> None:
        self.qa = qa
        self.planner = planner
        self.progress_callback = progress_callback or (lambda _stage, _progress: None)
        self.cancel_check = cancel_check or (lambda: False)
        self.subject_refresh = subject_refresh
        self.checkpointer = checkpointer
        self.min_evidence_score = float(
            os.getenv("KNOWLEDGE_RESEARCH_MIN_EVIDENCE_SCORE", "0.08")
        )
        self.target_coverage = float(
            os.getenv("KNOWLEDGE_RESEARCH_TARGET_COVERAGE", "1.0")
        )
        self.max_evidence = int(os.getenv("KNOWLEDGE_RESEARCH_MAX_EVIDENCE", "24"))
        self.graph = self._build_graph()

    def run(
        self,
        *,
        question: str,
        subject: SubjectScope,
        thread_id: str | None = None,
        max_rounds: int = 3,
        per_query_limit: int = 5,
    ) -> ResearchState:
        initial: ResearchState = {
            "question": question,
            "subject": subject,
            "max_rounds": max(1, min(max_rounds, 5)),
            "per_query_limit": max(2, min(per_query_limit, 10)),
            "round": 0,
            "subquestions": [],
            "pending_queries": [],
            "attempted_queries": [],
            "query_hits": {},
            "evidence": [],
        }
        config: dict[str, object] = {"recursion_limit": 20}
        if thread_id:
            config["configurable"] = {"thread_id": thread_id[:255]}
        return self.graph.invoke(initial, config)

    def _build_graph(self):
        builder = StateGraph(ResearchState)
        builder.add_node("plan", self._plan)
        builder.add_node("retrieve", self._retrieve)
        builder.add_node("assess", self._assess)
        builder.add_node("expand", self._expand)
        builder.add_node("synthesize", self._synthesize)
        builder.add_edge(START, "plan")
        builder.add_edge("plan", "retrieve")
        builder.add_edge("retrieve", "assess")
        builder.add_conditional_edges(
            "assess",
            self._route_after_assessment,
            {"expand": "expand", "synthesize": "synthesize"},
        )
        builder.add_conditional_edges(
            "expand",
            self._route_after_expansion,
            {"retrieve": "retrieve", "synthesize": "synthesize"},
        )
        builder.add_edge("synthesize", END)
        if self.checkpointer is not None:
            return builder.compile(checkpointer=self.checkpointer)
        return builder.compile()

    def _plan(self, state: ResearchState) -> dict[str, object]:
        with observed_span("research.stage.plan", stage="research.plan"):
            return self._plan_stage(state)

    def _plan_stage(self, state: ResearchState) -> dict[str, object]:
        self._checkpoint("planning", 10)
        subject = self._current_subject(state["subject"])
        subquestions = self.planner.plan(state["question"])
        return {
            "subject": subject,
            "subquestions": subquestions,
            "pending_queries": subquestions,
        }

    def _retrieve(self, state: ResearchState) -> dict[str, object]:
        with observed_span("research.stage.retrieve", stage="research.retrieve"):
            return self._retrieve_stage(state)

    def _retrieve_stage(self, state: ResearchState) -> dict[str, object]:
        next_round = state.get("round", 0) + 1
        max_rounds = state["max_rounds"]
        progress = 18 + int((next_round / max_rounds) * 42)
        self._checkpoint(f"retrieving_round_{next_round}", min(progress, 60))
        subject = self._current_subject(state["subject"])
        existing = {
            item.chunk.chunk_id: item
            for item in self._filter_accessible_evidence(
                state.get("evidence", []),
                subject,
            )
        }
        query_hits = dict(state.get("query_hits", {}))
        attempted = list(state.get("attempted_queries", []))
        for query in state.get("pending_queries", []):
            self._check_cancelled()
            results = self.qa.retriever.search(
                query,
                subject,
                limit=state["per_query_limit"],
            )
            accepted = [item for item in results if evidence_score(item) >= self.min_evidence_score]
            query_hits[query] = len(accepted)
            attempted.append(query)
            for item in accepted:
                current = existing.get(item.chunk.chunk_id)
                if current is None or evidence_score(item) > evidence_score(current):
                    existing[item.chunk.chunk_id] = item
        evidence = sorted(existing.values(), key=evidence_score, reverse=True)[: self.max_evidence]
        return {
            "subject": subject,
            "round": next_round,
            "evidence": evidence,
            "query_hits": query_hits,
            "attempted_queries": list(dict.fromkeys(attempted)),
            "pending_queries": [],
        }

    def _assess(self, state: ResearchState) -> dict[str, object]:
        with observed_span("research.stage.assess", stage="research.assess"):
            return self._assess_stage(state)

    def _assess_stage(self, state: ResearchState) -> dict[str, object]:
        self._checkpoint("checking_coverage", 68)
        subject = self._current_subject(state["subject"])
        evidence = self._filter_accessible_evidence(state.get("evidence", []), subject)
        assessment = self.planner.assess(
            state["question"],
            state["subquestions"],
            evidence,
            state.get("query_hits", {}),
        )
        return {"subject": subject, "evidence": evidence, "assessment": assessment}

    def _expand(self, state: ResearchState) -> dict[str, object]:
        with observed_span("research.stage.expand", stage="research.expand"):
            return self._expand_stage(state)

    def _expand_stage(self, state: ResearchState) -> dict[str, object]:
        self._checkpoint("expanding_queries", 74)
        subject = self._current_subject(state["subject"])
        assessment = state["assessment"]
        queries = self.planner.expand(
            state["question"],
            list(assessment.gaps),
            state.get("attempted_queries", []),
        )
        return {"subject": subject, "pending_queries": queries}

    def _synthesize(self, state: ResearchState) -> dict[str, object]:
        with observed_span("research.stage.synthesize", stage="research.synthesize"):
            return self._synthesize_stage(state)

    def _synthesize_stage(self, state: ResearchState) -> dict[str, object]:
        self._checkpoint("binding_citations", 86)
        subject = self._current_subject(state["subject"])
        evidence = self._filter_accessible_evidence(state.get("evidence", []), subject)
        answer = self.qa.answer_from_evidence(
            state["question"],
            subject,
            evidence,
        )
        self._checkpoint("verifying_evidence", 94)
        return {"subject": subject, "evidence": evidence, "answer": answer}

    def _route_after_assessment(
        self, state: ResearchState
    ) -> Literal["expand", "synthesize"]:
        assessment = state["assessment"]
        if (
            assessment.coverage < self.target_coverage
            and assessment.gaps
            and state["round"] < state["max_rounds"]
        ):
            return "expand"
        return "synthesize"

    @staticmethod
    def _route_after_expansion(
        state: ResearchState,
    ) -> Literal["retrieve", "synthesize"]:
        return "retrieve" if state.get("pending_queries") else "synthesize"

    def _checkpoint(self, stage: str, progress: int) -> None:
        self._check_cancelled()
        self.progress_callback(stage, progress)

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise ResearchCancelled("research job cancelled")

    def _current_subject(self, fallback: SubjectScope) -> SubjectScope:
        self._check_cancelled()
        if self.subject_refresh is None:
            return fallback
        return self.subject_refresh()

    def _filter_accessible_evidence(
        self,
        evidence: list[Evidence],
        subject: SubjectScope,
    ) -> list[Evidence]:
        store = getattr(self.qa, "store", None)
        if store is None:
            return list(evidence)
        filtered: list[Evidence] = []
        for item in evidence:
            chunk = store.get_accessible_chunk(item.chunk.chunk_id, subject)
            if chunk is None:
                continue
            current_version = store.get_current_version(chunk.document_id)
            if current_version is None or current_version.version_id != chunk.version_id:
                continue
            filtered.append(
                Evidence(
                    chunk=chunk,
                    citation=store.citation_for_chunk(chunk),
                    score=item.score,
                    keyword_score=item.keyword_score,
                    vector_score=item.vector_score,
                    metadata_score=item.metadata_score,
                    reranker_score=item.reranker_score,
                )
            )
        return filtered


def evidence_score(item: Evidence) -> float:
    return item.reranker_score if item.reranker_score is not None else item.score


__all__ = ["LongRunningResearchAgent", "ResearchCancelled", "ResearchState"]
