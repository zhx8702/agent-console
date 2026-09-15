from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from plugins.speaker_portrait.plugin import SpeakerPortraitPlugin
from plugins.speaker_portrait.store import SpeakerPortraitStore


async def _job_table(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE plugin_speaker_portrait_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
                    session_id VARCHAR(256) NOT NULL DEFAULT '',
                    speaker_id VARCHAR(256) NOT NULL DEFAULT '',
                    status VARCHAR(32) NOT NULL DEFAULT 'queued',
                    error TEXT NOT NULL DEFAULT '',
                    locked_by VARCHAR(128) NOT NULL DEFAULT '',
                    locked_until DATETIME,
                    started_at DATETIME,
                    finished_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )


async def _insert(engine, *, status: str, locked_until: datetime | None) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                """
                INSERT INTO plugin_speaker_portrait_jobs
                    (status, locked_by, locked_until, started_at, updated_at)
                VALUES (:status, 'speaker-portrait-old', :until, :now, :now)
                RETURNING id
                """
            ),
            {"status": status, "until": locked_until, "now": datetime.now(UTC)},
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_reap_expired_jobs_fails_only_lapsed_running_leases(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'portrait-jobs.db'}")
    await _job_table(engine)
    monkeypatch.setattr("plugins.speaker_portrait.store.get_engine", lambda: engine)
    now = datetime.now(UTC)

    # Worker died a week ago with this one; its lease lapsed long ago.
    zombie = await _insert(engine, status="running", locked_until=now - timedelta(days=7))
    # A job another worker is actively running; lease still valid.
    live = await _insert(engine, status="running", locked_until=now + timedelta(minutes=10))
    # Queued rows never have a lease and must be untouched.
    queued = await _insert(engine, status="queued", locked_until=None)

    store = SpeakerPortraitStore(SimpleNamespace())
    reaped = await store.reap_expired_jobs()

    assert reaped == [zombie]
    zombie_row = await store.get_job(zombie)
    assert zombie_row is not None
    assert zombie_row["status"] == "failed"
    assert zombie_row["error"] == "lease_expired"
    assert zombie_row["locked_by"] == ""
    assert zombie_row["locked_until"] is None
    assert zombie_row["finished_at"] is not None

    live_row = await store.get_job(live)
    queued_row = await store.get_job(queued)
    assert live_row is not None and live_row["status"] == "running"
    assert queued_row is not None and queued_row["status"] == "queued"

    # Idempotent: a second sweep has nothing left to do.
    assert await store.reap_expired_jobs() == []
    await engine.dispose()


class _LoopStore:
    def __init__(self, plugin: SpeakerPortraitPlugin) -> None:
        self.plugin = plugin
        self.calls: list[str] = []

    async def reap_expired_jobs(self) -> list[int]:
        self.calls.append("reap")
        return [100]

    async def claim_next_job(self, **_kwargs) -> None:
        self.calls.append("claim")
        # Stop the loop after one pass.
        self.plugin._accept_jobs = False
        return None

    async def due_hot_updates(self, **_kwargs) -> list[dict]:
        self.calls.append("due")
        return []


@pytest.mark.asyncio
async def test_worker_loop_reaps_expired_leases_before_claiming() -> None:
    plugin = SpeakerPortraitPlugin()
    store = _LoopStore(plugin)
    plugin._store = store  # type: ignore[assignment]
    plugin._accept_jobs = True
    plugin._ctx = SimpleNamespace(
        db_ok=True,
        settings=SimpleNamespace(
            app_process_role="scheduler",
            speaker_portrait_worker_roles="scheduler",
            speaker_portrait_timeout_seconds=600.0,
            speaker_portrait_hot_update_enabled=True,
            speaker_portrait_hot_update_min_messages=40,
            speaker_portrait_hot_update_min_seconds=3600.0,
        ),
    )
    plugin._worker_wakeup.set()

    await asyncio.wait_for(plugin._worker_loop(), timeout=5.0)

    assert store.calls[:2] == ["reap", "claim"]
    assert "due" in store.calls
