"""M6 Router engine."""
from __future__ import annotations

from typing import Any

from app.common.config import Settings, get_settings
from app.common.logging import get_logger
from app.common.types import PreprocessedMessage, RouteDecision, RouteType, Session
from app.router.rules import Rule, evaluate, load_rules

log = get_logger(__name__)


class Router:
    """Rule-based router. Loads YAML rules once at construction time."""

    def __init__(self, rules: list[Rule]):
        if not rules:
            raise ValueError("Router requires at least one rule")
        self._rules = rules

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    def decide(
        self,
        pre: PreprocessedMessage,
        session: Session | None = None,
        signals: dict[str, Any] | None = None,
    ) -> RouteDecision:
        decision = evaluate(self._rules, pre, session, signals or {})
        log.debug(
            "router.decide",
            route=decision.type.value,
            reason=decision.reason,
            rule=decision.hints.get("rule"),
        )
        return decision


def build_router(settings: Settings | None = None) -> Router:
    s = settings or get_settings()
    rules = load_rules(s.router_config_path)
    if not s.knowledge_features_enabled:
        rules = [
            Rule(
                name=rule.name,
                when=dict(rule.when),
                route=RouteType.LLM if rule.route in {RouteType.FAQ, RouteType.RAG} else rule.route,
                reason=(
                    f"{rule.reason} [knowledge features disabled -> llm]"
                    if rule.route in {RouteType.FAQ, RouteType.RAG}
                    else rule.reason
                ),
            )
            for rule in rules
        ]
    return Router(rules)
