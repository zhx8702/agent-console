from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

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


class _Store:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        privacy_policy: MemberPrivacyValues | None = None,
        privacy_error: Exception | None = None,
    ) -> None:
        self.rows = list(rows or [])
        self.created: list[dict[str, Any]] = []
        self.retrieve_calls: list[dict[str, Any]] = []
        self.forget_calls: list[dict[str, Any]] = []
        self.privacy_policy = privacy_policy or MemberPrivacyValues()
        self.privacy_error = privacy_error
        self.privacy_calls: list[dict[str, str]] = []

    async def get_group_member_privacy_policy(self, **kwargs: str) -> MemberPrivacyValues:
        self.privacy_calls.append(kwargs)
        if self.privacy_error is not None:
            raise self.privacy_error
        return self.privacy_policy

    async def create_memory_item(self, **kwargs: Any) -> dict[str, Any] | None:
        self.created.append(kwargs)
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
        item_id = kwargs.get("item_id")
        affected: list[int] = []
        for item in self.rows:
            if item.get("id") == item_id and item.get("user_id") == kwargs["user_id"]:
                item["status"] = "deleted"
                item["deleted_at"] = "now"
                affected.append(int(item["id"]))
        return {"ids": affected, "count": len(affected)}


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


@pytest.mark.asyncio
async def test_wo_jide_does_not_trigger_remember() -> None:
    store = _Store()
    hook = MemoryControlHook(store)  # type: ignore[arg-type]
    ctx = _ctx("我记得我喜欢 Adidas")

    await hook.run(ctx)

    assert store.created == []
    assert ctx.signals["memory_control"] == {"matched": False, "reason": "no_intent"}


@pytest.mark.asyncio
async def test_forget_single_match_soft_deletes() -> None:
    store = _Store([_item(id=7, content="用户喜欢 Adidas")])
    reply = await _run_hook(store, _ctx("忘记 Adidas"))

    assert reply == "已忘记 1 条记忆"
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
    store = _Store([
        _item(id=7, content="用户喜欢 Adidas", match_count=1),
        _item(id=8, content="用户住在上海", match_count=0),
    ])
    reply = await _run_hook(store, _ctx("忘记 Adidas"))

    assert reply == "已忘记 1 条记忆"
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
    store = _Store([
        _item(id=1, content="用户喜欢 Adidas"),
        _item(id=2, content="用户喜欢 Adidas 跑鞋"),
    ])
    reply = await _run_hook(store, _ctx("忘记 Adidas"))

    assert reply is not None
    assert "找到多条匹配记忆" in reply
    assert "#1 用户喜欢 Adidas" in reply
    assert "#2 用户喜欢 Adidas 跑鞋" in reply
    assert store.forget_calls == []
    assert [item["status"] for item in store.rows] == ["active", "active"]


@pytest.mark.asyncio
async def test_forget_pinned_or_manual_does_not_auto_delete() -> None:
    store = _Store([
        _item(id=3, content="用户是 VIP", source_type="manual", pinned=True),
    ])
    reply = await _run_hook(store, _ctx("忘记 VIP"))

    assert reply is not None
    assert "受保护记忆" in reply
    assert "#3 用户是 VIP" in reply
    assert store.forget_calls == []
    assert store.rows[0]["status"] == "active"


@pytest.mark.asyncio
async def test_search_excludes_pending_deleted_invalidated_sensitive() -> None:
    store = _Store([
        _item(id=1, content="可见记忆"),
        _item(id=2, content="pending 记忆", status="pending"),
        _item(id=3, content="deleted 记忆", status="deleted", deleted_at="now"),
        _item(id=4, content="invalidated 记忆", status="invalidated"),
        _item(id=5, content="sensitive 记忆", sensitivity="sensitive"),
    ])
    reply = await _run_hook(store, _ctx("我有哪些记忆"))

    assert reply == "找到 1 条记忆：\n- #1 可见记忆"


@pytest.mark.asyncio
async def test_search_query_excludes_visible_fallback_rows() -> None:
    store = _Store([
        _item(id=1, content="用户喜欢 Adidas", match_count=1),
        _item(id=2, content="用户住在上海", match_count=0),
    ])
    reply = await _run_hook(store, _ctx("搜索记忆 Adidas"))

    assert reply == "找到 1 条记忆：\n- #1 用户喜欢 Adidas"


@pytest.mark.asyncio
async def test_group_user_a_b_isolation() -> None:
    store = _Store([
        _item(id=1, user_id="user-a", content="A 的记忆"),
        _item(id=2, user_id="user-b", content="B 的记忆"),
    ])

    reply_a = await _run_hook(store, _ctx("查一下我的记忆", user_id="user-a"))
    reply_b = await _run_hook(store, _ctx("查一下我的记忆", user_id="user-b"))

    assert "A 的记忆" in str(reply_a)
    assert "B 的记忆" not in str(reply_a)
    assert "B 的记忆" in str(reply_b)
    assert "A 的记忆" not in str(reply_b)
    assert [call["user_id"] for call in store.retrieve_calls] == ["user-a", "user-b"]


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
