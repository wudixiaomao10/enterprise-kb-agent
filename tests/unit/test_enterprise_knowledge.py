from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from backend.app.agent.citation_binder import CitationBinder
from backend.app.agent.claims import LLMClaimGenerator
from backend.app.agent.verifier import EvidenceVerifier, detect_conflicts
from backend.app.agent.qa import KnowledgeQAService
from backend.app.evaluation.retrieval_eval import (
    RetrievalEvalCase,
    run_retrieval_evaluation,
)
from backend.app.ingestion.service import DocumentIngestionService
from backend.app.models.knowledge import (
    ACLEntry,
    Claim,
    Permission,
    SubjectScope,
    SubjectType,
)
from backend.app.repositories.memory_store import InMemoryKnowledgeStore
from backend.app.repositories.postgres_store import (
    build_hybrid_search_query,
    build_schema_sql,
    vector_literal,
)
from backend.app.repositories.sqlite_store import SQLiteKnowledgeStore
from backend.app.retrieval.embeddings import cosine_similarity
from backend.app.retrieval.providers import OpenAIEmbeddingProvider
from backend.app.retrieval.rerankers import LLMReranker


class EnterpriseKnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage_dir = Path(".codex-tmp/unit-tests/knowledge")
        self.store = InMemoryKnowledgeStore()
        self.ingestion = DocumentIngestionService(
            self.store,
            self.storage_dir,
        )
        self.qa = KnowledgeQAService(self.store)

        self.ingestion.ingest(
            filename="product.md",
            raw_bytes=(
                "# 产品资料\n\n"
                "企业知识库 Agent 支持混合检索、权限感知 RAG 和确定性引用。"
            ).encode("utf-8"),
            title="产品资料",
            owner_id="u_sales",
            department_id="sales",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "sales", Permission.READ)],
        )
        self.ingestion.ingest(
            filename="hr.md",
            raw_bytes=(
                "# HR 制度\n\n"
                "员工入职满一年后可以享受年假。该制度仅允许 HR 部门访问。"
            ).encode("utf-8"),
            title="HR 制度",
            owner_id="u_hr",
            department_id="hr",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "hr", Permission.READ)],
        )

    def test_acl_filter_runs_before_retrieval(self) -> None:
        subject = SubjectScope(user_id="u_sales", department_ids=("sales",))

        evidence = self.qa.retriever.search("年假制度", subject)

        self.assertEqual(evidence, [])

    def test_answer_contains_verified_citations(self) -> None:
        subject = SubjectScope(user_id="u_sales", department_ids=("sales",))

        answer = self.qa.answer("企业知识库支持什么能力？", subject)

        self.assertTrue(answer.verified)
        self.assertGreaterEqual(len(answer.citations), 1)
        self.assertTrue(answer.citations[0].chunk_id.startswith("chk_"))
        self.assertEqual(answer.citations[0].title, "产品资料")

    def test_hr_can_access_hr_policy(self) -> None:
        subject = SubjectScope(user_id="u_hr", department_ids=("hr",))

        answer = self.qa.answer("年假制度是什么？", subject)

        self.assertTrue(answer.verified)
        self.assertEqual(answer.citations[0].title, "HR 制度")

    def test_admin_role_can_access_all_document_acls(self) -> None:
        subject = SubjectScope(user_id="entra-admin", role_ids=("admin",))

        documents = self.store.list_accessible_documents(subject)
        evidence = self.qa.retriever.search("年假制度", subject)

        self.assertEqual({item.title for item in documents}, {"产品资料", "HR 制度"})
        self.assertTrue(any(item.chunk.section_path == "HR 制度" for item in evidence))

    def test_detail_lookups_apply_document_and_chunk_acl(self) -> None:
        sales = SubjectScope(user_id="u_sales", department_ids=("sales",))
        documents = {item.title: item for item in self.store.list_documents()}
        product = documents["产品资料"]
        hr_policy = documents["HR 制度"]
        product_chunk = next(
            item
            for item in self.store.chunks.values()
            if item.document_id == product.document_id
        )
        hr_chunk = next(
            item
            for item in self.store.chunks.values()
            if item.document_id == hr_policy.document_id
        )

        self.assertEqual(
            self.store.get_accessible_document(product.document_id, sales),
            product,
        )
        self.assertIsNone(
            self.store.get_accessible_document(hr_policy.document_id, sales)
        )
        self.assertEqual(
            self.store.get_accessible_chunk(product_chunk.chunk_id, sales),
            product_chunk,
        )
        self.assertIsNone(self.store.get_accessible_chunk(hr_chunk.chunk_id, sales))
        self.assertEqual(
            len(
                self.store.list_accessible_document_versions(
                    product.document_id,
                    sales,
                )
            ),
            1,
        )
        self.assertEqual(
            self.store.list_accessible_document_versions(hr_policy.document_id, sales),
            [],
        )


class SQLiteKnowledgeStoreTests(unittest.TestCase):
    def test_sqlite_store_persists_documents_chunks_and_acl(self) -> None:
        base_dir = Path(".codex-tmp/unit-tests/sqlite")
        db_path = base_dir / f"{uuid.uuid4().hex}.db"
        storage_dir = base_dir / "documents"

        store = SQLiteKnowledgeStore(db_path)
        ingestion = DocumentIngestionService(store, storage_dir)
        ingestion.ingest(
            filename="persisted.md",
            raw_bytes=(
                "# 持久化\n\n"
                "企业知识库需要把 documents、versions、chunks 和 ACL 持久化。"
            ).encode("utf-8"),
            title="持久化设计",
            owner_id="u_platform",
            department_id="platform",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "platform", Permission.READ)],
        )

        reopened_store = SQLiteKnowledgeStore(db_path)
        reopened_qa = KnowledgeQAService(reopened_store)
        subject = SubjectScope(user_id="u_platform", department_ids=("platform",))

        answer = reopened_qa.answer("企业知识库需要持久化什么？", subject)

        self.assertEqual(len(reopened_store.list_documents()), 1)
        self.assertTrue(answer.verified)
        self.assertEqual(answer.citations[0].title, "持久化设计")

    def test_sqlite_store_keeps_acl_after_reopen(self) -> None:
        base_dir = Path(".codex-tmp/unit-tests/sqlite")
        db_path = base_dir / f"{uuid.uuid4().hex}.db"
        storage_dir = base_dir / "documents"

        store = SQLiteKnowledgeStore(db_path)
        ingestion = DocumentIngestionService(store, storage_dir)
        ingestion.ingest(
            filename="private.md",
            raw_bytes="# 私有制度\n\n只有法务可以查看合同审批规则。".encode("utf-8"),
            title="法务制度",
            owner_id="u_legal",
            department_id="legal",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "legal", Permission.READ)],
        )

        reopened_qa = KnowledgeQAService(SQLiteKnowledgeStore(db_path))
        subject = SubjectScope(user_id="u_sales", department_ids=("sales",))

        answer = reopened_qa.answer("合同审批规则是什么？", subject)

        self.assertFalse(answer.verified)
        self.assertEqual(answer.refusal_reason, "no_accessible_evidence")

    def test_reindex_replaces_current_version_chunks(self) -> None:
        base_dir = Path(".codex-tmp/unit-tests/reindex")
        db_path = base_dir / f"{uuid.uuid4().hex}.db"
        storage_dir = base_dir / "documents"
        store = SQLiteKnowledgeStore(db_path)
        ingestion = DocumentIngestionService(store, storage_dir)
        result = ingestion.ingest(
            filename="reindex.md",
            raw_bytes="# 旧内容\n\n旧索引内容。".encode("utf-8"),
            title="重建索引",
            owner_id="u_platform",
            department_id="platform",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "platform", Permission.READ)],
        )
        document_id = str(result["document_id"])
        version_id = str(result["version_id"])
        version = store.get_current_version(document_id)
        assert version is not None
        Path(version.storage_uri).write_text(
            "# 新内容\n\n重新索引后应该只包含新的 chunk 内容。",
            encoding="utf-8",
        )

        reindex_result = ingestion.reindex_document(document_id)
        chunks = [
            chunk
            for chunk in store.list_accessible_chunks(
                SubjectScope(user_id="u_platform", department_ids=("platform",))
            )
            if chunk.version_id == version_id
        ]

        self.assertEqual(reindex_result["status"], "reindexed")
        self.assertEqual(len(chunks), 1)
        self.assertIn("新的 chunk 内容", chunks[0].content)


class FixedEmbeddingProvider:
    name = "fixed-test"
    dimensions = 3

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, query: str) -> list[float]:
        return [1.0, 0.0, 0.0]


class FakeEmbeddingClient:
    def __init__(self) -> None:
        self.embeddings = self
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        dimensions = kwargs["dimensions"]
        data = [
            SimpleNamespace(index=index, embedding=[float(index + 1)] * dimensions)
            for index, _ in enumerate(kwargs["input"])
        ]
        return SimpleNamespace(
            data=data,
            usage=SimpleNamespace(total_tokens=len(kwargs["input"])),
        )


class FakeJSONLLM:
    name = "fake-json"
    model = "fake-model"

    def __init__(self, response: dict) -> None:
        self.response = response

    def generate_json(self, **kwargs) -> dict:
        return self.response


class EmbeddingProviderTests(unittest.TestCase):
    def test_ingestion_records_embedding_provider_metadata(self) -> None:
        store = InMemoryKnowledgeStore()
        ingestion = DocumentIngestionService(
            store,
            Path(".codex-tmp/unit-tests/provider"),
            embedding_provider=FixedEmbeddingProvider(),
        )

        ingestion.ingest(
            filename="provider.md",
            raw_bytes="# Provider\n\nEmbedding provider metadata must be stored.".encode(
                "utf-8"
            ),
            title="Provider",
            owner_id="u_platform",
            department_id="platform",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "platform", Permission.READ)],
        )

        chunk = next(iter(store.chunks.values()))
        self.assertEqual(chunk.embedding, [1.0, 0.0, 0.0])
        self.assertEqual(chunk.metadata["embedding_provider"], "fixed-test")
        self.assertEqual(chunk.metadata["embedding_dimensions"], 3)

    def test_cosine_similarity_rejects_dimension_mismatch(self) -> None:
        self.assertEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]), 0.0)

    def test_remote_provider_batches_and_validates_dimensions(self) -> None:
        client = FakeEmbeddingClient()
        provider = OpenAIEmbeddingProvider(
            api_key="test",
            model="text-embedding-3-small",
            dimensions=3,
            batch_size=2,
            client=client,
        )

        vectors = provider.embed_texts(["one", "two", "three"])

        self.assertEqual(len(vectors), 3)
        self.assertTrue(all(len(vector) == 3 for vector in vectors))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["dimensions"], 3)
        self.assertEqual(provider.last_input_tokens, 3)


class GroundedAnswerPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryKnowledgeStore()
        self.ingestion = DocumentIngestionService(
            self.store,
            Path(".codex-tmp/unit-tests/grounded-pipeline"),
        )
        self.ingestion.ingest(
            filename="policy.md",
            raw_bytes="# 年假\n\n员工入职满一年后可以享受年假。".encode("utf-8"),
            title="员工制度",
            owner_id="u_hr",
            department_id="hr",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "hr", Permission.READ)],
        )
        self.subject = SubjectScope(user_id="u_hr", department_ids=("hr",))
        self.qa = KnowledgeQAService(self.store)
        self.evidence = self.qa.retriever.search("入职一年后有什么假期？", self.subject)

    def test_structured_claim_is_bound_to_retrieved_chunk(self) -> None:
        chunk_id = self.evidence[0].chunk.chunk_id
        generator = LLMClaimGenerator(
            FakeJSONLLM(
                {
                    "summary": "制度结论",
                    "claims": [
                        {
                            "text": "员工入职满一年后可以享受年假。",
                            "citation_chunk_ids": [chunk_id],
                            "confidence": 0.98,
                        }
                    ],
                }
            )
        )

        draft = generator.generate("年假制度是什么？", self.evidence)
        binding = CitationBinder(self.store).bind(
            draft.claims,
            self.evidence,
            self.subject,
        )

        self.assertTrue(binding.valid)
        self.assertEqual(binding.citations[0].chunk_id, chunk_id)

    def test_citation_binder_rejects_hallucinated_chunk_id(self) -> None:
        binding = CitationBinder(self.store).bind(
            [Claim("员工可以享受年假。", ["chk_hallucinated"])],
            self.evidence,
            self.subject,
        )

        self.assertFalse(binding.valid)
        self.assertEqual(binding.issues[0].code, "citation_not_retrieved")

    def test_verifier_rejects_semantically_unsupported_claim(self) -> None:
        chunk_id = self.evidence[0].chunk.chunk_id
        verifier = EvidenceVerifier(
            self.store,
            min_support_score=0.9,
        )

        report = verifier.verify_report(
            [Claim("The Mars office has free parking.", [chunk_id])],
            self.subject,
        )

        self.assertFalse(report.verified)
        self.assertIn("claim_not_supported", [issue.code for issue in report.issues])

    def test_llm_reranker_ignores_unknown_candidate_ids(self) -> None:
        valid_id = self.evidence[0].chunk.chunk_id
        reranker = LLMReranker(
            FakeJSONLLM(
                {
                    "rankings": [
                        {"chunk_id": "chk_not_retrieved", "score": 1.0},
                        {"chunk_id": valid_id, "score": 0.9},
                    ]
                }
            )
        )

        ranked = reranker.rerank("年假", self.evidence, 5)

        self.assertEqual(ranked[0].chunk.chunk_id, valid_id)

    def test_conflict_detector_flags_polarity_and_numbers(self) -> None:
        conflicts = detect_conflicts(
            Claim("试用期规则", ["one", "two"]),
            ["试用期为 3 个月，可以提前转正。", "试用期不得少于 6 个月。"],
        )

        self.assertGreaterEqual(len(conflicts), 2)


class PostgresHybridSearchTests(unittest.TestCase):
    def test_admin_role_uses_explicit_pre_retrieval_bypass(self) -> None:
        sql, params = build_hybrid_search_query(
            query="全部制度",
            query_embedding=[1.0, 0.0, 0.5],
            subject=SubjectScope(user_id="entra-admin", role_ids=("admin",)),
            limit=10,
            min_score=0.05,
        )

        self.assertIn("AND (TRUE)", sql)
        self.assertNotIn("acl_user_id", params)

    def test_hybrid_search_sql_uses_full_text_vector_and_acl_first_filter(self) -> None:
        subject = SubjectScope(
            user_id="u_1",
            department_ids=("sales",),
            role_ids=("manager",),
        )

        sql, params = build_hybrid_search_query(
            query="权限感知 RAG",
            query_embedding=[1.0, 0.0, 0.5],
            subject=subject,
            limit=10,
            min_score=0.05,
        )

        self.assertIn("WITH accessible_chunks AS", sql)
        self.assertIn("ts_rank_cd", sql)
        self.assertIn("plainto_tsquery", sql)
        self.assertIn("<=> %(embedding)s::vector", sql)
        self.assertIn("jsonb_array_elements(c.acl_json)", sql)
        self.assertIn("jsonb_array_elements(d.acl_json)", sql)
        self.assertIn("WHERE v.is_current = true", sql)
        self.assertEqual(params["embedding"], "[1,0,0.5]")
        self.assertEqual(params["acl_user_id"], "u_1")
        self.assertEqual(params["acl_department_ids"], ["sales"])
        self.assertEqual(params["acl_role_ids"], ["manager"])

    def test_schema_sql_uses_configured_vector_dimensions(self) -> None:
        sql = build_schema_sql(3)

        self.assertIn("embedding VECTOR(3)", sql)
        self.assertIn("USING ivfflat", sql)

    def test_vector_literal_formats_pgvector_input(self) -> None:
        self.assertEqual(vector_literal([1.0, 0.25, -2.0]), "[1,0.25,-2]")


class RetrievalEvaluationTests(unittest.TestCase):
    def test_retrieval_eval_reports_permission_leak_rate(self) -> None:
        store = InMemoryKnowledgeStore()
        ingestion = DocumentIngestionService(
            store,
            Path(".codex-tmp/unit-tests/eval"),
        )
        qa = KnowledgeQAService(store)
        ingestion.ingest(
            filename="product.md",
            raw_bytes="# 产品资料\n\n企业知识库支持确定性引用。".encode("utf-8"),
            title="产品资料",
            owner_id="u_sales",
            department_id="sales",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "sales", Permission.READ)],
        )
        ingestion.ingest(
            filename="hr.md",
            raw_bytes="# HR 制度\n\n员工入职满一年后可以享受年假。".encode("utf-8"),
            title="HR 制度",
            owner_id="u_hr",
            department_id="hr",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "hr", Permission.READ)],
        )

        report = run_retrieval_evaluation(
            qa,
            [
                RetrievalEvalCase(
                    id="product",
                    question="企业知识库支持什么？",
                    user_id="u_sales",
                    department_ids=("sales",),
                    expected_document_titles=("产品资料",),
                ),
                RetrievalEvalCase(
                    id="hr_refusal",
                    question="年假制度是什么？",
                    user_id="u_sales",
                    department_ids=("sales",),
                    expected_document_titles=(),
                    should_refuse=True,
                ),
            ],
        )

        self.assertEqual(report["case_count"], 2)
        self.assertEqual(report["permission_leak_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
