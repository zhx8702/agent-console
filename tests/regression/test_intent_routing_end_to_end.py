from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.common.intent import IntentDecision, IntentDomain
from app.common.intent_classify import StaticIntentClassifier
from app.common.types import (
    Channel,
    IntentCoarse,
    Message,
    RouteType,
    Session,
    SessionState,
)
from app.preprocessing.processor import build_preprocessor
from app.router.engine import Router
from app.router.rules import load_rules

_COARSE_TO_DOMAIN = {
    "handoff_request": (IntentDomain.HANDOFF, "request"),
    "complaint": (IntentDomain.COMPLAINT, "request"),
    "faq": (IntentDomain.FAQ, "ask"),
    "business": (IntentDomain.BUSINESS, "ask"),
    "chitchat": (IntentDomain.CHITCHAT, "greet"),
}

_REPO_ROOT = Path(__file__).parents[2]
_BASELINE_PATH = Path(__file__).parent / "routing_cases" / "raw_intent_baseline.yaml"


def _load_cases() -> list[dict[str, Any]]:
    raw = yaml.safe_load(_BASELINE_PATH.read_text(encoding="utf-8")) or {}
    cases = raw.get("cases")
    assert isinstance(cases, list), "raw intent baseline must define a cases list"
    assert cases, "raw intent baseline cases must not be empty"
    return cases


def _make_session(case_id: str) -> Session:
    return Session(
        session_id=f"se_raw_{case_id}",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WEB,
        state=SessionState.CHATTING,
    )


@pytest.fixture(scope="module")
def router() -> Router:
    return Router(load_rules(_REPO_ROOT / "config" / "router.yaml"))


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: case["id"])
async def test_raw_text_to_intent_and_route_baseline(
    router: Router,
    case: dict[str, Any],
) -> None:
    expected = case["expect"]
    domain_action = _COARSE_TO_DOMAIN.get(expected["intent"])
    classifier = (
        StaticIntentClassifier(
            IntentDecision(
                domain=domain_action[0],
                action=domain_action[1],
                confidence=0.95,
            )
        )
        if domain_action
        else None
    )
    pre = await build_preprocessor(classifier).run(Message(content=case["text"]))
    decision = router.decide(
        pre,
        _make_session(case["id"]),
        signals=case.get("signals", {}),
    )
    expected = case["expect"]

    assert pre.intent_coarse == IntentCoarse(expected["intent"])
    assert decision.type == RouteType(expected["route"])
    assert decision.hints["rule"] == expected["rule"]
