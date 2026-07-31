from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

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
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort
from app.social.contracts import MemberPrivacyValues
from plugins.memory.hooks import MemoryControlHook, MemoryControlStep
from plugins.memory.store import MemoryMutationError

_DEFAULT_RESULT = object()


def _item(**kwargs: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "user-a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "explicit_user",
        "memory_type": "note",
        "content": "用户喜欢 Adidas",
        "status": "active",
        "pinned": False,
        "sensitivity": "normal",
        "deleted_at": None,
    }
    data.update(kwargs)
    return data


def _group_recall_policy() -> MemberPrivacyValues:
    return MemberPrivacyValues(
        memory_enabled=True,
        allow_group_recall=True,
        audience_scope="session",
    )


class _Store:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        privacy_policy: MemberPrivacyValues | None = None,
        privacy_error: Exception | None = None,
        member_write_blocked: bool = False,
        member_write_error: Exception | None = None,
        create_error: Exception | None = None,
        create_result: object = _DEFAULT_RESULT,
        forget_result: object = _DEFAULT_RESULT,
        full_forget_result: object = _DEFAULT_RESULT,
    ) -> None:
        self.rows = list(rows or [])
        self.created: list[dict[str, Any]] = []
        self.retrieve_calls: list[dict[str, Any]] = []
        self.forget_calls: list[dict[str, Any]] = []
        self.privacy_policy = privacy_policy or MemberPrivacyValues()
        self.privacy_error = privacy_error
        self.privacy_calls: list[dict[str, str]] = []
        self.member_write_blocked = member_write_blocked
        self.member_write_error = member_write_error
        self.member_write_checks: list[dict[str, str]] = []
        self.create_error = create_error
        self.create_result = create_result
        self.forget_result = forget_result
        self.full_forget_result = full_forget_result
        self.forget_member_calls: list[dict[str, Any]] = []
        self.forget_member_detailed_calls: list[dict[str, Any]] = []

    async def get_group_member_privacy_policy(self, **kwargs: str) -> MemberPrivacyValues:
        self.privacy_calls.append(kwargs)
        if self.privacy_error is not None:
            raise self.privacy_error
        return self.privacy_policy

    async def create_memory_item(self, **kwargs: Any) -> dict[str, Any] | None:
        self.created.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        if self.create_result is not _DEFAULT_RESULT:
            return self.create_result if isinstance(self.create_result, dict) else None
        item = _item(
            id=100 + len(self.created),
            tenant_id=kwargs["tenant_id"],
            channel=kwargs["channel"],
            source_key=kwargs["source_key"],
            user_id=kwargs["user_id"],
            session_id=kwargs["session_id"],
            scope_type=kwargs["scope_type"],
            source_type=kwargs["source_type"],
            memory_type=kwargs["memory_type"],
            content=kwargs["content"],
            status=kwargs["status"],
            pinned=kwargs["pinned"],
            sensitivity="normal",
        )
        self.rows.append(item)
        return item

    async def _member_memory_write_blocked(self, **kwargs: str) -> bool:
        self.member_write_checks.append(kwargs)
        if self.member_write_error is not None:
            raise self.member_write_error
        return self.member_write_blocked

    async def retrieve_memory_items(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.retrieve_calls.append(kwargs)
        return [
            item
            for item in self.rows
            if item.get("tenant_id") == kwargs["tenant_id"]
            and item.get("channel") == kwargs["channel"]
            and item.get("source_key") == kwargs["source_key"]
            and item.get("user_id") == kwargs["user_id"]
        ][: int(kwargs.get("limit") or 20)]

    async def forget_memory_items(self, **kwargs: Any) -> dict[str, Any]:
        self.forget_calls.append(kwargs)
        if self.forget_result is not _DEFAULT_RESULT:
            return dict(self.forget_result) if isinstance(self.forget_result, dict) else {}
        item_id = kwargs.get("item_id")
        affected: list[int] = []
        for item in self.rows:
            if (
                item.get("id") == item_id
                and item.get("tenant_id") == kwargs["tenant_id"]
                and item.get("channel") == kwargs["channel"]
                and item.get("source_key") == kwargs["source_key"]
                and item.get("user_id") == kwargs["user_id"]
                and str(item.get("session_id") or "") == str(kwargs.get("session_id") or "")
            ):
                item["status"] = "deleted"
                item["deleted_at"] = "now"
                affected.append(int(item["id"]))
        return {"ids": affected, "count": len(affected)}

    def _erase_member_rows(self, kwargs: dict[str, Any]) -> int:
        affected = 0
        for item in self.rows:
            if (
                item.get("tenant_id") == kwargs["tenant_id"]
                and item.get("user_id") == kwargs["user_id"]
                and (
                    not kwargs.get("channel")
                    or item.get("channel") == kwargs.get("channel")
                )
                and item.get("status") != "deleted"
            ):
                item["status"] = "deleted"
                item["deleted_at"] = "now"
                affected += 1
        return affected

    async def forget_member_detailed(self, **kwargs: Any) -> object:
        self.forget_member_detailed_calls.append(kwargs)
        if self.full_forget_result is not _DEFAULT_RESULT:
            return self.full_forget_result
        return {
            "count": self._erase_member_rows(kwargs),
            "complete": True,
            "residual_by_table": {},
        }

    async def forget_member(self, **kwargs: Any) -> int:
        self.forget_member_calls.append(kwargs)
        if self.full_forget_result is not _DEFAULT_RESULT:
            if isinstance(self.full_forget_result, dict):
                return int(self.full_forget_result.get("count") or 0)
            return int(self.full_forget_result)
        return self._erase_member_rows(kwargs)


def _ctx(
    content: str,
    *,
    user_id: str = "user-a",
    session_id: str = "room@chatroom",
    channel: Channel = Channel.WECHAT,
    session_kind: str = "",
) -> PipelineContext:
    metadata = {"source": "wxbot" if channel == Channel.WECHAT else channel.value}
    if session_kind:
        metadata["session_kind"] = session_kind
    event = InboundEvent(
        message_id=f"msg-{user_id}",
        tenant_id="demo",
        channel=channel,
        user_id=user_id,
        session_id=session_id,
        message=Message(content=content),
        trace_id=f"trace-{user_id}",
        metadata=metadata,
    )
    session = Session(
        session_id=session_id,
        tenant_id="demo",
        user_id=session_id,
        channel=channel,
    )
    return PipelineContext(
        event=event,
        trace_id=event.trace_id,
        session=session,
        pre=PreprocessedMessage(original_text=content, cleaned_text=content.strip()),
    )


async def _run_hook(store: _Store, ctx: PipelineContext) -> str | None:
    hook = MemoryControlHook(store)  # type: ignore[arg-type]
    with pytest.raises(HookAbort) as exc:
        await hook.run(ctx)
    return exc.value.reply_text


@pytest.mark.asyncio
async def test_group_remember_intent_is_blocked_without_member_memory_opt_in() -> None:
    store = _Store()
    ctx = _ctx("帮我记一下 我喜欢 Adidas")
    reply = await _run_hook(store, ctx)

    assert reply == "当前群未开启成员记忆，未保存。"
    assert store.created == []
    assert ctx.signals["memory"]["member_capture_blocked"] is True
    assert ctx.signals["memory_control"] == {
        "matched": True,
        "intent": "remember",
        "blocked": True,
        "reason": "member_privacy_blocked",
    }


@pytest.mark.asyncio
async def test_group_remember_intent_uses_session_audience_after_opt_in() -> None:
    store = _Store(
        privacy_policy=MemberPrivacyValues(
            memory_enabled=True,
            allow_group_recall=True,
            audience_scope="session",
            retention_days=30,
        )
    )
    reply = await _run_hook(store, _ctx("帮我记一下 我喜欢 Adidas"))

    assert reply == "已记住：我喜欢 Adidas"
    assert store.created[0]["user_id"] == "user-a"
    assert store.created[0]["session_id"] == "room@chatroom"
    assert store.created[0]["scope_type"] == "session"
    assert store.created[0]["source_type"] == "explicit_user"
    assert store.created[0]["content"] == "我喜欢 Adidas"
    assert store.created[0]["pinned"] is False
    assert store.created[0]["sensitivity"] == "normal"
    assert store.created[0]["origin_session_kind"] == "group"
    assert store.created[0]["audience_scope"] == "session"
    assert store.created[0]["allowed_session_ids"] == ["room@chatroom"]
    assert store.created[0]["source_kind"] == "conversation"
    expiry = datetime.fromisoformat(str(store.created[0]["expires_at"]))
    assert datetime.now(UTC) + timedelta(days=29, hours=23) <= expiry
    assert expiry <= datetime.now(UTC) + timedelta(days=30, minutes=1)


@pytest.mark.asyncio
async def test_group_remember_intent_fails_closed_when_privacy_policy_load_fails() -> None:
    store = _Store(privacy_error=RuntimeError("policy unavailable"))
    ctx = _ctx("记住 我默认要中文回复")

    reply = await _run_hook(store, ctx)

    assert reply == "当前群未开启成员记忆，未保存。"
    assert store.created == []
    assert ctx.signals["memory"]["privacy_fail_closed"] is True
    assert ctx.signals["memory"]["member_capture_blocked"] is True


@pytest.mark.asyncio
async def test_group_remember_intent_fails_closed_without_privacy_loader() -> None:
    store = _Store(
        privacy_policy=MemberPrivacyValues(
            memory_enabled=True,
            audience_scope="session",
        )
    )
    store.get_group_member_privacy_policy = None  # type: ignore[method-assign]
    ctx = _ctx("记住 我默认要中文回复")

    reply = await _run_hook(store, ctx)

    assert reply == "当前群未开启成员记忆，未保存。"
    assert store.created == []
    assert ctx.signals["memory"]["privacy_fail_closed"] is True
    assert ctx.signals["memory"]["member_capture_blocked"] is True


@pytest.mark.asyncio
async def test_non_wechat_group_remember_uses_same_member_privacy_boundary() -> None:
    store = _Store()
    ctx = _ctx(
        "记住 我默认要中文回复",
        session_id="feishu-group-1",
        channel=Channel.FEISHU,
        session_kind="group",
    )

    reply = await _run_hook(store, ctx)

    assert reply == "当前群未开启成员记忆，未保存。"
    assert store.created == []
    assert store.privacy_calls == [
        {
            "tenant_id": "demo",
            "session_id": "feishu-group-1",
            "user_id": "user-a",
        }
    ]


@pytest.mark.asyncio
async def test_group_remember_intent_rejects_explicit_audience_without_current_group() -> None:
    store = _Store(
        privacy_policy=MemberPrivacyValues(
            memory_enabled=True,
            audience_scope="explicit",
            allowed_session_ids=["other-room@chatroom"],
        )
    )

    reply = await _run_hook(store, _ctx("记住 我默认要中文回复"))

    assert reply == "当前群未开启成员记忆，未保存。"
    assert store.created == []


@pytest.mark.asyncio
async def test_private_remember_intent_preserves_identity_scope() -> None:
    store = _Store()
    reply = await _run_hook(
        store,
        _ctx("帮我记一下 我喜欢 Adidas", session_id="user-a"),
    )

    assert reply == "已记住：我喜欢 Adidas"
    assert store.created[0]["user_id"] == "user-a"
    assert store.created[0]["session_id"] == ""
    assert store.created[0]["scope_type"] == "identity"
    assert "audience_scope" not in store.created[0]
    assert store.member_write_checks == [{"tenant_id": "demo", "user_id": "user-a"}]


@pytest.mark.asyncio
async def test_private_remember_intent_respects_member_opt_out_or_deletion_pending() -> None:
    store = _Store(member_write_blocked=True)
    ctx = _ctx("帮我记一下 我喜欢 Adidas", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == "当前未开启个人记忆，未保存。"
    assert store.created == []
    assert store.member_write_checks == [{"tenant_id": "demo", "user_id": "user-a"}]
    assert ctx.signals["memory"]["member_capture_blocked"] is True
    assert ctx.signals["memory_control"] == {
        "matched": True,
        "intent": "remember",
        "blocked": True,
        "reason": "member_control_blocked",
    }


@pytest.mark.asyncio
async def test_private_remember_preflight_prefers_public_member_write_check() -> None:
    store = _Store()
    public_check = AsyncMock(return_value=True)
    private_check = AsyncMock(return_value=False)
    store.member_memory_write_blocked = public_check  # type: ignore[attr-defined]
    store._member_memory_write_blocked = private_check  # type: ignore[method-assign]
    ctx = _ctx("帮我记一下 我喜欢 Adidas", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == "当前未开启个人记忆，未保存。"
    public_check.assert_awaited_once_with(tenant_id="demo", user_id="user-a")
    private_check.assert_not_awaited()
    assert store.created == []


@pytest.mark.asyncio
async def test_private_remember_handles_member_opt_out_race_during_create() -> None:
    store = _Store(
        create_error=MemoryMutationError(
            "member_memory_write_blocked",
            status_code=409,
        )
    )
    ctx = _ctx("帮我记一下 我喜欢 Adidas", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == "当前未开启个人记忆，未保存。"
    assert len(store.created) == 1
    assert ctx.signals["memory"]["member_capture_blocked"] is True
    assert ctx.signals["memory_control"] == {
        "matched": True,
        "intent": "remember",
        "blocked": True,
        "reason": "member_control_blocked",
    }
    runtime = ctx.signals["memory"]["runtime"]
    assert runtime["control"]["blocked"] is True
    assert runtime["control"]["reason"] == "member_control_blocked"
    assert runtime["save"]["reason"] == "memory_control_handled"
    assert "persistence_failed" not in ctx.signals["memory"]


@pytest.mark.asyncio
async def test_private_remember_intent_fails_closed_when_member_control_load_fails() -> None:
    store = _Store(member_write_error=RuntimeError("member control unavailable"))
    ctx = _ctx("帮我记一下 我喜欢 Adidas", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == "当前未开启个人记忆，未保存。"
    assert store.created == []
    assert ctx.signals["memory"]["member_control_fail_closed"] is True
    assert ctx.signals["memory_control"]["reason"] == "member_control_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "intent"),
    [
        ("查一下我的记忆", "list"),
        ("搜索记忆 Adidas", "search"),
    ],
)
async def test_private_list_and_search_are_blocked_after_member_opt_out(
    command: str,
    intent: str,
) -> None:
    store = _Store(
        [_item(id=7, content="不应重新召回的个人记忆")],
        member_write_blocked=True,
    )
    ctx = _ctx(command, session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == "当前未开启个人记忆召回，未操作记忆。"
    assert "不应重新召回的个人记忆" not in str(reply)
    assert store.retrieve_calls == []
    assert store.forget_calls == []
    assert ctx.signals["memory"]["member_recall_blocked"] is True
    assert ctx.signals["memory_control"] == {
        "matched": True,
        "intent": intent,
        "blocked": True,
        "reason": "member_control_blocked",
    }


@pytest.mark.asyncio
async def test_private_exact_forget_remains_available_after_member_opt_out() -> None:
    store = _Store(
        [_item(id=7, content="退出后仍应允许删除的个人记忆")],
        member_write_blocked=True,
    )
    ctx = _ctx("忘记 #7", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert str(reply).startswith("已删除 1 条匹配记忆记录")
    assert [call["session_id"] for call in store.forget_calls] == ["user-a", ""]
    assert store.rows[0]["status"] == "deleted"
    assert store.retrieve_calls == []
    assert ctx.signals["memory_control"]["outcome"] == "deleted"


@pytest.mark.asyncio
async def test_private_query_forget_after_opt_out_deletes_without_disclosing_content() -> None:
    secret = "退出后不应回显的个人秘密"
    store = _Store([_item(id=7, content=secret, match_count=1)], member_write_blocked=True)
    ctx = _ctx("忘记 个人秘密", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert str(reply).startswith("已删除 1 条匹配记忆记录")
    assert secret not in str(reply)
    assert store.rows[0]["status"] == "deleted"
    assert ctx.signals["memory_control"]["candidate_details_redacted"] is True
    assert ctx.signals["memory"]["runtime"]["control"]["candidate_details_redacted"] is True


@pytest.mark.asyncio
async def test_wo_jide_does_not_trigger_remember() -> None:
    store = _Store()
    hook = MemoryControlHook(store)  # type: ignore[arg-type]
    ctx = _ctx("我记得我喜欢 Adidas")

    await hook.run(ctx)

    assert store.created == []
    assert ctx.signals["memory_control"] == {"matched": False, "reason": "no_intent"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected_content"),
    [
        ("请记住：我喜欢绿茶", "我喜欢绿茶"),
        ("请记住我喜欢绿茶", "我喜欢绿茶"),
        ("长期记一下 默认使用中文", "默认使用中文"),
        ("remember that I like green tea", "I like green tea"),
        ("Please remember: replies should be short", "replies should be short"),
        ("save this to memory: I prefer Chinese", "I prefer Chinese"),
        ("store I prefer concise replies in my memory", "I prefer concise replies"),
    ],
)
async def test_bilingual_remember_variants_are_explicit_and_anchored(
    command: str,
    expected_content: str,
) -> None:
    store = _Store()

    reply = await _run_hook(store, _ctx(command, session_id="user-a"))

    assert reply == f"已记住：{expected_content}"
    assert store.created[0]["content"] == expected_content
    assert store.created[0]["source_type"] == "explicit_user"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "intent", "expected_query"),
    [
        ("列出我的记忆", "list", ""),
        ("show me my memories", "list", ""),
        ("what do you remember about me?", "list", ""),
        ("搜索记忆 Adidas", "search", "Adidas"),
        ("在记忆中查找 Adidas", "search", "Adidas"),
        ("find memories about Adidas", "search", "Adidas"),
        ("what do you remember about Adidas?", "search", "Adidas"),
    ],
)
async def test_bilingual_list_and_search_variants_use_memory_only_scope(
    command: str,
    intent: str,
    expected_query: str,
) -> None:
    store = _Store([_item(id=7, content="用户喜欢 Adidas", match_count=1)])
    ctx = _ctx(command, session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert "用户喜欢 Adidas" in str(reply)
    assert store.retrieve_calls[0]["query"] == expected_query
    assert ctx.signals["memory_control"]["intent"] == intent
    control_runtime = ctx.signals["memory"]["runtime"]["control"]
    assert control_runtime["intent"] == intent
    assert control_runtime["selected_ids"] == [7]
    assert "Adidas" not in str(control_runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "清空我的所有记忆",
        "请删除我的全部记忆",
        "删除我所有的记忆",
        "删除所有关于我的记忆",
        "忘记我所有记忆",
        "把我的全部记忆都删掉",
        "forget all my memories",
        "Please delete all of my memories",
        "forget everything you remember about me",
    ],
)
async def test_bilingual_full_forget_clears_current_member_even_after_opt_out(
    command: str,
) -> None:
    store = _Store(
        [
            _item(id=7, content="个人记忆", session_id=""),
            _item(id=8, content="群会话记忆", session_id="room@chatroom", scope_type="session"),
        ],
        member_write_blocked=True,
    )
    ctx = _ctx(command, session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == "已清除你的全部记忆数据（共 2 项）。"
    assert [item["status"] for item in store.rows] == ["deleted", "deleted"]
    assert len(store.forget_member_detailed_calls) == 1
    assert store.forget_member_calls == []
    call = store.forget_member_detailed_calls[0]
    assert call["tenant_id"] == "demo"
    assert call["channel"] == "wechat"
    assert call["user_id"] == "user-a"
    assert call["session_id"] == "user-a"
    assert call["idempotency_key"].startswith("memory-control:forget-all:")
    assert len(call["idempotency_key"]) == len("memory-control:forget-all:") + 64
    assert store.member_write_checks == []
    assert ctx.signals["memory_control"]["intent"] == "forget_all"
    assert ctx.signals["memory_control"]["complete"] is True
    assert ctx.signals["memory"]["runtime"]["control"]["affected_count"] == 2


@pytest.mark.asyncio
async def test_group_full_forget_only_erases_current_member_not_other_members_or_group_owner() -> None:
    store = _Store(
        [
            _item(id=7, user_id="user-a", content="A 私聊记忆"),
            _item(
                id=8,
                user_id="user-a",
                content="A 群记忆",
                session_id="other-room@chatroom",
                scope_type="session",
            ),
            _item(id=9, user_id="user-b", content="B 的记忆"),
            _item(id=10, user_id="__group_history__", content="群共享聚合"),
        ],
        privacy_policy=MemberPrivacyValues(
            memory_enabled=False,
            allow_group_recall=False,
            audience_scope="session",
        ),
    )
    ctx = _ctx("清空我的全部记忆", user_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == "已清除你的全部记忆数据（共 2 项）。"
    assert [item["status"] for item in store.rows] == [
        "deleted",
        "deleted",
        "active",
        "active",
    ]
    assert store.forget_member_detailed_calls[0]["user_id"] == "user-a"
    assert store.forget_member_detailed_calls[0]["channel"] == "wechat"
    assert store.privacy_calls == []


@pytest.mark.asyncio
async def test_full_forget_structured_partial_result_reports_residual_truthfully() -> None:
    store = _Store(
        full_forget_result={
            "count": 3,
            "complete": False,
            "residual_by_table": {
                "message_effect_intent": {
                    "prepared": 1,
                    "running": 1,
                }
            },
        }
    )
    ctx = _ctx("delete all my memories", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == (
        "已清理 3 项记忆数据，但仍检测到 2 项残留，尚未完全清除；"
        "请稍后重试或到记忆管理页检查。"
    )
    assert ctx.signals["memory_control"]["partial"] is True
    assert ctx.signals["memory_control"]["complete"] is False
    assert ctx.signals["memory_control"]["residual_count"] == 2
    runtime_control = ctx.signals["memory"]["runtime"]["control"]
    assert runtime_control["partial"] is True
    assert runtime_control["residual_count"] == 2


@pytest.mark.asyncio
async def test_full_forget_structured_result_requires_explicit_complete_confirmation() -> None:
    store = _Store(full_forget_result={"count": 2})
    ctx = _ctx("清空我的所有记忆", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == "本次全量清理结果未确认，请稍后重试或到记忆管理页检查。"
    assert ctx.signals["memory_control"]["complete"] is False
    assert ctx.signals["memory_control"]["outcome"] == "unknown"


@pytest.mark.asyncio
async def test_full_forget_falls_back_to_legacy_int_api_without_channel_keyword() -> None:
    store = _Store()
    legacy_calls: list[dict[str, Any]] = []

    async def legacy_forget_member(
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> int:
        legacy_calls.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
        )
        return 2

    store.forget_member_detailed = None  # type: ignore[method-assign]
    store.forget_member = legacy_forget_member  # type: ignore[method-assign]
    ctx = _ctx("清空我的所有记忆", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == "已清除你的全部记忆数据（共 2 项）。"
    assert len(legacy_calls) == 1
    assert "channel" not in legacy_calls[0]
    assert ctx.signals["memory_control"]["complete"] is True


@pytest.mark.asyncio
async def test_full_forget_passes_non_wechat_channel_scope() -> None:
    store = _Store(
        [
            _item(
                id=7,
                channel="discord",
                source_key="discord",
                content="Discord 记忆",
            ),
            _item(id=8, channel="wechat", source_key="wxbot", content="微信记忆"),
        ]
    )
    ctx = _ctx(
        "delete all my memories",
        session_id="discord-channel",
        channel=Channel.DISCORD,
    )

    reply = await _run_hook(store, ctx)

    assert reply == "已清除你的全部记忆数据（共 1 项）。"
    assert [item["status"] for item in store.rows] == ["deleted", "active"]
    call = store.forget_member_detailed_calls[0]
    assert call["channel"] == "discord"
    assert call["user_id"] == "user-a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "忘记 Adidas",
        "从记忆中删除 Adidas",
        "please forget Adidas",
        "remove Adidas from my memory",
        "删除记忆 #7",
    ],
)
async def test_bilingual_forget_variants_delete_only_one_confirmed_match(command: str) -> None:
    store = _Store([_item(id=7, content="用户喜欢 Adidas")])

    reply = await _run_hook(store, _ctx(command, session_id="user-a"))

    assert str(reply).startswith("已删除 1 条匹配记忆记录")
    expected_call_count = 2 if "#7" in command else 1
    assert len(store.forget_calls) == expected_call_count
    assert store.forget_calls[-1]["item_id"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ordinary_text",
    [
        "我记得我喜欢 Adidas",
        "你记住了吗？",
        "记住我们第一次见面了吗？",
        "请记住我的密码吗",
        "我忘记密码了",
        "忘记 带钥匙了",
        "忘记一个人需要多久？",
        "I remember liking green tea",
        "Do you remember my name?",
        "Remember when we first met?",
        "I forgot my password",
        "Forget it",
        "How does memory search work?",
        "我忘记所有密码了",
        "忘了所有事",
        "请忘记 我的全部记忆吗",
        "清空缓存",
        "删除所有文件",
        "clear all browser caches",
    ],
)
async def test_memory_control_hard_negatives_do_not_intercept_ordinary_dialogue(
    ordinary_text: str,
) -> None:
    store = _Store([_item(id=7, content="不应被操作")])
    ctx = _ctx(ordinary_text, session_id="user-a")

    await MemoryControlHook(store).run(ctx)  # type: ignore[arg-type]

    assert store.created == []
    assert store.retrieve_calls == []
    assert store.forget_calls == []
    assert store.forget_member_calls == []
    assert store.forget_member_detailed_calls == []
    assert ctx.signals["memory_control"] == {"matched": False, "reason": "no_intent"}
    assert ctx.signals["memory"]["runtime"]["control"]["status"] == "not_matched"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("create_result", "reply_fragment", "outcome"),
    [
        ({"id": 11, "status": "active", "occurrence_count": 1}, "已记住", "saved"),
        ({"id": 12, "status": "pending"}, "待处理", "pending"),
        (
            {"id": 13, "status": "pending", "acceptance_status": "needs_review"},
            "待审核",
            "review",
        ),
        (
            {"id": 14, "status": "active", "occurrence_count": 2},
            "已存在",
            "duplicate",
        ),
        ({"id": 15, "status": "blocked", "privacy_blocked": True}, "未保存", "privacy_blocked"),
        (None, "未确认落库", "not_saved"),
    ],
)
async def test_remember_feedback_reflects_confirmed_store_outcome(
    create_result: object,
    reply_fragment: str,
    outcome: str,
) -> None:
    store = _Store(create_result=create_result)
    ctx = _ctx("记住 我喜欢绿茶", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply_fragment in str(reply)
    assert ctx.signals["memory_control"]["outcome"] == outcome
    runtime_control = ctx.signals["memory"]["runtime"]["control"]
    assert runtime_control["outcome"] == outcome
    assert "我喜欢绿茶" not in str(runtime_control)


@pytest.mark.asyncio
async def test_forget_partial_result_reports_residual_without_claiming_full_forget() -> None:
    store = _Store(
        [_item(id=7, content="用户喜欢 Adidas")],
        forget_result={
            "ids": [7],
            "count": 1,
            "partial": True,
            "residual_count": 2,
        },
    )
    ctx = _ctx("忘记 #7", session_id="user-a")

    reply = await _run_hook(store, ctx)

    assert reply == "已删除 1 条匹配记忆记录，但仍检测到 2 项相关残留，尚未完全清除。"
    assert "已忘记" not in str(reply)
    assert ctx.signals["memory_control"]["partial"] is True
    assert ctx.signals["memory_control"]["residual_count"] == 2
    runtime_control = ctx.signals["memory"]["runtime"]["control"]
    assert runtime_control["partial"] is True
    assert runtime_control["residual_count"] == 2


@pytest.mark.asyncio
async def test_forget_single_match_soft_deletes() -> None:
    store = _Store(
        [_item(id=7, content="用户喜欢 Adidas")],
        privacy_policy=_group_recall_policy(),
    )
    reply = await _run_hook(store, _ctx("忘记 Adidas"))

    assert reply == "已删除 1 条匹配记忆记录。相关摘要或派生信息可能需要稍后完成清理。"
    assert store.forget_calls == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "user-a",
            "session_id": "",
            "item_id": 7,
            "query": "",
            "allow_pinned": False,
            "limit": 1,
        }
    ]
    assert store.rows[0]["status"] == "deleted"


@pytest.mark.asyncio
async def test_forget_query_ignores_visible_fallback_and_deletes_only_real_match() -> None:
    store = _Store(
        [
            _item(id=7, content="用户喜欢 Adidas", match_count=1),
            _item(id=8, content="用户住在上海", match_count=0),
        ],
        privacy_policy=_group_recall_policy(),
    )
    reply = await _run_hook(store, _ctx("忘记 Adidas"))

    assert reply == "已删除 1 条匹配记忆记录。相关摘要或派生信息可能需要稍后完成清理。"
    assert store.forget_calls == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "user-a",
            "session_id": "",
            "item_id": 7,
            "query": "",
            "allow_pinned": False,
            "limit": 1,
        }
    ]
    assert store.rows[0]["status"] == "deleted"
    assert store.rows[1]["status"] == "active"


@pytest.mark.asyncio
async def test_forget_multi_candidates_does_not_delete_and_returns_candidates() -> None:
    store = _Store(
        [
            _item(id=1, content="用户喜欢 Adidas"),
            _item(id=2, content="用户喜欢 Adidas 跑鞋"),
        ],
        privacy_policy=_group_recall_policy(),
    )
    reply = await _run_hook(store, _ctx("忘记 Adidas"))

    assert reply is not None
    assert "找到多条匹配记忆" in reply
    assert "#1 用户喜欢 Adidas" in reply
    assert "#2 用户喜欢 Adidas 跑鞋" in reply
    assert store.forget_calls == []
    assert [item["status"] for item in store.rows] == ["active", "active"]


@pytest.mark.asyncio
async def test_forget_pinned_or_manual_does_not_auto_delete() -> None:
    store = _Store(
        [_item(id=3, content="用户是 VIP", source_type="manual", pinned=True)],
        privacy_policy=_group_recall_policy(),
    )
    reply = await _run_hook(store, _ctx("忘记 VIP"))

    assert reply is not None
    assert "受保护记忆" in reply
    assert "#3 用户是 VIP" in reply
    assert store.forget_calls == []
    assert store.rows[0]["status"] == "active"


@pytest.mark.asyncio
async def test_search_excludes_pending_deleted_invalidated_sensitive() -> None:
    store = _Store(
        [
            _item(id=1, content="可见记忆"),
            _item(id=2, content="pending 记忆", status="pending"),
            _item(id=3, content="deleted 记忆", status="deleted", deleted_at="now"),
            _item(id=4, content="invalidated 记忆", status="invalidated"),
            _item(id=5, content="sensitive 记忆", sensitivity="sensitive"),
        ],
        privacy_policy=_group_recall_policy(),
    )
    reply = await _run_hook(store, _ctx("我有哪些记忆"))

    assert reply == "找到 1 条记忆：\n- #1 可见记忆"


@pytest.mark.asyncio
async def test_search_query_excludes_visible_fallback_rows() -> None:
    store = _Store(
        [
            _item(id=1, content="用户喜欢 Adidas", match_count=1),
            _item(id=2, content="用户住在上海", match_count=0),
        ],
        privacy_policy=_group_recall_policy(),
    )
    reply = await _run_hook(store, _ctx("搜索记忆 Adidas"))

    assert reply == "找到 1 条记忆：\n- #1 用户喜欢 Adidas"


@pytest.mark.asyncio
async def test_group_user_a_b_isolation() -> None:
    store = _Store(
        [
            _item(id=1, user_id="user-a", content="A 的记忆"),
            _item(id=2, user_id="user-b", content="B 的记忆"),
        ],
        privacy_policy=_group_recall_policy(),
    )

    reply_a = await _run_hook(store, _ctx("查一下我的记忆", user_id="user-a"))
    reply_b = await _run_hook(store, _ctx("查一下我的记忆", user_id="user-b"))

    assert "A 的记忆" in str(reply_a)
    assert "B 的记忆" not in str(reply_a)
    assert "B 的记忆" in str(reply_b)
    assert "A 的记忆" not in str(reply_b)
    assert [call["user_id"] for call in store.retrieve_calls] == ["user-a", "user-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "intent"),
    [
        ("查一下我的记忆", "list"),
        ("搜索记忆 秘密", "search"),
    ],
)
async def test_group_list_and_search_are_blocked_without_recall_consent(
    command: str,
    intent: str,
) -> None:
    store = _Store(
        [_item(id=7, content="不应回显的秘密")],
        privacy_policy=MemberPrivacyValues(
            memory_enabled=True,
            allow_group_recall=False,
            audience_scope="session",
        ),
    )
    ctx = _ctx(command)

    reply = await _run_hook(store, ctx)

    assert reply == "当前群未开启成员记忆召回，未操作记忆。"
    assert "不应回显的秘密" not in str(reply)
    assert store.retrieve_calls == []
    assert store.forget_calls == []
    assert store.rows[0]["status"] == "active"
    assert ctx.signals["memory"]["member_recall_blocked"] is True
    assert ctx.signals["memory_control"] == {
        "matched": True,
        "intent": intent,
        "blocked": True,
        "reason": "member_privacy_blocked",
    }


@pytest.mark.asyncio
async def test_group_exact_forget_remains_available_without_recall_consent() -> None:
    secret = "退出群记忆后仍应允许删除"
    store = _Store(
        [
            _item(
                id=7,
                content=secret,
                session_id="room@chatroom",
                scope_type="session",
            )
        ],
        privacy_policy=MemberPrivacyValues(
            memory_enabled=False,
            allow_group_recall=False,
            audience_scope="session",
        ),
    )
    ctx = _ctx("忘记 #7")

    reply = await _run_hook(store, ctx)

    assert str(reply).startswith("已删除 1 条匹配记忆记录")
    assert secret not in str(reply)
    assert store.rows[0]["status"] == "deleted"
    assert store.retrieve_calls == []
    assert store.forget_calls[0]["session_id"] == "room@chatroom"


@pytest.mark.asyncio
async def test_group_query_forget_without_recall_consent_deletes_single_match_privately() -> None:
    secret = "群内不可回显的配送偏好"
    store = _Store(
        [
            _item(
                id=7,
                content=secret,
                match_count=1,
                session_id="room@chatroom",
                scope_type="session",
            )
        ],
        privacy_policy=MemberPrivacyValues(
            memory_enabled=False,
            allow_group_recall=False,
            audience_scope="session",
        ),
    )
    ctx = _ctx("忘记 配送偏好")

    reply = await _run_hook(store, ctx)

    assert str(reply).startswith("已删除 1 条匹配记忆记录")
    assert secret not in str(reply)
    assert store.rows[0]["status"] == "deleted"
    assert ctx.signals["memory_control"]["candidate_details_redacted"] is True


@pytest.mark.asyncio
async def test_group_query_forget_without_recall_consent_redacts_ambiguous_candidates() -> None:
    secrets = ["秘密偏好 A", "秘密偏好 B"]
    store = _Store(
        [
            _item(
                id=index,
                content=content,
                match_count=1,
                session_id="room@chatroom",
                scope_type="session",
            )
            for index, content in enumerate(secrets, start=7)
        ],
        privacy_policy=MemberPrivacyValues(
            memory_enabled=False,
            allow_group_recall=False,
            audience_scope="session",
        ),
    )
    ctx = _ctx("忘记 秘密偏好")

    reply = await _run_hook(store, ctx)

    assert "找到多条匹配记忆" in str(reply)
    assert "记忆管理页" in str(reply)
    assert all(secret not in str(reply) for secret in secrets)
    assert store.forget_calls == []
    assert ctx.signals["memory_control"]["candidate_details_redacted"] is True
    assert ctx.signals["memory_control"]["candidate_count"] == 2


@pytest.mark.asyncio
async def test_faq_ordinary_message_not_intercepted() -> None:
    store = _Store([_item(id=1, content="用户喜欢 Adidas")])
    hook = MemoryControlHook(store)  # type: ignore[arg-type]
    ctx = _ctx("退货政策是什么")

    await hook.run(ctx)

    assert store.created == []
    assert store.retrieve_calls == []
    assert store.forget_calls == []
    assert ctx.signals["memory_control"]["matched"] is False


@pytest.mark.asyncio
async def test_memory_control_step_stops_before_ordinary_route() -> None:
    store = _Store()
    step = MemoryControlStep(store)  # type: ignore[arg-type]
    result = await step.run(_ctx("记住 我默认要中文回复"))

    assert result.action == "stop"
    assert result.reason == "memory_control_intent"
    assert isinstance(result.result, CapabilityResult)
    assert result.result.route == RouteType.CANNED
    assert result.result.reply_text == "当前群未开启成员记忆，未保存。"
    assert store.created == []
    assert result.result.metadata == {"response_guard_allow_echo": True}
    assert result.finalize is True
    assert result.skip_output_safety is True
