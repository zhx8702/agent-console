from types import SimpleNamespace

import pytest

from app.orchestrator.effects import EffectCommitRecord
from app.orchestrator.flow import MessageEffect
from plugins.wxbot.effects import WxbotSdkTriggerConfigEffectHandler


@pytest.mark.asyncio
async def test_sdk_trigger_config_effect_forwards_stable_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_sdk_request(_store, _bridge, method, path, **kwargs):
        calls.append({"method": method, "path": path, **kwargs})
        return {"saved": True}

    monkeypatch.setattr("plugins.wxbot.effects._sdk_request", fake_sdk_request)
    handler = WxbotSdkTriggerConfigEffectHandler(SimpleNamespace())
    effect = MessageEffect(
        type="sdk_trigger_config",
        owner="wxbot",
        payload={"group_require_at_me": True},
        idempotency_key="stable-key",
    )
    record = EffectCommitRecord(
        type=effect.type,
        owner=effect.owner,
        idempotency_key=effect.idempotency_key,
        payload=effect.payload,
        tenant_id="tenant-a",
    )
    ctx = SimpleNamespace(event=SimpleNamespace(tenant_id="tenant-a"))

    await handler(effect, ctx, record)

    assert calls == [
        {
            "method": "POST",
            "path": "/debug/trigger-config",
            "json_body": {"group_require_at_me": True},
            "request_headers": {"Idempotency-Key": "stable-key"},
        }
    ]


@pytest.mark.asyncio
async def test_sdk_trigger_config_effect_rejects_unexpected_payload() -> None:
    handler = WxbotSdkTriggerConfigEffectHandler(SimpleNamespace())
    effect = MessageEffect(
        type="sdk_trigger_config",
        owner="wxbot",
        payload={"group_require_at_me": "true"},
        idempotency_key="stable-key",
    )
    record = EffectCommitRecord(
        type=effect.type,
        owner=effect.owner,
        idempotency_key=effect.idempotency_key,
        payload=effect.payload,
        tenant_id="tenant-a",
    )
    ctx = SimpleNamespace(event=SimpleNamespace(tenant_id="tenant-a"))

    with pytest.raises(ValueError, match="boolean"):
        await handler(effect, ctx, record)
