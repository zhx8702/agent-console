from __future__ import annotations

import pytest

from app.common.intent import IntentDecision, IntentDomain
from app.common.intent_classify import (
    LlmIntentClassifier,
    NullIntentClassifier,
    StaticIntentClassifier,
    semantic_classify_skip_reason,
)
from app.common.types import ChatResponse, ChatUsage


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_request = None
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        self.last_request = request
        return ChatResponse(
            content=self.content,
            model="fake",
            usage=ChatUsage(),
        )


class _FlakyLLM:
    def __init__(self, failures: int, content: str) -> None:
        self.failures = failures
        self.content = content
        self.calls = 0

    async def chat(self, request):
        _ = request
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("upstream_unavailable")
        return ChatResponse(content=self.content, model="fake", usage=ChatUsage())


@pytest.mark.asyncio
async def test_null_classifier_is_unknown() -> None:
    decision = await NullIntentClassifier().classify("帮我转人工")
    assert decision.domain is IntentDomain.NONE
    assert decision.confidence == 0.0


@pytest.mark.asyncio
async def test_static_classifier_returns_preloaded_decision() -> None:
    loaded = IntentDecision(domain=IntentDomain.CREDITS, action="rank", confidence=0.9)
    decision = await StaticIntentClassifier(loaded).classify("ignored")
    assert decision.domain is IntentDomain.CREDITS
    assert decision.action == "rank"


@pytest.mark.asyncio
async def test_llm_classifier_parses_json_and_strips_fences() -> None:
    llm = _FakeLLM('```json\n{"domain":"handoff","action":"request","confidence":0.91}\n```')
    decision = await LlmIntentClassifier(llm).classify(
        "请转人工",
        context={"mentioned_me": True},
    )
    assert decision.domain is IntentDomain.HANDOFF
    assert decision.action == "request"
    assert decision.confidence == 0.91
    assert llm.last_request.metadata["route"] == "intent_classify"
    assert llm.last_request.metadata["openai_web_search"] is False


@pytest.mark.asyncio
async def test_llm_classifier_retries_transient_failures() -> None:
    llm = _FlakyLLM(2, '{"domain":"chitchat","action":"greet","confidence":0.8}')
    decision = await LlmIntentClassifier(llm).classify(
        "你好",
        context={"is_group": False},
    )
    assert llm.calls == 3
    assert decision.domain is IntentDomain.CHITCHAT


@pytest.mark.asyncio
async def test_llm_classifier_gives_up_after_three_failures() -> None:
    llm = _FlakyLLM(5, '{"domain":"chitchat"}')
    decision = await LlmIntentClassifier(llm).classify(
        "你好",
        context={"is_group": False},
    )
    assert llm.calls == 3
    assert decision == IntentDecision()


@pytest.mark.asyncio
async def test_llm_classifier_fail_closed_on_invalid_json() -> None:
    decision = await LlmIntentClassifier(_FakeLLM("not json")).classify(
        "hello",
        context={"is_group": False},
    )
    assert decision == IntentDecision()


def test_semantic_classify_skips_unaddressed_group_chat() -> None:
    assert (
        semantic_classify_skip_reason(
            "昨天还卖出去一万个gmail呢",
            {"is_group": True, "mentioned_me": False},
        )
        == "not_addressed"
    )


def test_semantic_classify_runs_when_mentioned_or_private() -> None:
    assert (
        semantic_classify_skip_reason("帮我转人工", {"is_group": True, "mentioned_me": True})
        == ""
    )
    assert semantic_classify_skip_reason("帮我转人工", {"is_group": False}) == ""


def test_semantic_classify_skips_commands_and_self_sent() -> None:
    assert semantic_classify_skip_reason("/draw cat", {"is_group": False}) == "command"
    assert (
        semantic_classify_skip_reason(
            "hello",
            {"is_group": False, "is_self_sent": True},
        )
        == "self_sent"
    )


@pytest.mark.asyncio
async def test_llm_classifier_does_not_call_model_for_group_chatter() -> None:
    llm = _FakeLLM('{"domain":"chitchat"}')
    decision = await LlmIntentClassifier(llm).classify(
        "昨天还卖出去一万个gmail呢",
        context={"is_group": True, "mentioned_me": False},
    )
    assert decision == IntentDecision()
    assert llm.last_request is None
