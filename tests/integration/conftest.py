"""Integration test fixtures (real Redis, real Postgres)."""
from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/14")
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("TENANT_DEMO_SECRET", "integ_secret")

import pytest
import pytest_asyncio


def _has_redis() -> bool:
    import socket

    try:
        with socket.create_connection(("localhost", 6379), timeout=0.5):
            return True
    except Exception:
        return False


def _has_postgres() -> bool:
    import socket

    try:
        with socket.create_connection(("localhost", 5432), timeout=0.5):
            return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(not _has_redis(), reason="requires redis at localhost:6379")
requires_postgres = pytest.mark.skipif(
    not _has_postgres(), reason="requires postgres at localhost:5432"
)


@pytest_asyncio.fixture
async def _reset_singletons():
    from app.common.config import get_settings
    from app.infra.db import dispose_engine
    from app.infra.redis_client import close_redis

    get_settings.cache_clear()
    await close_redis()
    await dispose_engine()
    yield
    await close_redis()
    await dispose_engine()


@pytest_asyncio.fixture
async def redis_client(_reset_singletons):
    from app.infra.redis_client import get_redis

    r = get_redis()
    await r.flushdb()
    yield r
    await r.flushdb()
