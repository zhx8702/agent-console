from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ParticipationStatus(StrEnum):
    MUST_REPLY = "must_reply"
    MAY_REPLY = "may_reply"
    OBSERVE_ONLY = "observe_only"
    DEFER = "defer"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class ParticipationPolicy:
    """Conservative defaults for a group-chat participant.

    ``enabled`` is the group/tenant kill switch. Direct calls bypass soft
    budgets and quiet hours, but never bypass the kill switch or a self-message
    guard.
    """

    enabled: bool = True
    threshold: int = 60
    quiet_start_hour: int = 23
    quiet_end_hour: int = 8
    timezone: str = "Asia/Shanghai"
    max_soft_replies_10m: int = 2
    max_soft_replies_hour: int = 6
    max_bot_ratio_last_40: float = 0.15
    max_consecutive_bot_messages: int = 2
    proactive_enabled: bool = False
    max_proactive_per_day: int = 1
    proactive_min_silence_seconds: int = 3 * 60 * 60
    mention_sender_strategy: Literal["never", "reply_or_ambiguous"] = "never"
    prompt_context_retention_seconds: int = 60 * 60

    def __post_init__(self) -> None:
        if not 0 <= self.threshold <= 200:
            raise ValueError("threshold must be between 0 and 200")
        if not 0 <= self.quiet_start_hour <= 23:
            raise ValueError("quiet_start_hour must be between 0 and 23")
        if not 0 <= self.quiet_end_hour <= 23:
            raise ValueError("quiet_end_hour must be between 0 and 23")
        if self.max_soft_replies_10m < 0 or self.max_soft_replies_hour < 0:
            raise ValueError("reply budgets cannot be negative")
        if not 0.0 <= self.max_bot_ratio_last_40 <= 1.0:
            raise ValueError("max_bot_ratio_last_40 must be between 0 and 1")
        if self.max_consecutive_bot_messages < 0:
            raise ValueError("max_consecutive_bot_messages cannot be negative")
        if self.mention_sender_strategy not in {"never", "reply_or_ambiguous"}:
            raise ValueError("mention_sender_strategy must be never or reply_or_ambiguous")
        if not 0 <= self.prompt_context_retention_seconds <= 24 * 60 * 60:
            raise ValueError("prompt_context_retention_seconds must be between 0 and 86400")


@dataclass(frozen=True, slots=True)
class ParticipationContext:
    tenant_id: str
    session_id: str
    message_id: str
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Hard address signals.
    mentioned_me: bool = False
    replied_to_bot: bool = False
    explicit_command: bool = False
    safety_response_required: bool = False

    # Soft score signals.
    explicit_question_to_bot: bool = False
    keyword_triggered: bool = False
    topic_continuation: bool = False
    unfinished_task_continuation: bool = False
    directed_to_other_member: bool = False
    rapid_multi_party_chat: bool = False
    bot_replied_within_60s: bool = False
    valid_member_answer_exists: bool = False
    intent_confidence: float = 1.0

    # Existing channel policy must nominate a message before soft participation.
    base_eligible: bool = False
    base_reason: str = ""

    # Durable group budget observations.
    bot_messages_last_40: int = 0
    total_messages_last_40: int = 0
    soft_replies_last_10m: int = 0
    soft_replies_last_hour: int = 0
    consecutive_bot_messages: int = 0
    proactive_messages_today: int = 0
    group_silence_seconds: int = 0

    # Send-time observations.
    is_self_sent: bool = False
    topic_changed: bool = False
    superseded_by_newer_message: bool = False
    requested_proactive: bool = False
    response_kind: str = "short"
    reply_target_ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class ParticipationDecision:
    status: ParticipationStatus
    score: int
    reason_codes: tuple[str, ...]
    not_before: datetime | None = None
    expires_at: datetime | None = None
    mention_sender: bool = False

    @property
    def should_generate(self) -> bool:
        return self.status in {
            ParticipationStatus.MUST_REPLY,
            ParticipationStatus.MAY_REPLY,
            # A deferred reply is a durable scheduled candidate.  It is
            # generated once and queued with ``not_before`` so a restart cannot
            # turn a temporary budget/quiet-hours decision into permanent
            # silence; the bridge revalidates it when it becomes due.
            ParticipationStatus.DEFER,
        }

    @property
    def should_send(self) -> bool:
        return self.status in {
            ParticipationStatus.MUST_REPLY,
            ParticipationStatus.MAY_REPLY,
        }


class SocialParticipationService:
    """Make a deterministic, auditable group-participation decision."""

    def decide(
        self,
        context: ParticipationContext,
        policy: ParticipationPolicy | None = None,
    ) -> ParticipationDecision:
        active_policy = policy or ParticipationPolicy()
        now = _aware_utc(context.now)

        if not active_policy.enabled:
            return self._decision(
                ParticipationStatus.OBSERVE_ONLY,
                0,
                ("participation_disabled",),
            )
        if context.is_self_sent:
            return self._decision(
                ParticipationStatus.CANCEL,
                0,
                ("self_message",),
            )
        must_reasons: list[str] = []
        must_score = 0
        if context.mentioned_me:
            must_reasons.append("direct_mention")
            must_score = max(must_score, 85)
        if context.replied_to_bot:
            must_reasons.append("reply_to_bot")
            must_score = max(must_score, 85)
        if context.explicit_command:
            must_reasons.append("explicit_command")
            must_score = max(must_score, 100)
        if context.safety_response_required:
            must_reasons.append("safety_response_required")
            must_score = max(must_score, 100)
        if must_reasons:
            not_before, _ = self._timing(
                context,
                ParticipationStatus.MUST_REPLY,
            )
            return self._decision(
                ParticipationStatus.MUST_REPLY,
                must_score,
                tuple(must_reasons),
                not_before=not_before,
                # A direct address is a durable conversation obligation.  It
                # can be deterministically superseded or answered at send
                # time, but must not disappear merely because a worker was
                # delayed beyond the short-reply SLO.
                expires_at=None,
                mention_sender=self._should_mention_sender(context, active_policy),
            )

        if context.requested_proactive:
            return self._decide_proactive(context, active_policy, now)

        if context.topic_changed:
            return self._decision(
                ParticipationStatus.CANCEL,
                0,
                ("topic_changed",),
            )
        if context.superseded_by_newer_message:
            return self._decision(
                ParticipationStatus.CANCEL,
                0,
                ("superseded_by_newer_message",),
            )

        if not context.base_eligible:
            return self._decision(
                ParticipationStatus.OBSERVE_ONLY,
                0,
                (context.base_reason or "base_policy_not_eligible",),
            )

        score = 0
        reasons: list[str] = []
        score = _add_signal(
            score,
            reasons,
            context.explicit_question_to_bot,
            60,
            "explicit_question_to_bot",
        )
        score = _add_signal(
            score,
            reasons,
            context.keyword_triggered,
            35,
            "keyword_trigger",
        )
        score = _add_signal(
            score,
            reasons,
            context.topic_continuation,
            25,
            "recent_topic_continuation",
        )
        score = _add_signal(
            score,
            reasons,
            context.unfinished_task_continuation,
            20,
            "unfinished_task_continuation",
        )
        score = _add_signal(
            score,
            reasons,
            context.directed_to_other_member,
            -60,
            "directed_to_other_member",
        )
        score = _add_signal(
            score,
            reasons,
            context.rapid_multi_party_chat,
            -30,
            "rapid_multi_party_chat",
        )
        score = _add_signal(
            score,
            reasons,
            context.bot_replied_within_60s,
            -25,
            "bot_replied_recently",
        )
        ratio = _bot_ratio(context)
        score = _add_signal(
            score,
            reasons,
            ratio > active_policy.max_bot_ratio_last_40,
            -30,
            "bot_ratio_above_limit",
        )
        score = _add_signal(
            score,
            reasons,
            context.valid_member_answer_exists,
            -40,
            "valid_member_answer_exists",
        )
        score = _add_signal(
            score,
            reasons,
            context.intent_confidence < 0.6,
            -20,
            "low_intent_confidence",
        )

        if context.valid_member_answer_exists:
            return self._decision(
                ParticipationStatus.CANCEL,
                score,
                (*reasons, "answered_by_member"),
            )
        if score < active_policy.threshold:
            return self._decision(
                ParticipationStatus.OBSERVE_ONLY,
                score,
                (*reasons, "score_below_threshold"),
            )
        if self._in_quiet_hours(now, active_policy):
            not_before = self._next_quiet_end(now, active_policy)
            return self._decision(
                ParticipationStatus.DEFER,
                score,
                (*reasons, "quiet_hours"),
                not_before=not_before,
                expires_at=not_before + timedelta(minutes=10),
            )
        budget_reason = self._soft_budget_reason(context, active_policy)
        if budget_reason:
            return self._decision(
                ParticipationStatus.DEFER,
                score,
                (*reasons, budget_reason),
                not_before=now + timedelta(seconds=45),
                expires_at=now + timedelta(minutes=10),
            )

        not_before, expires_at = self._timing(
            context,
            ParticipationStatus.MAY_REPLY,
        )
        return self._decision(
            ParticipationStatus.MAY_REPLY,
            score,
            (*reasons, "score_threshold_met"),
            not_before=not_before,
            expires_at=expires_at,
            mention_sender=self._should_mention_sender(context, active_policy),
        )

    def revalidate(
        self,
        decision: ParticipationDecision,
        context: ParticipationContext,
        policy: ParticipationPolicy | None = None,
    ) -> ParticipationDecision:
        """Re-check volatile send conditions after generation and queue delay."""

        now = _aware_utc(context.now)
        active_policy = policy or ParticipationPolicy()
        if (
            active_policy.mention_sender_strategy == "never"
            and decision.mention_sender
        ):
            decision = replace(decision, mention_sender=False)
        if not active_policy.enabled:
            return replace(
                decision,
                status=ParticipationStatus.CANCEL,
                reason_codes=(*decision.reason_codes, "participation_disabled_at_send"),
            )
        if context.is_self_sent:
            return replace(
                decision,
                status=ParticipationStatus.CANCEL,
                reason_codes=(*decision.reason_codes, "self_message_at_send"),
            )
        if decision.status is ParticipationStatus.MUST_REPLY:
            if context.valid_member_answer_exists:
                return replace(
                    decision,
                    status=ParticipationStatus.CANCEL,
                    reason_codes=(
                        *decision.reason_codes,
                        "obligation_answered_before_send",
                    ),
                )
            if context.superseded_by_newer_message:
                return replace(
                    decision,
                    status=ParticipationStatus.CANCEL,
                    reason_codes=(
                        *decision.reason_codes,
                        "obligation_superseded_before_send",
                    ),
                )
            if (
                active_policy.max_consecutive_bot_messages > 0
                and context.consecutive_bot_messages
                >= active_policy.max_consecutive_bot_messages
            ):
                return replace(
                    decision,
                    status=ParticipationStatus.DEFER,
                    not_before=now + timedelta(seconds=45),
                    # Keep the obligation durable while waiting for a human
                    # turn to break the bot-message run.  Each send attempt is
                    # still fenced by policy version, kill switch, self-send,
                    # answer and supersession checks.
                    expires_at=datetime.max.replace(tzinfo=UTC),
                    reason_codes=(
                        *decision.reason_codes,
                        "obligation_waiting_for_human_turn",
                    ),
                )
        if (
            decision.status is not ParticipationStatus.MUST_REPLY
            and context.valid_member_answer_exists
        ):
            return replace(
                decision,
                status=ParticipationStatus.CANCEL,
                reason_codes=(*decision.reason_codes, "answered_before_send"),
            )
        if (
            decision.status is not ParticipationStatus.MUST_REPLY
            and context.topic_changed
        ):
            return replace(
                decision,
                status=ParticipationStatus.CANCEL,
                reason_codes=(*decision.reason_codes, "topic_changed_before_send"),
            )
        if (
            decision.status is not ParticipationStatus.MUST_REPLY
            and context.superseded_by_newer_message
        ):
            return replace(
                decision,
                status=ParticipationStatus.CANCEL,
                reason_codes=(*decision.reason_codes, "superseded_before_send"),
            )
        if (
            decision.status is not ParticipationStatus.MUST_REPLY
            and decision.expires_at is not None
            and now >= _aware_utc(decision.expires_at)
        ):
            return replace(
                decision,
                status=ParticipationStatus.CANCEL,
                reason_codes=(*decision.reason_codes, "reply_expired"),
            )
        if decision.not_before is not None and now < _aware_utc(decision.not_before):
            return replace(
                decision,
                status=ParticipationStatus.DEFER,
                reason_codes=(*decision.reason_codes, "not_before_pending"),
            )
        if decision.status in {
            ParticipationStatus.MAY_REPLY,
            ParticipationStatus.DEFER,
        }:
            if self._in_quiet_hours(now, active_policy):
                next_due = self._next_quiet_end(now, active_policy)
                return replace(
                    decision,
                    status=ParticipationStatus.DEFER,
                    not_before=next_due,
                    expires_at=next_due + timedelta(minutes=10),
                    reason_codes=(*decision.reason_codes, "quiet_hours_at_send"),
                )
            budget_reason = self._soft_budget_reason(
                context,
                active_policy,
            )
            if budget_reason:
                return replace(
                    decision,
                    status=ParticipationStatus.DEFER,
                    not_before=now + timedelta(seconds=45),
                    expires_at=now + timedelta(minutes=10),
                    reason_codes=(*decision.reason_codes, f"{budget_reason}_at_send"),
                )
            if decision.status is ParticipationStatus.DEFER:
                return replace(
                    decision,
                    status=ParticipationStatus.MAY_REPLY,
                    not_before=None,
                    reason_codes=(*decision.reason_codes, "defer_revalidated"),
                )
        return decision

    def _decide_proactive(
        self,
        context: ParticipationContext,
        policy: ParticipationPolicy,
        now: datetime,
    ) -> ParticipationDecision:
        if not policy.proactive_enabled:
            return self._decision(
                ParticipationStatus.OBSERVE_ONLY,
                0,
                ("proactive_disabled",),
            )
        if self._in_quiet_hours(now, policy):
            not_before = self._next_quiet_end(now, policy)
            return self._decision(
                ParticipationStatus.DEFER,
                0,
                ("proactive_quiet_hours",),
                not_before=not_before,
                expires_at=not_before + timedelta(minutes=15),
            )
        if context.proactive_messages_today >= policy.max_proactive_per_day:
            return self._decision(
                ParticipationStatus.OBSERVE_ONLY,
                0,
                ("proactive_daily_budget_exhausted",),
            )
        if context.group_silence_seconds < policy.proactive_min_silence_seconds:
            return self._decision(
                ParticipationStatus.OBSERVE_ONLY,
                0,
                ("proactive_group_not_silent_long_enough",),
            )
        budget_reason = self._soft_budget_reason(context, policy)
        if budget_reason:
            return self._decision(
                ParticipationStatus.DEFER,
                0,
                (budget_reason,),
                not_before=now + timedelta(minutes=10),
                expires_at=now + timedelta(minutes=20),
            )
        not_before, expires_at = self._timing(
            context,
            ParticipationStatus.MAY_REPLY,
            proactive=True,
        )
        return self._decision(
            ParticipationStatus.MAY_REPLY,
            policy.threshold,
            ("proactive_opted_in", "proactive_silence_met"),
            not_before=not_before,
            expires_at=expires_at,
        )

    @staticmethod
    def _should_mention_sender(
        context: ParticipationContext,
        policy: ParticipationPolicy,
    ) -> bool:
        """Apply the versioned group strategy to every service-owned mention.

        Member-level ``no_group_mentions`` is intentionally applied later by
        the channel policy hook, so it remains the final, stricter override.
        """

        return bool(
            policy.mention_sender_strategy == "reply_or_ambiguous"
            and (context.replied_to_bot or context.reply_target_ambiguous)
        )

    @staticmethod
    def _soft_budget_reason(
        context: ParticipationContext,
        policy: ParticipationPolicy,
    ) -> str:
        if context.soft_replies_last_10m >= policy.max_soft_replies_10m:
            return "soft_budget_10m_exhausted"
        if context.soft_replies_last_hour >= policy.max_soft_replies_hour:
            return "soft_budget_hour_exhausted"
        if context.consecutive_bot_messages >= policy.max_consecutive_bot_messages:
            return "consecutive_bot_message_limit"
        if (
            context.total_messages_last_40 > 0
            and _projected_bot_ratio(context) > policy.max_bot_ratio_last_40
        ):
            return "projected_bot_ratio_limit"
        return ""

    @staticmethod
    def _in_quiet_hours(now: datetime, policy: ParticipationPolicy) -> bool:
        if policy.quiet_start_hour == policy.quiet_end_hour:
            return False
        try:
            zone: tzinfo = ZoneInfo(policy.timezone)
        except ZoneInfoNotFoundError:
            zone = UTC
        hour = now.astimezone(zone).hour
        if policy.quiet_start_hour < policy.quiet_end_hour:
            return policy.quiet_start_hour <= hour < policy.quiet_end_hour
        return hour >= policy.quiet_start_hour or hour < policy.quiet_end_hour

    @staticmethod
    def _next_quiet_end(now: datetime, policy: ParticipationPolicy) -> datetime:
        try:
            zone: tzinfo = ZoneInfo(policy.timezone)
        except ZoneInfoNotFoundError:
            zone = UTC
        local = now.astimezone(zone)
        candidate = local.replace(
            hour=policy.quiet_end_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    def timing_for(
        self,
        context: ParticipationContext,
        status: ParticipationStatus,
        *,
        proactive: bool = False,
    ) -> tuple[datetime, datetime]:
        """Expose deterministic timing so post-capability hooks can retime tools."""

        return self._timing(context, status, proactive=proactive)

    @staticmethod
    def _timing(
        context: ParticipationContext,
        status: ParticipationStatus,
        *,
        proactive: bool = False,
    ) -> tuple[datetime, datetime]:
        now = _aware_utc(context.now)
        kind = str(context.response_kind or "short").strip().lower()
        if proactive:
            lower, upper, ttl = 2.0, 6.0, 45.0
        elif kind == "tool_progress":
            lower, upper, ttl = 0.4, 1.2, 20.0
        elif kind == "tool_result":
            lower, upper, ttl = 0.5, 1.5, 30.0
        elif status == ParticipationStatus.MAY_REPLY:
            lower, upper, ttl = 2.0, 6.0, 45.0
        else:
            lower, upper, ttl = 0.8, 2.5, 30.0
        seconds = _stable_delay(
            f"{context.tenant_id}:{context.session_id}:{context.message_id}:{kind}",
            lower,
            upper,
        )
        return now + timedelta(seconds=seconds), now + timedelta(seconds=ttl)

    @staticmethod
    def _decision(
        status: ParticipationStatus,
        score: int,
        reason_codes: tuple[str, ...],
        *,
        not_before: datetime | None = None,
        expires_at: datetime | None = None,
        mention_sender: bool = False,
    ) -> ParticipationDecision:
        return ParticipationDecision(
            status=status,
            score=score,
            reason_codes=reason_codes or ("unspecified",),
            not_before=not_before,
            expires_at=expires_at,
            # Only mention when a channel adapter proves ambiguity. The default
            # avoids tagging every sender and feeling like a notification bot.
            mention_sender=mention_sender,
        )


def _add_signal(
    score: int,
    reasons: list[str],
    enabled: bool,
    value: int,
    code: str,
) -> int:
    if not enabled:
        return score
    prefix = "plus" if value >= 0 else "minus"
    reasons.append(f"{code}:{prefix}{abs(value)}")
    return score + value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bot_ratio(context: ParticipationContext) -> float:
    if context.total_messages_last_40 <= 0:
        return 0.0
    return max(0, context.bot_messages_last_40) / max(
        1,
        context.total_messages_last_40,
    )


def _projected_bot_ratio(context: ParticipationContext) -> float:
    return (max(0, context.bot_messages_last_40) + 1) / max(
        1,
        context.total_messages_last_40 + 1,
    )


def _stable_delay(seed: str, lower: float, upper: float) -> float:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
    return lower + ((upper - lower) * fraction)
