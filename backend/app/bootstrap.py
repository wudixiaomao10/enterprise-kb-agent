from __future__ import annotations

import os
from pathlib import Path

from backend.app.agent.claims import ExtractiveClaimGenerator, LLMClaimGenerator
from backend.app.agent.qa import KnowledgeQAService
from backend.app.database import ensure_database_schema
from backend.app.ingestion.service import DocumentIngestionService
from backend.app.identity.directory import (
    DirectoryMembership,
    DirectorySyncSnapshot,
    DirectoryUnit,
    DirectoryUser,
    IdentityDirectory,
    InMemoryIdentityDirectory,
    PostgresIdentityDirectory,
)
from backend.app.identity.feishu import FeishuConfig, FeishuSyncService
from backend.app.identity.microsoft_graph import (
    MicrosoftGraphConfig,
    MicrosoftGraphSyncService,
)
from backend.app.identity.provisioning import PostgresIdentityProvisioningStore
from backend.app.jobs.dlq import (
    DeadLetterQueue,
    InMemoryDeadLetterQueue,
    PostgresDeadLetterQueue,
)
from backend.app.jobs.repository import (
    InMemoryIndexJobRepository,
    PostgresIndexJobRepository,
)
from backend.app.jobs.service import (
    DeferredInlineDispatcher,
    IndexJobService,
    create_job_dispatcher,
)
from backend.app.llm.providers import create_json_llm
from backend.app.models.knowledge import ACLEntry, Permission, SubjectType
from backend.app.repositories.base import KnowledgeStore
from backend.app.repositories.memory_store import InMemoryKnowledgeStore
from backend.app.repositories.postgres_store import PostgresKnowledgeStore
from backend.app.repositories.sqlite_store import SQLiteKnowledgeStore
from backend.app.research.checkpointing import (
    PostgresResearchCheckpointerFactory,
    research_checkpoints_enabled,
)
from backend.app.research.planner import ResearchPlanner
from backend.app.research.repository import (
    InMemoryResearchJobRepository,
    PostgresResearchJobRepository,
)
from backend.app.research.service import (
    DeferredResearchDispatcher,
    DirectoryResearchSubjectResolver,
    DramatiqResearchDispatcher,
    InlineResearchDispatcher,
    ResearchJobService,
)
from backend.app.retrieval.providers import EmbeddingProvider, create_embedding_provider
from backend.app.retrieval.rerankers import create_reranker
from backend.app.storage.object_store import create_object_storage


def create_demo_services(storage_dir: Path | None = None) -> tuple[
    KnowledgeStore,
    DocumentIngestionService,
    KnowledgeQAService,
]:
    embedding_provider = create_embedding_provider()
    json_llm = create_json_llm()
    reranker = create_reranker(json_llm)
    claim_generator = (
        LLMClaimGenerator(json_llm)
        if json_llm is not None
        else ExtractiveClaimGenerator()
    )
    store = create_store(embedding_provider)
    resolved_storage_dir = storage_dir or Path(".codex-tmp/knowledge-documents")
    ingestion = DocumentIngestionService(
        store=store,
        storage_dir=resolved_storage_dir,
        embedding_provider=embedding_provider,
        object_storage=create_object_storage(resolved_storage_dir),
    )
    qa = KnowledgeQAService(
        store,
        embedding_provider,
        reranker=reranker,
        claim_generator=claim_generator,
    )
    if not store.list_documents():
        seed_demo_documents(ingestion)
    return store, ingestion, qa


def create_store(embedding_provider: EmbeddingProvider | None = None) -> KnowledgeStore:
    store_kind = os.getenv("KNOWLEDGE_STORE", "sqlite").lower()
    if store_kind == "memory":
        return InMemoryKnowledgeStore()
    if store_kind == "postgres":
        dsn = os.getenv("KNOWLEDGE_DATABASE_URL")
        if not dsn:
            raise RuntimeError(
                "KNOWLEDGE_DATABASE_URL is required when KNOWLEDGE_STORE=postgres"
            )
        vector_dimensions = (
            embedding_provider.dimensions if embedding_provider is not None else 1536
        )
        ensure_database_schema(dsn, vector_dimensions)
        store = PostgresKnowledgeStore(
            dsn,
            vector_dimensions=vector_dimensions,
            initialize_schema=False,
        )
        store.validate_schema()
        return store
    db_path = Path(os.getenv("KNOWLEDGE_DB_PATH", ".codex-tmp/knowledge.db"))
    return SQLiteKnowledgeStore(db_path)


def create_index_job_service(
    store: KnowledgeStore,
    ingestion: DocumentIngestionService,
    dlq: DeadLetterQueue | None = None,
) -> IndexJobService:
    if isinstance(store, PostgresKnowledgeStore):
        repository = PostgresIndexJobRepository(store.dsn)
    else:
        repository = InMemoryIndexJobRepository()
    return IndexJobService(repository, ingestion, create_job_dispatcher(), dlq=dlq)


def create_research_job_service(
    store: KnowledgeStore,
    qa: KnowledgeQAService,
    identity_directory: IdentityDirectory | None = None,
    dlq: DeadLetterQueue | None = None,
) -> ResearchJobService:
    repository = (
        PostgresResearchJobRepository(store.dsn)
        if isinstance(store, PostgresKnowledgeStore)
        else InMemoryResearchJobRepository()
    )
    mode = os.getenv("KNOWLEDGE_JOB_MODE", "inline").strip().lower()
    if mode == "inline":
        dispatcher = InlineResearchDispatcher()
    elif mode in {"dramatiq", "redis"}:
        dispatcher = DramatiqResearchDispatcher()
    else:
        raise RuntimeError("Unsupported KNOWLEDGE_JOB_MODE. Use inline or dramatiq.")
    subject_resolver = (
        DirectoryResearchSubjectResolver(identity_directory)
        if identity_directory is not None
        and os.getenv("KNOWLEDGE_IDENTITY_MODE", "claims").strip().lower() == "directory"
        else None
    )
    checkpointer_factory = (
        PostgresResearchCheckpointerFactory(
            store.dsn,
            setup=os.getenv("KNOWLEDGE_RESEARCH_CHECKPOINT_SETUP", "1") != "0",
            delete_on_terminal=(
                os.getenv("KNOWLEDGE_RESEARCH_CHECKPOINT_DELETE_ON_TERMINAL", "1")
                != "0"
            ),
        )
        if isinstance(store, PostgresKnowledgeStore)
        and research_checkpoints_enabled(default=True)
        else None
    )
    return ResearchJobService(
        repository,
        qa,
        ResearchPlanner(create_json_llm()),
        dispatcher,
        subject_resolver=subject_resolver,
        checkpointer_factory=checkpointer_factory,
        dlq=dlq,
    )


def create_identity_directory(store: KnowledgeStore) -> IdentityDirectory:
    directory: IdentityDirectory
    if isinstance(store, PostgresKnowledgeStore):
        directory = PostgresIdentityDirectory(store.dsn)
    else:
        directory = InMemoryIdentityDirectory()
    if (
        os.getenv("KNOWLEDGE_IDENTITY_MODE", "claims").lower() == "directory"
        and os.getenv("KNOWLEDGE_AUTH_MODE", "local").lower() == "local"
    ):
        seed_local_identity_directory(directory)
    return directory


def create_identity_provisioning_store(
    store: KnowledgeStore,
) -> PostgresIdentityProvisioningStore | None:
    if not isinstance(store, PostgresKnowledgeStore):
        return None
    return PostgresIdentityProvisioningStore(store.dsn)


def create_dead_letter_queue(store: KnowledgeStore) -> DeadLetterQueue:
    if isinstance(store, PostgresKnowledgeStore):
        return PostgresDeadLetterQueue(store.dsn)
    return InMemoryDeadLetterQueue()


def create_microsoft_graph_sync_service(
    provisioning_store: PostgresIdentityProvisioningStore | None,
) -> MicrosoftGraphSyncService | None:
    if provisioning_store is None:
        return None
    return MicrosoftGraphSyncService(
        MicrosoftGraphConfig.from_env(), provisioning_store
    )


def create_feishu_sync_service(
    directory: IdentityDirectory,
    provisioning_store: PostgresIdentityProvisioningStore | None,
) -> FeishuSyncService:
    return FeishuSyncService(
        FeishuConfig.from_env(),
        directory,
        provisioning_store=provisioning_store,
    )


def seed_local_identity_directory(directory: IdentityDirectory) -> None:
    issuer = os.getenv("KNOWLEDGE_JWT_ISSUER", "enterprise-kb-agent")
    if directory.resolve_user(issuer, "u_admin") is not None:
        return
    users = tuple(
        DirectoryUser(
            external_id=user_id,
            user_id=user_id,
            subject=user_id,
            issuer=issuer,
            display_name=display_name,
        )
        for user_id, display_name in [
            ("u_admin", "Local Administrator"),
            ("u_sales", "Sales User"),
            ("u_hr", "HR User"),
            ("u_finance", "Finance User"),
        ]
    )
    departments = tuple(
        DirectoryUnit(external_id=item, unit_id=item, name=item)
        for item in ["sales", "hr", "finance", "platform"]
    )
    roles = (DirectoryUnit(external_id="admin", unit_id="admin", name="admin"),)
    directory.sync(
        DirectorySyncSnapshot(
            source="local-dev",
            users=users,
            departments=departments,
            roles=roles,
            user_departments=(
                DirectoryMembership("u_admin", "platform"),
                DirectoryMembership("u_admin", "sales"),
                DirectoryMembership("u_admin", "hr"),
                DirectoryMembership("u_admin", "finance"),
                DirectoryMembership("u_sales", "sales"),
                DirectoryMembership("u_hr", "hr"),
                DirectoryMembership("u_finance", "finance"),
            ),
            user_roles=(DirectoryMembership("u_admin", "admin"),),
            deactivate_missing=False,
        )
    )


def create_worker_job_service() -> IndexJobService:
    embedding_provider = create_embedding_provider()
    store = create_store(embedding_provider)
    storage_dir = Path(
        os.getenv("KNOWLEDGE_STORAGE_DIR", ".codex-tmp/knowledge-documents")
    )
    ingestion = DocumentIngestionService(
        store=store,
        storage_dir=storage_dir,
        embedding_provider=embedding_provider,
        object_storage=create_object_storage(storage_dir),
    )
    repository = (
        PostgresIndexJobRepository(store.dsn)
        if isinstance(store, PostgresKnowledgeStore)
        else InMemoryIndexJobRepository()
    )
    return IndexJobService(
        repository,
        ingestion,
        DeferredInlineDispatcher(),
        dlq=create_dead_letter_queue(store),
    )


def create_worker_graph_sync_service() -> MicrosoftGraphSyncService:
    embedding_provider = create_embedding_provider()
    store = create_store(embedding_provider)
    provisioning_store = create_identity_provisioning_store(store)
    service = create_microsoft_graph_sync_service(provisioning_store)
    if service is None:
        raise RuntimeError("Microsoft Graph provisioning requires PostgreSQL")
    return service


def create_worker_feishu_sync_service() -> FeishuSyncService:
    embedding_provider = create_embedding_provider()
    store = create_store(embedding_provider)
    directory = create_identity_directory(store)
    provisioning_store = create_identity_provisioning_store(store)
    return create_feishu_sync_service(directory, provisioning_store)


def create_worker_research_job_service() -> ResearchJobService:
    embedding_provider = create_embedding_provider()
    json_llm = create_json_llm()
    reranker = create_reranker(json_llm)
    claim_generator = (
        LLMClaimGenerator(json_llm)
        if json_llm is not None
        else ExtractiveClaimGenerator()
    )
    store = create_store(embedding_provider)
    qa = KnowledgeQAService(
        store,
        embedding_provider,
        reranker=reranker,
        claim_generator=claim_generator,
    )
    repository = (
        PostgresResearchJobRepository(store.dsn)
        if isinstance(store, PostgresKnowledgeStore)
        else InMemoryResearchJobRepository()
    )
    identity_directory = create_identity_directory(store)
    subject_resolver = (
        DirectoryResearchSubjectResolver(identity_directory)
        if os.getenv("KNOWLEDGE_IDENTITY_MODE", "claims").strip().lower() == "directory"
        else None
    )
    checkpointer_factory = (
        PostgresResearchCheckpointerFactory(
            store.dsn,
            setup=os.getenv("KNOWLEDGE_RESEARCH_CHECKPOINT_SETUP", "1") != "0",
            delete_on_terminal=(
                os.getenv("KNOWLEDGE_RESEARCH_CHECKPOINT_DELETE_ON_TERMINAL", "1")
                != "0"
            ),
        )
        if isinstance(store, PostgresKnowledgeStore)
        and research_checkpoints_enabled(default=True)
        else None
    )
    return ResearchJobService(
        repository,
        qa,
        ResearchPlanner(json_llm),
        DeferredResearchDispatcher(),
        subject_resolver=subject_resolver,
        checkpointer_factory=checkpointer_factory,
        dlq=create_dead_letter_queue(store),
    )


def seed_demo_documents(ingestion: DocumentIngestionService) -> None:
    public_acl = [
        ACLEntry(SubjectType.DEPARTMENT, "sales", Permission.READ),
        ACLEntry(SubjectType.DEPARTMENT, "hr", Permission.READ),
    ]
    hr_acl = [ACLEntry(SubjectType.DEPARTMENT, "hr", Permission.READ)]

    ingestion.ingest(
        filename="product-handbook.md",
        title="产品资料手册",
        owner_id="u_admin",
        department_id="sales",
        acl=public_acl,
        raw_bytes=(
            "# 企业知识库 Agent\n\n"
            "产品支持 PDF、Word、Markdown 文档上传，并在回答中绑定 document_id、version、page 和 chunk_id。"
            "系统要求使用权限感知检索，必须在检索前执行 ACL 过滤。\n\n"
            "# 引用策略\n\n"
            "每条关键结论都必须绑定有效引用；引用失效时禁止输出对应结论。"
        ).encode("utf-8"),
    )
    ingestion.ingest(
        filename="hr-policy.md",
        title="员工制度",
        owner_id="u_hr",
        department_id="hr",
        acl=hr_acl,
        raw_bytes=(
            "# 年假制度\n\n"
            "员工入职满一年后可享受年假。HR 制度文档仅允许 HR 部门访问，其他部门不能检索到本内容。"
        ).encode("utf-8"),
    )
