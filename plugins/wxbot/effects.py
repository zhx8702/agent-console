"""Durable wxbot effect handlers."""

from __future__ import annotations

from dataclasses import dataclass

from app.orchestrator.effects import EffectCommitRecord
from app.orchestrator.flow import MessageEffect
from app.orchestrator.pipeline import PipelineContext
from plugins.wxbot.router import _sdk_request
from plugins.wxbot.store import WxbotStore


@dataclass(slots=True)
class WxbotSdkTriggerConfigEffectHandler:
    store: WxbotStore

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        if effect.owner != "wxbot" or effect.type != "sdk_trigger_config":
            raise ValueError("unexpected wxbot effect")
        if set(effect.payload) != {"group_require_at_me"}:
            raise ValueError("invalid sdk trigger config effect payload")
        group_require_at_me = effect.payload.get("group_require_at_me")
        if not isinstance(group_require_at_me, bool):
            raise ValueError("group_require_at_me must be boolean")
        if ctx.event.tenant_id != record.tenant_id:
            raise ValueError("effect tenant mismatch")
        await _sdk_request(
            self.store,
            None,
            "POST",
            "/debug/trigger-config",
            json_body={"group_require_at_me": group_require_at_me},
            request_headers={"Idempotency-Key": record.idempotency_key},
        )
