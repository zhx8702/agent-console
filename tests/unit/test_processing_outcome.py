from __future__ import annotations

from types import SimpleNamespace

from app.orchestrator.outcome import (
    ProcessingOutcome,
    ProcessingStatus,
    normalize_processing_outcome,
)


def test_legacy_none_and_route_results_remain_completed() -> None:
    none_result = normalize_processing_outcome(None)
    route_result = normalize_processing_outcome("faq")

    assert none_result.status == ProcessingStatus.COMPLETED
    assert none_result.reason == "legacy_none_result"
    assert route_result.status == ProcessingStatus.COMPLETED
    assert route_result.route_label == "faq"


def test_legacy_flow_terminal_states_are_adapted() -> None:
    stopped = normalize_processing_outcome(
        SimpleNamespace(status="stopped", stop_reason="policy_off")
    )
    failed = normalize_processing_outcome(
        SimpleNamespace(status="failed", error="database_unavailable")
    )

    assert stopped.status == ProcessingStatus.INTENTIONALLY_SUPPRESSED
    assert stopped.reason == "policy_off"
    assert failed.status == ProcessingStatus.RETRYABLE_FAILURE
    assert failed.reason == "database_unavailable"


def test_unknown_structured_result_is_not_acknowledged() -> None:
    outcome = normalize_processing_outcome({"ok": True})

    assert outcome.status == ProcessingStatus.RETRYABLE_FAILURE
    assert outcome.error_type == "InvalidProcessingOutcome"
    assert not outcome.ackable


def test_only_completed_and_intentional_outcomes_are_ackable() -> None:
    assert ProcessingOutcome.completed().ackable
    assert ProcessingOutcome.intentionally_suppressed().ackable
    assert not ProcessingOutcome.retryable_failure().ackable
    assert not ProcessingOutcome.permanent_failure().ackable
