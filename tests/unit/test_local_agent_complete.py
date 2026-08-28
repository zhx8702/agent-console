from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.local_agent.client import LocalAgentClientError
from plugins.local_agent.complete import (
    complete_chat,
    compose_completion_prompt,
    resolve_local_backend,
)


def test_resolve_local_backend_aliases() -> None:
    assert resolve_local_backend("http") == ""
    assert resolve_local_backend("local_grok") == "grok"
    assert resolve_local_backend("grok") == "grok"
    assert resolve_local_backend("local_codex") == "codex"
    assert resolve_local_backend("auto") == "auto"
    assert resolve_local_backend("claude") == ""


def test_compose_completion_prompt_includes_system_and_user() -> None:
    prompt = compose_completion_prompt(system="简洁", user="写日报")
    assert "简洁" in prompt
    assert "写日报" in prompt


class _FakeCompleteClient:
    configured = True

    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.polls = 0

    async def create_task(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(task_id="sid-9", status="queued", result_text="", error="")

    async def get_task(self, task_id: str):
        self.polls += 1
        assert task_id == "sid-9"
        if self.polls < 2:
            return SimpleNamespace(task_id=task_id, status="running", result_text="", error="")
        return SimpleNamespace(task_id=task_id, status="succeeded", result_text="日报正文", error="")


@pytest.mark.asyncio
async def test_complete_chat_polls_until_success() -> None:
    client = _FakeCompleteClient()
    result = await complete_chat(
        SimpleNamespace(),
        backend="local_grok",
        system="系统",
        user="用户",
        timeout_seconds=30,
        client=client,
        poll_interval_seconds=0.01,
    )
    assert result.content == "日报正文"
    assert result.backend == "grok"
    assert result.model == "local-grok"
    assert client.created[0]["backend"] == "grok"
    assert "系统" in str(client.created[0]["prompt"])


@pytest.mark.asyncio
async def test_complete_chat_raises_on_sidecar_failure() -> None:
    class _FailClient:
        configured = True

        async def create_task(self, **kwargs):
            _ = kwargs
            return SimpleNamespace(task_id="sid-f", status="queued")

        async def get_task(self, task_id: str):
            _ = task_id
            return SimpleNamespace(task_id="sid-f", status="failed", result_text="", error="boom")

    with pytest.raises(LocalAgentClientError, match="boom"):
        await complete_chat(
            SimpleNamespace(),
            backend="grok",
            system="",
            user="x",
            timeout_seconds=10,
            client=_FailClient(),
            poll_interval_seconds=0.01,
        )
