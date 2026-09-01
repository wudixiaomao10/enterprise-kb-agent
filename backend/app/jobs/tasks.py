from __future__ import annotations

import os
from urllib.parse import urlparse

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from backend.app.retrieval.providers import load_dotenv_if_available


load_dotenv_if_available()
redis_url = os.getenv("KNOWLEDGE_REDIS_URL", "redis://127.0.0.1:6379/0")
parsed = urlparse(redis_url)
broker = RedisBroker(
    host=parsed.hostname or "127.0.0.1",
    port=parsed.port or 6379,
    db=int((parsed.path or "/0").strip("/") or "0"),
    password=parsed.password,
)
dramatiq.set_broker(broker)


@dramatiq.actor(
    queue_name="document-indexing",
    max_retries=3,
    min_backoff=5_000,
    max_backoff=60_000,
    time_limit=30 * 60 * 1000,
)
def process_index_job(job_id: str) -> None:
    from backend.app.bootstrap import create_worker_job_service

    service = create_worker_job_service()
    service.execute(job_id)


@dramatiq.actor(
    queue_name="identity-sync",
    max_retries=5,
    min_backoff=10_000,
    max_backoff=5 * 60_000,
    time_limit=30 * 60 * 1000,
)
def process_graph_directory_sync(resources: list[str] | None = None) -> None:
    from backend.app.bootstrap import create_worker_graph_sync_service

    service = create_worker_graph_sync_service()
    service.sync(resources)


@dramatiq.actor(
    queue_name="identity-sync",
    max_retries=5,
    min_backoff=10_000,
    max_backoff=5 * 60_000,
    time_limit=30 * 60 * 1000,
)
def process_graph_subscription_maintenance() -> None:
    from backend.app.bootstrap import create_worker_graph_sync_service

    service = create_worker_graph_sync_service()
    service.handle_lifecycle_notifications()


@dramatiq.actor(
    queue_name="knowledge-research",
    max_retries=2,
    min_backoff=10_000,
    max_backoff=2 * 60_000,
    time_limit=45 * 60 * 1000,
)
def process_research_job(job_id: str) -> None:
    from backend.app.bootstrap import create_worker_research_job_service

    service = create_worker_research_job_service()
    service.execute(job_id)
