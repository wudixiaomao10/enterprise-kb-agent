"""Add durable dead-letter storage for indexing and research jobs."""

from __future__ import annotations

from alembic import op

from backend.app.jobs.dlq import build_dlq_schema_sql


revision = "0002_reliability_controls"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(build_dlq_schema_sql())


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS job_dead_letters CASCADE")
