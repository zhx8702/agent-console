from __future__ import annotations

from app.common.intent import IntentDecision
from app.common.intent_classify import semantic_classify_skip_reason
from app.jev.models import answer, confidence, redact


class JevIntentClassifier:
    """A bounded second opinion; never invent tool arguments or authorization."""

    def __init__(self, primary, service):
        self.primary = primary
        self.service = service

    async def classify(self, text: str, *, context=None) -> IntentDecision:
        baseline = await self.primary.classify(text, context=context)
        extra = dict(context or {})
        if semantic_classify_skip_reason(text, extra):
            return baseline
        tenant = str(extra.get("tenant_id") or "")
        if not tenant:
            return baseline
        result, active = await self.service.online(
            tenant_id=tenant, session_id=str(extra.get("session_id") or ""), domain="intent",
            trace_id=str(extra.get("trace_id") or ""),
            state={"message": redact(text), "proposed_intent": {
                "domain": baseline.domain.value, "operation": baseline.operation.value,
                "source": baseline.source.value, "artifact": baseline.artifact.value,
                "action": baseline.action, "needs_tool": baseline.needs_tool,
            }, "mentioned_me": bool(extra.get("mentioned_me")),
                   "has_attachment": bool(extra.get("has_attachment"))})
        if not active or not result or answer(result, "decision") != "abstain":
            return baseline
        # High-confidence disagreement may remove a tool action, never escalate
        # to a different executable action. Conversation-only routing is allowed.
        domain = answer(result, "domain")
        policy = await self.service.policy(tenant)
        if confidence(result, "domain") < policy.min_confidence:
            return IntentDecision()
        actions = {"chitchat": "greet", "faq": "ask", "business": "ask",
                   "handoff": "request", "complaint": "request"}
        if domain not in actions:
            return IntentDecision()
        return IntentDecision.from_dict({"domain": domain, "action": actions[domain],
            "operation": "handoff" if domain == "handoff" else "converse", "query": text,
            "confidence": min(confidence(result), confidence(result, "domain"))})
