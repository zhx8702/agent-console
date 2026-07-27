"""Deterministic group-level rollout stages for humanization changes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from app.social.contracts import KillSwitches


class HumanizationRolloutStage(StrEnum):
    SHADOW = "shadow"
    PRIVACY_5 = "privacy_5"
    STYLE_10 = "style_10"
    CONTEXTUAL = "contextual"
    PROACTIVE = "proactive"


@dataclass(frozen=True, slots=True)
class HumanizationFeatures:
    stage: HumanizationRolloutStage
    bucket_percent: float
    shadow_only: bool
    preserve_baseline_participation: bool
    cohort: str
    privacy_controls_enabled: bool
    send_revalidation_enabled: bool
    style_guard_enabled: bool
    speech_budget_enabled: bool
    duplicate_guard_enabled: bool
    contextual_soft_reply_enabled: bool
    proactive_enabled: bool
    reason: str


def resolve_humanization_features(
    *,
    tenant_id: str,
    session_id: str,
    stage: HumanizationRolloutStage | str,
    opted_in: bool,
    kill_switches: KillSwitches,
    proactive_percent: int = 5,
) -> HumanizationFeatures:
    active_stage = HumanizationRolloutStage(str(stage))
    bucket = _stable_bucket_percent(tenant_id, session_id)
    disabled = not kill_switches.effective_enabled
    if disabled:
        return HumanizationFeatures(
            stage=active_stage,
            bucket_percent=bucket,
            shadow_only=False,
            preserve_baseline_participation=False,
            cohort="disabled",
            privacy_controls_enabled=False,
            send_revalidation_enabled=False,
            style_guard_enabled=False,
            speech_budget_enabled=False,
            duplicate_guard_enabled=False,
            contextual_soft_reply_enabled=False,
            proactive_enabled=False,
            reason="kill_switch_disabled",
        )
    if active_stage is HumanizationRolloutStage.SHADOW:
        return HumanizationFeatures(
            stage=active_stage,
            bucket_percent=bucket,
            shadow_only=True,
            preserve_baseline_participation=True,
            cohort="shadow",
            privacy_controls_enabled=False,
            send_revalidation_enabled=False,
            style_guard_enabled=False,
            speech_budget_enabled=False,
            duplicate_guard_enabled=False,
            contextual_soft_reply_enabled=False,
            proactive_enabled=False,
            reason="shadow_only",
        )

    privacy_enabled = active_stage in {
        HumanizationRolloutStage.CONTEXTUAL,
        HumanizationRolloutStage.PROACTIVE,
    } or (
        opted_in
        and (
            (active_stage is HumanizationRolloutStage.PRIVACY_5 and bucket < 5)
            or (active_stage is HumanizationRolloutStage.STYLE_10 and bucket < 10)
        )
    )
    style_enabled = active_stage in {
        HumanizationRolloutStage.CONTEXTUAL,
        HumanizationRolloutStage.PROACTIVE,
    } or (
        active_stage is HumanizationRolloutStage.STYLE_10
        and opted_in
        and bucket < 10
    )
    contextual_enabled = active_stage in {
        HumanizationRolloutStage.CONTEXTUAL,
        HumanizationRolloutStage.PROACTIVE,
    }
    proactive_enabled = (
        active_stage is HumanizationRolloutStage.PROACTIVE
        and opted_in
        and bucket < max(0, min(100, int(proactive_percent)))
    )
    preserve_baseline = active_stage in {
        HumanizationRolloutStage.PRIVACY_5,
        HumanizationRolloutStage.STYLE_10,
    }
    if active_stage is HumanizationRolloutStage.PRIVACY_5:
        cohort = "privacy_canary" if privacy_enabled else "privacy_baseline"
    elif active_stage is HumanizationRolloutStage.STYLE_10:
        cohort = "style_canary" if style_enabled else "style_baseline"
    elif active_stage is HumanizationRolloutStage.CONTEXTUAL:
        cohort = "contextual"
    else:
        cohort = "proactive_canary" if proactive_enabled else "proactive_baseline"
    return HumanizationFeatures(
        stage=active_stage,
        bucket_percent=bucket,
        shadow_only=False,
        preserve_baseline_participation=preserve_baseline,
        cohort=cohort,
        privacy_controls_enabled=privacy_enabled,
        send_revalidation_enabled=privacy_enabled,
        style_guard_enabled=style_enabled,
        # Privacy/style canaries preserve the channel's baseline participation
        # and therefore cannot silently introduce the contextual speech budget.
        # The budget becomes authoritative only with contextual participation.
        speech_budget_enabled=contextual_enabled,
        duplicate_guard_enabled=privacy_enabled or style_enabled,
        contextual_soft_reply_enabled=contextual_enabled,
        proactive_enabled=proactive_enabled,
        reason="enabled" if any((privacy_enabled, style_enabled, contextual_enabled, proactive_enabled)) else "outside_canary",
    )


def _stable_bucket_percent(tenant_id: str, session_id: str) -> float:
    scope = f"{str(tenant_id).strip()}\0{str(session_id).strip()}".encode()
    digest = hashlib.sha256(scope).digest()
    return (int.from_bytes(digest[:8], "big") % 10_000) / 100
