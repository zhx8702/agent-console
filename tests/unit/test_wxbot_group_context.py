from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.common.types import (
    Channel,
    InboundEvent,
    Message,
    Role,
    Session,
    Turn,
)
from app.orchestrator.pipeline import PipelineContext
from plugins.wxbot.group_context import (
    GROUP_CONTEXT_VARIABLE,
    WxbotGroupContextHook,
    WxbotGroupSummaryService,
    render_group_observation,
)

REFERENCE_TIME = datetime(2026, 4, 24, 3, 20, tzinfo=UTC)


class _PolicyStore:
    def __init__(self, retention_seconds: int = 3600, *, fail: bool = False) -> None:
        self.retention_seconds = retention_seconds
        self.fail = fail

    async def get_group_policy(self, tenant_id: str, session_id: str):
        assert (tenant_id, session_id) == ("demo", "room@chatroom")
        if self.fail:
            raise RuntimeError("policy unavailable")
        return SimpleNamespace(
            policy=SimpleNamespace(
                prompt_context_retention_seconds=self.retention_seconds,
            )
        )


class _ContextStore:
    def __init__(self) -> None:
        self.summary = {
            "summary_text": "群里已经决定周五发布。",
            "last_observation_id": 2,
            "version": 3,
        }
        self.rows = [
            {
                "id": 5,
                "message_id": "m-missed",
                "sender_name": "李四",
                "content": "上线前还要补一次回归测试",
                "occurred_ts": 1777000500,
                "metadata": {},
            },
            {
                "id": 4,
                "message_id": "m-in-session",
                "sender_name": "王五",
                "content": "这条已经在 session 里",
                "occurred_ts": 1777000480,
                "metadata": {},
            },
            {
                "id": 3,
                "message_id": "m-current",
                "sender_name": "张三",
                "content": "@机器人 那现在还差什么",
                "occurred_ts": 1777000520,
                "mentioned_me": True,
                "bot_addressed": True,
                "metadata": {"bot_normalized_content": "那现在还差什么"},
            },
            {
                "id": 2,
                "message_id": "m-covered",
                "sender_name": "赵六",
                "content": "已被摘要覆盖",
                "occurred_ts": 1777000400,
                "metadata": {},
            },
        ]

    async def get_group_summary_state(self, tenant_id: str, session_id: str):
        assert (tenant_id, session_id) == ("demo", "room@chatroom")
        return self.summary

    async def list_recent_group_observations(self, tenant_id: str, session_id: str, *, limit: int):
        assert (tenant_id, session_id) == ("demo", "room@chatroom")
        assert limit == 80
        return self.rows


def _group_ctx() -> PipelineContext:
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
        metadata={"session_kind": "group"},
        turns=[
            Turn(
                session_id="room@chatroom",
                role=Role.USER,
                content="这条已经在 session 里",
                metadata={"msg_svr_id": "m-in-session"},
            )
        ],
    )
    event = InboundEvent(
        message_id="m-current",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_current",
        session_id="room@chatroom",
        message=Message(content="@机器人 那现在还差什么"),
        received_at=REFERENCE_TIME,
        trace_id="trace-current",
        metadata={"msg_svr_id": "m-current", "mentioned_me": True},
    )
    return PipelineContext(event=event, trace_id=event.trace_id, session=session)


@pytest.mark.asyncio
async def test_group_context_hook_adds_summary_and_only_missing_session_messages() -> None:
    ctx = _group_ctx()
    hook = WxbotGroupContextHook(
        _ContextStore(),  # type: ignore[arg-type]
        SimpleNamespace(
            wxbot_group_context_enabled=True,
            wxbot_group_context_recent_limit=80,
            wxbot_group_context_budget_chars=6000,
            social_policy_legacy_wxbot_fallback_enabled=True,
        ),
        None,
    )

    await hook.run(ctx)

    payload = ctx.session.variables[GROUP_CONTEXT_VARIABLE]  # type: ignore[union-attr]
    assert payload["summary"] == "群里已经决定周五发布。"
    assert [row["message_id"] for row in payload["recent_observations"]] == ["m-missed"]
    assert "回归测试" in payload["recent_text"]
    assert ctx.signals["channel"]["wechat"]["group_context"]["recent_count"] == 1


@pytest.mark.asyncio
async def test_group_context_budget_prioritizes_newest_observations() -> None:
    ctx = _group_ctx()
    ctx.session.turns = []  # type: ignore[union-attr]
    ctx.event.message_id = "current-not-in-store"
    store = _ContextStore()
    store.summary = {"summary_text": "", "last_observation_id": 0, "version": 0}
    store.rows = [
        {
            "id": 3,
            "message_id": "newest",
            "sender_name": "新消息",
            "content": "最新话题" + ("新" * 30),
            "occurred_ts": 1777000500,
            "metadata": {},
        },
        {
            "id": 2,
            "message_id": "middle",
            "sender_name": "中间消息",
            "content": "中间话题" + ("中" * 30),
            "occurred_ts": 1777000450,
            "metadata": {},
        },
        {
            "id": 1,
            "message_id": "oldest",
            "sender_name": "旧消息",
            "content": "过期话题" + ("旧" * 30),
            "occurred_ts": 1777000400,
            "metadata": {},
        },
    ]
    newest_size = len(render_group_observation(store.rows[0]))
    hook = WxbotGroupContextHook(
        store,  # type: ignore[arg-type]
        SimpleNamespace(
            wxbot_group_context_enabled=True,
            wxbot_group_context_recent_limit=80,
            wxbot_group_context_budget_chars=newest_size + 8,
        ),
        _PolicyStore(),  # type: ignore[arg-type]
    )

    await hook.run(ctx)

    payload = ctx.session.variables[GROUP_CONTEXT_VARIABLE]  # type: ignore[union-attr]
    assert [item["message_id"] for item in payload["recent_observations"]] == [
        "newest"
    ]
    assert "最新话题" in payload["recent_text"]
    assert "过期话题" not in payload["recent_text"]


@pytest.mark.asyncio
async def test_group_context_retention_excludes_stale_observations_and_summary() -> None:
    ctx = _group_ctx()
    ctx.session.turns = []  # type: ignore[union-attr]
    ctx.event.message_id = "current-not-in-store"
    store = _ContextStore()
    store.summary = {
        "summary_text": "三个月前的秘密：旧版本口令是 stale-secret-7788。",
        "last_observation_id": 2,
        "version": 4,
    }
    store.rows = [
        {
            "id": 7,
            "message_id": "future-dated",
            "sender_name": "异常时钟",
            "content": "未来时间不能绕过保留窗口",
            "occurred_ts": 1778000000,
            "metadata": {},
        },
        {
            "id": 6,
            "message_id": "stale-new-id",
            "sender_name": "旧成员",
            "content": "这条虽然 ID 新但已经超过窗口",
            "occurred_ts": 1776990000,
            "metadata": {},
        },
        {
            "id": 5,
            "message_id": "fresh",
            "sender_name": "新成员",
            "content": "窗口内的新进展",
            "occurred_ts": 1777000500,
            "metadata": {},
        },
        {
            "id": 2,
            "message_id": "summary-source",
            "sender_name": "旧成员",
            "content": "摘要刚被一条普通新消息刷新",
            "occurred_ts": 1777000400,
            "metadata": {},
        },
    ]
    hook = WxbotGroupContextHook(
        store,  # type: ignore[arg-type]
        SimpleNamespace(
            wxbot_group_context_enabled=True,
            wxbot_group_context_recent_limit=80,
            wxbot_group_context_budget_chars=6000,
        ),
        _PolicyStore(retention_seconds=3600),  # type: ignore[arg-type]
    )

    await hook.run(ctx)

    payload = ctx.session.variables[GROUP_CONTEXT_VARIABLE]  # type: ignore[union-attr]
    assert payload["summary"] == ""
    assert [row["message_id"] for row in payload["recent_observations"]] == [
        "summary-source",
        "fresh",
    ]
    assert "stale-secret-7788" not in str(payload)
    assert "已经超过窗口" not in str(payload)
    assert "未来时间" not in str(payload)


@pytest.mark.asyncio
async def test_group_context_policy_unavailable_or_error_fails_closed() -> None:
    settings = SimpleNamespace(
        wxbot_group_context_enabled=True,
        wxbot_group_context_recent_limit=80,
        wxbot_group_context_budget_chars=6000,
        social_policy_legacy_wxbot_fallback_enabled=False,
    )
    for policy_store in (None, _PolicyStore(fail=True)):
        ctx = _group_ctx()
        hook = WxbotGroupContextHook(
            _ContextStore(),  # type: ignore[arg-type]
            settings,
            policy_store,  # type: ignore[arg-type]
        )

        await hook.run(ctx)

        assert GROUP_CONTEXT_VARIABLE not in ctx.session.variables  # type: ignore[union-attr]
        assert ctx.extras["wxbot_group_context"] == {
            "loaded": False,
            "reason": "social_policy_unavailable",
        }


@pytest.mark.asyncio
async def test_zero_retention_disables_prompt_context_without_deleting_storage() -> None:
    ctx = _group_ctx()
    store = _ContextStore()
    hook = WxbotGroupContextHook(
        store,  # type: ignore[arg-type]
        SimpleNamespace(wxbot_group_context_enabled=True),
        _PolicyStore(retention_seconds=0),  # type: ignore[arg-type]
    )

    await hook.run(ctx)

    assert GROUP_CONTEXT_VARIABLE not in ctx.session.variables  # type: ignore[union-attr]
    assert ctx.extras["wxbot_group_context"] == {
        "loaded": False,
        "reason": "prompt_context_retention_disabled",
    }
    # No observation/summary read or prune is needed; the underlying fake rows
    # remain untouched, mirroring the production control's prompt-only scope.
    assert len(store.rows) == 4


@pytest.mark.asyncio
async def test_explicit_legacy_fallback_authorizes_unbounded_context_load() -> None:
    settings = SimpleNamespace(
        wxbot_group_context_enabled=True,
        wxbot_group_context_recent_limit=80,
        wxbot_group_context_budget_chars=6000,
        social_policy_legacy_wxbot_fallback_enabled=True,
    )
    for policy_store in (None, _PolicyStore(fail=True)):
        ctx = _group_ctx()
        hook = WxbotGroupContextHook(
            _ContextStore(),  # type: ignore[arg-type]
            settings,
            policy_store,  # type: ignore[arg-type]
        )

        await hook.run(ctx)

        payload = ctx.session.variables[GROUP_CONTEXT_VARIABLE]  # type: ignore[union-attr]
        assert payload["summary"] == "群里已经决定周五发布。"
        assert payload["retention_seconds"] is None


@pytest.mark.asyncio
async def test_group_context_hook_clears_stale_runtime_payload_for_private_chat() -> None:
    ctx = _group_ctx()
    ctx.event.session_id = "private-user"
    ctx.session.session_id = "private-user"  # type: ignore[union-attr]
    ctx.session.variables[GROUP_CONTEXT_VARIABLE] = {"summary": "stale"}  # type: ignore[union-attr]
    hook = WxbotGroupContextHook(_ContextStore(), SimpleNamespace())  # type: ignore[arg-type]

    await hook.run(ctx)

    assert GROUP_CONTEXT_VARIABLE not in ctx.session.variables  # type: ignore[union-attr]


class _SummaryStore:
    def __init__(self) -> None:
        self.completed: list[dict] = []
        self.failed: list[dict] = []
        self.deferred: list[dict] = []
        self.pruned: list[dict] = []

    async def claim_group_summary_job(self, *, worker_id: str, lock_ttl_seconds: float):
        assert worker_id == "worker-a"
        assert lock_ttl_seconds == 180.0
        return {
            "tenant_id": "demo",
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "claim_token": "claim-1",
            "claimed_through_observation_id": 4,
        }

    async def get_group_summary_state(self, tenant_id: str, session_id: str):
        return {
            "summary_text": "旧摘要",
            "last_observation_id": 2,
            "message_count": 2,
        }

    async def list_group_observations_after(
        self,
        tenant_id: str,
        session_id: str,
        *,
        after_id: int,
        limit: int,
    ):
        assert after_id == 2
        assert limit == 80
        return [
            {
                "id": 3,
                "message_id": "m3",
                "sender_name": "张三",
                "content": "周五发布",
                "metadata": {},
            },
            {
                "id": 4,
                "message_id": "m4",
                "sender_name": "李四",
                "content": "发布前补回归测试",
                "metadata": {},
            },
        ]

    async def complete_group_summary_job(self, **kwargs):
        self.completed.append(kwargs)
        return True

    async def fail_group_summary_job(self, **kwargs):
        self.failed.append(kwargs)

    async def defer_group_summary_job(self, **kwargs):
        self.deferred.append(kwargs)
        return True

    async def prune_group_observations(self, **kwargs):
        self.pruned.append(kwargs)
        return 0


class _SummaryLLM:
    def __init__(self) -> None:
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        return SimpleNamespace(content="已确认事项\n- 周五发布\n未完成事项\n- 发布前补回归测试")


@pytest.mark.asyncio
async def test_group_summary_service_updates_job_without_session_lock() -> None:
    store = _SummaryStore()
    llm = _SummaryLLM()
    service = WxbotGroupSummaryService(
        store,  # type: ignore[arg-type]
        llm,
        SimpleNamespace(
            wxbot_group_summary_lock_ttl_seconds=180.0,
            wxbot_group_summary_batch_size=80,
            wxbot_group_summary_input_budget_chars=12000,
            wxbot_group_summary_timeout_seconds=5.0,
            wxbot_group_summary_max_chars=4000,
            wxbot_group_observation_retention_days=30,
        ),
    )

    gate_calls: list[tuple[str, str]] = []

    async def scope_allowed(tenant_id: str, session_id: str) -> bool:
        gate_calls.append((tenant_id, session_id))
        return True

    result = await service.drain_once(
        worker_id="worker-a",
        scope_execution_allowed=scope_allowed,
    )

    assert result == {"claimed": 1, "succeeded": 1, "failed": 0}
    assert len(llm.requests) == 1
    assert "周五发布" in llm.requests[0].messages[0].content
    assert store.completed == [
        {
            "tenant_id": "demo",
            "session_id": "room@chatroom",
            "covered_observation_id": 4,
            "summary_text": "已确认事项\n- 周五发布\n未完成事项\n- 发布前补回归测试",
            "worker_id": "worker-a",
            "claim_token": "claim-1",
        }
    ]
    assert store.failed == []
    assert store.deferred == []
    assert gate_calls == [
        ("demo", "room@chatroom"),
        ("demo", "room@chatroom"),
    ]
    assert store.pruned == [{"retention_days": 30, "keep_recent": 200}]


@pytest.mark.asyncio
async def test_group_summary_service_defers_disabled_scope_before_llm() -> None:
    store = _SummaryStore()
    llm = _SummaryLLM()
    service = WxbotGroupSummaryService(
        store,  # type: ignore[arg-type]
        llm,
        SimpleNamespace(wxbot_group_summary_lock_ttl_seconds=180.0),
    )

    async def scope_allowed(_tenant_id: str, _session_id: str) -> bool:
        return False

    result = await service.drain_once(
        worker_id="worker-a",
        scope_execution_allowed=scope_allowed,
    )

    assert result == {"claimed": 0, "succeeded": 0, "failed": 0}
    assert llm.requests == []
    assert store.completed == []
    assert store.failed == []
    assert store.deferred == [
        {
            "tenant_id": "demo",
            "session_id": "room@chatroom",
            "worker_id": "worker-a",
            "claim_token": "claim-1",
        }
    ]


@pytest.mark.asyncio
async def test_group_summary_service_fails_closed_when_scope_gate_is_missing() -> None:
    store = _SummaryStore()
    llm = _SummaryLLM()
    service = WxbotGroupSummaryService(
        store,  # type: ignore[arg-type]
        llm,
        SimpleNamespace(wxbot_group_summary_lock_ttl_seconds=180.0),
    )

    result = await service.drain_once(worker_id="worker-a")

    assert result == {"claimed": 0, "succeeded": 0, "failed": 0}
    assert llm.requests == []
    assert store.completed == []
    assert store.failed == []
    assert len(store.deferred) == 1


@pytest.mark.asyncio
async def test_group_summary_service_rechecks_scope_after_llm_before_write() -> None:
    store = _SummaryStore()
    enabled = True

    class _DisableScopeLLM(_SummaryLLM):
        async def chat(self, request):
            nonlocal enabled
            response = await super().chat(request)
            enabled = False
            return response

    llm = _DisableScopeLLM()
    service = WxbotGroupSummaryService(
        store,  # type: ignore[arg-type]
        llm,
        SimpleNamespace(
            wxbot_group_summary_lock_ttl_seconds=180.0,
            wxbot_group_summary_batch_size=80,
            wxbot_group_summary_input_budget_chars=12000,
            wxbot_group_summary_timeout_seconds=5.0,
            wxbot_group_summary_max_chars=4000,
        ),
    )

    async def scope_allowed(_tenant_id: str, _session_id: str) -> bool:
        return enabled

    result = await service.drain_once(
        worker_id="worker-a",
        scope_execution_allowed=scope_allowed,
    )

    assert result == {"claimed": 0, "succeeded": 0, "failed": 0}
    assert len(llm.requests) == 1
    assert store.completed == []
    assert store.failed == []
    assert len(store.deferred) == 1


@pytest.mark.asyncio
async def test_group_summary_cancel_releases_claim_before_escaping() -> None:
    store = _SummaryStore()
    llm_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class _BlockingLLM:
        async def chat(self, _request):
            llm_started.set()
            await asyncio.Event().wait()

    async def blocking_defer(**kwargs):
        cleanup_started.set()
        await release_cleanup.wait()
        store.deferred.append(kwargs)
        return True

    store.defer_group_summary_job = blocking_defer  # type: ignore[method-assign]
    service = WxbotGroupSummaryService(
        store,  # type: ignore[arg-type]
        _BlockingLLM(),
        SimpleNamespace(
            wxbot_group_summary_lock_ttl_seconds=180.0,
            wxbot_group_summary_batch_size=80,
            wxbot_group_summary_input_budget_chars=12000,
            wxbot_group_summary_timeout_seconds=5.0,
        ),
    )

    async def scope_allowed(_tenant_id: str, _session_id: str) -> bool:
        return True

    drain = asyncio.create_task(
        service.drain_once(
            worker_id="worker-a",
            scope_execution_allowed=scope_allowed,
        )
    )
    await asyncio.wait_for(llm_started.wait(), timeout=1)
    drain.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    drain.cancel()
    await asyncio.sleep(0)

    assert drain.done() is False
    release_cleanup.set()
    outcome = await asyncio.gather(drain, return_exceptions=True)

    assert isinstance(outcome[0], asyncio.CancelledError)
    assert store.completed == []
    assert store.failed == []
    assert store.deferred == [
        {
            "tenant_id": "demo",
            "session_id": "room@chatroom",
            "worker_id": "worker-a",
            "claim_token": "claim-1",
        }
    ]
