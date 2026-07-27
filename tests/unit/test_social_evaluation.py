import pytest

from app.social.evaluation import (
    HUMANIZATION_SCENARIOS,
    HumanizationObservation,
    evaluate_humanization,
    validate_scenario_contract,
)


def test_humanization_replay_meets_every_locked_release_gate() -> None:
    # Synthetic observations test the SLO arithmetic only; they are not release evidence.
    observations = [
        HumanizationObservation(
            scenario=HUMANIZATION_SCENARIOS[index % len(HUMANIZATION_SCENARIOS)],
            directly_addressed=True,
            expected_reply=True,
            sent=True,
            final_delivery_state="succeeded",
            rollout_stage="contextual",
            cohort="contextual",
            valid_member_answer_exists=False,
            cross_audience_memory_leak=False,
            near_duplicate_within_24h=False,
            duplicate_guard_triggered=False,
            bot_ratio_last_40=0.15,
            consecutive_bot_messages=0,
            added_scheduling_delay_seconds=7.5,
            actual_delivery_delay_seconds=9.0,
            naturalness_before=3.5,
            naturalness_after=4.1,
            task_completed_before=True,
            task_completed_after=True,
        )
        for index in range(1_000)
    ]
    observations.extend(
        HumanizationObservation(
            scenario=HUMANIZATION_SCENARIOS[index % len(HUMANIZATION_SCENARIOS)],
            directly_addressed=False,
            expected_reply=False,
            sent=False,
            final_delivery_state="not_sent",
            rollout_stage="contextual",
            cohort="contextual",
            valid_member_answer_exists=True,
            cross_audience_memory_leak=False,
            bot_ratio_last_40=0.1,
            consecutive_bot_messages=0,
            naturalness_before=3.4,
            naturalness_after=4.0,
            task_completed_before=True,
            task_completed_after=True,
        )
        for index in range(1_000)
    )

    report = evaluate_humanization(observations)

    assert report.passed is True
    assert report.direct_call_recall == 1.0
    assert report.non_call_false_insertion_rate == 0.0
    assert report.answered_before_send_rate == 0.0
    assert report.bot_ratio_last_40_p95 == 0.15
    assert report.added_scheduling_delay_p95_seconds == 7.5
    assert report.naturalness_improvement is not None
    assert report.naturalness_improvement >= 0.5
    assert report.sample_sizes["direct_calls"] == 1_000
    assert report.sample_sizes["answered_contexts"] == 1_000
    assert report.wilson_intervals["direct_call_recall"][0] >= 0.99
    assert report.wilson_intervals["answered_before_send_rate"][1] < 0.005
    assert report.final_delivery_state_counts == {
        "not_sent": 1_000,
        "succeeded": 1_000,
    }
    assert report.rollout_stage_counts == {"contextual": 2_000}
    assert report.cohort_counts == {"contextual": 2_000}
    assert report.scenario_counts == {scenario: 200 for scenario in HUMANIZATION_SCENARIOS}
    assert report.missing_scenarios == ()
    assert report.gates["all_required_scenarios_covered"] is True
    assert report.segment_summaries["contextual|contextual"]["sample_count"] == 2_000
    # The locked SLO applies to the added scheduling delay, not arbitrary
    # provider/network latency after the queue releases the reply.
    assert report.actual_delivery_delay_p95_seconds == 9.0
    assert report.gates["added_scheduling_delay_p95_lt_8s"] is True


def test_humanization_replay_fails_closed_when_required_labels_are_missing() -> None:
    report = evaluate_humanization([])

    assert report.passed is False
    assert not any(report.gates.values())


def test_humanization_replay_cannot_pass_with_defaulted_safety_labels() -> None:
    observations = [
        HumanizationObservation(
            scenario=HUMANIZATION_SCENARIOS[index % len(HUMANIZATION_SCENARIOS)],
            directly_addressed=index < 1_000,
            expected_reply=index < 1_000,
            sent=index < 1_000,
            final_delivery_state="succeeded" if index < 1_000 else "not_sent",
            rollout_stage="contextual",
            cohort="contextual",
            actual_delivery_delay_seconds=1.0 if index < 1_000 else None,
            naturalness_before=3.0,
            naturalness_after=4.0,
            task_completed_before=True,
            task_completed_after=True,
        )
        for index in range(2_000)
    ]

    report = evaluate_humanization(observations)

    assert report.passed is False
    assert report.gates["runtime_slo_fields_complete"] is False


def test_humanization_replay_exposes_each_regression() -> None:
    report = evaluate_humanization(
        [
            HumanizationObservation(
                scenario="direct_mention",
                directly_addressed=True,
                expected_reply=True,
                sent=False,
                cross_audience_memory_leak=True,
                bot_ratio_last_40=0.3,
                consecutive_bot_messages=3,
                naturalness_before=4.0,
                naturalness_after=3.0,
                task_completed_before=True,
                task_completed_after=False,
            ),
            HumanizationObservation(
                scenario="member_answer_exists",
                directly_addressed=False,
                expected_reply=False,
                sent=True,
                valid_member_answer_exists=True,
                near_duplicate_within_24h=True,
                bot_ratio_last_40=0.3,
                consecutive_bot_messages=3,
                added_scheduling_delay_seconds=9.0,
                naturalness_before=4.0,
                naturalness_after=3.0,
                task_completed_before=True,
                task_completed_after=False,
            ),
        ]
    )

    assert report.passed is False
    assert not any(report.gates.values())


def test_humanization_replay_fails_closed_below_locked_sample_sizes() -> None:
    report = evaluate_humanization(
        [
            HumanizationObservation(
                scenario=HUMANIZATION_SCENARIOS[index % len(HUMANIZATION_SCENARIOS)],
                directly_addressed=True,
                expected_reply=True,
                sent=True,
                final_delivery_state="succeeded",
                rollout_stage="contextual",
                cohort="contextual",
                actual_delivery_delay_seconds=1.0,
                naturalness_before=3.0,
                naturalness_after=4.0,
                task_completed_before=True,
                task_completed_after=True,
            )
            for index in range(99)
        ]
    )

    assert report.passed is False
    assert report.gates["critical_rate_samples_gte_1000"] is False
    assert report.gates["score_samples_gte_100"] is False


def test_scenario_contract_requires_each_locked_privacy_safe_label() -> None:
    observations = [
        HumanizationObservation(
            scenario=scenario,
            directly_addressed=scenario in {"direct_mention", "inline_mention"},
            expected_reply=scenario in {"direct_mention", "inline_mention"},
            sent=False,
        )
        for scenario in HUMANIZATION_SCENARIOS
    ]

    complete = validate_scenario_contract(observations)
    missing_one = validate_scenario_contract(observations[:-1])
    report = evaluate_humanization(observations)

    assert complete.passed is True
    assert complete.scenario_counts == {scenario: 1 for scenario in HUMANIZATION_SCENARIOS}
    assert complete.missing_scenarios == ()
    assert missing_one.passed is False
    assert missing_one.missing_scenarios == ("topic_changed_before_send",)
    assert report.gates["all_required_scenarios_covered"] is True
    assert report.passed is False
    assert report.gates["critical_rate_samples_gte_1000"] is False


def test_humanization_observation_rejects_unknown_scenario_without_echoing_it() -> None:
    with pytest.raises(ValueError) as exc_info:
        HumanizationObservation(
            scenario="unsupported",  # type: ignore[arg-type]
            directly_addressed=False,
            expected_reply=False,
            sent=False,
        )

    assert str(exc_info.value) == "scenario must be one of the supported privacy-safe labels"
    assert "unsupported" not in str(exc_info.value)
