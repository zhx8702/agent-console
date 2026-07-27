from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.common.types import (
    CapabilityResult,
    Channel,
    InboundEvent,
    Message,
    PreprocessedMessage,
    RouteType,
    Session,
)
from app.orchestrator.effect_handlers import EffectDispatcher, EffectHandlerRegistry
from app.orchestrator.effects import (
    EFFECT_STATUS_DUPLICATE,
    EFFECT_STATUS_RECORDED,
    InMemoryEffectCommitter,
)
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort
from plugins.moderation.hooks import (
    REMINDER_TEXT,
    ModerationAppendReminderHook,
    ModerationAuditEffectHandler,
    ModerationAuditHook,
    ModerationDecorateOutputStep,
    ModerationEnforceInputStep,
    ModerationInspectInputStep,
    ModerationReplaceReminderHook,
)


class _FakeModerationStore:
    def __init__(
        self,
        *,
        reminder_mode: str = "append",
        webhook_enabled: bool = False,
        webhook_url: str = "",
    ) -> None:
        self.reminder_mode = reminder_mode
        self.webhook_enabled = webhook_enabled
        self.webhook_url = webhook_url
        self.scope_allowed = True
        self.settings = SimpleNamespace(
            moderation_webhook_allowed_hosts="example.com,qyapi.weixin.qq.com"
        )
        self.logged: list[dict[str, object]] = []
        self.updated: list[dict[str, object]] = []

    async def scope_execution_allowed(self, tenant_id: str, session_id: str) -> bool:
        _ = (tenant_id, session_id)
        return self.scope_allowed

    async def get_config(self, tenant_id: str, session_id: str) -> dict:
        return {
            "enabled": True,
            "reminder_mode": self.reminder_mode,
            "webhook_enabled": self.webhook_enabled,
            "webhook_url": self.webhook_url,
        }

    async def match_keywords(self, tenant_id: str, session_id: str, text: str) -> list[str]:
        return ["敏感词"] if "敏感词" in text else []

    async def log_event(self, **kwargs):
        self.logged.append(kwargs)
        return 101

    async def update_event(self, event_id: int, **kwargs) -> None:
        self.updated.append({"event_id": event_id, **kwargs})


def _make_ctx(text: str = "这条消息包含敏感词") -> PipelineContext:
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WEB,
        user_id="u1",
        session_id="s1",
        message=Message(content=text),
        trace_id="trace-1",
        metadata={"source": "web-console"},
    )
    session = Session(
        session_id="s1",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WEB,
    )
    pre = PreprocessedMessage(original_text=text, cleaned_text=text)
    return PipelineContext(event=event, trace_id="trace-1", session=session, pre=pre)


@pytest.mark.asyncio
async def test_moderation_append_mode_logs_and_appends_reminder() -> None:
    store = _FakeModerationStore(reminder_mode="append")
    audit = ModerationAuditHook(store)
    append = ModerationAppendReminderHook(store)
    ctx = _make_ctx()

    await audit.run(ctx)
    ctx.result = CapabilityResult(route=RouteType.LLM, reply_text="正常回复")
    await append.run(ctx)

    assert store.logged
    assert ctx.extras["_moderation_reminder_mode"] == "append"
    assert ctx.result.reply_text.endswith(REMINDER_TEXT)


@pytest.mark.asyncio
async def test_moderation_flow_steps_inspect_and_append_reminder() -> None:
    store = _FakeModerationStore(reminder_mode="append")
    inspect = ModerationInspectInputStep(store)
    enforce = ModerationEnforceInputStep(store)
    decorate = ModerationDecorateOutputStep(store)
    ctx = _make_ctx()

    inspect_result = await inspect.run(ctx)
    enforce_result = await enforce.run(ctx)
    ctx.result = CapabilityResult(route=RouteType.LLM, reply_text="正常回复")
    decorate_result = await decorate.run(ctx)

    assert inspect_result.reason == "matched"
    assert len(inspect_result.effects) == 1
    assert inspect_result.effects[0].type == "write_audit_event"
    assert inspect_result.effects[0].owner == "moderation"
    assert inspect_result.effects[0].payload["event_id"] == 101
    assert inspect_result.effects[0].payload["keywords"] == ["敏感词"]
    assert inspect_result.effects[0].idempotency_key == "moderation:audit:demo:s1:trace-1"
    assert store.logged
    assert ctx.signals["moderation"]["input"]["matched"] is True
    assert ctx.signals["moderation"]["input"]["keywords"] == ["敏感词"]
    assert enforce_result.action == "continue"
    assert enforce_result.reason == "not_replace"
    assert decorate_result.reason == "appended"
    assert decorate_result.result is ctx.result
    assert ctx.result.reply_text.endswith(REMINDER_TEXT)


@pytest.mark.asyncio
async def test_moderation_inspect_step_can_defer_audit_to_effect_handler() -> None:
    store = _FakeModerationStore(reminder_mode="append")
    inspect = ModerationInspectInputStep(store, effect_handler_enabled=True)
    ctx = _make_ctx()

    result = await inspect.run(ctx)

    assert result.reason == "matched"
    assert len(result.effects) == 1
    effect = result.effects[0]
    assert effect.type == "write_audit_event"
    assert effect.payload["commit_semantics"] == "gate_before_side_effect"
    assert effect.payload["event_id"] is None
    assert effect.payload["keywords"] == ["敏感词"]
    assert effect.payload["message_text"] == "这条消息包含敏感词"
    assert store.logged == []
    assert ctx.extras["_moderation_reminder_mode"] == "append"
    assert ctx.signals["moderation"]["input"]["matched"] is True


@pytest.mark.asyncio
async def test_moderation_audit_effect_handler_records_once_after_commit() -> None:
    store = _FakeModerationStore(reminder_mode="append")
    inspect = ModerationInspectInputStep(store, effect_handler_enabled=True)
    ctx = _make_ctx()
    registry = EffectHandlerRegistry()
    registry.register(
        "write_audit_event",
        "moderation",
        ModerationAuditEffectHandler(store),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())

    step_result = await inspect.run(ctx)
    first = await dispatcher.dispatch(step_result.effects[0], ctx)
    second = await dispatcher.dispatch(step_result.effects[0], ctx)

    assert first.status == EFFECT_STATUS_RECORDED
    assert second.status == EFFECT_STATUS_DUPLICATE
    assert len(store.logged) == 1
    assert store.logged[0]["matched"] == ["敏感词"]
    assert store.logged[0]["message_text"] == "这条消息包含敏感词"
    assert ctx.extras["_moderation_event_id"] == 101
    assert ctx.signals["effects"]["moderation"] == [
        {
            "type": "write_audit_event",
            "owner": "moderation",
            "idempotency_key": "moderation:audit:demo:s1:trace-1",
            "event_id": 101,
            "webhook_status": "",
            "status": "recorded",
        }
    ]


@pytest.mark.asyncio
async def test_moderation_replace_mode_aborts_before_capability() -> None:
    store = _FakeModerationStore(reminder_mode="replace")
    audit = ModerationAuditHook(store)
    replace = ModerationReplaceReminderHook(store)
    ctx = _make_ctx()

    await audit.run(ctx)

    with pytest.raises(HookAbort) as excinfo:
        await replace.run(ctx)

    assert excinfo.value.reply_text == REMINDER_TEXT


@pytest.mark.asyncio
async def test_moderation_flow_step_replace_mode_stops_for_finalization() -> None:
    store = _FakeModerationStore(reminder_mode="replace")
    inspect = ModerationInspectInputStep(store)
    enforce = ModerationEnforceInputStep(store)
    ctx = _make_ctx()

    await inspect.run(ctx)
    result = await enforce.run(ctx)

    assert result.action == "stop"
    assert result.finalize is True
    assert result.skip_output_safety is True
    assert result.reason == "moderation_reminder_replace"
    assert result.result is not None
    assert result.result.route == RouteType.CANNED
    assert result.result.reply_text == REMINDER_TEXT


@pytest.mark.asyncio
async def test_moderation_webhook_updates_event_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_safe_post(client, url: str, *, json: dict, policy):
        _ = client
        assert url == "https://example.com/webhook"
        assert json["matched_keywords"] == ["敏感词"]
        assert policy.allowed_hosts == frozenset(
            {"example.com", "qyapi.weixin.qq.com"}
        )
        assert policy.max_redirects == 0
        assert policy.max_response_bytes == 64 * 1024
        return httpx.Response(200)

    monkeypatch.setattr("plugins.moderation.hooks.safe_post", fake_safe_post)

    store = _FakeModerationStore(
        reminder_mode="off",
        webhook_enabled=True,
        webhook_url="https://example.com/webhook",
    )
    audit = ModerationAuditHook(store)
    ctx = _make_ctx()

    await audit.run(ctx)

    assert store.updated == [{"event_id": 101, "webhook_status": "sent"}]


@pytest.mark.asyncio
async def test_moderation_webhook_rechecks_scope_at_egress_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_safe_post(*args, **kwargs):
        _ = (args, kwargs)
        calls.append("sent")
        return httpx.Response(200)

    monkeypatch.setattr("plugins.moderation.hooks.safe_post", fake_safe_post)
    store = _FakeModerationStore(
        reminder_mode="off",
        webhook_enabled=True,
        webhook_url="https://example.com/webhook",
    )
    store.scope_allowed = False

    await ModerationAuditHook(store).run(_make_ctx())

    assert calls == []
    assert store.updated == [
        {"event_id": 101, "webhook_status": "skipped:scope_disabled"}
    ]


@pytest.mark.asyncio
async def test_moderation_webhook_blocks_non_allowlisted_private_destination() -> None:
    store = _FakeModerationStore(
        reminder_mode="off",
        webhook_enabled=True,
        webhook_url="https://127.0.0.1/internal",
    )
    audit = ModerationAuditHook(store)
    ctx = _make_ctx()

    await audit.run(ctx)

    assert store.updated == [
        {
            "event_id": 101,
            "webhook_status": "error:UnsafeOutboundURLError",
        }
    ]
