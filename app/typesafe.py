"""Small, optional client for TypeSafe's structured evaluation API.

TypeSafe is intentionally kept outside the normal LLM provider abstraction.
It is useful for evaluating an already extracted relation (or a memory item)
in shadow mode, while the existing extraction and response paths continue to
own their own provider, retries, and failure policy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.common.config import Settings, get_settings

try:  # The dependency is declared in pyproject.toml.
    from typesafe_sdk import (
        AsyncTypeSafeClient,
        Choice,
        Noul,
        RetryPolicy,
        Score,
    )
except ImportError as exc:  # pragma: no cover - protects minimal local tooling
    raise RuntimeError("typesafe-sdk is required to use app.typesafe") from exc


Question = Noul | Choice | Score | Mapping[str, Any]


class TypeSafeConfigurationError(RuntimeError):
    """Raised when TypeSafe was explicitly enabled without an API key."""


@dataclass(frozen=True)
class TypeSafeEvaluation:
    """A JSON-friendly evaluation result returned by :class:`TypeSafeClient`."""

    model: str
    answers: dict[str, Any]
    usage: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "answers": self.answers, "usage": self.usage}

    def confidence(self, name: str) -> float:
        """Return an answer's confidence, or ``noul`` probability when present."""

        answer = self.answers.get(name)
        if not isinstance(answer, Mapping):
            return 0.0
        value = answer.get("confidence")
        if value is None and answer.get("type") == "noul":
            value = answer.get("noul")
        try:
            number = float(value or 0.0)
            return number if math.isfinite(number) and 0 <= number <= 1 else 0.0
        except (TypeError, ValueError):
            return 0.0

    def meets_confidence(self, name: str, minimum: float) -> bool:
        return self.confidence(name) >= minimum


def _question_from_mapping(question: Mapping[str, Any]) -> Noul | Choice | Score:
    """Convert a JSON question into the SDK's typed question model."""

    kind = str(question.get("type", "")).strip().lower()
    values = {key: value for key, value in question.items() if key != "type"}
    if kind == "noul":
        return Noul(**values)
    if kind == "choice":
        return Choice(**values)
    if kind == "score":
        return Score(**values)
    raise ValueError(f"Unsupported TypeSafe question type: {kind!r}")


def _json_value(value: Any) -> Any:
    """Serialize SDK/Pydantic values without exposing internal client state."""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class TypeSafeClient:
    """Async TypeSafe evaluator with lazy connection creation.

    The client is disabled by default.  A missing key is treated as a
    configuration error only when ``TYPESAFE_ENABLED=true``; callers can use
    ``enabled``/``configured`` to keep shadow hooks fail-closed.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.enabled = bool(self.settings.typesafe_enabled)
        self.shadow_only = bool(self.settings.typesafe_shadow_only)
        self.min_confidence = float(self.settings.typesafe_min_confidence)
        self.configured = bool(self.settings.typesafe_api_key)
        self._client: AsyncTypeSafeClient | None = None

    def _ensure_client(self) -> AsyncTypeSafeClient:
        if not self.enabled:
            raise TypeSafeConfigurationError("TypeSafe is disabled")
        api_key = self.settings.typesafe_api_key
        if not api_key:
            raise TypeSafeConfigurationError(
                "TYPESAFE_API_KEY is required when TYPESAFE_ENABLED=true"
            )
        if self._client is None:
            retry = RetryPolicy(
                max_retries=self.settings.typesafe_max_retries,
                timeout=max(self.settings.typesafe_timeout, 1.0)
                * (self.settings.typesafe_max_retries + 1),
            )
            self._client = AsyncTypeSafeClient(
                api_key=api_key,
                model=self.settings.typesafe_model,
                base_url=self.settings.typesafe_base_url.rstrip("/"),
                timeout=self.settings.typesafe_timeout,
                retry=retry,
            )
        return self._client

    async def evaluate(
        self,
        *,
        state: Any,
        questions: Mapping[str, Question],
        model: str | None = None,
    ) -> TypeSafeEvaluation | None:
        """Evaluate structured state, returning ``None`` while disabled.

        ``questions`` accepts either SDK question objects or their JSON form,
        making it safe to build prompts from configuration without coupling
        callers to TypeSafe's model classes.
        """

        if not self.enabled:
            return None
        typed_questions: dict[str, Noul | Choice | Score] = {}
        for name, question in questions.items():
            typed_questions[name] = (
                _question_from_mapping(question) if isinstance(question, Mapping) else question
            )
        response = await self._ensure_client().system_one(
            state=state,
            questions=typed_questions,
            model=model or self.settings.typesafe_model,
        )
        payload = _json_value(response)
        return TypeSafeEvaluation(
            model=str(payload.get("model", model or self.settings.typesafe_model)),
            answers=dict(payload.get("answers") or {}),
            usage=dict(payload.get("usage") or {}),
        )

    async def system_one(
        self,
        state: Any,
        questions: Mapping[str, Question],
        *,
        model: str | None = None,
    ) -> TypeSafeEvaluation | None:
        """Alias matching the upstream API name."""

        return await self.evaluate(state=state, questions=questions, model=model)

    async def evaluate_group_relationship(
        self,
        *,
        state: Any,
        questions: Mapping[str, Question],
        model: str | None = None,
    ) -> TypeSafeEvaluation | None:
        """Named adapter entry point for the group-graph shadow verifier."""

        return await self.evaluate(state=state, questions=questions, model=model)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> TypeSafeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


__all__ = [
    "Choice",
    "Noul",
    "Score",
    "TypeSafeClient",
    "TypeSafeConfigurationError",
    "TypeSafeEvaluation",
]
