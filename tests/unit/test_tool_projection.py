from __future__ import annotations

from app.common.tool_projection import project_tool_result


def test_tool_projection_removes_private_reply_effects() -> None:
    result = project_tool_result(
        {"ok": True, "value": "small", "channel_reply_effects": [{"text": "private"}]}
    )

    assert result == {"ok": True, "value": "small"}


def test_tool_projection_caps_large_nested_results() -> None:
    result = project_tool_result({"content": "x" * 20_000, "items": ["y" * 1000] * 50}, max_chars=500)

    assert isinstance(result, dict)
    assert result.get("_projection") == "truncated"
    assert int(result.get("original_chars") or 0) > 500
    assert len(str(result.get("preview") or "")) <= 500
