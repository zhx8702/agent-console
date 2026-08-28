import json

import pytest
from pydantic import ValidationError

from app.common.intent import (
    IntentArtifact,
    IntentDecision,
    IntentOperation,
    IntentSource,
)


def test_normal_intent_is_serializable_and_has_compact_output() -> None:
    decision = IntentDecision(
        operation=IntentOperation.RETRIEVE,
        source=IntentSource.X,
        artifact=IntentArtifact.TEXT,
        query="怎么快速搞钱",
        confidence=0.96,
        needs_tool=True,
        tool_name="x_search",
    )

    assert decision.to_minimal_dict() == {
        "operation": "retrieve",
        "source": "x",
        "artifact": "text",
        "domain": "none",
        "confidence": 0.96,
        "needs_tool": True,
        "query": "怎么快速搞钱",
        "tool_name": "x_search",
    }
    assert json.loads(decision.to_minimal_json()) == decision.to_minimal_dict()


def test_unknown_values_downgrade_without_breaking_message_path() -> None:
    decision = IntentDecision.from_dict(
        {
            "operation": "invented_operation",
            "source": "future_network",
            "artifact": "unknown_media",
            "query": "  hello  ",
            "confidence": 0.4,
            "needs_tool": "true",
            "extra_provider_field": {"ignored": True},
        }
    )

    assert decision.operation is IntentOperation.UNKNOWN
    assert decision.source is IntentSource.UNKNOWN
    assert decision.artifact is IntentArtifact.UNKNOWN
    assert decision.query == "hello"
    assert decision.confidence == 0.4
    assert decision.needs_tool is True


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan"), float("inf"), True])
def test_direct_confidence_validation_rejects_invalid_values(confidence: float) -> None:
    with pytest.raises(ValidationError):
        IntentDecision(confidence=confidence)


def test_safe_dict_parse_downgrades_invalid_confidence_only() -> None:
    decision = IntentDecision.from_dict(
        {
            "operation": "retrieve",
            "source": "web",
            "confidence": 2,
        }
    )

    assert decision.operation is IntentOperation.RETRIEVE
    assert decision.source is IntentSource.WEB
    assert decision.confidence == 0.0


def test_json_parse_accepts_bytes_and_invalid_json_is_unknown() -> None:
    decision = IntentDecision.from_json(
        b'{"operation":"create","artifact":"image","needs_tool":true}'
    )
    assert decision.operation is IntentOperation.CREATE
    assert decision.artifact is IntentArtifact.IMAGE
    assert decision.needs_tool is True

    assert IntentDecision.from_json("not-json") == IntentDecision()
    assert IntentDecision.from_json("[1, 2, 3]") == IntentDecision()
