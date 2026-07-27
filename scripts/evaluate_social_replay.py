"""Evaluate privacy-safe JSONL replay observations against release gates."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import fields
from pathlib import Path

from app.social.evaluation import (
    HumanizationObservation,
    evaluate_humanization,
    validate_scenario_contract,
)


def _load(path: Path) -> list[HumanizationObservation]:
    allowed = {item.name for item in fields(HumanizationObservation)}
    observations: list[HumanizationObservation] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"line {line_number}: expected an object")
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(
                f"line {line_number}: unknown/private fields are forbidden: {unknown}"
            )
        try:
            observations.append(HumanizationObservation(**payload))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"line {line_number}: invalid observation: {exc}") from exc
    return observations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl", type=Path)
    parser.add_argument(
        "--validate-scenarios-only",
        action="store_true",
        help=(
            "validate coverage of the ten privacy-safe scenario labels without "
            "claiming production SLO evaluation"
        ),
    )
    args = parser.parse_args(argv)
    observations = _load(args.jsonl)
    if args.validate_scenarios_only:
        contract = validate_scenario_contract(observations)
        print(json.dumps(contract.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if contract.passed else 1

    report = evaluate_humanization(observations)
    payload = {
        "mode": "production_slo",
        "production_slo_evaluated": True,
        **report.to_dict(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
