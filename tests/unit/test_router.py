from __future__ import annotations

import pytest

from app.common.config import Settings, get_settings
from app.common.types import (
    Channel,
    EmotionLabel,
    IntentCoarse,
    PreprocessedMessage,
    RouteType,
    Session,
    SessionState,
)
from app.router.engine import build_router
from app.router.rules import Rule, evaluate, load_rules


def _make_pre(
    *,
    sensitive: bool = False,
    intent: IntentCoarse = IntentCoarse.UNKNOWN,
    emotion: EmotionLabel = EmotionLabel.NEUTRAL,
    cleaned: str = "hello",
) -> PreprocessedMessage:
    return PreprocessedMessage(
        original_text=cleaned,
        cleaned_text=cleaned,
        language="zh",
        sensitive=sensitive,
        intent_coarse=intent,
        emotion=emotion,
    )


def _make_session() -> Session:
    return Session(
        session_id="se_test00000000000001",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WEB,
        state=SessionState.CHATTING,
    )


# ---- YAML loader -----------------------------------------------------------

def test_load_real_config():
    s = get_settings()
    rules = load_rules(s.router_config_path)
    assert len(rules) == 9
    names = [r.name for r in rules]
    assert names == [
        "safety_blocked",
        "user_requested_handoff",
        "strong_negative_emotion",
        "faq_hit",
        "business_with_tools",
        "group_tools_query",
        "business_knowledge",
        "faq_intent",
        "default",
    ]


# ---- individual rule firing -----------------------------------------------

@pytest.fixture(scope="module")
def router():
    return build_router(get_settings())


def test_router_safety_blocked_beats_everything(router):
    pre = _make_pre(sensitive=True, intent=IntentCoarse.HANDOFF_REQUEST)
    d = router.decide(pre, _make_session())
    assert d.type == RouteType.CANNED
    assert d.hints["rule"] == "safety_blocked"


def test_router_handoff_beats_emotion_and_faq(router):
    pre = _make_pre(intent=IntentCoarse.HANDOFF_REQUEST, emotion=EmotionLabel.NEGATIVE)
    d = router.decide(pre, _make_session(), signals={"faq_similarity": 0.99})
    assert d.type == RouteType.HANDOFF
    assert d.hints["rule"] == "user_requested_handoff"


def test_router_emotion_handoff_after_fallbacks(router):
    pre = _make_pre(emotion=EmotionLabel.NEGATIVE)
    d = router.decide(pre, _make_session(), signals={"consecutive_fallbacks": 3})
    assert d.type == RouteType.HANDOFF
    assert d.hints["rule"] == "strong_negative_emotion"


def test_router_emotion_rule_not_fired_without_fallbacks(router):
    pre = _make_pre(emotion=EmotionLabel.NEGATIVE)
    d = router.decide(pre, _make_session(), signals={"consecutive_fallbacks": 1})
    # should NOT be handoff; with unknown intent falls to default -> LLM
    assert d.type == RouteType.LLM
    assert d.hints["rule"] == "default"


def test_router_faq_hit(router):
    pre = _make_pre(intent=IntentCoarse.BUSINESS)
    d = router.decide(pre, _make_session(), signals={"faq_similarity": 0.95})
    assert d.type == RouteType.FAQ
    assert d.hints["rule"] == "faq_hit"


def test_router_business_with_tools(router):
    pre = _make_pre(intent=IntentCoarse.BUSINESS)
    d = router.decide(pre, _make_session(), signals={"tools_available": True})
    assert d.type == RouteType.AGENT
    assert d.hints["rule"] == "business_with_tools"


def test_router_business_knowledge(router):
    pre = _make_pre(intent=IntentCoarse.BUSINESS)
    d = router.decide(pre, _make_session(), signals={"tools_available": False})
    assert d.type == RouteType.RAG
    assert d.hints["rule"] == "business_knowledge"


def test_router_group_tools_query_without_business_intent(router):
    pre = _make_pre(intent=IntentCoarse.UNKNOWN, cleaned="这个群有哪些人")
    d = router.decide(pre, _make_session(), signals={"tools_available": True})
    assert d.type == RouteType.AGENT
    assert d.hints["rule"] == "group_tools_query"


def test_router_faq_intent(router):
    pre = _make_pre(intent=IntentCoarse.FAQ)
    d = router.decide(pre, _make_session())
    assert d.type == RouteType.RAG
    assert d.hints["rule"] == "faq_intent"


def test_router_default(router):
    pre = _make_pre(intent=IntentCoarse.CHITCHAT)
    d = router.decide(pre, _make_session())
    assert d.type == RouteType.LLM
    assert d.hints["rule"] == "default"


def test_router_knowledge_routes_fall_back_to_llm_when_disabled():
    settings = Settings(
        app_env="prod",
        knowledge_features_enabled=False,
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_embed_provider="fake",
        outbound_hmac_secret="prod_secret",
        admin_bearer_token="prod_admin_token",
        tenant_demo_secret="prod_tenant_secret",
    )
    router = build_router(settings)

    faq_hit = router.decide(
        _make_pre(intent=IntentCoarse.BUSINESS),
        _make_session(),
        signals={"faq_similarity": 0.95},
    )
    business_knowledge = router.decide(
        _make_pre(intent=IntentCoarse.BUSINESS),
        _make_session(),
        signals={"tools_available": False},
    )
    faq_intent = router.decide(_make_pre(intent=IntentCoarse.FAQ), _make_session())

    assert faq_hit.type == RouteType.LLM
    assert business_knowledge.type == RouteType.LLM
    assert faq_intent.type == RouteType.LLM


# ---- ordering --------------------------------------------------------------

def test_router_rule_ordering_matches_spec(router):
    """Assert ordering safety_blocked > handoff > emotion > faq_hit > business_with_tools
    > business_knowledge > faq_intent > default."""
    names = [r.name for r in router.rules]
    expected = [
        "safety_blocked",
        "user_requested_handoff",
        "strong_negative_emotion",
        "faq_hit",
        "business_with_tools",
        "group_tools_query",
        "business_knowledge",
        "faq_intent",
        "default",
    ]
    assert names == expected


# ---- evaluate() edge cases -------------------------------------------------

def test_evaluate_unsupported_when_rejected_by_loader(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "rules:\n  - name: bad\n    when:\n      wat: 1\n    route: llm\n    reason: x\n",
        encoding="utf-8",
    )
    from app.common.exceptions import ConfigError

    with pytest.raises(ConfigError):
        load_rules(p)


def test_evaluate_empty_when_matches_default():
    rules = [Rule(name="d", when={}, route=RouteType.LLM, reason="fallback")]
    pre = _make_pre()
    d = evaluate(rules, pre, None, {})
    assert d.type == RouteType.LLM
    assert d.hints["rule"] == "d"
