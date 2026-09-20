from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.typesafe import TypeSafeEvaluation
from plugins.memory.store import MemoryStore


def _candidate() -> dict:
    return {
        "subject": "private-user-a",
        "object": "private-user-b",
        "predicate": "replied_to",
        "confidence": 0.9,
        "signals": {"direct_reply": 1},
        "evidence_event_ids": [1, 1],
        "evidence_observation_ids": [1],
        "reason": "private transcript must not leave the process",
    }


def _window() -> dict:
    return {
        "index": 0, "first_event_id": 1, "last_event_id": 1,
        "event_ids": [1], "sender_ids": ["private-user-a", "private-user-b"],
    }


@pytest.mark.asyncio
async def test_shadow_preserves_typed_audit_and_independent_evidence_counts() -> None:
    client = SimpleNamespace(evaluate_group_relationship=AsyncMock(return_value=TypeSafeEvaluation(
        model="jev-1.13.0",
        answers={
            "decision": {"type": "choice", "choice": "rejected", "confidence": 0.93,
                         "probabilities": {"accepted": 0.01, "rejected": 0.99}},
            "supported": {"type": "noul", "noul": 0.01},
            "quality": {"type": "score", "score": 0.2},
        },
        usage={"input_tokens": 125},
    )))
    store = MemoryStore(SimpleNamespace(typesafe_enabled=True))
    store.typesafe_client = client
    result = await store._run_typesafe_group_shadow(
        candidate=_candidate(), window=_window(), target_date="2026-09-20",
    )
    assert result["model"] == "jev-1.13.0"
    assert result["usage"]["input_tokens"] == 125
    assert result["decision"]["confidence"] == 0.93
    assert result["decision"]["probabilities"]["rejected"] == 0.99
    state = client.evaluate_group_relationship.call_args.kwargs["state"]
    assert state["evidence_count"] == 2
    assert "private" not in json.dumps(state)

    # Persist the audit, while retaining the existing deterministic decision.
    store._find_memory_item_by_normalized_key = AsyncMock(return_value=[])
    store._insert_or_touch_memory_item = AsyncMock(return_value={"id": 1})
    await store._apply_group_relationship_window_candidate(
        tenant_id="demo", channel="wechat", source_key="wxbot", user_id="__group__",
        session_id="test-session", target_date="2026-09-20", window=_window(),
        candidate={**_candidate(), "typesafe_shadow": result},
    )
    saved = store._insert_or_touch_memory_item.call_args.kwargs["value_json"]
    assert saved["relation"]["typesafe_shadow"] == result
    await store._apply_group_relationship_window_candidate(
        tenant_id="demo", channel="wechat", source_key="wxbot", user_id="__group__",
        session_id="test-session", target_date="2026-09-20", window=_window(),
        candidate=_candidate(),
    )
    baseline = store._insert_or_touch_memory_item.call_args.kwargs["value_json"]
    assert saved["acceptance"] == baseline["acceptance"]
    assert saved["acceptance"]["status"] != "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout", "empty"])
async def test_shadow_failure_does_not_escape(failure: str) -> None:
    async def evaluate(**kwargs):
        if failure == "exception":
            raise RuntimeError("upstream unavailable")
        if failure == "timeout":
            await asyncio.sleep(10)
        return None

    store = MemoryStore(SimpleNamespace(
        typesafe_enabled=True, typesafe_group_graph_shadow_timeout_seconds=0.1,
    ))
    store.typesafe_client = SimpleNamespace(evaluate_group_relationship=evaluate)
    result = await asyncio.wait_for(store._run_typesafe_group_shadow(
        candidate=_candidate(), window=_window(), target_date="2026-09-20",
    ), timeout=1)
    assert result["status"] == ("unavailable" if failure == "empty" else "error")


@pytest.mark.asyncio
async def test_disabled_shadow_never_calls_upstream() -> None:
    evaluate = AsyncMock()
    store = MemoryStore(SimpleNamespace(typesafe_enabled=False))
    store.typesafe_client = SimpleNamespace(evaluate_group_relationship=evaluate)
    assert await store._run_typesafe_group_shadow(
        candidate=_candidate(), window=_window(), target_date="2026-09-20",
    ) is None
    evaluate.assert_not_awaited()


def test_shadow_audit_is_bounded_and_json_safe() -> None:
    result = MemoryStore._normalize_typesafe_shadow_result({
        "model": "m" * 1000,
        "answers": {"quality": {"score": float("nan"), "probabilities": {
            str(i): 0.1 for i in range(100)
        }}},
        "secret": "must not be persisted",
    })
    assert len(result["model"]) == 160
    assert result["quality"]["score"] is None
    assert len(result["quality"]["probabilities"]) == 16
    assert "secret" not in result
    json.dumps(result, allow_nan=False)
