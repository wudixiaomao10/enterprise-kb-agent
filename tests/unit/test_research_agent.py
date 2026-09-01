from __future__ import annotations

import unittest

from backend.app.models.knowledge import (
    Citation,
    DocumentChunk,
    Evidence,
    KnowledgeAnswer,
    SubjectScope,
)
from backend.app.research.graph import LongRunningResearchAgent
from backend.app.research.models import ResearchAssessment, ResearchJobStatus
from backend.app.research.repository import InMemoryResearchJobRepository
from backend.app.research.service import (
    InlineResearchDispatcher,
    ResearchAuthorizationRevoked,
    ResearchJobService,
)


def make_evidence(chunk_id: str, content: str, score: float = 0.8) -> Evidence:
    chunk = DocumentChunk(
        chunk_id=chunk_id,
        document_id="doc_1",
        version_id="ver_1",
        page=1,
        section_path="policy",
        content=content,
        content_hash=f"hash_{chunk_id}",
        acl=[],
        embedding=[1.0, 0.0],
    )
    return Evidence(
        chunk=chunk,
        citation=Citation("doc_1", "ver_1", 1, chunk_id, "Policy", "policy"),
        score=score,
        keyword_score=score,
        vector_score=score,
        metadata_score=0.0,
        reranker_score=score,
    )


class FakeRetriever:
    def __init__(self):
        self.subjects = []

    def search(self, query, subject, limit=5):
        self.subjects.append(subject)
        if query == "alpha":
            return [make_evidence("chunk_a", "alpha evidence")]
        if query == "beta detail":
            return [make_evidence("chunk_b", "beta evidence")]
        return []


class FakeQA:
    def __init__(self):
        self.retriever = FakeRetriever()
        self.answer_subjects = []

    def answer_from_evidence(self, question, subject, evidence):
        self.answer_subjects.append(subject)
        return KnowledgeAnswer(
            answer="verified research answer",
            claims=[],
            citations=[item.citation for item in evidence],
            evidence=evidence,
            verified=True,
        )


class FakePlanner:
    def __init__(self):
        self.assessment_calls = 0

    def plan(self, question):
        return ["alpha", "beta"]

    def assess(self, question, subquestions, evidence, query_hits):
        self.assessment_calls += 1
        if self.assessment_calls == 1:
            return ResearchAssessment(0.5, ("alpha",), ("beta",), ())
        return ResearchAssessment(1.0, ("alpha", "beta"), (), ())

    def expand(self, question, gaps, attempted_queries):
        return ["beta detail"]


class LangGraphResearchTests(unittest.TestCase):
    def test_graph_expands_uncovered_question_and_deduplicates_evidence(self):
        progress = []
        agent = LongRunningResearchAgent(
            FakeQA(),
            FakePlanner(),
            progress_callback=lambda stage, value: progress.append((stage, value)),
        )

        state = agent.run(
            question="compare alpha and beta",
            subject=SubjectScope("u_1", ("sales",), ()),
            max_rounds=3,
            per_query_limit=5,
        )

        self.assertEqual(state["round"], 2)
        self.assertEqual({item.chunk.chunk_id for item in state["evidence"]}, {"chunk_a", "chunk_b"})
        self.assertEqual(state["assessment"].coverage, 1.0)
        self.assertTrue(state["answer"].verified)
        self.assertIn(("expanding_queries", 74), progress)

    def test_inline_research_job_persists_result_and_scope(self):
        repository = InMemoryResearchJobRepository()
        service = ResearchJobService(
            repository,
            FakeQA(),
            FakePlanner(),
            InlineResearchDispatcher(),
        )
        scope = SubjectScope("u_1", ("sales",), ("reader",))

        job = service.submit(
            question="compare alpha and beta",
            requested_by="u_1",
            subject=scope,
            identity_issuer="issuer",
            identity_subject="subject-1",
            max_rounds=3,
            per_query_limit=5,
        )

        self.assertEqual(job.status, ResearchJobStatus.COMPLETED)
        self.assertEqual(job.subject, scope)
        self.assertEqual(job.identity_issuer, "issuer")
        self.assertEqual(job.identity_subject, "subject-1")
        self.assertEqual(job.result["research"]["rounds"], 2)
        self.assertTrue(job.result["answer"]["verified"])

    def test_research_job_refreshes_subject_between_rounds(self):
        repository = InMemoryResearchJobRepository()
        qa = FakeQA()
        planner = FakePlanner()
        current_scope = {"value": SubjectScope("u_1", ("sales",), ("reader",))}

        original_assess = planner.assess

        def assess_and_revoke_sales(question, subquestions, evidence, query_hits):
            assessment = original_assess(question, subquestions, evidence, query_hits)
            current_scope["value"] = SubjectScope("u_1", ("hr",), ("reader",))
            return assessment

        planner.assess = assess_and_revoke_sales
        service = ResearchJobService(
            repository,
            qa,
            planner,
            InlineResearchDispatcher(),
            subject_resolver=lambda _job: current_scope["value"],
        )

        service.submit(
            question="compare alpha and beta",
            requested_by="u_1",
            subject=SubjectScope("u_1", ("sales",), ("reader",)),
            max_rounds=3,
            per_query_limit=5,
        )

        retrieved_scopes = [subject.department_ids for subject in qa.retriever.subjects]
        self.assertIn(("sales",), retrieved_scopes)
        self.assertIn(("hr",), retrieved_scopes)
        self.assertEqual(qa.answer_subjects[-1].department_ids, ("hr",))

    def test_research_job_cancels_when_authorization_is_revoked(self):
        repository = InMemoryResearchJobRepository()
        service = ResearchJobService(
            repository,
            FakeQA(),
            FakePlanner(),
            InlineResearchDispatcher(),
            subject_resolver=lambda _job: (_ for _ in ()).throw(
                ResearchAuthorizationRevoked("directory user disabled")
            ),
        )

        job = service.submit(
            question="compare alpha and beta",
            requested_by="u_1",
            subject=SubjectScope("u_1", ("sales",), ("reader",)),
            identity_issuer="issuer",
            identity_subject="subject-1",
            max_rounds=3,
            per_query_limit=5,
        )

        self.assertEqual(job.status, ResearchJobStatus.CANCELLED)
        self.assertEqual(job.stage, "authorization_revoked")
        self.assertEqual(job.result, {})
        self.assertEqual(job.error_message, "directory user disabled")


if __name__ == "__main__":
    unittest.main()
