from __future__ import annotations

from plugins.memory.observability import build_safe_memory_profile_signal


def test_safe_memory_signal_exposes_counts_and_opaque_ids_only() -> None:
    payload = build_safe_memory_profile_signal(
        {
            "user_id": "wxid_sensitive",
            "short_term_memory": "手机号 13800138000",
            "session_summary": "用户住在具体地址",
            "manual_notes": "VIP 原始备注",
            "message_count": 7,
            "identity_message_count": 5,
            "session_message_count": 2,
            "summary_version": 3,
            "last_compacted_at": "2026-07-30T10:00:00Z",
            "audience_scope": "private",
            "memory_items": {
                "identity": [{"id": 11, "content": "敏感偏好"}],
                "session": [{"id": 12, "content": "本轮正文"}],
            },
            "relevant_memory_items": [
                {"id": 11, "content": "敏感偏好"},
                {"id": "not safe / id", "content": "另一个正文"},
            ],
            "relevant_graph_facts": [{"id": "fact:19", "content": "关系正文"}],
            "relevant_graph_episodes": [{"id": 23, "content": "事件正文"}],
        }
    )

    assert payload["loaded"] is True
    assert payload["message_count"] == 7
    assert payload["memory_item_counts"] == {"identity": 1, "session": 1, "relevant": 2}
    assert payload["selected_item_ids"] == [11]
    assert payload["selected_graph_fact_ids"] == ["fact:19"]
    assert payload["selected_graph_episode_ids"] == [23]
    assert payload["has_session_summary"] is True
    assert payload["has_manual_notes"] is True
    assert payload["audience_scope"] == "private"
    serialized = repr(payload)
    assert "wxid_sensitive" not in serialized
    assert "13800138000" not in serialized
    assert "敏感偏好" not in serialized
    assert "VIP 原始备注" not in serialized


def test_safe_memory_signal_empty_profile_has_stable_shape() -> None:
    assert build_safe_memory_profile_signal({}) == {
        "loaded": False,
        "message_count": 0,
        "identity_message_count": 0,
        "session_message_count": 0,
        "imported_message_count": 0,
        "summary_version": 1,
        "memory_item_counts": {"identity": 0, "session": 0, "relevant": 0},
        "selected_item_ids": [],
        "selected_graph_fact_ids": [],
        "selected_graph_episode_ids": [],
    }
