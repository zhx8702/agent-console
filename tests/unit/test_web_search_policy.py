from __future__ import annotations

from app.common.web_search import live_web_search_requested


def test_explicit_search_request_enables_live_search() -> None:
    assert live_web_search_requested("帮我联网查一下今天的北京新闻") is True
    assert live_web_search_requested("现在上海天气怎么样") is True


def test_local_history_query_does_not_enable_live_search() -> None:
    assert live_web_search_requested("查一下群里最近提到的 draw") is False


def test_request_metadata_is_authoritative() -> None:
    assert live_web_search_requested("今天的新闻", {"openai_web_search": False}) is False
    assert live_web_search_requested("普通聊天", {"openai_web_search": True}) is True
    assert live_web_search_requested("普通聊天", {"openai_web_search_required": True}) is True


def test_negated_search_request_stays_disabled() -> None:
    assert live_web_search_requested("不要联网搜索，直接说你的看法") is False
