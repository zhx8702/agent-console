from __future__ import annotations

from plugins.memory.store import (
    _build_short_term_summary,
    _finalize_session_profile,
    _update_session_state,
)


def _apply(profile: dict, user_text: str, assistant_text: str = "ok", index: int = 1) -> dict:
    state = _update_session_state(
        profile,
        session_id="s1",
        user_text=user_text,
        assistant_text=assistant_text,
        created_at=f"2026-05-10T00:00:{index:02d}",
    )
    return {**profile, **state}


def test_recent_turns_rolls_to_bounded_limit() -> None:
    profile: dict = {}

    for index in range(12):
        profile = _apply(profile, f"turn {index}", index=index)

    assert len(profile["recent_turns"]) == 8
    assert profile["recent_turns"][0]["user_text"] == "turn 4"
    assert profile["recent_turns"][-1]["user_text"] == "turn 11"


def test_open_items_add_and_close() -> None:
    profile: dict = {}

    profile = _apply(profile, "todo check invoice later", index=1)
    assert [item["text"] for item in profile["open_items"]] == ["todo check invoice later"]

    profile = _apply(profile, "done check invoice", index=2)
    assert profile["open_items"] == []
    assert any(item["kind"] == "close" for item in profile["decisions"])
    assert "Closed open item" in profile["decisions"][-1]["text"]


def test_decisions_extract_and_preserve_existing() -> None:
    profile: dict = {
        "decisions": [{"key": "existing", "text": "decided use concise replies"}],
    }

    profile = _apply(profile, "confirmed adopt JSON storage", index=1)

    assert [item["text"] for item in profile["decisions"]] == [
        "decided use concise replies",
        "confirmed adopt JSON storage",
    ]
    assert "confirmed adopt JSON storage" in profile["session_summary"]


def test_legacy_short_term_memory_still_generated() -> None:
    short_items = [
        {"user_text": f"u{index}", "assistant_text": f"a{index}"}
        for index in range(8)
    ]

    summary = _build_short_term_summary(short_items)

    assert "用户最近说：u4" in summary
    assert "系统最近回复：a7" in summary
    assert "u0" not in summary


def test_finalize_session_profile_returns_structured_fields_and_recent_fallback() -> None:
    profile = _finalize_session_profile(
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "s1",
            "user_id": "wxid_a",
            "short_term_memory": "用户最近说：hi",
            "manual_notes": "",
            "short_term_items_json": '[{"user_text":"hi","assistant_text":"ok"}]',
            "session_summary": "Recent context: hi",
            "open_items_json": '[{"text":"todo follow up","status":"open"}]',
            "decisions_json": '[{"text":"decided use sqlite"}]',
            "recent_turns_json": "[]",
            "last_compacted_at": None,
            "summary_version": 1,
            "message_count": 1,
            "imported_message_count": 0,
            "last_seen_at": None,
            "updated_at": None,
        }
    )

    assert profile["session_summary"] == "Recent context: hi"
    assert profile["open_items"][0]["text"] == "todo follow up"
    assert profile["decisions"][0]["text"] == "decided use sqlite"
    assert profile["recent_turns"][0]["user_text"] == "hi"
