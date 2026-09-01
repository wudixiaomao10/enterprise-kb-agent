from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any


_pool_lock = threading.Lock()
_pools: dict[tuple[str, str], Any] = {}
_migrations_lock = threading.Lock()
_migrated_dsns: set[str] = set()
_DEFAULT_ROW_FACTORY = object()


def get_postgres_pool(dsn: str, *, row_factory: Any = _DEFAULT_ROW_FACTORY):
    """Return one bounded psycopg pool per process and row shape."""

    pool_key = (dsn, "dict" if row_factory is _DEFAULT_ROW_FACTORY else "default")

    with _pool_lock:
        pool = _pools.get(pool_key)
        if pool is not None:
            return pool
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as error:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "PostgreSQL requires psycopg[binary,pool]. Install requirements first."
            ) from error

        kwargs = {}
        if row_factory is _DEFAULT_ROW_FACTORY:
            from psycopg.rows import dict_row

            kwargs["row_factory"] = dict_row
        elif row_factory is not None:
            kwargs["row_factory"] = row_factory
        min_size = max(0, env_int("KNOWLEDGE_DB_POOL_MIN_SIZE", 1))
        max_size = max(min_size, env_int("KNOWLEDGE_DB_POOL_MAX_SIZE", 10), 1)
        pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            max_waiting=max(1, env_int("KNOWLEDGE_DB_POOL_MAX_WAITING", 50)),
            timeout=max(1.0, env_float("KNOWLEDGE_DB_POOL_TIMEOUT_SECONDS", 30.0)),
            kwargs=kwargs,
            open=True,
        )
        try:
            pool.wait(timeout=max(1.0, env_float("KNOWLEDGE_DB_POOL_WAIT_SECONDS", 30.0)))
        except Exception:
            pool.close()
            raise
        _pools[pool_key] = pool
        return pool


def close_postgres_pools() -> None:
    with _pool_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


def ensure_database_schema(dsn: str, vector_dimensions: int) -> None:
    """Run Alembic once per process when automatic migration is enabled."""

    if os.getenv("KNOWLEDGE_AUTO_MIGRATE", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return
    with _migrations_lock:
        if dsn in _migrated_dsns:
            return
        try:
            from alembic import command
            from alembic.config import Config
        except ImportError as error:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "Alembic is required when KNOWLEDGE_AUTO_MIGRATE is enabled"
            ) from error

        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
        config.attributes["knowledge_vector_dimensions"] = vector_dimensions
        command.upgrade(config, "head")
        _migrated_dsns.add(dsn)


def reset_database_state_for_tests() -> None:
    with _migrations_lock:
        _migrated_dsns.clear()


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


__all__ = [
    "close_postgres_pools",
    "ensure_database_schema",
    "get_postgres_pool",
    "reset_database_state_for_tests",
]
