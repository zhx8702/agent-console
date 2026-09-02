"""Load a compact speaker portrait when the bot is about to reply."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.channel.models import configuration_session_id
from app.common.types import channel_id_value
from app.orchestrator.flow import StepResult
from app.orchestrator.pipeline import PipelineContext
from plugins.speaker_portrait.pipeline import compact_portrait_for_prompt
from plugins.speaker_portrait.store import SpeakerPortraitStore


def _speaker_id(ctx: PipelineContext) -> str:
    metadata = dict(ctx.event.metadata or {})
    return str(
        metadata.get("sender_wxid")
        or metadata.get("sender_id")
        or ctx.event.user_id
        or ""
    ).strip()


def _external_session_id(ctx: PipelineContext) -> str:
    """Return the SDK-facing conversation id without persisting a cx1 fallback."""

    session_id = configuration_session_id(ctx.event, ctx.session).strip()
    return "" if session_id.startswith("cx1:") else session_id


@dataclass
class SpeakerPortraitEnrichStep:
    store: SpeakerPortraitStore
    kind: str = "plugin.speaker_portrait.enrich"
    owner: str = "speaker_portrait"
    name: str = "Load speaker portrait"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session"})
    outputs: set[str] = field(default_factory=lambda: {"signals.speaker_portrait"})
    timeout_seconds: float = 1.5
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.session is None:
            return StepResult(reason="no_session")
        ctx.session.variables.pop("speaker_portrait", None)
        speaker_id = _speaker_id(ctx)
        if not speaker_id:
            return StepResult(reason="no_speaker")
        try:
            record = await self.store.get_portrait(
                tenant_id=ctx.event.tenant_id,
                speaker_id=speaker_id,
                channel=channel_id_value(ctx.event.channel) or "wechat",
                source_key="wxbot",
            )
        except Exception:
            return StepResult(reason="portrait_lookup_failed")
        if not record or not isinstance(record.get("portrait"), dict):
            ctx.signals["speaker_portrait"] = {"matched": False}
            return StepResult(reason="no_portrait")
        compact = compact_portrait_for_prompt(record["portrait"])
        if not compact:
            ctx.signals["speaker_portrait"] = {"matched": False}
            return StepResult(reason="empty_portrait")
        ctx.session.variables["speaker_portrait"] = {
            "speaker_id": speaker_id,
            "display_name": str(record.get("display_name") or ""),
            "compact": compact,
            "portrait_id": record.get("id"),
            "revision_id": record.get("current_revision_id"),
        }
        ctx.signals["speaker_portrait"] = {
            "matched": True,
            "portrait_id": record.get("id"),
        }
        return StepResult(reason="loaded")


@dataclass
class SpeakerPortraitNoteStep:
    store: SpeakerPortraitStore
    kind: str = "plugin.speaker_portrait.note"
    owner: str = "speaker_portrait"
    name: str = "Note speaker message for hot update"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session"})
    outputs: set[str] = field(default_factory=lambda: {"signals.speaker_portrait_note"})
    timeout_seconds: float = 0.8
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if bool(ctx.event.metadata.get("is_self_sent")):
            return StepResult(reason="self_sent")
        if not str(ctx.event.message.content or "").strip():
            return StepResult(reason="no_text")
        speaker_id = _speaker_id(ctx)
        if not speaker_id:
            return StepResult(reason="no_speaker")
        metadata = dict(ctx.event.metadata or {})
        received = getattr(ctx.event, "received_at", None)
        timestamp = (
            received.isoformat() if hasattr(received, "isoformat") else str(received or metadata.get("timestamp") or "")
        )
        noted = await self.store.note_speaker_message(
            tenant_id=ctx.event.tenant_id,
            speaker_id=speaker_id,
            speaker_name=str(metadata.get("sender_name") or ""),
            session_id=_external_session_id(ctx),
            timestamp=timestamp[:64],
            channel=channel_id_value(ctx.event.channel) or "wechat",
        )
        ctx.signals["speaker_portrait_note"] = {"noted": bool(noted)}
        return StepResult(reason="noted" if noted else "no_portrait")
