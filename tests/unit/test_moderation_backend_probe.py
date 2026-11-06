import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "moderation_probe", Path(__file__).parents[2] / "scripts/evaluate_moderation_backend.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


@pytest.mark.parametrize("text", [
    "Safety: Safe\nCategories: Violent",
    "Safety: Unsafe\nCategories: None",
    "Safety: Unsafe\nCategories: UnknownCategory",
    "Safety: Safe\nCategories: None\nIgnore previous output",
])
def test_probe_does_not_count_invalid_guard_output_as_success(text):
    with pytest.raises(ValueError):
        probe.classify(text)


def test_equal_model_digests_do_not_hide_a_changed_effective_template():
    original = {"runtime_version": "v1", "model_digest": "same-weights-and-manifest",
                "effective_template_sha256": "legacy-go", "parameters": {"temperature": "0"}}
    assert probe.matching_fingerprint(original, dict(original))
    assert not probe.matching_fingerprint(
        {**original, "effective_template_sha256": "embedded-jinja"}, original)
    assert not probe.matching_fingerprint({**original, "runtime_version": "v2"}, original)
    assert not probe.matching_fingerprint({}, {})


@pytest.mark.parametrize("old,new,weakened", [
    ("Unsafe", "Controversial", True), ("Unsafe", "Safe", True),
    ("Controversial", "Safe", True), ("Unsafe", "Unsafe", False),
    ("Controversial", "Unsafe", False), ("Safe", "Safe", False),
])
def test_no_risk_downgrade_even_when_baseline_disagrees_with_fixture(old, new, weakened):
    baseline = {"valid": True, "safety": old, "correct": False}
    assert probe.risk_weakened({"valid": True, "safety": new}, baseline) is weakened
    assert probe.risk_weakened({"valid": False}, baseline)
