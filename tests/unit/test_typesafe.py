from __future__ import annotations

import pytest

from app.common.config import Settings
from app.typesafe import TypeSafeClient, TypeSafeConfigurationError


class _Answer:
    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "model": "jev-1.13.0",
            "answers": {
                "supported": {"type": "noul", "noul": 0.92},
                "decision": {
                    "type": "choice",
                    "choice": "accepted",
                    "confidence": 0.88,
                },
            },
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }


class _FakeSDK:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def system_one(self, **kwargs: object) -> _Answer:
        self.calls.append(kwargs)
        return _Answer()

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_disabled_client_is_noop() -> None:
    client = TypeSafeClient(Settings(app_env="test", typesafe_enabled=False))
    assert await client.evaluate(state="text", questions={}) is None


@pytest.mark.asyncio
async def test_enabled_client_requires_key() -> None:
    client = TypeSafeClient(Settings(app_env="test", typesafe_enabled=True))
    with pytest.raises(TypeSafeConfigurationError):
        await client.evaluate(state="text", questions={})


@pytest.mark.asyncio
async def test_client_coerces_questions_and_serializes_response() -> None:
    client = TypeSafeClient(
        Settings(
            app_env="test",
            typesafe_enabled=True,
            typesafe_api_key="unit-test-secret",
            typesafe_timeout=7,
            typesafe_max_retries=1,
        )
    )
    fake = _FakeSDK()
    client._client = fake  # type: ignore[assignment]
    result = await client.evaluate(
        state={"text": "甲回复了乙"},
        questions={
            "supported": {
                "type": "noul",
                "instructions": "是否支持这个关系？",
            },
            "decision": {
                "type": "choice",
                "instructions": "如何处理？",
                "criteria": {"accepted": "接受", "needs_review": "审核"},
            },
        },
    )
    assert result is not None
    assert result.confidence("supported") == 0.92
    assert result.meets_confidence("decision", 0.8)
    assert fake.calls[0]["state"] == {"text": "甲回复了乙"}
    assert fake.calls[0]["questions"]["supported"].type == "noul"  # type: ignore[index]


def test_typesafe_environment_fields_are_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_SHADOW_ONLY", "false")
    monkeypatch.setenv("TYPESAFE_MIN_CONFIDENCE", "0.91")
    monkeypatch.setenv("TYPESAFE_MAX_RETRIES", "4")
    monkeypatch.setenv("TYPESAFE_TIMEOUT", "12")
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-test")
    settings = Settings(app_env="test", typesafe_api_key="secret")
    assert settings.typesafe_enabled is True
    assert settings.typesafe_shadow_only is False
    assert settings.typesafe_min_confidence == 0.91
    assert settings.typesafe_max_retries == 4
    assert settings.typesafe_timeout == 12
    assert settings.typesafe_model == "jev-test"
