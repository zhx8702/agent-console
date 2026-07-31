from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.common.types import ChatResponse
from plugins.memory.store import MemoryStore


class _RecordingLLM:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def chat(self, request: Any) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(
            content=(
                '{"entities":[],"facts":[],"episodes":[],'
                '"invalidations":[],"conflicts":[]}'
            )
        )


@pytest.mark.asyncio
async def test_graph_llm_only_receives_active_normal_accepted_memory_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _RecordingLLM()
    store = MemoryStore(
        SimpleNamespace(
            memory_graph_llm_extraction_enabled=True,
            memory_graph_llm_extraction_timeout_seconds=1.0,
        ),
        llm_service=llm,
    )
    list_calls: list[dict[str, Any]] = []

    async def list_memory_items(**kwargs: Any) -> list[dict[str, Any]]:
        list_calls.append(kwargs)
        base = {
            "memory_type": "preference",
            "status": "active",
            "sensitivity": "normal",
            "sensitivity_category": "normal",
            "acceptance_status": "accepted",
        }
        return [
            {
                **base,
                "id": 1,
                "normalized_key": "safe-key",
                "content": "SAFE_ACTIVE_NORMAL",
            },
            {
                **base,
                "id": 2,
                "normalized_key": "pii-key",
                "content": "PII_SECRET",
                "sensitivity": "pii",
                "sensitivity_category": "pii",
            },
            {
                **base,
                "id": 3,
                "normalized_key": "sensitive-key",
                "content": "CATEGORY_SECRET",
                "sensitivity_category": "sensitive",
            },
            {
                **base,
                "id": 4,
                "normalized_key": "pending-key",
                "content": "PENDING_SECRET",
                "status": "pending",
            },
            {
                **base,
                "id": 5,
                "normalized_key": "rejected-key",
                "content": "REJECTED_SECRET",
                "acceptance_status": "rejected",
            },
            {
                **base,
                "id": 6,
                "normalized_key": "nested-rejected-key",
                "content": "NESTED_REJECTED_SECRET",
                "acceptance_status": "",
                "value_json": {"acceptance": {"status": "rejected"}},
            },
            {
                **base,
                "id": 7,
                "normalized_key": "deleted-key",
                "content": "DELETED_SECRET",
                "deleted_at": "2026-07-30T00:00:00Z",
            },
            {
                **base,
                "id": 8,
                "normalized_key": "contact:legacy@example.com",
                "content": "旧数据误标 normal，手机号 13800138000",
            },
        ]

    async def get_session_profile(**_kwargs: Any) -> dict[str, Any]:
        return {"session_summary": "benign session summary"}

    async def allow_scope(_tenant_id: str, _session_id: str) -> bool:
        return True

    async def renew_claim() -> bool:
        return True

    monkeypatch.setattr(store, "list_memory_items", list_memory_items)
    monkeypatch.setattr(store, "get_session_profile", get_session_profile)

    await store._enhance_memory_graph_with_llm(
        tenant_id="tenant-a",
        channel="wechat",
        source_key="wxbot",
        user_id="member-a",
        session_id="member-a",
        user_text="本轮普通用户文本",
        assistant_text="本轮普通回复",
        trace_id="trace-a",
        source_event_id=1,
        scope_execution_allowed=allow_scope,
        claim_lease_renew=renew_claim,
        job_id=11,
    )

    assert list_calls == [
        {
            "tenant_id": "tenant-a",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "member-a",
            "session_id": "",
            "scope_type": "identity",
            "status": "active",
            "include_deleted": False,
            "limit": 20,
        }
    ]
    assert len(llm.requests) == 1
    request_payload = json.loads(llm.requests[0].messages[0].content)
    memory_summary = request_payload["memory_items_summary"]
    assert "SAFE_ACTIVE_NORMAL" in memory_summary
    assert "[redacted-memory-pii]" in memory_summary
    assert "13800138000" not in memory_summary
    assert "legacy@example.com" not in memory_summary
    for forbidden_content in (
        "PII_SECRET",
        "CATEGORY_SECRET",
        "PENDING_SECRET",
        "REJECTED_SECRET",
        "NESTED_REJECTED_SECRET",
        "DELETED_SECRET",
    ):
        assert forbidden_content not in memory_summary
