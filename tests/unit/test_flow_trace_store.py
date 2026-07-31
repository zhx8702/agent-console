from __future__ import annotations

import pytest

from app.orchestrator.runner import FLOW_RUN_COMPLETED, FlowRunResult, FlowRunStepTrace
from app.orchestrator.trace_store import (
    flow_trace_snapshot_key,
    read_flow_trace_snapshots,
    write_flow_trace_snapshot,
)


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.values[key] = value
        if ex is not None:
            self.expiries[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.mark.asyncio
async def test_flow_trace_snapshot_roundtrip_is_safe_and_ttl_backed() -> None:
    redis = _Redis()
    result = FlowRunResult(
        flow_name="default_group_channel_flow",
        flow_version=1,
        status=FLOW_RUN_COMPLETED,
        trace_id="tr_test",
        tenant_id="default",
        session_id="group-1",
        steps=[
            FlowRunStepTrace(
                id="capability",
                kind="core.capability",
                owner="core",
                status="ok",
                elapsed_ms=12.3,
            )
        ],
        effect_commits=[
            {
                "owner": "wxbot",
                "type": "enqueue_channel_reply",
                "status": "recorded",
                "dry_run": False,
                "payload": {"must_not": "persist"},
            }
        ],
        decision_trace={
            "intent": {
                "coarse": "business",
                "language": "zh",
                "sensitive": False,
                "raw_text": "must_not_persist",
            },
            "route": {
                "type": "agent",
                "confidence": 0.92,
                "rule": "tool_intent",
                "matched_conditions": ["tool_intent_matched", "tools_available"],
            },
            "router_signals": {
                "tool_intent_matched": True,
                "effective_tool_count": 2,
                "tool_preflight_failed": False,
                "faq_similarity": float("nan"),
                "payload": {"must_not": "persist"},
            },
            "result": {
                "route": "agent",
                "tool_preselection_verdict": "AMBIGUOUS",
                "tool_preselection_selected": ["search", "export"],
                "tool_preselection_scores": {
                    "search": 3,
                    "export": 2.5,
                    "invalid": float("inf"),
                },
            },
            "raw_text": "must_not_persist",
        },
    )

    await write_flow_trace_snapshot(
        redis,
        result,
        mode="runtime",
        ttl_seconds=123,
        key_prefix="test:flow:trace",
    )

    key = flow_trace_snapshot_key("test:flow:trace", "tr_test", "runtime")
    assert redis.expiries[key] == 123

    snapshots = await read_flow_trace_snapshots(
        redis,
        "tr_test",
        key_prefix="test:flow:trace",
    )
    runtime = snapshots["runtime"]
    assert runtime is not None
    assert runtime["trace_id"] == "tr_test"
    assert runtime["mode"] == "runtime"
    assert runtime["schema_version"] == 2
    assert runtime["steps"][0]["id"] == "capability"
    assert runtime["effect_commits"][0] == {
        "owner": "wxbot",
        "type": "enqueue_channel_reply",
        "status": "recorded",
        "dry_run": False,
    }
    assert runtime["decision_trace"] == {
        "intent": {
            "coarse": "business",
            "language": "zh",
            "sensitive": False,
        },
        "route": {
            "type": "agent",
            "confidence": 0.92,
            "rule": "tool_intent",
            "matched_conditions": ["tool_intent_matched", "tools_available"],
        },
        "router_signals": {
            "tool_intent_matched": True,
            "effective_tool_count": 2,
            "tool_preflight_failed": False,
        },
        "result": {
            "route": "agent",
            "tool_preselection_verdict": "AMBIGUOUS",
            "tool_preselection_selected": ["search", "export"],
            "tool_preselection_scores": {"search": 3.0, "export": 2.5},
        },
    }
    assert "raw_text" not in runtime["decision_trace"]
    assert snapshots["shadow"] is None
