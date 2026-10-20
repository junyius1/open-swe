"""Postgres persistence for the local (in-memory runtime) dev server.

``langgraph dev`` (inmem runtime) normally persists thread state and long-term
memory to pickle files under ``.langgraph_api/``. When ``DATABASE_URI`` is set
(see ``docker-compose.infra.yml`` and ``docs/INSTALLATION.md`` step 10) this
module backs both with Postgres, so the dev server uses the same store a
standalone Docker deployment would. Without ``DATABASE_URI`` it falls back to
the in-memory equivalents (no persistence) so the server still starts.

Wired through ``langgraph.json``:

    "checkpointer": {"path": "agent/postgres_persistence.py:create_checkpointer", ...}
    "store":        {"path": "agent/postgres_persistence.py:create_store"}
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from psycopg.rows import dict_row

logger = structlog.get_logger(__name__)


def _database_uri() -> str:
    # .env wins over process env: sandbox/CI shells may export placeholder
    # values (DATABASE_URI=:memory:, REDIS_URI=fake) that must not shadow the
    # deployment configuration.
    from dotenv import dotenv_values

    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    file_value = (dotenv_values(env_file).get("DATABASE_URI") or "").strip()
    if file_value:
        return file_value
    return os.environ.get("DATABASE_URI", "").strip()


_DATABASE_URI: str = _database_uri()
_POOL_MIN_SIZE: int = int(os.environ.get("POSTGRES_PERSISTENCE_POOL_MIN_SIZE", "1"))
_POOL_MAX_SIZE: int = int(os.environ.get("POSTGRES_PERSISTENCE_POOL_MAX_SIZE", "5"))
_CONN_KWARGS: dict[str, Any] = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}


def create_checkpointer():
    """Async context manager yielding an ``AsyncPostgresSaver`` (migrations run on setup)."""
    if not _DATABASE_URI:
        return _in_memory_checkpointer()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    @asynccontextmanager
    async def _make() -> AsyncIterator[AsyncPostgresSaver]:
        pool = AsyncConnectionPool(
            _DATABASE_URI,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            kwargs=dict(_CONN_KWARGS),
        )
        async with pool:
            saver = AsyncPostgresSaver(conn=pool)
            await saver.setup()
            yield saver

    return _make()


def create_store():
    """Async context manager yielding an ``AsyncPostgresStore`` (migrations run on setup)."""
    if not _DATABASE_URI:
        return _in_memory_store()

    from langgraph.store.postgres.aio import AsyncPostgresStore

    @asynccontextmanager
    async def _make() -> AsyncIterator[AsyncPostgresStore]:
        async with AsyncPostgresStore.from_conn_string(
            _DATABASE_URI,
            pool_config={
                "min_size": _POOL_MIN_SIZE,
                "max_size": _POOL_MAX_SIZE,
                "kwargs": dict(_CONN_KWARGS),
            },
        ) as store:
            await store.setup()
            yield store

    return _make()


@asynccontextmanager
async def _in_memory_checkpointer() -> AsyncIterator[Any]:
    from langgraph.checkpoint.memory import InMemorySaver

    logger.warning(
        "checkpointer: DATABASE_URI not set - using in-memory checkpoints "
        "(NOT persisted). Run `docker compose -f docker-compose.infra.yml up -d` "
        "and set DATABASE_URI in .env to persist thread state to Postgres."
    )
    yield InMemorySaver()


@asynccontextmanager
async def _in_memory_store() -> AsyncIterator[Any]:
    from langgraph.store.memory import InMemoryStore

    logger.warning(
        "store: DATABASE_URI not set - using in-memory long-term memory "
        "(NOT persisted). Run `docker compose -f docker-compose.infra.yml up -d` "
        "and set DATABASE_URI in .env to persist store data to Postgres."
    )
    yield InMemoryStore()
