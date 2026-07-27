from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.social.evaluation import HUMANIZATION_SCENARIOS, validate_scenario_contract
from scripts.evaluate_social_replay import _load, main

_SCENARIO_CONTRACT_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "social_replay_scenario_contract.jsonl"
)


def test_jsonl_fixture_covers_the_exact_scenario_contract(capsys) -> None:
    observations = _load(_SCENARIO_CONTRACT_FIXTURE)
    contract = validate_scenario_contract(observations)

    assert tuple(item.scenario for item in observations) == HUMANIZATION_SCENARIOS
    assert contract.passed is True
    assert main([str(_SCENARIO_CONTRACT_FIXTURE), "--validate-scenarios-only"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "scenario_contract_only"
    assert payload["scenario_contract_passed"] is True
    assert payload["production_slo_evaluated"] is False
    assert payload["production_slo_passed"] is None
    assert payload["missing_scenarios"] == []


def test_contract_fixture_cannot_masquerade_as_production_slo_evidence(capsys) -> None:
    assert main([str(_SCENARIO_CONTRACT_FIXTURE)]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "production_slo"
    assert payload["production_slo_evaluated"] is True
    assert payload["passed"] is False
    assert payload["gates"]["all_required_scenarios_covered"] is True
    assert payload["gates"]["critical_rate_samples_gte_1000"] is False
    assert payload["gates"]["score_samples_gte_100"] is False


def test_scenario_contract_mode_fails_when_one_required_case_is_missing(
    tmp_path: Path,
    capsys,
) -> None:
    lines = _SCENARIO_CONTRACT_FIXTURE.read_text(encoding="utf-8").splitlines()
    incomplete = tmp_path / "incomplete.jsonl"
    incomplete.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    assert main(["--validate-scenarios-only", str(incomplete)]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["scenario_contract_passed"] is False
    assert payload["missing_scenarios"] == ["topic_changed_before_send"]
    assert payload["production_slo_evaluated"] is False


def test_jsonl_loader_rejects_unknown_scenario_without_echoing_value(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid-scenario.jsonl"
    invalid.write_text(
        json.dumps(
            {
                "scenario": "not-a-contract-label",
                "directly_addressed": False,
                "expected_reply": False,
                "sent": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        _load(invalid)

    assert "line 1: invalid observation" in str(exc_info.value)
    assert "supported privacy-safe labels" in str(exc_info.value)
    assert "not-a-contract-label" not in str(exc_info.value)


def test_jsonl_loader_rejects_chat_text_and_other_private_fields(tmp_path: Path) -> None:
    invalid = tmp_path / "private-field.jsonl"
    invalid.write_text(
        json.dumps(
            {
                "scenario": "direct_mention",
                "directly_addressed": True,
                "expected_reply": True,
                "sent": True,
                "chat_text": "must never enter the replay contract",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        _load(invalid)

    assert "unknown/private fields are forbidden: ['chat_text']" in str(exc_info.value)
    assert "must never enter" not in str(exc_info.value)
