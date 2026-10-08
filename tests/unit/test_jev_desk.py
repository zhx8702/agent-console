import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.channel.session_aliases import collect_session_aliases, session_policy_aliases
from app.jev.desk import (
    attach_turns,
    build_desk,
    classify_turn,
    desk_conflicts,
    evaluation_lane,
    funnel_from_rows,
)
from app.jev.models import JevPolicy


def _item(*, choice="reply", applied=False, reason="low_confidence", status="completed", error="", domain="participation"):
    return {
        "domain": domain,
        "status": status,
        "applied": applied,
        "error_type": error,
        "result": {
            "answers": {"decision": {"choice": choice, "confidence": 0.71}},
            "_audit": {"reason": reason, "trace_id": "tr-help", "min_confidence": 0.8},
        },
    }


def test_session_aliases_include_external_and_canonical_ids():
    aliases = collect_session_aliases(
        "cx1:c:room@chatroom",
        [{
            "session_id": "cx1:c:room@chatroom",
            "metadata": {
                "external_conversation_id": "49025625236@chatroom",
                "canonical_conversation_id": "cx1:c:room@chatroom",
            },
        }],
    )
    assert aliases == ["cx1:c:room@chatroom", "49025625236@chatroom"]


@pytest.mark.asyncio
async def test_session_policy_aliases_survive_missing_sessions_table() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        aliases = await session_policy_aliases("tenant-a", "room@chatroom", db=db)
    await engine.dispose()
    assert aliases == ["room@chatroom"]


@pytest.mark.asyncio
async def test_session_policy_aliases_read_sqlite_json_metadata() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "CREATE TABLE sessions (tenant_id TEXT, session_id TEXT, metadata TEXT)"
        )
        await connection.exec_driver_sql(
            "INSERT INTO sessions (tenant_id, session_id, metadata) VALUES "
            "('tenant-a', 'cx1:c:room@chatroom', "
            "'{\"external_conversation_id\":\"49025625236@chatroom\"}')"
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        aliases = await session_policy_aliases(
            "tenant-a",
            "49025625236@chatroom",
            db=db,
        )
    await engine.dispose()
    assert aliases == ["49025625236@chatroom", "cx1:c:room@chatroom"]


def test_lanes_split_observe_blocked_applied_and_timeout():
    assert evaluation_lane(_item(choice="observe", reason="no_change")) == "observe"
    assert evaluation_lane(_item(choice="reply", applied=False, reason="low_confidence")) == "reply_blocked"
    assert evaluation_lane(_item(choice="reply", applied=True, reason="applied")) == "reply_applied"
    assert evaluation_lane(_item(status="failed", error="TimeoutError", reason="online_timeout")) == "failed"
    assert evaluation_lane(_item(status="pending")) == "pending"


def test_funnel_counts_blocked_reasons():
    funnel = funnel_from_rows([
        _item(choice="observe", reason="no_change"),
        _item(choice="observe", reason="no_change"),
        _item(choice="reply", applied=False, reason="low_confidence"),
        _item(choice="reply", applied=False, reason="shadow_only"),
        _item(choice="reply", applied=True, reason="applied"),
    ])
    assert funnel["evaluated"] == 5
    assert funnel["lanes"]["observe"] == 2
    assert funnel["lanes"]["reply_blocked"] == 2
    assert funnel["lanes"]["reply_applied"] == 1
    assert funnel["blocked_reasons"] == {"low_confidence": 1, "shadow_only": 1}


def test_desk_flags_observe_mode_missing_whitelist_and_empty_keywords():
    policy = JevPolicy(participation_shadow_only=True, help_sessions=["room@chatroom"])
    conflicts = desk_conflicts(
        policy,
        "other@chatroom",
        {"reply_mode": "contains", "keyword_count": 0, "has_session_row": False},
    )
    assert conflicts == [
        "jev_observe_only",
        "not_in_help_sessions",
        "contains_without_keywords",
        "no_session_policy",
    ]
    desk = build_desk(policy, "room@chatroom", {
        "reply_mode": "contains",
        "configured_reply_mode": "contains",
        "inherits_global_keywords": False,
        "keyword_count": 46,
        "keywords": ["降智"],
        "mention_sender": False,
        "has_session_row": True,
    })
    assert desk["in_help_sessions"] is True
    assert desk["channel"]["keyword_count"] == 46
    assert "keywords" not in desk["channel"]
    assert desk["conflicts"] == ["jev_observe_only"]


def test_desk_matches_help_session_alias_and_flags_closed_proactive():
    policy = JevPolicy(participation_shadow_only=False, help_sessions=["cx1:c:room@chatroom"])
    desk = build_desk(
        policy,
        "49025625236@chatroom",
        {"reply_mode": "contains", "keyword_count": 46, "has_session_row": True},
        aliases=["49025625236@chatroom", "cx1:c:room@chatroom"],
        social={"effective_enabled": True, "group_enabled": True, "proactive_enabled": False},
    )
    assert desk["in_help_sessions"] is True
    assert desk["social"]["proactive_enabled"] is False
    assert desk["conflicts"] == ["social_proactive_disabled"]


def test_desk_flags_closed_group_participation():
    policy = JevPolicy(participation_shadow_only=False, help_sessions=["room@chatroom"])
    conflicts = desk_conflicts(
        policy,
        "room@chatroom",
        {"reply_mode": "contains", "keyword_count": 1, "has_session_row": True},
        social={"effective_enabled": False, "group_enabled": False, "proactive_enabled": True},
    )
    assert conflicts == ["social_disabled"]


def test_turn_pairs_redacted_message_keyword_hit_and_outcome():
    turn = classify_turn(
        _item(),
        {"content": "一直 312，wxid_someone 要换节点吗", "deliveries": [], "decisions": [],
         "processing_status": "intentionally_suppressed", "processing_reason": "low_confidence"},
        keywords=["312", "节点"],
        reply_mode="contains",
    )
    assert turn["lane"] == "reply_blocked"
    assert turn["keyword_hit"] is True
    assert "wxid_someone" not in (turn["message"] or "")
    assert "312" in (turn["message"] or "")
    assert turn["outcome"] == "suppressed"

    sent = classify_turn(
        _item(applied=True, reason="applied"),
        {"content": "谢谢", "deliveries": [{"status": "sent"}], "decisions": [{"status": "may_reply", "reasons": []}]},
        keywords=["代理"],
        reply_mode="contains",
    )
    assert sent["outcome"] == "sent"
    assert sent["keyword_hit"] is False


def test_opted_out_member_hides_message_text():
    turn = classify_turn(
        _item(),
        {"content": "证书过期了吗", "hidden": True},
        keywords=["证书"],
        reply_mode="contains",
    )
    assert turn["message"] is None
    assert turn["keyword_hit"] is None


def test_attach_turns_only_enriches_participation_rows():
    items = [
        _item(),
        {**_item(domain="memory"), "result": {"_audit": {"trace_id": "tr-mem"}}},
    ]
    attach_turns(
        items,
        {"tr-help": {"content": "是不是还要买一个动态代理？", "deliveries": []}},
        keywords=["代理"],
        reply_mode="contains",
    )
    assert items[0]["turn"]["keyword_hit"] is True
    assert items[0]["turn"]["outcome"] == "jev_blocked"
    assert "turn" not in items[1]
