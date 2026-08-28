"""Synchronous-style completions through the host sidecar."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

from plugins.local_agent.client import LocalAgentClient, LocalAgentClientError
from plugins.local_agent.sidecar.backends import BACKENDS


class _TaskClient(Protocol):
    configured: bool

    async def create_task(
        self,
        *,
        backend: str,
        prompt: str,
        timeout_seconds: float | None = None,
        cwd: str = "",
        max_turns: int | None = None,
    ) -> Any: ...

    async def get_task(self, task_id: str) -> Any: ...


LOCAL_BACKEND_ALIASES = {
    "grok": "grok",
    "local_grok": "grok",
    "codex": "codex",
    "local_codex": "codex",
    "auto": "auto",
}


@dataclass(frozen=True)
class LocalCompleteResult:
    content: str
    model: str
    backend: str


def resolve_local_backend(value: str) -> str:
    """Return grok/codex, or empty when the caller should keep HTTP LLM."""

    raw = str(value or "").strip().lower()
    if raw in {"", "http", "llm", "remote"}:
        return ""
    return LOCAL_BACKEND_ALIASES.get(raw, "")


def compose_completion_prompt(*, system: str, user: str) -> str:
    system_text = str(system or "").strip()
    user_text = str(user or "").strip()
    if system_text and user_text:
        return f"系统指令：\n{system_text}\n\n用户输入：\n{user_text}"
    return system_text or user_text


async def complete_chat(
    settings: Any,
    *,
    backend: str,
    system: str,
    user: str,
    timeout_seconds: float,
    client: _TaskClient | None = None,
    poll_interval_seconds: float = 2.0,
    cwd: str = "",
    max_turns: int | None = None,
) -> LocalCompleteResult:
    resolved = resolve_local_backend(backend)
    if resolved == "auto":
        resolved = "grok"
    if resolved not in BACKENDS:
        raise LocalAgentClientError("unknown_backend", backend)
    runner = client or LocalAgentClient(settings)
    if not getattr(runner, "configured", True):
        raise LocalAgentClientError("not_configured")
    timeout = max(5.0, float(timeout_seconds or 0.0))
    task = await runner.create_task(
        backend=resolved,
        prompt=compose_completion_prompt(system=system, user=user),
        timeout_seconds=timeout,
        cwd=cwd,
        max_turns=max_turns,
    )
    deadline = time.monotonic() + timeout + 15.0
    latest = task
    while time.monotonic() < deadline:
        latest = await runner.get_task(str(getattr(task, "task_id", "") or task.task_id))
        status = str(getattr(latest, "status", "") or "")
        if status == "succeeded":
            text = str(getattr(latest, "result_text", "") or "").strip()
            if not text:
                raise LocalAgentClientError("empty_completion")
            return LocalCompleteResult(content=text, model=f"local-{resolved}", backend=resolved)
        if status == "failed":
            raise LocalAgentClientError(
                "sidecar_failed",
                str(getattr(latest, "error", "") or "sidecar_failed"),
            )
        await asyncio.sleep(max(0.2, float(poll_interval_seconds)))
    raise LocalAgentClientError("sidecar_timeout", f"timed out after {timeout:.0f}s")
