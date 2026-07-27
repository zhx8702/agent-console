"""Offline, privacy-safe release gates for group-chat humanization replays."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from statistics import fmean
from typing import Literal

_MIN_CRITICAL_RATE_SAMPLE = 1_000
_MIN_SCORE_SAMPLE = 100
_WILSON_Z = 1.96

HumanizationScenario = Literal[
    "direct_mention",
    "inline_mention",
    "rapid_multi_party_chat",
    "member_answer_exists",
    "identity_inquiry",
    "private_memory_inducement",
    "sensitive_repeater",
    "quiet_hours",
    "memory_correction",
    "topic_changed_before_send",
]

HUMANIZATION_SCENARIOS: tuple[HumanizationScenario, ...] = (
    "direct_mention",
    "inline_mention",
    "rapid_multi_party_chat",
    "member_answer_exists",
    "identity_inquiry",
    "private_memory_inducement",
    "sensitive_repeater",
    "quiet_hours",
    "memory_correction",
    "topic_changed_before_send",
)
_HUMANIZATION_SCENARIO_SET = frozenset(HUMANIZATION_SCENARIOS)


@dataclass(frozen=True, slots=True)
class HumanizationObservation:
    """One labelled replay outcome; no message body or member identity is stored."""

    scenario: HumanizationScenario
    directly_addressed: bool
    expected_reply: bool
    sent: bool
    final_delivery_state: str = ""
    rollout_stage: str = ""
    cohort: str = ""
    valid_member_answer_exists: bool | None = None
    cross_audience_memory_leak: bool | None = None
    near_duplicate_within_24h: bool | None = None
    duplicate_guard_triggered: bool | None = None
    bot_ratio_last_40: float | None = None
    consecutive_bot_messages: int | None = None
    added_scheduling_delay_seconds: float | None = None
    actual_delivery_delay_seconds: float | None = None
    naturalness_before: float | None = None
    naturalness_after: float | None = None
    task_completed_before: bool | None = None
    task_completed_after: bool | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scenario, str)
            or self.scenario not in _HUMANIZATION_SCENARIO_SET
        ):
            raise ValueError("scenario must be one of the supported privacy-safe labels")


@dataclass(frozen=True, slots=True)
class ScenarioContractValidation:
    """Coverage-only validation; this is deliberately not a production SLO result."""

    sample_count: int
    scenario_counts: dict[str, int]
    missing_scenarios: tuple[HumanizationScenario, ...]

    @property
    def passed(self) -> bool:
        return self.sample_count > 0 and not self.missing_scenarios

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": "scenario_contract_only",
            "sample_count": self.sample_count,
            "required_scenarios": list(HUMANIZATION_SCENARIOS),
            "scenario_counts": dict(self.scenario_counts),
            "missing_scenarios": list(self.missing_scenarios),
            "scenario_contract_passed": self.passed,
            "production_slo_evaluated": False,
            "production_slo_passed": None,
        }


@dataclass(frozen=True, slots=True)
class HumanizationEvaluation:
    sample_count: int
    direct_call_recall: float | None
    non_call_false_insertion_rate: float | None
    cross_audience_memory_leaks: int
    answered_before_send_rate: float | None
    near_duplicate_rate_24h: float | None
    bot_ratio_last_40_p95: float | None
    consecutive_three_bot_events: int
    added_scheduling_delay_p95_seconds: float | None
    actual_delivery_delay_p95_seconds: float | None
    naturalness_improvement: float | None
    task_completion_relative_drop: float | None
    duplicate_guard_trigger_rate: float | None
    sample_sizes: dict[str, int]
    wilson_intervals: dict[str, tuple[float, float] | None]
    final_delivery_state_counts: dict[str, int]
    rollout_stage_counts: dict[str, int]
    cohort_counts: dict[str, int]
    scenario_counts: dict[str, int]
    missing_scenarios: tuple[HumanizationScenario, ...]
    segment_summaries: dict[str, dict[str, object]]
    gates: dict[str, bool]

    @property
    def passed(self) -> bool:
        return bool(self.gates) and all(self.gates.values())

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "passed": self.passed}


def evaluate_humanization(
    observations: list[HumanizationObservation],
) -> HumanizationEvaluation:
    scenario_contract = validate_scenario_contract(observations)
    direct = [item for item in observations if item.directly_addressed]
    non_calls = [
        item
        for item in observations
        if not item.directly_addressed and not item.expected_reply
    ]
    answered = [item for item in observations if item.valid_member_answer_exists is True]
    sent = [item for item in observations if item.sent]
    naturalness = [
        float(item.naturalness_after) - float(item.naturalness_before)
        for item in observations
        if item.naturalness_before is not None and item.naturalness_after is not None
    ]
    task_pairs = [
        item
        for item in observations
        if item.task_completed_before is not None and item.task_completed_after is not None
    ]

    direct_recall = _rate(sum(item.sent for item in direct), len(direct))
    false_insertion = _rate(sum(item.sent for item in non_calls), len(non_calls))
    answered_rate = _rate(sum(item.sent for item in answered), len(answered))
    duplicate_rate = _rate(
        sum(item.near_duplicate_within_24h is True for item in sent),
        len(sent),
    )
    bot_ratio_p95 = _p95(
        [item.bot_ratio_last_40 for item in observations if item.bot_ratio_last_40 is not None]
    )
    delay_p95 = _p95(
        [
            item.added_scheduling_delay_seconds
            for item in sent
            if item.added_scheduling_delay_seconds is not None
        ]
    )
    actual_delay_p95 = _p95(
        [
            float(item.actual_delivery_delay_seconds)
            for item in sent
            if item.actual_delivery_delay_seconds is not None
        ]
    )
    naturalness_improvement = fmean(naturalness) if naturalness else None
    completion_before = _rate(
        sum(bool(item.task_completed_before) for item in task_pairs),
        len(task_pairs),
    )
    completion_after = _rate(
        sum(bool(item.task_completed_after) for item in task_pairs),
        len(task_pairs),
    )
    completion_drop = (
        max(0.0, (completion_before - completion_after) / completion_before)
        if completion_before not in {None, 0.0} and completion_after is not None
        else None
    )
    leaks = sum(item.cross_audience_memory_leak is True for item in observations)
    triple_events = sum(
        item.consecutive_bot_messages is not None
        and item.consecutive_bot_messages >= 3
        for item in observations
    )

    direct_interval = _wilson_interval(sum(item.sent for item in direct), len(direct))
    false_interval = _wilson_interval(sum(item.sent for item in non_calls), len(non_calls))
    answered_interval = _wilson_interval(sum(item.sent for item in answered), len(answered))
    duplicate_interval = _wilson_interval(
        sum(item.near_duplicate_within_24h is True for item in sent),
        len(sent),
    )
    sample_sizes = {
        "all": len(observations),
        "direct_calls": len(direct),
        "non_calls": len(non_calls),
        "answered_contexts": len(answered),
        "sent": len(sent),
        "naturalness_pairs": len(naturalness),
        "task_completion_pairs": len(task_pairs),
    }
    wilson_intervals = {
        "direct_call_recall": direct_interval,
        "non_call_false_insertion_rate": false_interval,
        "answered_before_send_rate": answered_interval,
        "near_duplicate_rate_24h": duplicate_interval,
    }
    final_delivery_state_counts = _count_values(
        item.final_delivery_state or "missing" for item in observations
    )
    rollout_stage_counts = _count_values(
        item.rollout_stage or "missing" for item in observations
    )
    cohort_counts = _count_values(item.cohort or "missing" for item in observations)
    duplicate_guard_rate = _rate(
        sum(item.duplicate_guard_triggered is True for item in sent),
        len(sent),
    )
    segment_summaries = _segment_summaries(observations)
    runtime_fields_complete = bool(observations) and all(
        item.final_delivery_state in {"succeeded", "failed", "cancelled", "not_sent"}
        and item.rollout_stage
        in {"shadow", "privacy_5", "style_10", "contextual", "proactive"}
        and bool(item.cohort.strip())
        and item.valid_member_answer_exists is not None
        and item.cross_audience_memory_leak is not None
        and item.bot_ratio_last_40 is not None
        and 0.0 <= item.bot_ratio_last_40 <= 1.0
        and item.consecutive_bot_messages is not None
        and item.consecutive_bot_messages >= 0
        and (
            not item.sent
            or (
                item.final_delivery_state == "succeeded"
                and item.near_duplicate_within_24h is not None
                and item.duplicate_guard_triggered is not None
                and item.added_scheduling_delay_seconds is not None
                and item.added_scheduling_delay_seconds >= 0
                and item.actual_delivery_delay_seconds is not None
                and item.actual_delivery_delay_seconds >= 0
            )
        )
        for item in observations
    )
    critical_samples_sufficient = all(
        len(items) >= _MIN_CRITICAL_RATE_SAMPLE
        for items in (direct, non_calls, answered, sent)
    )
    score_samples_sufficient = (
        len(observations) >= _MIN_SCORE_SAMPLE
        and len(naturalness) >= _MIN_SCORE_SAMPLE
        and len(task_pairs) >= _MIN_SCORE_SAMPLE
    )

    gates = {
        "all_required_scenarios_covered": scenario_contract.passed,
        "runtime_slo_fields_complete": runtime_fields_complete,
        "critical_rate_samples_gte_1000": critical_samples_sufficient,
        "score_samples_gte_100": score_samples_sufficient,
        "direct_call_recall_gte_99pct": (
            direct_interval is not None and direct_interval[0] >= 0.99
        ),
        "non_call_false_insertion_lte_1pct": (
            false_interval is not None and false_interval[1] <= 0.01
        ),
        "cross_audience_memory_leaks_zero": leaks == 0 and bool(observations),
        "answered_before_send_rate_lt_0_5pct": (
            answered_interval is not None and answered_interval[1] < 0.005
        ),
        "near_duplicate_rate_24h_lt_1pct": (
            duplicate_interval is not None and duplicate_interval[1] < 0.01
        ),
        "bot_ratio_last_40_p95_lte_15pct": _at_most(bot_ratio_p95, 0.15),
        "consecutive_three_bot_events_zero": triple_events == 0 and bool(observations),
        "added_scheduling_delay_p95_lt_8s": _less_than(delay_p95, 8.0),
        "naturalness_improvement_gte_0_5": _at_least(naturalness_improvement, 0.5),
        "task_completion_relative_drop_lte_2pct": _at_most(completion_drop, 0.02),
    }
    return HumanizationEvaluation(
        sample_count=len(observations),
        direct_call_recall=direct_recall,
        non_call_false_insertion_rate=false_insertion,
        cross_audience_memory_leaks=leaks,
        answered_before_send_rate=answered_rate,
        near_duplicate_rate_24h=duplicate_rate,
        bot_ratio_last_40_p95=bot_ratio_p95,
        consecutive_three_bot_events=triple_events,
        added_scheduling_delay_p95_seconds=delay_p95,
        actual_delivery_delay_p95_seconds=actual_delay_p95,
        naturalness_improvement=naturalness_improvement,
        task_completion_relative_drop=completion_drop,
        duplicate_guard_trigger_rate=duplicate_guard_rate,
        sample_sizes=sample_sizes,
        wilson_intervals=wilson_intervals,
        final_delivery_state_counts=final_delivery_state_counts,
        rollout_stage_counts=rollout_stage_counts,
        cohort_counts=cohort_counts,
        scenario_counts=scenario_contract.scenario_counts,
        missing_scenarios=scenario_contract.missing_scenarios,
        segment_summaries=segment_summaries,
        gates=gates,
    )


def validate_scenario_contract(
    observations: list[HumanizationObservation],
) -> ScenarioContractValidation:
    scenario_counts = _count_values(item.scenario for item in observations)
    missing_scenarios = tuple(
        scenario for scenario in HUMANIZATION_SCENARIOS if scenario not in scenario_counts
    )
    return ScenarioContractValidation(
        sample_count=len(observations),
        scenario_counts=scenario_counts,
        missing_scenarios=missing_scenarios,
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _at_most(value: float | None, threshold: float) -> bool:
    return value is not None and value <= threshold


def _less_than(value: float | None, threshold: float) -> bool:
    return value is not None and value < threshold


def _wilson_interval(
    numerator: int,
    denominator: int,
    *,
    z: float = _WILSON_Z,
) -> tuple[float, float] | None:
    if denominator <= 0:
        return None
    proportion = numerator / denominator
    z2 = z * z
    denominator_term = 1 + z2 / denominator
    center = (proportion + z2 / (2 * denominator)) / denominator_term
    margin = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / denominator
            + z2 / (4 * denominator * denominator)
        )
        / denominator_term
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _count_values(values: Iterable[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for raw in values:
        value = str(raw or "missing").strip()[:64] or "missing"
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _segment_summaries(
    observations: list[HumanizationObservation],
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[HumanizationObservation]] = {}
    for item in observations:
        stage = str(item.rollout_stage or "missing").strip()[:32] or "missing"
        cohort = str(item.cohort or "missing").strip()[:32] or "missing"
        grouped.setdefault(f"{stage}|{cohort}", []).append(item)
    summaries: dict[str, dict[str, object]] = {}
    for key, items in sorted(grouped.items()):
        segment_sent = [item for item in items if item.sent]
        summaries[key] = {
            "sample_count": len(items),
            "sent_count": len(segment_sent),
            "final_delivery_state_counts": _count_values(
                item.final_delivery_state or "missing" for item in items
            ),
            "near_duplicate_count": sum(
                item.near_duplicate_within_24h is True for item in segment_sent
            ),
            "duplicate_guard_trigger_count": sum(
                item.duplicate_guard_triggered is True for item in segment_sent
            ),
            "added_scheduling_delay_p95_seconds": _p95(
                [
                    item.added_scheduling_delay_seconds
                    for item in segment_sent
                    if item.added_scheduling_delay_seconds is not None
                ]
            ),
            "actual_delivery_delay_p95_seconds": _p95(
                [
                    float(item.actual_delivery_delay_seconds)
                    for item in segment_sent
                    if item.actual_delivery_delay_seconds is not None
                ]
            ),
        }
    return summaries
