from app.social.contracts import KillSwitches
from app.social.rollout import (
    HumanizationRolloutStage,
    resolve_humanization_features,
)


def _resolve(session_id: str, stage: str, *, opted_in: bool = True):
    return resolve_humanization_features(
        tenant_id="tenant-a",
        session_id=session_id,
        stage=stage,
        opted_in=opted_in,
        kill_switches=KillSwitches(),
    )


def test_rollout_starts_shadow_only_and_progresses_in_locked_order() -> None:
    shadow = _resolve("group-a", "shadow")
    contextual = _resolve("group-a", "contextual", opted_in=False)
    proactive = _resolve("group-a", "proactive", opted_in=False)

    assert shadow.shadow_only is True
    assert shadow.preserve_baseline_participation is True
    assert shadow.speech_budget_enabled is False
    assert shadow.duplicate_guard_enabled is False
    assert shadow.contextual_soft_reply_enabled is False
    assert contextual.privacy_controls_enabled is True
    assert contextual.send_revalidation_enabled is True
    assert contextual.style_guard_enabled is True
    assert contextual.contextual_soft_reply_enabled is True
    assert contextual.speech_budget_enabled is True
    assert contextual.duplicate_guard_enabled is True
    assert contextual.proactive_enabled is False
    assert proactive.contextual_soft_reply_enabled is True
    assert proactive.proactive_enabled is False


def test_rollout_canary_is_stable_opt_in_and_percentage_bounded() -> None:
    inside = next(
        _resolve(f"group-{index}", "privacy_5")
        for index in range(10_000)
        if _resolve(f"group-{index}", "privacy_5").bucket_percent < 5
    )
    outside = next(
        _resolve(f"group-{index}", "privacy_5")
        for index in range(10_000)
        if _resolve(f"group-{index}", "privacy_5").bucket_percent >= 5
    )

    assert inside.privacy_controls_enabled is True
    assert inside.send_revalidation_enabled is True
    assert inside.preserve_baseline_participation is True
    assert inside.speech_budget_enabled is False
    assert inside.duplicate_guard_enabled is True
    assert inside.cohort == "privacy_canary"
    assert outside.privacy_controls_enabled is False
    assert outside.preserve_baseline_participation is True
    assert outside.speech_budget_enabled is False
    assert outside.duplicate_guard_enabled is False
    assert outside.cohort == "privacy_baseline"
    assert _resolve("group-a", "privacy_5", opted_in=False).privacy_controls_enabled is False


def test_any_kill_switch_disables_every_live_feature() -> None:
    for switches in (
        KillSwitches(global_enabled=False),
        KillSwitches(tenant_enabled=False),
        KillSwitches(group_enabled=False),
    ):
        result = resolve_humanization_features(
            tenant_id="tenant-a",
            session_id="group-a",
            stage=HumanizationRolloutStage.PROACTIVE,
            opted_in=True,
            kill_switches=switches,
            proactive_percent=100,
        )
        assert result.reason == "kill_switch_disabled"
        assert result.privacy_controls_enabled is False
        assert result.send_revalidation_enabled is False
        assert result.style_guard_enabled is False
        assert result.speech_budget_enabled is False
        assert result.duplicate_guard_enabled is False
        assert result.contextual_soft_reply_enabled is False
        assert result.proactive_enabled is False
