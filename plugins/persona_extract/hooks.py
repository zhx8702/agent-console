from __future__ import annotations

from dataclasses import dataclass, field

from app.channel.models import configuration_session_id
from app.common.types import channel_id_value
from app.orchestrator.flow import StepResult
from app.plugin.hooks import HookPoint, PipelineHook
from plugins.persona_extract.store import (
    PersonaExtractStore,
    normalize_persona_runtime_source_key,
)


def _clear_persona_session_variables(ctx) -> None:
    if ctx.session is None:
        return
    ctx.session.variables.pop("persona_skill", None)
    ctx.session.variables.pop("persona_profile", None)


class PersonaSkillHook(PipelineHook):
    name = "persona_extract.skill_injector"
    point = HookPoint.BEFORE_CAPABILITY
    priority = 40
    timeout_seconds = 1.5
    error_policy = "fail_open"

    def __init__(self, store: PersonaExtractStore) -> None:
        self._store = store

    async def run(self, ctx) -> None:
        if ctx.session is None:
            return

        # These values are valid only for the current turn. Clear them before
        # resolution so a failed query cannot reuse a previous turn's style.
        _clear_persona_session_variables(ctx)
        channel = channel_id_value(ctx.event.channel)
        source = normalize_persona_runtime_source_key(
            channel,
            str(ctx.event.metadata.get("source") or "*"),
        )
        profile = await self._store.resolve_profile(
            tenant_id=ctx.event.tenant_id,
            session_id=configuration_session_id(ctx.event, ctx.session),
            channel=channel,
            source_key=source,
        )

        if not profile:
            return

        artifact = profile.get("artifact") if isinstance(profile.get("artifact"), dict) else {}
        files = artifact.get("files") if isinstance(artifact.get("files"), dict) else {}
        target = artifact.get("target") if isinstance(artifact.get("target"), dict) else {}
        source_meta = artifact.get("source") if isinstance(artifact.get("source"), dict) else {}
        meta = artifact.get("meta") if isinstance(artifact.get("meta"), dict) else {}
        prompt_text = str(files.get("skill_prompt") or profile.get("prompt_text") or "").strip()

        ctx.session.variables["persona_skill"] = prompt_text
        ctx.session.variables["persona_profile"] = {
            "profile_id": profile["id"],
            "name": profile["profile_name"],
            "channel": profile["channel"],
            "source_key": profile["source_key"],
            "source_label": profile["source_label"],
            "job_id": profile.get("job_id"),
            "skill_slug": profile.get("skill_slug") or artifact.get("slug"),
            "target_user_id": profile.get("target_user_id") or target.get("user_id"),
            "target_name": profile.get("target_name") or target.get("name"),
            "artifact_version": artifact.get("version"),
            "generated_at": artifact.get("generated_at"),
            "impression": meta.get("impression"),
            "session_name": source_meta.get("session_name"),
        }


def _sync_persona_signal(ctx) -> dict[str, object]:
    if ctx.session is None:
        signal: dict[str, object] = {"matched": False, "reason": "no_session"}
    else:
        skill = str(ctx.session.variables.get("persona_skill") or "")
        profile = ctx.session.variables.get("persona_profile")
        profile_payload = dict(profile) if isinstance(profile, dict) else {}
        signal = {
            "matched": bool(skill or profile_payload),
            "skill_prompt": skill,
            "profile": profile_payload,
        }
    ctx.signals.setdefault("persona", {})["skill"] = signal
    return signal


@dataclass
class PersonaSkillEnrichStep:
    store: PersonaExtractStore
    kind: str = "plugin.persona_extract.skill_enrich"
    owner: str = "persona_extract"
    name: str = "Persona skill enrich"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre", "route"})
    outputs: set[str] = field(default_factory=lambda: {"signals.persona.skill"})
    timeout_seconds: float = 1.5
    error_policy: str = "fail_open"

    async def run(self, ctx) -> StepResult:
        await PersonaSkillHook(self.store).run(ctx)
        signal = _sync_persona_signal(ctx)
        if signal.get("reason") == "no_session":
            return StepResult(reason="no_session")
        return StepResult(reason="loaded" if signal.get("matched") else "not_found")
