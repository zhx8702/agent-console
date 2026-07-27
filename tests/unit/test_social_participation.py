from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.social import (
    ParticipationContext,
    ParticipationDecision,
    ParticipationPolicy,
    ParticipationStatus,
    SocialParticipationService,
)

NOW = datetime(2026, 7, 16, 4, 0, tzinfo=UTC)  # 12:00 Asia/Shanghai


def _ctx(**overrides: object) -> ParticipationContext:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "session_id": "room@chatroom",
        "message_id": "msg-1",
        "now": NOW,
    }
    values.update(overrides)
    return ParticipationContext(**values)  # type: ignore[arg-type]


def test_direct_calls_are_must_reply_and_bypass_soft_budgets() -> None:
    decision = SocialParticipationService().decide(
        _ctx(
            mentioned_me=True,
            soft_replies_last_10m=99,
            consecutive_bot_messages=99,
        )
    )

    assert decision.status == ParticipationStatus.MUST_REPLY
    assert decision.score == 85
    assert "direct_mention" in decision.reason_codes
    assert decision.not_before is not None
    assert NOW + timedelta(seconds=0.8) <= decision.not_before <= NOW + timedelta(seconds=2.5)


def test_mentions_sender_only_when_versioned_group_strategy_allows_it() -> None:
    service = SocialParticipationService()

    plain_mention = service.decide(_ctx(mentioned_me=True))
    default_quoted_reply = service.decide(_ctx(replied_to_bot=True))
    quoted_reply = service.decide(
        _ctx(replied_to_bot=True),
        ParticipationPolicy(mention_sender_strategy="reply_or_ambiguous"),
    )
    ambiguous = service.decide(
        _ctx(
            base_eligible=True,
            explicit_question_to_bot=True,
            reply_target_ambiguous=True,
        ),
        ParticipationPolicy(
            max_bot_ratio_last_40=1,
            mention_sender_strategy="reply_or_ambiguous",
        ),
    )

    assert not plain_mention.mention_sender
    assert not default_quoted_reply.mention_sender
    assert quoted_reply.mention_sender
    assert ambiguous.mention_sender


def test_group_kill_switch_and_self_messages_always_fail_closed() -> None:
    service = SocialParticipationService()

    disabled = service.decide(
        _ctx(mentioned_me=True),
        ParticipationPolicy(enabled=False),
    )
    self_sent = service.decide(_ctx(mentioned_me=True, is_self_sent=True))

    assert disabled.status == ParticipationStatus.OBSERVE_ONLY
    assert disabled.reason_codes == ("participation_disabled",)
    assert self_sent.status == ParticipationStatus.CANCEL
    assert self_sent.reason_codes == ("self_message",)


def test_soft_score_uses_documented_weights_and_threshold() -> None:
    decision = SocialParticipationService().decide(
        _ctx(
            base_eligible=True,
            explicit_question_to_bot=True,
            keyword_triggered=True,
            rapid_multi_party_chat=True,
        ),
        ParticipationPolicy(max_bot_ratio_last_40=1),
    )

    assert decision.status == ParticipationStatus.MAY_REPLY
    assert decision.score == 65
    assert decision.reason_codes == (
        "explicit_question_to_bot:plus60",
        "keyword_trigger:plus35",
        "rapid_multi_party_chat:minus30",
        "score_threshold_met",
    )
    assert decision.not_before is not None
    assert NOW + timedelta(seconds=2) <= decision.not_before <= NOW + timedelta(seconds=6)


def test_low_score_observes_and_valid_member_answer_cancels() -> None:
    service = SocialParticipationService()
    low = service.decide(
        _ctx(base_eligible=True, keyword_triggered=True),
        ParticipationPolicy(max_bot_ratio_last_40=1),
    )
    answered = service.decide(
        _ctx(
            base_eligible=True,
            explicit_question_to_bot=True,
            valid_member_answer_exists=True,
        ),
        ParticipationPolicy(max_bot_ratio_last_40=1),
    )

    assert low.status == ParticipationStatus.OBSERVE_ONLY
    assert low.score == 35
    assert low.reason_codes[-1] == "score_below_threshold"
    assert answered.status == ParticipationStatus.CANCEL
    assert answered.score == 20
    assert "valid_member_answer_exists:minus40" in answered.reason_codes


def test_soft_budget_ratio_and_quiet_hours_defer() -> None:
    service = SocialParticipationService()
    budgeted = service.decide(
        _ctx(
            base_eligible=True,
            explicit_question_to_bot=True,
            soft_replies_last_10m=2,
        ),
        ParticipationPolicy(max_bot_ratio_last_40=1),
    )
    quiet = service.decide(
        _ctx(
            now=datetime(2026, 7, 16, 16, 30, tzinfo=UTC),  # 00:30 Shanghai
            base_eligible=True,
            explicit_question_to_bot=True,
        ),
        ParticipationPolicy(max_bot_ratio_last_40=1),
    )

    assert budgeted.status == ParticipationStatus.DEFER
    assert budgeted.reason_codes[-1] == "soft_budget_10m_exhausted"
    assert quiet.status == ParticipationStatus.DEFER
    assert quiet.reason_codes[-1] == "quiet_hours"


def test_proactive_is_opt_in_daily_limited_and_requires_three_hours_silence() -> None:
    service = SocialParticipationService()
    policy = ParticipationPolicy(proactive_enabled=True, max_bot_ratio_last_40=1)

    too_soon = service.decide(
        _ctx(requested_proactive=True, group_silence_seconds=100),
        policy,
    )
    allowed = service.decide(
        _ctx(requested_proactive=True, group_silence_seconds=3 * 60 * 60),
        policy,
    )
    exhausted = service.decide(
        _ctx(
            requested_proactive=True,
            group_silence_seconds=3 * 60 * 60,
            proactive_messages_today=1,
        ),
        policy,
    )

    assert too_soon.status == ParticipationStatus.OBSERVE_ONLY
    assert allowed.status == ParticipationStatus.MAY_REPLY
    assert allowed.reason_codes == ("proactive_opted_in", "proactive_silence_met")
    assert exhausted.reason_codes == ("proactive_daily_budget_exhausted",)


def test_send_time_revalidation_cancels_stale_or_answered_reply() -> None:
    service = SocialParticipationService()
    decision = ParticipationDecision(
        status=ParticipationStatus.MAY_REPLY,
        score=80,
        reason_codes=("score_threshold_met",),
        not_before=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=30),
    )

    answered = service.revalidate(
        decision,
        _ctx(valid_member_answer_exists=True),
    )
    changed = service.revalidate(decision, _ctx(topic_changed=True))
    expired = service.revalidate(
        decision,
        _ctx(now=NOW + timedelta(seconds=31)),
    )

    assert answered.status == ParticipationStatus.CANCEL
    assert answered.reason_codes[-1] == "answered_before_send"
    assert changed.reason_codes[-1] == "topic_changed_before_send"
    assert expired.reason_codes[-1] == "reply_expired"


def test_send_time_revalidation_clears_mentions_when_group_strategy_is_now_never() -> None:
    service = SocialParticipationService()
    decision = ParticipationDecision(
        status=ParticipationStatus.MUST_REPLY,
        score=85,
        reason_codes=("reply_to_bot",),
        mention_sender=True,
    )

    revalidated = service.revalidate(
        decision,
        _ctx(replied_to_bot=True),
        ParticipationPolicy(mention_sender_strategy="never"),
    )

    assert revalidated.status == ParticipationStatus.MUST_REPLY
    assert revalidated.mention_sender is False


def test_must_reply_revalidation_ignores_unrelated_chatter_and_short_expiry() -> None:
    service = SocialParticipationService()
    decision = ParticipationDecision(
        status=ParticipationStatus.MUST_REPLY,
        score=85,
        reason_codes=("direct_mention",),
        not_before=NOW - timedelta(seconds=60),
        expires_at=NOW - timedelta(seconds=30),
    )

    revalidated = service.revalidate(
        decision,
        _ctx(topic_changed=True),
    )

    assert revalidated.status == ParticipationStatus.MUST_REPLY
    assert revalidated.reason_codes == ("direct_mention",)


def test_must_reply_revalidation_cancels_only_tied_answer_or_supersession() -> None:
    service = SocialParticipationService()
    decision = ParticipationDecision(
        status=ParticipationStatus.MUST_REPLY,
        score=85,
        reason_codes=("direct_mention",),
    )

    answered = service.revalidate(
        decision,
        _ctx(valid_member_answer_exists=True),
    )
    superseded = service.revalidate(
        decision,
        _ctx(superseded_by_newer_message=True),
    )

    assert answered.status == ParticipationStatus.CANCEL
    assert answered.reason_codes[-1] == "obligation_answered_before_send"
    assert superseded.status == ParticipationStatus.CANCEL
    assert superseded.reason_codes[-1] == "obligation_superseded_before_send"


def test_must_reply_waits_for_a_human_turn_before_third_bot_message() -> None:
    service = SocialParticipationService()
    decision = ParticipationDecision(
        status=ParticipationStatus.MUST_REPLY,
        score=85,
        reason_codes=("direct_mention",),
    )
    policy = ParticipationPolicy(max_consecutive_bot_messages=2)

    waiting = service.revalidate(
        decision,
        _ctx(consecutive_bot_messages=2),
        policy,
    )
    after_human = service.revalidate(
        decision,
        _ctx(consecutive_bot_messages=0),
        policy,
    )

    assert waiting.status == ParticipationStatus.DEFER
    assert waiting.not_before == NOW + timedelta(seconds=45)
    assert waiting.expires_at == datetime.max.replace(tzinfo=UTC)
    assert waiting.reason_codes[-1] == "obligation_waiting_for_human_turn"
    assert after_human.status == ParticipationStatus.MUST_REPLY


def test_invalid_policy_bounds_are_rejected() -> None:
    with pytest.raises(ValueError):
        ParticipationPolicy(max_bot_ratio_last_40=1.1)
    with pytest.raises(ValueError):
        ParticipationPolicy(quiet_start_hour=24)
    with pytest.raises(ValueError):
        ParticipationPolicy(prompt_context_retention_seconds=86_401)
    with pytest.raises(ValueError):
        ParticipationPolicy(mention_sender_strategy="always")  # type: ignore[arg-type]
