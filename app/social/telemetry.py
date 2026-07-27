"""Low-cardinality, content-free metrics for social participation SLOs."""

from __future__ import annotations

from datetime import UTC

from prometheus_client import Counter, Histogram

from app.social.participation import (
    ParticipationContext,
    ParticipationDecision,
)

SOCIAL_PARTICIPATION_DECISIONS = Counter(
    "cs_social_participation_decisions_total",
    "Group participation decisions without tenant, member, or message labels.",
    ["status", "directly_addressed", "valid_member_answer"],
)
SOCIAL_SEND_REVALIDATIONS = Counter(
    "cs_social_send_revalidations_total",
    "Send-time participation revalidation outcomes.",
    ["result"],
)
SOCIAL_ADDED_SCHEDULING_DELAY = Histogram(
    "cs_social_added_scheduling_delay_seconds",
    "Humanized delay added before a group reply can be sent.",
    buckets=(0.25, 0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15),
)
SOCIAL_BOT_RATIO_LAST_40 = Histogram(
    "cs_social_bot_ratio_last_40",
    "Observed bot share in the last 40 group messages.",
    buckets=(0.025, 0.05, 0.075, 0.1, 0.125, 0.15, 0.2, 0.3, 0.5, 1),
)
SOCIAL_PRIVACY_ACTIONS = Counter(
    "cs_social_privacy_actions_total",
    "Natural privacy/correction actions and their result.",
    ["action", "result"],
)
SOCIAL_RUNTIME_EVENT_PERSISTENCE = Counter(
    "cs_social_runtime_event_persistence_total",
    "Content-free persistence outcomes for runtime participation events.",
    ["result", "obligation"],
)
SOCIAL_FINAL_DELIVERIES = Counter(
    "cs_social_final_deliveries_total",
    "Final group-reply delivery outcomes by bounded release cohort.",
    ["result", "stage", "cohort", "speech_class"],
)
SOCIAL_ACTUAL_DELIVERY_DELAY = Histogram(
    "cs_social_actual_delivery_delay_seconds",
    "Actual queue-to-final-delivery delay for group replies.",
    ["stage", "speech_class"],
    buckets=(0.25, 0.5, 1, 2, 3, 4, 5, 6, 8, 10, 15, 30, 60, 300),
)
SOCIAL_DUPLICATE_GUARD = Counter(
    "cs_social_duplicate_guard_total",
    "Near-duplicate guard outcomes without message content.",
    ["speech_class", "action"],
)


def observe_participation_decision(
    context: ParticipationContext,
    decision: ParticipationDecision,
) -> None:
    directly_addressed = bool(
        context.mentioned_me
        or context.replied_to_bot
        or context.explicit_command
        or context.safety_response_required
    )
    SOCIAL_PARTICIPATION_DECISIONS.labels(
        status=decision.status.value,
        directly_addressed=str(directly_addressed).lower(),
        valid_member_answer=str(bool(context.valid_member_answer_exists)).lower(),
    ).inc()
    if context.total_messages_last_40 > 0:
        SOCIAL_BOT_RATIO_LAST_40.observe(
            max(0, context.bot_messages_last_40)
            / max(1, context.total_messages_last_40)
        )
    if decision.not_before is not None:
        now = context.now
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        delay = max(0.0, (decision.not_before - now).total_seconds())
        SOCIAL_ADDED_SCHEDULING_DELAY.observe(delay)


def observe_send_revalidation(
    before: ParticipationDecision,
    after: ParticipationDecision,
) -> None:
    if after.status.value == "defer":
        result = "rescheduled"
    elif after.status == before.status:
        result = "unchanged"
    elif after.should_send:
        result = "rescheduled"
    else:
        result = "cancelled"
    SOCIAL_SEND_REVALIDATIONS.labels(result=result).inc()


def observe_privacy_action(action: str, *, succeeded: bool) -> None:
    normalized = str(action or "unknown").strip().lower().replace(" ", "_")[:48]
    SOCIAL_PRIVACY_ACTIONS.labels(
        action=normalized or "unknown",
        result="succeeded" if succeeded else "failed_closed",
    ).inc()


def observe_runtime_event_persistence(*, succeeded: bool, obligation: bool) -> None:
    SOCIAL_RUNTIME_EVENT_PERSISTENCE.labels(
        result="succeeded" if succeeded else "failed",
        obligation=str(bool(obligation)).lower(),
    ).inc()


def observe_final_delivery(
    *,
    result: str,
    stage: str,
    cohort: str,
    speech_class: str,
    actual_delay_seconds: float | None = None,
) -> None:
    normalized_result = str(result or "unknown").strip().lower()
    if normalized_result not in {"succeeded", "failed", "cancelled", "expired"}:
        normalized_result = "unknown"
    normalized_stage = str(stage or "legacy").strip().lower()
    if normalized_stage not in {
        "legacy",
        "shadow",
        "privacy_5",
        "style_10",
        "contextual",
        "proactive",
    }:
        normalized_stage = "unknown"
    normalized_cohort = str(cohort or "legacy").strip().lower()[:32]
    if normalized_cohort not in {
        "legacy",
        "shadow",
        "disabled",
        "privacy_canary",
        "privacy_baseline",
        "style_canary",
        "style_baseline",
        "contextual",
        "proactive_canary",
        "proactive_baseline",
    }:
        normalized_cohort = "unknown"
    normalized_class = str(speech_class or "soft").strip().lower()
    if normalized_class not in {"obligation", "soft", "scheduled"}:
        normalized_class = "unknown"
    SOCIAL_FINAL_DELIVERIES.labels(
        result=normalized_result,
        stage=normalized_stage,
        cohort=normalized_cohort,
        speech_class=normalized_class,
    ).inc()
    if actual_delay_seconds is not None and actual_delay_seconds >= 0:
        SOCIAL_ACTUAL_DELIVERY_DELAY.labels(
            stage=normalized_stage,
            speech_class=normalized_class,
        ).observe(float(actual_delay_seconds))


def observe_duplicate_guard(*, speech_class: str, action: str) -> None:
    normalized_class = str(speech_class or "soft").strip().lower()
    if normalized_class not in {"obligation", "soft", "scheduled"}:
        normalized_class = "unknown"
    normalized_action = str(action or "unknown").strip().lower()
    if normalized_action not in {"allowed", "cancelled", "rewritten", "preserved"}:
        normalized_action = "unknown"
    SOCIAL_DUPLICATE_GUARD.labels(
        speech_class=normalized_class,
        action=normalized_action,
    ).inc()
