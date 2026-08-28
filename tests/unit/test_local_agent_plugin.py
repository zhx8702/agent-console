from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.channel import ChannelSendResult, ChannelTarget
from app.common.types import (
    CapabilityResult,
    Channel,
    InboundEvent,
    Message,
    MessageType,
    PreprocessedMessage,
    Role,
    RouteDecision,
    RouteType,
    Session,
    Turn,
)
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort
from plugins.local_agent.hooks import build_local_agent_command_definitions
from plugins.local_agent.overflow import LocalAgentOverflowHook, LocalAgentOverflowRetryHook
from plugins.local_agent.probe import BackendStatus, LocalAgentProbe, ProbeSnapshot
from plugins.local_agent.store import LocalAgentJob
from plugins.local_agent.worker import ACCEPTED_OVERFLOW_TEXT, ACCEPTED_TEXT, process_job


class _FakeStore:
    def __init__(self) -> None:
        self.jobs: dict[str, LocalAgentJob] = {}

    async def create_job(self, **kwargs) -> LocalAgentJob:
        job = LocalAgentJob(
            job_id="job-1",
            backend=str(kwargs.get("backend") or ""),
            status="queued",
            prompt=str(kwargs.get("prompt") or ""),
            tenant_id=str(kwargs.get("tenant_id") or ""),
            channel=str(kwargs.get("channel") or ""),
            session_id=str(kwargs.get("session_id") or ""),
            user_id=str(kwargs.get("user_id") or ""),
            adapter_id=str(kwargs.get("adapter_id") or ""),
            connection_id=str(kwargs.get("connection_id") or ""),
            request_id=str(kwargs.get("request_id") or ""),
            trace_id=str(kwargs.get("trace_id") or ""),
            original_message_id=str(kwargs.get("original_message_id") or ""),
            callback_target=dict(kwargs.get("callback_target") or {}),
            source_message=dict(kwargs.get("source_message") or {}),
        )
        self.jobs[job.job_id] = job
        return job

    async def get_job(self, job_id: str) -> LocalAgentJob | None:
        return self.jobs.get(job_id)

    async def mark_submitted(self, job_id: str, sidecar_task_id: str) -> None:
        job = self.jobs[job_id]
        job.sidecar_task_id = sidecar_task_id
        job.status = "submitted"

    async def mark_running(self, job_id: str) -> None:
        self.jobs[job_id].status = "running"

    async def mark_succeeded(self, job_id: str, result_text: str) -> None:
        job = self.jobs[job_id]
        job.status = "succeeded"
        job.result_text = result_text

    async def mark_failed(self, job_id: str, error_code: str, error_message: str) -> None:
        job = self.jobs[job_id]
        job.status = "failed"
        job.error_code = error_code
        job.error_message = error_message

    async def mark_callback_sent(self, job_id: str) -> None:
        self.jobs[job_id].callback_sent = True

    async def mark_callback_error(self, job_id: str, error: str) -> None:
        self.jobs[job_id].callback_error = error

    async def release_lock(self, job_id: str) -> None:
        self.jobs[job_id].locked_by = ""


class _BothReadyProbe:
    async def snapshot(self, *, force: bool = False) -> ProbeSnapshot:
        _ = force
        return ProbeSnapshot(
            ok=True,
            configured=True,
            error="",
            backends={
                "grok": BackendStatus(name="grok", ok=True, version="grok 1.0.5"),
                "codex": BackendStatus(name="codex", ok=True, version="codex-cli 0.149.1"),
            },
            probed_at=1.0,
        )


class _ReadyProbe:
    async def snapshot(self, *, force: bool = False) -> ProbeSnapshot:
        _ = force
        return ProbeSnapshot(
            ok=True,
            configured=True,
            error="",
            backends={
                "grok": BackendStatus(name="grok", ok=True, version="grok 1.0.5"),
                "codex": BackendStatus(name="codex", ok=False, error="executable_not_found"),
            },
            probed_at=1.0,
        )


class _FakeOutbound:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def get_session_policy(self, target: ChannelTarget) -> dict:
        _ = target
        return {}

    async def send_text(self, target, text, options=None):
        self.sent.append((target.session_id, text))
        _ = options
        return ChannelSendResult(message_id="m1")

    async def send_image(self, target, media, options=None):
        raise AssertionError("image not expected")

    async def send_video(self, target, media, options=None):
        raise AssertionError("video not expected")

    async def send_file(self, target, file, options=None):
        raise AssertionError("file not expected")


class _FakeSidecar:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    async def create_task(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(task_id="sid-1", status="queued", result_text="", error="")

    async def get_task(self, task_id: str):
        _ = task_id
        return SimpleNamespace(task_id="sid-1", status="succeeded", result_text="OKAY", error="")


def _event(text: str = "/grok hello") -> InboundEvent:
    return InboundEvent(
        tenant_id="t1",
        channel=Channel.WECHAT,
        user_id="u1",
        session_id="s1",
        message_id="m1",
        trace_id="tr1",
        message=Message(type=MessageType.TEXT, content=text),
    )


def _ctx(text: str = "/grok hello") -> PipelineContext:
    event = _event(text)
    session = Session(
        tenant_id="t1",
        channel=Channel.WECHAT,
        user_id="u1",
        session_id="s1",
        turns=[
            Turn(session_id="s1", role=Role.USER, content="之前的问题", trace_id="old"),
            Turn(session_id="s1", role=Role.ASSISTANT, content="之前的回答", trace_id="old"),
        ],
    )
    return PipelineContext(event=event, trace_id="tr1", session=session)


@pytest.mark.asyncio
async def test_grok_command_queues_job_when_backend_ready() -> None:
    store = _FakeStore()
    definitions = build_local_agent_command_definitions(store, _ReadyProbe(), None)
    ctx = _ctx("/grok 用一句话说你好")
    reply = await definitions[0].handler(ctx, ["用一句话说你好"])
    assert reply == ACCEPTED_TEXT["grok"]
    job = next(iter(store.jobs.values()))
    assert job.backend == "grok"
    assert "用一句话说你好" in job.prompt
    assert "之前的问题" in job.prompt


@pytest.mark.asyncio
async def test_codex_command_reports_unavailable() -> None:
    store = _FakeStore()
    definitions = build_local_agent_command_definitions(store, _ReadyProbe(), None)
    ctx = _ctx("/codex only reply OKAY")
    reply = await definitions[1].handler(ctx, ["only", "reply", "OKAY"])
    assert "不可用" in reply
    assert not store.jobs


@pytest.mark.asyncio
async def test_process_job_submits_and_sends_callback() -> None:
    store = _FakeStore()
    job = await store.create_job(
        backend="grok",
        prompt="say OKAY",
        tenant_id="t1",
        channel="wechat",
        session_id="s1",
        user_id="u1",
        callback_target=asdict(
            ChannelTarget(tenant_id="t1", channel="wechat", session_id="s1", user_id="u1")
        ),
    )
    outbound = _FakeOutbound()
    registry = SimpleNamespace(require_outbound_for_target=lambda target: outbound)
    await process_job(
        store=store,
        client=_FakeSidecar(),
        channel_registry=registry,  # type: ignore[arg-type]
        job=job,
        settings=SimpleNamespace(local_agent_task_timeout_seconds=30),
        scope_execution_allowed=None,
    )
    latest = await store.get_job(job.job_id)
    assert latest is not None
    assert latest.status == "succeeded"
    assert latest.callback_sent is True
    assert outbound.sent == [("s1", "OKAY")]


@pytest.mark.asyncio
async def test_probe_cache_returns_cached_snapshot() -> None:
    class _Client:
        configured = True
        calls = 0

        async def backends(self):
            type(self).calls += 1
            return {
                "backends": {
                    "grok": {"ok": True, "version": "v1"},
                    "codex": {"ok": True, "version": "v2"},
                }
            }

    probe = LocalAgentProbe(_Client(), SimpleNamespace(local_agent_probe_cache_seconds=60))
    first = await probe.snapshot()
    second = await probe.snapshot()
    assert first.ok is True
    assert second.backends["codex"].version == "v2"
    assert _Client.calls == 1


def _overflow_ctx(text: str, *, route: RouteType = RouteType.LLM) -> PipelineContext:
    ctx = _ctx(text)
    ctx.pre = PreprocessedMessage(original_text=text, cleaned_text=text)
    ctx.route = RouteDecision(type=route, reason="test")
    return ctx


def _overflow_settings(**overrides: object) -> SimpleNamespace:
    values = {
        "local_agent_overflow_enabled": True,
        "local_agent_overflow_min_chars": 1000,
        "local_agent_overflow_backend": "auto",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_overflow_hook_queues_codex_for_long_prompt() -> None:
    store = _FakeStore()
    hook = LocalAgentOverflowHook(store, _BothReadyProbe(), _overflow_settings())
    ctx = _overflow_ctx("x" * 1200)
    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)
    assert excinfo.value.reason == "local_agent_overflow"
    assert ACCEPTED_OVERFLOW_TEXT.format(backend="codex") == excinfo.value.reply_text
    job = next(iter(store.jobs.values()))
    assert job.backend == "codex"
    assert len(job.prompt) >= 80


@pytest.mark.asyncio
async def test_overflow_hook_skips_short_prompt() -> None:
    store = _FakeStore()
    hook = LocalAgentOverflowHook(store, _BothReadyProbe(), _overflow_settings())
    ctx = _overflow_ctx("short")
    await hook.run(ctx)
    assert store.jobs == {}


@pytest.mark.asyncio
async def test_overflow_hook_skips_when_disabled() -> None:
    store = _FakeStore()
    hook = LocalAgentOverflowHook(
        store,
        _BothReadyProbe(),
        _overflow_settings(local_agent_overflow_enabled=False),
    )
    ctx = _overflow_ctx("x" * 1200)
    await hook.run(ctx)
    assert store.jobs == {}


@pytest.mark.asyncio
async def test_overflow_retry_hook_catches_llm_context_error() -> None:
    store = _FakeStore()
    hook = LocalAgentOverflowRetryHook(store, _ReadyProbe(), _overflow_settings())
    ctx = _overflow_ctx("hello")
    ctx.result = CapabilityResult(
        route=RouteType.CANNED,
        reply_text="upstream 413 context_length exceeded",
        metadata={"degradation_reason": "capability_failed:llm"},
    )
    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)
    assert excinfo.value.reason == "local_agent_overflow"
    job = next(iter(store.jobs.values()))
    assert job.backend == "grok"


@pytest.mark.asyncio
async def test_overflow_hook_skips_explicit_command() -> None:
    store = _FakeStore()
    hook = LocalAgentOverflowHook(store, _BothReadyProbe(), _overflow_settings())
    ctx = _overflow_ctx("x" * 1200)
    ctx.extras["_command_token"] = "/grok"
    await hook.run(ctx)
    assert store.jobs == {}
