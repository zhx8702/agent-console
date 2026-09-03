from __future__ import annotations

import asyncio

import pytest

from app.llm.activity import record_llm_stream_event, wait_for_llm_activity


@pytest.mark.asyncio
async def test_wait_for_llm_activity_keeps_active_stream_alive() -> None:
    async def active_stream() -> str:
        await asyncio.sleep(0.01)
        record_llm_stream_event(idle_timeout_seconds=0.06)
        await asyncio.sleep(0.03)
        record_llm_stream_event(idle_timeout_seconds=0.06)
        await asyncio.sleep(0.03)
        return "done"

    assert await wait_for_llm_activity(active_stream(), timeout=0.02) == "done"


@pytest.mark.asyncio
async def test_wait_for_llm_activity_still_times_out_without_events() -> None:
    with pytest.raises(TimeoutError):
        await wait_for_llm_activity(asyncio.sleep(0.1), timeout=0.01)


@pytest.mark.asyncio
async def test_wait_for_llm_activity_times_out_after_stream_goes_idle() -> None:
    async def stalled_stream() -> None:
        record_llm_stream_event(idle_timeout_seconds=0.02)
        await asyncio.sleep(0.1)

    with pytest.raises(TimeoutError):
        await wait_for_llm_activity(stalled_stream(), timeout=0.01)
