from __future__ import annotations

import pytest

from app.common.prompting import augment_prompt_with_persona_and_memory
from app.common.types import Channel, Session
from plugins.speaker_portrait.jobs import sync_applied_styles
from plugins.speaker_portrait.pipeline import (
    apply_coverage,
    build_portrait_prompt,
    compact_portrait_for_prompt,
    compile_reply_style,
    merge_portrait,
    parse_portrait_payload,
)
from plugins.speaker_portrait.workspace import build_tool_prompt, write_messages_jsonl


def test_parse_portrait_payload_accepts_fenced_json() -> None:
    portrait = parse_portrait_payload(
        """```json
        {"summary":"爱聊数码","likes":[{"text":"键盘","count":4,"last_seen":"2026-08-01"}],"topics":[{"text":"数码","count":3}],"confidence":0.8}
        ```"""
    )
    assert portrait["summary"] == "爱聊数码"
    assert portrait["likes"][0]["text"] == "键盘"
    assert portrait["likes"][0]["count"] == 4
    assert portrait["confidence"] == 0.8
    assert portrait["unknowns"] == []


def test_parse_portrait_payload_invalid_is_empty_lists() -> None:
    portrait = parse_portrait_payload("not json")
    assert portrait["summary"] == ""
    assert portrait["likes"] == []
    assert portrait["confidence"] == 0.0


def test_parse_drops_singleton_likes() -> None:
    portrait = parse_portrait_payload(
        '{"summary":"x","likes":[{"text":"一次提到的梗","count":1}],"topics":[{"text":"代码","count":5}]}'
    )
    assert portrait["likes"] == []
    assert portrait["topics"][0]["text"] == "代码"


def test_merge_keeps_old_likes_unless_removed() -> None:
    previous = {
        "likes": [{"text": "烤鱼", "count": 8, "last_seen": "2026-08-01"}],
        "topics": [{"text": "上班", "count": 6}],
        "summary": "旧摘要",
    }
    updated = parse_portrait_payload(
        '{"summary":"新摘要","likes":[{"text":"奶茶","count":3}],"changes":{"removed":["上班"]}}'
    )
    merged = merge_portrait(previous, updated)
    texts = {item["text"] for item in merged["likes"]}
    assert "烤鱼" in texts
    assert "奶茶" in texts
    assert merged["changes"]["added"] == ["奶茶"]
    assert "上班" in merged["changes"]["removed"]


def test_merge_full_rerun_keeps_old_likes_when_new_lists_empty() -> None:
    previous = {
        "likes": [{"text": "烤鱼", "count": 16, "last_seen": "2026-08-21", "examples": ["烤鱼吃多了"]}],
        "topics": [{"text": "下班", "count": 68}],
        "voice": [],
        "summary": "旧摘要",
    }
    updated = parse_portrait_payload(
        '{"summary":"新摘要","voice":[{"text":"短句连发","count":1}],"likes":[],"topics":[]}'
    )
    merged = merge_portrait(previous, updated)
    assert {item["text"] for item in merged["likes"]} == {"烤鱼"}
    assert {item["text"] for item in merged["topics"]} == {"下班"}
    assert merged["voice"][0]["text"] == "短句连发"
    assert merged["summary"] == "新摘要"


def test_apply_coverage_caps_confidence_when_unread() -> None:
    portrait = {"summary": "x", "confidence": 0.9, "confidence_provided": True, "unknowns": []}
    apply_coverage(portrait, lines_total=1000)
    assert portrait["confidence"] <= 0.55
    assert portrait["coverage"]["complete"] is False


def test_apply_coverage_fills_missing_confidence_when_complete() -> None:
    portrait = {
        "summary": "x",
        "confidence": 0.0,
        "confidence_provided": False,
        "likes": [{"text": "烤鱼", "count": 4}],
        "topics": [{"text": "代码", "count": 8}],
        "unknowns": [],
        "coverage": {"lines_read": 100, "complete": True},
    }
    apply_coverage(portrait, lines_total=100)
    assert portrait["coverage"]["complete"] is True
    assert portrait["confidence"] >= 0.7


def test_format_transcript_keeps_recent_messages_within_budget() -> None:
    messages = [
        {"timestamp": "1", "sender_name": "A", "text": "old"},
        {"timestamp": "2", "sender_name": "A", "text": "new-message"},
    ]
    system, user, stats = build_portrait_prompt(
        speaker_name="A",
        messages=messages,
        max_chars=40,
    )
    assert "人物画像" in system
    assert "new-message" in user
    assert stats["used_messages"] >= 1


def test_full_prompt_spreads_early_and_recent_messages() -> None:
    messages = [
        {"timestamp": str(i), "sender_name": "A", "text": f"msg-{i}-{'x' * 80}"}
        for i in range(120)
    ]
    _, user, stats = build_portrait_prompt(
        speaker_name="A",
        messages=messages,
        max_chars=4000,
    )
    assert "msg-119-" in user
    assert stats["used_messages"] < 120
    assert stats["used_messages"] >= 1


def test_incremental_prompt_keeps_previous_portrait() -> None:
    system, user, stats = build_portrait_prompt(
        speaker_name="小海",
        messages=[{"timestamp": "2", "sender_name": "小海", "text": "又点了烤鱼"}],
        max_chars=4000,
        previous_portrait={"summary": "爱吃烤鱼", "likes": ["烤鱼"], "confidence": 0.7},
    )
    assert stats["mode"] == "incremental"
    assert "热更新" in system
    assert "爱吃烤鱼" in user
    assert "又点了烤鱼" in user


def test_compact_portrait_for_prompt_lists_likes() -> None:
    text = compact_portrait_for_prompt(
        {
            "summary": "爱买外设",
            "likes": ["机械键盘", "咖啡"],
            "topics": ["数码"],
        }
    )
    assert "爱买外设" in text
    assert "机械键盘" in text
    assert "数码" in text


def test_workspace_writes_jsonl_and_tool_prompt_points_at_file(tmp_path) -> None:
    path = tmp_path / "messages.jsonl"
    count = write_messages_jsonl(
        path,
        [
            {"timestamp": "1", "sender_name": "小海", "text": "先点烤鱼"},
            {"timestamp": "2", "sender_name": "小海", "text": ""},
        ],
    )
    assert count == 1
    assert "烤鱼" in path.read_text(encoding="utf-8")
    system, user, stats = build_tool_prompt(
        speaker_name="小海",
        messages=[{"timestamp": "1", "sender_name": "小海", "text": "先点烤鱼"}],
    )
    assert "messages.jsonl" in user
    assert "按行号分块阅读" in system
    assert stats["tool_use"] is True


def test_compile_reply_style_uses_first_person_and_examples() -> None:
    prompt = compile_reply_style(
        {
            "summary": "爱吃烤鱼的打工人",
            "voice": [{"text": "短句连发", "examples": ["下班"]}],
            "social": [{"text": "求带", "examples": ["带带我"]}],
            "likes": [{"text": "烤鱼", "count": 16, "examples": ["想吃烤鱼了"]}],
        },
        name="小海",
    )
    assert "你就是小海" in prompt
    assert "短句连发" in prompt
    assert "带带我" in prompt
    assert "烤鱼" in prompt
    assert "按自己平时怎么过、最近在忙什么来答" in prompt


class _FakePersonaStore:
    def __init__(self, profiles: list[dict]) -> None:
        self.profiles = profiles
        self.slug_queries: list[tuple[str, str]] = []
        self.upserts: list[dict] = []

    async def list_profiles_by_slug(self, tenant_id: str, skill_slug: str) -> list[dict]:
        self.slug_queries.append((tenant_id, skill_slug))
        return list(self.profiles)

    async def upsert_profile(self, **kwargs) -> dict:
        self.upserts.append(kwargs)
        return {"id": kwargs.get("profile_id"), **kwargs}


@pytest.mark.asyncio
async def test_style_sync_recompiles_applied_profiles() -> None:
    store = _FakePersonaStore(
        [
            {
                "id": 7,
                "session_id": "room@chatroom",
                "channel": "wechat",
                "source_key": "wxbot",
                "source_label": "小海",
                "profile_name": "小海",
                "target_name": "小海",
                "target_user_id": "wxid_hai",
                "enabled": True,
            }
        ]
    )
    synced = await sync_applied_styles(
        object(),
        tenant_id="demo",
        speaker_id="wxid_hai",
        speaker_name="小海",
        portrait={"summary": "爱吃烤鱼的打工人", "likes": [{"text": "烤鱼", "count": 9}]},
        persona_store=store,  # type: ignore[arg-type]
    )
    assert synced == 1
    assert store.slug_queries == [("demo", "portrait-wxid-hai")]
    upsert = store.upserts[0]
    assert upsert["profile_id"] == 7
    assert upsert["enabled"] is True
    assert upsert["skill_slug"] == "portrait-wxid-hai"
    assert "你就是小海" in upsert["prompt_text"]
    assert "烤鱼" in upsert["prompt_text"]


@pytest.mark.asyncio
async def test_style_sync_without_applied_profiles_is_noop() -> None:
    store = _FakePersonaStore([])
    synced = await sync_applied_styles(
        object(),
        tenant_id="demo",
        speaker_id="wxid_hai",
        speaker_name="小海",
        portrait={"summary": "x"},
        persona_store=store,  # type: ignore[arg-type]
    )
    assert synced == 0
    assert store.upserts == []


def test_prompt_includes_speaker_portrait_without_imitation() -> None:
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_a",
        channel=Channel.WECHAT,
    )
    session.variables["speaker_portrait"] = {
        "compact": "喜好：机械键盘",
        "display_name": "A",
    }
    prompt = augment_prompt_with_persona_and_memory(
        "base",
        session,
        memory_intro="记忆：",
    )
    assert "<speaker_portrait>" in prompt
    assert "机械键盘" in prompt
    assert "不要扮演对方" in prompt
