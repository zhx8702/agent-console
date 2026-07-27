from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.common.config import get_settings
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

_CASES_DIR = Path(__file__).parent / "routing_cases"
_BASELINE_PATH = _CASES_DIR / "baseline.yaml"


def _load_cases() -> list[dict[str, Any]]:
    raw = yaml.safe_load(_BASELINE_PATH.read_text(encoding="utf-8")) or {}
    cases = raw.get("cases")
    assert isinstance(cases, list), "routing baseline must define a cases list"
    assert cases, "routing baseline cases must not be empty"
    return cases


def _make_pre(message: dict[str, Any]) -> PreprocessedMessage:
    cleaned_text = message.get("cleaned_text", "hello")
    return PreprocessedMessage(
        original_text=message.get("original_text", cleaned_text),
        cleaned_text=cleaned_text,
        language=message.get("language", "zh"),
        sensitive=message.get("sensitive", False),
        intent_coarse=IntentCoarse(message.get("intent_coarse", IntentCoarse.UNKNOWN.value)),
        emotion=EmotionLabel(message.get("emotion", EmotionLabel.NEUTRAL.value)),
    )


def _make_session(case_id: str) -> Session:
    return Session(
        session_id=f"se_routing_{case_id[:16]:0<16}",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WEB,
        state=SessionState.CHATTING,
    )


@pytest.fixture(scope="module")
def router():
    return build_router(get_settings())


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: case["id"])
def test_routing_baseline_case(router, case: dict[str, Any]) -> None:
    expected = case["expect"]

    decision = router.decide(
        _make_pre(case.get("message", {})),
        _make_session(case["id"]),
        signals=case.get("signals", {}),
    )

    assert decision.type == RouteType(expected["route"])
    assert decision.hints["rule"] == expected["rule"]
