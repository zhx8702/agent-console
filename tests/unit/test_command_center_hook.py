from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import plugins.commands.plugin as commands_plugin_module
from app.billing import BillingCapture, BillingCoordinator, BillingQuote, BillingReservation
from app.billing.models import BillingResource, BillingSubject
from app.commands import CommandDefinition, CommandRegistryService
from app.common.types import Channel, InboundEvent, Message
from app.orchestrator.effect_handlers import EffectDispatcher, EffectHandlerRegistry
from app.orchestrator.effects import (
    EFFECT_STATUS_DUPLICATE,
    EFFECT_STATUS_RECORDED,
    InMemoryEffectCommitter,
)
from app.orchestrator.flow import StepResult
from app.orchestrator.outcome import RetryableProcessingError
from app.orchestrator.owner_gate import OwnerExecutionDecision
from app.orchestrator.pipeline import PipelineContext
from app.plugin.base import PluginContext
from app.plugin.hooks import (
    RESULT_PRODUCER_OWNER_KEY,
    HookAbort,
    HookPoint,
    HookRunner,
    trusted_result_producer_owner,
)
from plugins.commands.hooks import (
    CommandBillingAuditEffectHandler,
    CommandBillingSettlementEffectHandler,
    CommandCenterHook,
    CommandDispatchStep,
)
from plugins.commands.plugin import CommandsPlugin
from plugins.commands.store import CommandStore
from plugins.credits.hooks import build_credit_command_definitions
from plugins.wxbot.commands import build_wxbot_command_definitions


def test_commands_plugin_exposes_complete_read_only_owner_catalog() -> None:
    plugin = CommandsPlugin()

    async def handler(ctx, args: list[str]) -> str:
        _ = (ctx, args)
        return "ok"

    plugin.register_definitions(
        [
            CommandDefinition(
                plugin_name="ignored",
                command="/hello",
                aliases=("/hi", "/你好"),
                handler=handler,
            )
        ],
        owner="demo",
    )

    assert plugin.command_tokens_by_owner("demo") == ("/hello", "/hi", "/你好")
    catalog = plugin.catalog_by_owner()
    assert [item["owner"] for item in catalog["demo"]] == ["demo"]
    catalog["demo"][0]["command"] = "/mutated-copy"
    assert plugin.command_tokens_by_owner("demo") == ("/hello", "/hi", "/你好")


class _FakeCommandStore:
    def __init__(self) -> None:
        self.config = {
            "admin_user_ids": ["admin-user"],
            "user_commands": ["/签到", "/checkin", "/余额", "/balance"],
            "admin_commands": ["/sign-in", "/signin", "/签到模式"],
        }

    async def get_config(self, tenant_id: str, *, catalog: list[dict[str, object]]) -> dict:
        return dict(self.config, catalog=catalog, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_commands_plugin_injects_registry_execution_gate_into_both_dispatch_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _InitStore:
        async def ensure_tables(self) -> None:
            return None

    async def owner_gate(owner: str, ctx: PipelineContext) -> bool:
        _ = (owner, ctx)
        return True

    store = _InitStore()
    monkeypatch.setattr(commands_plugin_module, "CommandStore", lambda _settings: store)
    plugin = CommandsPlugin()
    await plugin.initialize(
        PluginContext(
            container=SimpleNamespace(
                billing=None,
                plugin_registry=SimpleNamespace(execution_allowed=owner_gate),
            ),
            settings=SimpleNamespace(),
        )
    )

    hook = plugin.get_pipeline_hooks()[0]
    step = plugin.get_flow_executors()["plugin.commands.dispatch"]

    assert hook.owner_gate is owner_gate
    assert step.owner_gate is owner_gate
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_command_owner_denial_skips_before_predicate_billing_and_handler() -> None:
    calls: list[str] = []

    class _StoreMustNotRun:
        async def get_config(self, tenant_id: str, *, catalog: list[dict[str, object]]) -> dict:
            _ = (tenant_id, catalog)
            raise AssertionError("command config must not load after owner denial")

    def should_handle(ctx: PipelineContext) -> bool:
        _ = ctx
        calls.append("predicate")
        return True

    def billing_metadata(ctx: PipelineContext, args: list[str]) -> dict[str, object]:
        _ = (ctx, args)
        calls.append("billing_metadata")
        return {}

    async def handler(ctx: PipelineContext, args: list[str]) -> str:
        _ = (ctx, args)
        calls.append("handler")
        return "must not run"

    async def owner_gate(owner: str, ctx: PipelineContext) -> OwnerExecutionDecision:
        _ = ctx
        calls.append(f"gate:{owner}")
        return OwnerExecutionDecision(False, "scope_disabled")

    service = CommandRegistryService()
    service.register(
        [
            CommandDefinition(
                plugin_name="guarded",
                command="/guarded",
                handler=handler,
                should_handle=should_handle,
                billing_metadata=billing_metadata,
            )
        ]
    )
    billing, provider = _fake_billing()
    ctx = _ctx("/guarded")
    hook = CommandCenterHook(
        _StoreMustNotRun(),
        service,
        billing,
        owner_gate=owner_gate,
    )
    step = CommandDispatchStep(
        _StoreMustNotRun(),
        service,
        billing,
        owner_gate=owner_gate,
    )

    with pytest.raises(HookAbort) as exc:
        await hook.run(ctx)

    assert exc.value.reply_text == ""
    assert exc.value.reason == "owner_disabled"
    assert calls == ["gate:guarded"]
    assert provider.reservations == []
    assert provider.captures == []
    assert provider.releases == []
    assert "_command_token" not in ctx.extras
    assert "_billing_command_reservation" not in ctx.extras
    assert ctx.signals["command"]["matched"] is False
    assert ctx.signals["command"]["command"] == "/guarded"
    assert ctx.signals["command"]["plugin_name"] == "guarded"
    assert ctx.signals["command"]["reason"] == "owner_disabled"
    assert ctx.signals["command"]["owner_gate_reason"] == "scope_disabled"
    assert ctx.signals["command"]["candidate"] is True
    assert ctx.signals["command"]["suppressed"] is True
    assert ctx.extras["suppress_outbound"] is True

    calls.clear()
    step_ctx = _ctx("/guarded")
    result = await step.run(step_ctx)

    assert calls == ["gate:guarded"]
    assert result.action == "stop"
    assert result.reason == "owner_disabled"
    assert result.publish_outbound is False
    assert result.append_assistant_turn is False
    assert result.effects == []
    assert provider.reservations == []
    assert provider.captures == []
    assert provider.releases == []
    assert "_command_token" not in step_ctx.extras
    assert "_billing_command_reservation" not in step_ctx.extras
    assert step_ctx.signals["command"]["owner_gate_reason"] == "scope_disabled"


@pytest.mark.asyncio
async def test_command_handler_reply_is_dropped_when_owner_is_disabled_on_return() -> None:
    enabled = True
    calls: list[str] = []

    async def handler(ctx: PipelineContext, args: list[str]) -> str:
        nonlocal enabled
        _ = (ctx, args)
        calls.append("handler")
        enabled = False
        return "stale command reply"

    async def owner_gate(owner: str, ctx: PipelineContext) -> bool:
        _ = ctx
        calls.append(f"gate:{owner}")
        return enabled

    store = _FakeCommandStore()
    store.config["user_commands"] = ["/race"]
    service = CommandRegistryService()
    service.register(
        [
            CommandDefinition(
                plugin_name="racing",
                command="/race",
                handler=handler,
            )
        ]
    )
    ctx = _ctx("/race")

    result = await CommandDispatchStep(
        store,
        service,
        owner_gate=owner_gate,
    ).run(ctx)

    assert calls == ["gate:racing", "handler", "gate:racing"]
    assert result.action == "stop"
    assert result.reason == "owner_disabled"
    assert result.result is None
    assert result.publish_outbound is False
    assert result.append_assistant_turn is False
    assert RESULT_PRODUCER_OWNER_KEY not in ctx.extras


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gate_mode", "expected_reason"),
    [
        ("error", "owner_gate_error"),
        ("timeout", "owner_gate_timeout"),
    ],
)
async def test_transient_command_owner_gate_failure_propagates_as_retryable(
    gate_mode: str,
    expected_reason: str,
) -> None:
    calls: list[str] = []

    def should_handle(ctx: PipelineContext) -> bool:
        _ = ctx
        calls.append("predicate")
        return True

    def billing_metadata(ctx: PipelineContext, args: list[str]) -> dict[str, object]:
        _ = (ctx, args)
        calls.append("billing_metadata")
        return {}

    async def handler(ctx: PipelineContext, args: list[str]) -> str:
        _ = (ctx, args)
        calls.append("handler")
        return "must not run"

    async def owner_gate(owner: str, ctx: PipelineContext) -> bool:
        _ = (owner, ctx)
        calls.append("gate")
        if gate_mode == "error":
            raise ConnectionError("durable plugin state unavailable")
        await asyncio.sleep(0.05)
        return True

    service = CommandRegistryService()
    service.register(
        [
            CommandDefinition(
                plugin_name="guarded",
                command="/guarded",
                handler=handler,
                should_handle=should_handle,
                billing_metadata=billing_metadata,
            )
        ]
    )
    billing, provider = _fake_billing()
    hook = CommandCenterHook(
        _FakeCommandStore(),
        service,
        billing,
        owner_gate=owner_gate,
        owner_gate_timeout_seconds=0.001,
    )
    step = CommandDispatchStep(
        _FakeCommandStore(),
        service,
        billing,
        owner_gate=owner_gate,
        owner_gate_timeout_seconds=0.001,
    )

    for runtime in (hook, step):
        ctx = _ctx("/guarded")
        with pytest.raises(RetryableProcessingError) as exc:
            await runtime.run(ctx)
        assert exc.value.reason == expected_reason
        assert exc.value.error_type == "CommandOwnerGateUnavailable"
        assert ctx.signals["command"]["reason"] == "owner_gate_unavailable"
        assert ctx.signals["command"]["owner_gate_reason"] == expected_reason
        assert "_command_token" not in ctx.extras
        assert "_billing_command_reservation" not in ctx.extras

    assert calls == ["gate", "gate"]
    assert provider.reservations == []
    assert provider.captures == []
    assert provider.releases == []


class _FakeBillingProvider:
    name = "credits"

    def __init__(self, amount: int = 5) -> None:
        self.amount = amount
        self.reservations: list[BillingReservation] = []
        self.captures: list[BillingReservation] = []
        self.releases: list[BillingReservation] = []

    async def quote(self, subject: BillingSubject, resource: BillingResource) -> BillingQuote:
        return BillingQuote(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=self.amount,
            currency="credits",
        )

    async def reserve(
        self, subject: BillingSubject, resource: BillingResource
    ) -> BillingReservation:
        reservation = BillingReservation(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=self.amount,
            currency="credits",
            reservation_id=f"reservation-{len(self.reservations) + 1}",
        )
        self.reservations.append(reservation)
        return reservation

    async def capture(
        self,
        reservation: BillingReservation,
        *,
        amount: int | None = None,
    ) -> BillingCapture:
        self.captures.append(reservation)
        return BillingCapture(
            provider=self.name,
            subject=reservation.subject,
            resource=reservation.resource,
            amount=amount or reservation.amount,
            currency=reservation.currency,
        )

    async def release(self, reservation: BillingReservation) -> None:
        self.releases.append(reservation)


def _fake_billing(
    provider: _FakeBillingProvider | None = None,
) -> tuple[BillingCoordinator, _FakeBillingProvider]:
    billing = BillingCoordinator()
    provider = provider or _FakeBillingProvider()
    billing.register_provider(provider)
    return billing, provider


class _FakeResearchService:
    async def research_group_messages(
        self, session, arguments: dict[str, object]
    ) -> dict[str, object]:
        return {
            "question": str(arguments.get("question") or ""),
            "time_window_hours": int(arguments.get("hours") or 24),
            "found": True,
            "total": 2,
            "keywords": ["draw"],
            "matched_keywords": ["draw"],
            "summary": "最近 24 小时内查到 2 条相关消息，主要发送者有 张三。",
            "messages": [
                {
                    "timestamp": "2026-04-24 12:00:00",
                    "sender_name": "张三",
                    "text": "今天有人提到 draw 功能",
                }
            ],
        }


class _FakeCreditCommandStore:
    async def get_config(self, tenant_id: str, session_id: str | None = None, **kwargs) -> dict:
        _ = tenant_id, session_id, kwargs
        return {
            "enabled": True,
            "credit_name": "积分",
        }

    async def get_top(
        self,
        tenant_id: str,
        session_id: str,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        _ = tenant_id, session_id, limit
        return [{"user_id": "u2", "display_name": "李四", "credits": 42}]


class _FakeWxbotBanStore:
    def __init__(self) -> None:
        self.bans: dict[tuple[str, str, str], dict[str, object]] = {}

    async def create_user_ban(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_wxid: str,
        user_name: str = "",
        reason: str = "",
        created_by: str = "",
        expires_at=None,
    ) -> dict[str, object]:
        row = {
            "id": len(self.bans) + 1,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "user_wxid": user_wxid,
            "user_name": user_name,
            "reason": reason,
            "created_by": created_by,
            "expires_at": expires_at,
            "revoked_at": None,
        }
        self.bans[(tenant_id, session_id, user_wxid)] = row
        return row

    async def revoke_user_ban(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_wxid: str,
    ) -> bool:
        return self.bans.pop((tenant_id, session_id, user_wxid), None) is not None

    async def list_active_user_bans(
        self,
        tenant_id: str,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        _ = limit
        return [
            row
            for (tid, sid, _wxid), row in self.bans.items()
            if tid == tenant_id and sid == session_id
        ]


class _FakeWxbotToolService:
    async def list_group_roster_members(self, session) -> list[dict[str, object]]:
        _ = session
        return [
            {"wxid": "wxid_target", "display_name": "张三"},
            {"wxid": "wxid_other", "display_name": "李四"},
        ]


def _ctx(
    content: str,
    *,
    user_id: str = "user-1",
    session_id: str = "room@chatroom",
    channel: Channel = Channel.WECHAT,
    metadata: dict[str, object] | None = None,
) -> PipelineContext:
    event_metadata = {"sender_name": "测试用户", "sender_wxid": user_id}
    event_metadata.update(metadata or {})
    event = InboundEvent(
        message_id="msg-1",
        tenant_id="tenant-1",
        channel=channel,
        user_id=user_id,
        session_id=session_id,
        message=Message(content=content),
        trace_id="trace-1",
        metadata=event_metadata,
    )
    return PipelineContext(event=event, trace_id="trace-1")


def _build_service() -> CommandRegistryService:
    service = CommandRegistryService()

    async def _checkin(ctx: PipelineContext, args: list[str]) -> str:
        return "签到成功"

    async def _signin_mode(ctx: PipelineContext, args: list[str]) -> str:
        return f"已切换到 {args[-1]}"

    service.register(
        [
            CommandDefinition(
                plugin_name="credits",
                command="/签到",
                aliases=("/checkin",),
                description="签到",
                handler=_checkin,
            ),
            CommandDefinition(
                plugin_name="credits",
                command="/sign-in",
                aliases=("/signin", "/签到模式"),
                description="切换签到模式",
                admin_only=True,
                handler=_signin_mode,
            ),
        ]
    )
    return service


def _build_echo_service(record: list[list[str]]) -> CommandRegistryService:
    service = CommandRegistryService()

    async def _echo(ctx: PipelineContext, args: list[str]) -> str:
        record.append(list(args))
        return "ok"

    service.register(
        [
            CommandDefinition(
                plugin_name="echo",
                command="/echo",
                aliases=(),
                description="echo",
                handler=_echo,
            ),
        ]
    )
    return service


def _build_release_duplicate_service() -> CommandRegistryService:
    service = CommandRegistryService()

    async def _dedupe(ctx: PipelineContext, args: list[str]) -> str:
        _ = args
        ctx.extras["_billing_command_force_release"] = True
        return "收到, 正在画。"

    service.register(
        [
            CommandDefinition(
                plugin_name="draw",
                command="/draw",
                aliases=(),
                description="dedupe",
                handler=_dedupe,
            ),
        ]
    )
    return service


def _build_credit_service() -> CommandRegistryService:
    service = CommandRegistryService()
    service.register(build_credit_command_definitions(_FakeCreditCommandStore()))
    return service


def _build_wxbot_ban_service(store: _FakeWxbotBanStore) -> CommandRegistryService:
    service = CommandRegistryService()
    service.register(build_wxbot_command_definitions(_FakeWxbotToolService(), store))
    return service


def test_command_registry_unregister_owner_removes_commands() -> None:
    service = _build_service()

    assert service.resolve("/签到") is not None
    assert service.unregister_owner("credits") == 2
    assert service.resolve("/签到") is None
    assert service.catalog() == []


@pytest.mark.asyncio
async def test_command_center_hook_handles_user_command() -> None:
    hook = CommandCenterHook(_FakeCommandStore(), _build_service())

    with pytest.raises(HookAbort) as exc:
        await hook.run(_ctx("/签到"))

    assert exc.value.reply_text == "签到成功"
    assert exc.value.result_producer_owner == "credits"


@pytest.mark.asyncio
async def test_command_hook_runner_preserves_registered_handler_owner() -> None:
    hook = CommandCenterHook(_FakeCommandStore(), _build_service())
    runner = HookRunner()
    runner.register(hook, owner="commands")
    ctx = _ctx("/签到")

    with pytest.raises(HookAbort) as exc:
        await runner.run(HookPoint.BEFORE_ROUTE, ctx)

    assert exc.value.result_producer_owner == "credits"
    assert trusted_result_producer_owner(ctx) == "credits"
    assert ctx.extras[RESULT_PRODUCER_OWNER_KEY] == "credits"


@pytest.mark.asyncio
async def test_command_center_hook_handles_user_command_on_non_wechat_channel() -> None:
    hook = CommandCenterHook(_FakeCommandStore(), _build_service())

    with pytest.raises(HookAbort) as exc:
        await hook.run(_ctx("/签到", channel=Channel.DISCORD, session_id="discord-room"))

    assert exc.value.reply_text == "签到成功"


@pytest.mark.asyncio
async def test_command_dispatch_step_handles_user_command() -> None:
    step = CommandDispatchStep(_FakeCommandStore(), _build_service())
    ctx = _ctx("/签到", channel=Channel.DISCORD, session_id="discord-room")

    result = await step.run(ctx)

    assert isinstance(result, StepResult)
    assert result.action == "stop"
    assert result.finalize is True
    assert result.skip_output_safety is True
    assert result.reason == "credits_command"
    assert result.result is not None
    assert result.result.reply_text == "签到成功"
    assert ctx.signals["command"]["matched"] is True
    assert ctx.signals["command"]["plugin_name"] == "credits"
    assert ctx.extras[RESULT_PRODUCER_OWNER_KEY] == "credits"


@pytest.mark.asyncio
async def test_wxbot_ban_commands_create_list_and_revoke() -> None:
    store = _FakeWxbotBanStore()
    command_store = _FakeCommandStore()
    command_store.config["admin_commands"] = [
        "/ban",
        "/禁言",
        "/unban",
        "/解禁",
        "/banlist",
        "/禁言列表",
    ]
    step = CommandDispatchStep(command_store, _build_wxbot_ban_service(store))

    ban_ctx = _ctx(
        "/ban @张三 10m 刷屏",
        user_id="admin-user",
        metadata={"sender_wxid": "admin-user", "at_wxids": ["wxid_target"]},
    )
    ban_result = await step.run(ban_ctx)

    assert ban_result.action == "stop"
    assert "已禁言" in ban_result.result.reply_text
    row = store.bans[("tenant-1", "room@chatroom", "wxid_target")]
    assert row["reason"] == "刷屏"
    assert row["created_by"] == "admin-user"
    assert row["expires_at"] is not None

    list_result = await step.run(
        _ctx("/banlist", user_id="admin-user", metadata={"sender_wxid": "admin-user"})
    )

    assert "张三" in list_result.result.reply_text
    assert "刷屏" in list_result.result.reply_text

    unban_result = await step.run(
        _ctx("/unban wxid_target", user_id="admin-user", metadata={"sender_wxid": "admin-user"})
    )

    assert "已解禁" in unban_result.result.reply_text
    assert store.bans == {}


@pytest.mark.asyncio
async def test_wxbot_ban_dispatches_with_legacy_admin_commands_text_missing_ban(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _build_wxbot_ban_service(_FakeWxbotBanStore())
    catalog = service.catalog()

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = sql, params
        return [
            {
                "tenant_id": "tenant-1",
                "admin_user_ids_text": "admin-user",
                "user_commands_text": "",
                "admin_commands_text": "/sign-in",
                "updated_at": None,
            }
        ]

    monkeypatch.setattr("plugins.commands.store._exec", fake_exec)
    cfg = await CommandStore(settings=None).get_config("tenant-1", catalog=catalog)
    command_store = _FakeCommandStore()
    command_store.config = cfg
    ban_store = _FakeWxbotBanStore()
    step = CommandDispatchStep(command_store, _build_wxbot_ban_service(ban_store))
    ctx = _ctx(
        "/ban @张三",
        user_id="admin-user",
        metadata={
            "sender_wxid": "admin-user",
            "at_wxids": ["wxid_target"],
            "mentioned_me": False,
        },
    )

    result = await step.run(ctx)

    assert "/ban" in cfg["admin_commands"]
    assert result.action == "stop"
    assert "已禁言" in result.result.reply_text
    assert ("tenant-1", "room@chatroom", "wxid_target") in ban_store.bans


@pytest.mark.asyncio
async def test_non_admin_wxbot_ban_does_not_execute() -> None:
    ban_store = _FakeWxbotBanStore()
    command_store = _FakeCommandStore()
    command_store.config["admin_commands"] = ["/ban", "/禁言"]
    step = CommandDispatchStep(command_store, _build_wxbot_ban_service(ban_store))
    ctx = _ctx(
        "/ban @张三",
        user_id="normal-user",
        metadata={
            "sender_wxid": "normal-user",
            "at_wxids": ["wxid_target"],
            "mentioned_me": False,
        },
    )

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.result is None
    assert result.reason == "command_denied"
    assert result.publish_outbound is False
    assert ctx.signals["command"]["denied"] is True
    assert ctx.signals["command"]["suppressed"] is True
    assert ban_store.bans == {}


@pytest.mark.asyncio
async def test_admin_only_command_in_user_commands_still_requires_admin_user_ids() -> None:
    ban_store = _FakeWxbotBanStore()
    command_store = _FakeCommandStore()
    command_store.config["user_commands"] = ["/ban"]
    command_store.config["admin_commands"] = []
    step = CommandDispatchStep(command_store, _build_wxbot_ban_service(ban_store))

    result = await step.run(
        _ctx(
            "/ban @张三",
            user_id="normal-user",
            metadata={"sender_wxid": "normal-user", "at_wxids": ["wxid_target"]},
        )
    )

    assert result.action == "stop"
    assert result.reason == "disabled"
    assert result.publish_outbound is False
    assert ban_store.bans == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["/禁言 @张三 1h", "禁言 @张三 1h"])
async def test_wxbot_ban_chinese_alias_dispatches_without_group_mention(content: str) -> None:
    ban_store = _FakeWxbotBanStore()
    command_store = _FakeCommandStore()
    command_store.config["admin_commands"] = ["/ban", "/禁言"]
    step = CommandDispatchStep(command_store, _build_wxbot_ban_service(ban_store))
    ctx = _ctx(
        content,
        user_id="admin-user",
        metadata={
            "sender_wxid": "admin-user",
            "at_wxids": ["wxid_target"],
            "mentioned_me": False,
        },
    )

    result = await step.run(ctx)

    assert result.action == "stop"
    assert "已禁言" in result.result.reply_text
    assert ("tenant-1", "room@chatroom", "wxid_target") in ban_store.bans


@pytest.mark.asyncio
async def test_wxbot_ban_command_rejects_self_and_bot() -> None:
    store = _FakeWxbotBanStore()
    command_store = _FakeCommandStore()
    command_store.config["admin_commands"] = ["/ban", "/禁言"]
    step = CommandDispatchStep(command_store, _build_wxbot_ban_service(store))

    self_result = await step.run(
        _ctx(
            "/ban wxid_admin",
            user_id="admin-user",
            metadata={"sender_wxid": "wxid_admin"},
        )
    )
    bot_result = await step.run(
        _ctx(
            "/ban wxid_bot",
            user_id="admin-user",
            metadata={"sender_wxid": "wxid_admin", "self_wxid": "wxid_bot"},
        )
    )

    assert self_result.result.reply_text == "不能禁言自己"
    assert bot_result.result.reply_text == "不能禁言机器人"
    assert store.bans == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["/积分排名", "/rank"])
async def test_command_dispatch_step_routes_credit_rank_aliases(content: str) -> None:
    store = _FakeCommandStore()
    store.config["user_commands"].extend(["/榜单", "/top"])
    step = CommandDispatchStep(store, _build_credit_service())
    ctx = _ctx(content)

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.reason == "credits_command"
    assert result.result is not None
    assert "积分 榜 Top 10：" in result.result.reply_text
    assert "李四" in result.result.reply_text
    assert "发积分排名试试" not in result.result.reply_text
    assert ctx.signals["command"]["matched"] is True
    assert ctx.signals["command"]["canonical_command"] == "/榜单"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["/积分排名", "/rank"])
async def test_command_dispatch_step_keeps_credit_rank_aliases_disabled_when_canonical_disabled(
    content: str,
) -> None:
    store = _FakeCommandStore()
    store.config["user_commands"].extend(["/top"])
    step = CommandDispatchStep(store, _build_credit_service())
    ctx = _ctx(content)

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.reason == "disabled"
    assert result.publish_outbound is False
    assert ctx.signals["command"]["matched"] is False
    assert ctx.signals["command"]["command"] == content.lower()
    assert ctx.signals["command"]["suppressed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["/积分排名", "/rank"])
async def test_command_dispatch_step_routes_credit_rank_aliases_from_saved_config_defaults(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    catalog = _build_credit_service().catalog()

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = sql, params
        return [
            {
                "tenant_id": "tenant-1",
                "admin_user_ids_text": "",
                "user_commands_text": "/签到\n/余额\n/榜单",
                "admin_commands_text": "",
                "updated_at": None,
            }
        ]

    monkeypatch.setattr("plugins.commands.store._exec", fake_exec)
    cfg = await CommandStore(settings=None).get_config("tenant-1", catalog=catalog)
    store = _FakeCommandStore()
    store.config = cfg
    step = CommandDispatchStep(store, _build_credit_service())
    ctx = _ctx(content)

    result = await step.run(ctx)

    assert "/rank" in cfg["user_commands"]
    assert "/积分排名" in cfg["user_commands"]
    assert cfg["user_commands_text"] == "/签到\n/余额\n/榜单"
    assert result.action == "stop"
    assert result.reason == "credits_command"
    assert result.result is not None
    assert "积分 榜 Top 10：" in result.result.reply_text


@pytest.mark.asyncio
async def test_command_dispatch_step_returns_billing_effects() -> None:
    billing, provider = _fake_billing()
    step = CommandDispatchStep(_FakeCommandStore(), _build_service(), billing)
    ctx = _ctx("/签到")

    result = await step.run(ctx)

    assert result.action == "stop"
    assert len(provider.reservations) == 1
    assert len(provider.captures) == 1
    assert [effect.type for effect in result.effects] == [
        "reserve_credits",
        "capture_credits",
    ]
    assert result.effects[0].owner == "commands"
    assert result.effects[0].payload["reservation_id"] == "reservation-1"
    assert result.effects[0].idempotency_key == ("commands:reserve_credits:reservation-1")
    assert result.effects[1].payload["settlement"] == "captured"
    assert result.effects[1].idempotency_key == ("commands:capture_credits:reservation-1")


@pytest.mark.asyncio
async def test_command_dispatch_step_releases_for_duplicate_command_handler() -> None:
    store = _FakeCommandStore()
    store.config["user_commands"] = ["/draw"]
    billing, provider = _fake_billing()
    step = CommandDispatchStep(store, _build_release_duplicate_service(), billing)
    ctx = _ctx("/draw 一只柴犬")

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.result is not None
    assert result.result.reply_text == "收到, 正在画。"
    assert len(provider.reservations) == 1
    assert provider.captures == []
    assert len(provider.releases) == 1
    assert [effect.type for effect in result.effects] == [
        "reserve_credits",
        "release_credits",
    ]
    assert result.effects[1].payload["settlement"] == "released"


@pytest.mark.asyncio
async def test_command_dispatch_step_can_defer_capture_to_effect_handler() -> None:
    billing, provider = _fake_billing()
    step = CommandDispatchStep(
        _FakeCommandStore(),
        _build_service(),
        billing,
        effect_handler_enabled=True,
    )
    ctx = _ctx("/签到")

    result = await step.run(ctx)

    assert result.action == "stop"
    assert len(provider.reservations) == 1
    assert provider.captures == []
    assert [effect.type for effect in result.effects] == [
        "reserve_credits",
        "capture_credits",
    ]
    assert result.effects[0].payload["commit_semantics"] == "audit_after_side_effect"
    assert result.effects[1].payload["commit_semantics"] == "gate_before_side_effect"
    assert result.effects[1].payload["settlement"] == "captured"
    assert result.effects[1].payload["display_name"] == "测试用户"
    assert result.effects[1].payload["resource_operation"] == "/签到"


@pytest.mark.asyncio
async def test_command_billing_settlement_handler_captures_once_after_commit() -> None:
    billing, provider = _fake_billing()
    step = CommandDispatchStep(
        _FakeCommandStore(),
        _build_service(),
        billing,
        effect_handler_enabled=True,
    )
    ctx = _ctx("/签到")
    registry = EffectHandlerRegistry()
    registry.register("reserve_credits", "commands", CommandBillingAuditEffectHandler())
    registry.register(
        "capture_credits",
        "commands",
        CommandBillingSettlementEffectHandler(billing),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())

    step_result = await step.run(ctx)
    reserve = await dispatcher.dispatch(step_result.effects[0], ctx)
    first = await dispatcher.dispatch(step_result.effects[1], ctx)
    second = await dispatcher.dispatch(step_result.effects[1], ctx)

    assert reserve.status == EFFECT_STATUS_RECORDED
    assert first.status == EFFECT_STATUS_RECORDED
    assert second.status == EFFECT_STATUS_DUPLICATE
    assert len(provider.captures) == 1
    assert provider.captures[0].reservation_id == "reservation-1"
    assert provider.captures[0].resource.operation == "/签到"
    assert ctx.signals["effects"]["commands"][0]["status"] == "audited"
    assert ctx.signals["effects"]["commands"][1]["status"] == "captured"


@pytest.mark.asyncio
async def test_command_dispatch_step_can_defer_release_to_effect_handler() -> None:
    async def _fail(ctx: PipelineContext, args: list[str]) -> str:
        raise ValueError("参数错误")

    store = _FakeCommandStore()
    store.config["user_commands"] = ["/fail"]
    service = CommandRegistryService()
    service.register(
        [
            CommandDefinition(
                plugin_name="fail",
                command="/fail",
                aliases=(),
                description="fail",
                handler=_fail,
            ),
        ]
    )
    billing, provider = _fake_billing()
    step = CommandDispatchStep(store, service, billing, effect_handler_enabled=True)
    ctx = _ctx("/fail")
    registry = EffectHandlerRegistry()
    registry.register(
        "release_credits",
        "commands",
        CommandBillingSettlementEffectHandler(billing),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())

    step_result = await step.run(ctx)

    assert step_result.action == "stop"
    assert step_result.result is not None
    assert step_result.result.reply_text == "参数错误"
    assert provider.releases == []
    assert step_result.effects[1].type == "release_credits"
    assert step_result.effects[1].payload["commit_semantics"] == "gate_before_side_effect"
    release = await dispatcher.dispatch(step_result.effects[1], ctx)

    assert release.status == EFFECT_STATUS_RECORDED
    assert len(provider.releases) == 1
    assert provider.releases[0].reservation_id == "reservation-1"
    assert ctx.signals["effects"]["commands"][0]["status"] == "released"


@pytest.mark.asyncio
async def test_command_dispatch_step_silently_stops_group_command_when_disabled() -> None:
    store = _FakeCommandStore()
    store.config["user_commands"] = []
    step = CommandDispatchStep(store, _build_service())
    ctx = _ctx("/签到")

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.reason == "disabled"
    assert result.result is None
    assert result.publish_outbound is False
    assert result.append_assistant_turn is False
    assert ctx.signals["command"]["matched"] is False
    assert ctx.signals["command"]["suppressed"] is True
    assert ctx.extras["skip_state_transition"] is True


@pytest.mark.asyncio
async def test_command_dispatch_step_silently_stops_unknown_group_slash_command() -> None:
    step = CommandDispatchStep(_FakeCommandStore(), _build_service())
    ctx = _ctx("@机器人 '/unknown 帮我看看", metadata={"mentioned_me": True})

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.reason == "unknown_command"
    assert result.result is None
    assert result.publish_outbound is False
    assert result.append_assistant_turn is False
    assert ctx.signals["command"]["candidate"] is True
    assert ctx.signals["command"]["suppressed"] is True


@pytest.mark.asyncio
async def test_command_dispatch_step_keeps_private_disabled_command_compatible() -> None:
    store = _FakeCommandStore()
    store.config["user_commands"] = []
    step = CommandDispatchStep(store, _build_service())
    ctx = _ctx("/签到", session_id="private-user")

    result = await step.run(ctx)

    assert result.action == "continue"
    assert result.reason == "disabled"
    assert ctx.signals["command"]["matched"] is False
    assert "suppressed" not in ctx.signals["command"]


@pytest.mark.asyncio
async def test_command_dispatch_step_keeps_private_permission_denial_reply() -> None:
    step = CommandDispatchStep(_FakeCommandStore(), _build_service())
    ctx = _ctx("/sign-in mode 2", user_id="normal-user", session_id="private-user")

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.result is not None
    assert result.result.reply_text == "你没有权限使用这个命令"


@pytest.mark.asyncio
async def test_command_center_hook_reserves_and_captures_user_command_charge() -> None:
    billing, provider = _fake_billing()
    hook = CommandCenterHook(_FakeCommandStore(), _build_service(), billing)

    with pytest.raises(HookAbort) as exc:
        await hook.run(_ctx("/签到"))

    assert exc.value.reply_text == "签到成功"
    assert len(provider.reservations) == 1
    assert provider.reservations[0].resource.kind == "command"
    assert provider.reservations[0].resource.operation == "/签到"
    assert len(provider.captures) == 1
    assert provider.releases == []


@pytest.mark.asyncio
async def test_command_center_hook_releases_charge_when_handler_returns_value_error() -> None:
    async def _fail(ctx: PipelineContext, args: list[str]) -> str:
        raise ValueError("参数错误")

    store = _FakeCommandStore()
    store.config["user_commands"] = ["/fail"]
    service = CommandRegistryService()
    service.register(
        [
            CommandDefinition(
                plugin_name="fail",
                command="/fail",
                aliases=(),
                description="fail",
                handler=_fail,
            ),
        ]
    )
    billing, provider = _fake_billing()
    hook = CommandCenterHook(store, service, billing)

    with pytest.raises(HookAbort) as exc:
        await hook.run(_ctx("/fail"))

    assert exc.value.reply_text == "参数错误"
    assert len(provider.reservations) == 1
    assert provider.captures == []
    assert len(provider.releases) == 1


@pytest.mark.asyncio
async def test_command_center_hook_silently_denies_group_admin_command_for_normal_user() -> None:
    hook = CommandCenterHook(_FakeCommandStore(), _build_service())

    with pytest.raises(HookAbort) as exc:
        await hook.run(_ctx("/sign-in mode 2", user_id="normal-user"))

    assert exc.value.reply_text == ""
    assert exc.value.reason == "command_denied"


@pytest.mark.asyncio
async def test_command_center_hook_silently_stops_disabled_group_command() -> None:
    store = _FakeCommandStore()
    store.config["user_commands"] = []
    hook = CommandCenterHook(store, _build_service())
    ctx = _ctx("/签到")

    with pytest.raises(HookAbort) as exc:
        await hook.run(ctx)

    assert exc.value.reply_text == ""
    assert exc.value.reason == "disabled"
    assert ctx.extras["suppress_outbound"] is True


@pytest.mark.asyncio
async def test_command_center_hook_handles_command_with_leading_quote_noise() -> None:
    hook = CommandCenterHook(_FakeCommandStore(), _build_service())

    with pytest.raises(HookAbort) as exc:
        await hook.run(_ctx("'/签到"))

    assert exc.value.reply_text == "签到成功"


@pytest.mark.asyncio
async def test_command_center_hook_handles_command_after_mention_and_quote() -> None:
    hook = CommandCenterHook(_FakeCommandStore(), _build_service())

    with pytest.raises(HookAbort) as exc:
        await hook.run(_ctx("@机器人 ‘/签到"))

    assert exc.value.reply_text == "签到成功"


@pytest.mark.asyncio
async def test_command_center_hook_trims_edge_mentions_from_known_command_args() -> None:
    record: list[list[str]] = []
    store = _FakeCommandStore()
    store.config["user_commands"] = ["/echo"]
    hook = CommandCenterHook(store, _build_echo_service(record))
    ctx = _ctx("@机器人 /echo @机器人 一只橘猫 @机器人")
    ctx.event.metadata["mentioned_me"] = True

    with pytest.raises(HookAbort) as exc:
        await hook.run(ctx)

    assert exc.value.reply_text == "ok"
    assert record == [["一只橘猫"]]


@pytest.mark.asyncio
async def test_command_center_hook_silently_stops_unknown_group_command_even_when_mentioned() -> (
    None
):
    hook = CommandCenterHook(_FakeCommandStore(), _build_service())
    ctx = _ctx("@机器人 /不存在的命令 帮我看看")
    ctx.event.metadata["mentioned_me"] = True

    with pytest.raises(HookAbort) as exc:
        await hook.run(ctx)

    assert exc.value.reply_text == ""
    assert exc.value.reason == "unknown_command"
    assert ctx.signals["command"]["suppressed"] is True
    assert ctx.extras["suppress_outbound"] is True


@pytest.mark.asyncio
async def test_command_center_hook_handles_wxbot_research_command() -> None:
    store = _FakeCommandStore()
    store.config["user_commands"] = ["/research", "/查记录"]
    service = CommandRegistryService()
    service.register(build_wxbot_command_definitions(_FakeResearchService()))
    hook = CommandCenterHook(store, service)

    with pytest.raises(HookAbort) as exc:
        await hook.run(_ctx("/research 24 最近谁提到 draw 功能"))

    assert "聊天记录 research" in exc.value.reply_text
    assert "draw" in exc.value.reply_text


@pytest.mark.asyncio
async def test_wxbot_research_command_ignores_non_wechat_channel() -> None:
    store = _FakeCommandStore()
    store.config["user_commands"] = ["/research", "/查记录"]
    service = CommandRegistryService()
    service.register(build_wxbot_command_definitions(_FakeResearchService()))
    hook = CommandCenterHook(store, service)

    await hook.run(
        _ctx(
            "/research 24 最近谁提到 draw 功能", channel=Channel.DISCORD, session_id="discord-room"
        )
    )
