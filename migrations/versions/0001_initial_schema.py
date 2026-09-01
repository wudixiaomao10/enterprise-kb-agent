"""Create the knowledge, identity, job, and research tables."""

from __future__ import annotations

import os

from alembic import context, op

from backend.app.identity.directory import build_directory_schema_sql
from backend.app.identity.provisioning import build_provisioning_schema_sql
from backend.app.jobs.repository import build_job_schema_sql
from backend.app.repositories.postgres_store import build_schema_sql
from backend.app.research.repository import build_research_job_schema_sql


revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    dimensions = int(
        os.getenv(
            "KNOWLEDGE_EMBEDDING_DIMENSIONS",
            str(
                context.config.attributes.get("knowledge_vector_dimensions", 1536)
            ),
        )
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(build_schema_sql(dimensions))
    op.execute(build_job_schema_sql())
    op.execute(build_research_job_schema_sql())
    op.execute(build_directory_schema_sql())
    op.execute(build_provisioning_schema_sql())


def downgrade() -> None:
    for table in (
        "identity_graph_subscriptions",
        "identity_webhook_events",
        "identity_sync_cursors",
        "directory_sync_runs",
        "directory_user_roles",
        "directory_user_departments",
        "directory_roles",
        "directory_departments",
        "directory_users",
        "research_jobs",
        "indexing_jobs",
        "citations",
        "answers",
        "queries",
        "document_chunks",
        "document_versions",
        "documents",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
