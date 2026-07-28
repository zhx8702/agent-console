"""
End-to-end test fixtures.

Strategy
--------
* Use real Postgres + Redis + Qdrant (started by Docker Compose or CI services).
* Override ``OUTBOUND_WEBHOOK_URL`` to point at an in-process "capture" server
  that records every delivered payload together with its HMAC signature.
* Flush Redis DB 15 before each test to avoid cross-test pollution from the
  bus streams / consumer groups.
* Build independent inbound and outbound role containers via
  ``app.main.build_container`` and run both workers as asyncio tasks.
* Tests drive the ingress gateway with ``httpx.AsyncClient`` over an ASGI
  transport — no network port needed for the inbound side.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- Capture server ----------------------------------------------------------


class CaptureStore:
    """Thread-safe buffer of delivered outbound payloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._items.append(record)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    async def wait_for(self, session_id: str, *, timeout: float = 8.0) -> dict[str, Any]:
        """Block until we see at least one delivery for the given session_id."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for rec in self.snapshot():
                body = rec.get("body") or {}
                if body.get("session_id") == session_id:
                    return rec
            await asyncio.sleep(0.05)
        raise AssertionError(f"no outbound delivery seen for session {session_id} within {timeout}s")


def _build_capture_app(store: CaptureStore) -> FastAPI:
    capture_app = FastAPI()

    @capture_app.post("/deliver")
    async def deliver(request: Request) -> dict[str, str]:
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            import orjson

            body = orjson.loads(raw)
        except Exception:
            body = None
        store.append({"body": body, "headers": headers, "raw": raw})
        return {"status": "ok"}

    @capture_app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return capture_app


class _CaptureServerThread(threading.Thread):
    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        super().__init__(daemon=True)
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)

    def run(self) -> None:
        self._server.run()

    def stop(self) -> None:
        self._server.should_exit = True


# -- Global environment overrides (must be set before any app.* import) ------

_CAPTURE_PORT = _pick_free_port()
os.environ["APP_ENV"] = "test"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["OUTBOUND_WEBHOOK_URL"] = f"http://127.0.0.1:{_CAPTURE_PORT}/deliver"
os.environ["OUTBOUND_HMAC_SECRET"] = "e2e_outbound_secret"
os.environ["ADMIN_BEARER_TOKEN"] = "e2e_admin_token"
os.environ["TENANT_DEMO_SECRET"] = "e2e_demo_secret"
os.environ["INBOUND_DEFAULT_RATE_LIMIT"] = "500"
os.environ["QDRANT_URL"] = "http://127.0.0.1:6333"


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture(scope="session")
def capture_store() -> CaptureStore:
    return CaptureStore()


@pytest.fixture(scope="session", autouse=True)
def _capture_server(capture_store: CaptureStore):
    app = _build_capture_app(capture_store)
    thread = _CaptureServerThread(app, "127.0.0.1", _CAPTURE_PORT)
    thread.start()

    # Busy-wait for readiness (max ~5s).
    import time as _time

    deadline = _time.time() + 5
    while _time.time() < deadline:
        try:
            resp = httpx.get(
                f"http://127.0.0.1:{_CAPTURE_PORT}/healthz",
                timeout=0.5,
                trust_env=False,
            )
            if resp.status_code == 200:
                break
        except Exception:
            pass
        _time.sleep(0.1)
    else:
        raise RuntimeError("capture server failed to start")

    yield
    thread.stop()
    thread.join(timeout=2)


@pytest_asyncio.fixture
async def _reset_singletons():
    """Drop cached Redis/DB/settings between tests so each test's event loop is clean."""
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
async def redis_db15(_reset_singletons):
    from redis.exceptions import ConnectionError as RedisConnectionError

    from app.infra.redis_client import get_redis

    r = get_redis()
    try:
        await r.flushdb()
    except RedisConnectionError as exc:
        pytest.skip(f"requires Redis at localhost:6379 ({exc.__class__.__name__})")
    yield r
    await r.flushdb()


@pytest_asyncio.fixture
async def app_stack(redis_db15) -> AsyncIterator[dict[str, Any]]:
    """Build the live container + start workers. Yields a handle for the test."""
    # Make sure settings reflect the env we set.
    from app.common.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    # Ensure DB schema is present (idempotent; e2e assumes `alembic upgrade head` was run).
    from sqlalchemy import text

    from app.infra.db import get_engine

    engine = get_engine()
    async with engine.connect() as conn:
        # Quick probe — the caller is expected to have run migrations.
        await conn.execute(text("select 1"))

    # Build stack.
    from app.main import build_container

    inbound_settings = settings.model_copy(update={"app_process_role": "inbound"})
    outbound_settings = settings.model_copy(update={"app_process_role": "outbound"})
    inbound_container = await build_container(inbound_settings)
    outbound_container = await build_container(outbound_settings)

    from app.container import InboundContainer, OutboundContainer

    assert isinstance(inbound_container, InboundContainer)
    assert isinstance(outbound_container, OutboundContainer)

    # Hand-roll the workers so we can stop them cleanly.
    from app.workers.inbound_worker import InboundWorker
    from app.workers.outbound_worker import OutboundWorker

    inbound = InboundWorker(
        inbound_container.bus,
        inbound_container.orchestrator,
        inbound_settings,
    )
    outbound = OutboundWorker(
        outbound_container.dispatcher,
        outbound_container.outbox_relay,
    )

    inbound_task = asyncio.create_task(inbound.run(), name="e2e-inbound")
    outbound_task = asyncio.create_task(outbound.run(), name="e2e-outbound")

    # Build an ASGI client pointed at the FastAPI app (mount ingress only).
    from fastapi import FastAPI

    test_app = FastAPI()
    from app.admin.kb_router import build_admin_router
    from app.ingress.router import build_router as build_ingress_router

    test_app.include_router(build_ingress_router(inbound_container))
    test_app.include_router(
        build_admin_router(
            inbound_container.faq_store,
            inbound_container.kb_service,
        )
    )

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        try:
            yield {
                "client": client,
                "container": inbound_container,
                "outbound_container": outbound_container,
                "settings": settings,
            }
        finally:
            inbound_task.cancel()
            outbound_task.cancel()
            with contextlib.suppress(BaseException):
                await inbound_task
            with contextlib.suppress(BaseException):
                await outbound_task
            with contextlib.suppress(Exception):
                await inbound_container.bus.close()
            with contextlib.suppress(Exception):
                await outbound_container.bus.close()
            with contextlib.suppress(Exception):
                await outbound_container.http_client.aclose()
            with contextlib.suppress(Exception):
                await inbound_container.plugin_registry.shutdown_all()
