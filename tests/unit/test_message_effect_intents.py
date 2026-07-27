from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.common.types import Channel, InboundEvent, Message, MessageType
from app.models.reliability import MessageEffectIntentRow
from app.orchestrator.effect_handlers import (
    EffectHandlerRegistry,
    EffectOwnerExecutionDenied,
)
from app.orchestrator.flow import (
    FLOW_STATUS_ACTIVE,
    CompiledFlow,
    CompiledStep,
    MessageEffect,
    StepResult,
)
from app.orchestrator.pipeline import PipelineContext
from app.orchestrator.runner import FLOW_RUN_COMPLETED, FlowRunner
from app.reliability import MessageEffectIntentRelay, MessageReliabilityStore


def _event() -> InboundEvent:
    return InboundEvent(
        tenant_id="tenant-a",
        channel=Channel.WEB,
        message_id="message-1",
        session_id="session-1",
        user_id="user-1",
        message=Message(type=MessageType.TEXT, content="hello"),
        trace_id="trace-1",
    )


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(MessageEffectIntentRow.__table__.create)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _commit_prepared_intent(
    store: MessageReliabilityStore,
    factory,
    *,
    idempotency_key: str = "plugin:effect:1",
    owner: str = "plugin",
    producer_owner: str = "",
    effect_type: str = "run",
) -> None:
    event = _event()
    ctx = PipelineContext(event=event, trace_id=event.trace_id)
    with store.stage():
        record = await store.stage_effect(
            MessageEffect(
                type=effect_type,
                owner=owner,
                producer_owner=producer_owner,
                payload={"value": 1},
                idempotency_key=idempotency_key,
            ),
            ctx,
            deferred=True,
        )
        assert record.status == "prepared"
        async with factory() as db:
            async with db.begin():
                await store.flush_stage(db)


class _DeferredEffectStep:
    async def run(self, ctx: PipelineContext) -> StepResult:
        _ = ctx
        return StepResult(
            effects=[
                MessageEffect(
                    type="run",
                    owner="wxbot",
                    producer_owner="untrusted-step-value",
                    payload={"value": 1},
                    idempotency_key="draw:run:flow-relay-1",
                )
            ]
        )


@pytest.mark.asyncio
async def test_relay_claims_each_serial_handler_with_a_fresh_lease() -> None:
    class _RecordingRelay(MessageEffectIntentRelay):
        def __init__(self) -> None:
            super().__init__(
                object(),  # type: ignore[arg-type]
                EffectHandlerRegistry(),
                worker_id="lease-contract",
                batch_size=4,
            )
            self.claim_limits: list[int | None] = []
            self.results = iter(((1, 1), (0, 1), (0, 0)))

        async def _drain_claimed_batch(
            self,
            *,
            claim_limit: int | None = None,
        ) -> tuple[int, int]:
            self.claim_limits.append(claim_limit)
            return next(self.results)

        async def backlog(self) -> int:
            return 0

    relay = _RecordingRelay()

    assert await relay.drain_once() == 1
    assert relay.claim_limits == [1, 1, 1]


@pytest.mark.asyncio
async def test_effect_intent_is_committed_with_source_context(factory) -> None:
    store = MessageReliabilityStore(factory)
    await _commit_prepared_intent(store, factory)

    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()

    assert row.status == "prepared"
    assert row.source_message_id == "message-1"
    assert row.tenant_id == "tenant-a"
    assert row.session_id == "session-1"
    assert row.producer_owner == "plugin"
    assert row.context["event"]["message"]["content"] == "hello"


@pytest.mark.asyncio
async def test_effect_intent_rolls_back_with_ambient_transaction(factory) -> None:
    store = MessageReliabilityStore(factory)
    event = _event()
    ctx = PipelineContext(event=event, trace_id=event.trace_id)

    with pytest.raises(RuntimeError, match="rollback boundary"):
        with store.stage():
            await store.stage_effect(
                MessageEffect(
                    type="run",
                    owner="plugin",
                    payload={"value": 1},
                    idempotency_key="plugin:effect:rollback",
                ),
                ctx,
                deferred=True,
            )
            async with factory() as db:
                async with db.begin():
                    await store.flush_stage(db)
                    raise RuntimeError("rollback boundary")

    async with factory() as db:
        assert (await db.execute(select(MessageEffectIntentRow))).scalars().all() == []


@pytest.mark.asyncio
async def test_admin_transaction_can_atomically_enqueue_sdk_effect(factory) -> None:
    store = MessageReliabilityStore(factory)
    async with factory() as db:
        async with db.begin():
            record = await store.enqueue_effect_intent(
                db,
                tenant_id="tenant-a",
                session_id="room@chatroom",
                source_message_id="admin-request-1",
                trace_id="trace-admin-1",
                owner="wxbot",
                effect_type="sdk_trigger_config",
                idempotency_key="wxbot:sdk-trigger:tenant-a:request-1",
                payload={"enabled": True, "keywords": ["报价"]},
                user_id="admin-1",
                context={"actor_id": "admin-1"},
            )

    assert record.status == "prepared"
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert row.owner == "wxbot"
    assert row.effect_type == "sdk_trigger_config"
    assert row.context["actor_id"] == "admin-1"
    assert row.context["event"]["metadata"]["effect_intent_source"] == ("admin_transaction")


@pytest.mark.asyncio
async def test_effect_intent_rejects_same_key_for_different_producer(factory) -> None:
    store = MessageReliabilityStore(factory)
    arguments = {
        "tenant_id": "tenant-a",
        "session_id": "session-1",
        "source_message_id": "message-1",
        "trace_id": "trace-1",
        "owner": "wxbot",
        "effect_type": "run",
        "idempotency_key": "shared-producer-key",
        "payload": {"value": 1},
    }
    async with factory() as db:
        async with db.begin():
            record = await store.enqueue_effect_intent(
                db,
                producer_owner="draw",
                **arguments,
            )

    assert record.producer_owner == "draw"
    async with factory() as db:
        with pytest.raises(RuntimeError, match="effect_intent_idempotency_conflict"):
            async with db.begin():
                await store.enqueue_effect_intent(
                    db,
                    producer_owner="memory",
                    **arguments,
                )


@pytest.mark.asyncio
async def test_effect_relay_completes_once_and_preserves_idempotency(factory) -> None:
    store = MessageReliabilityStore(factory)
    await _commit_prepared_intent(store, factory)
    calls: list[str] = []

    async def handler(effect, ctx, record) -> None:
        assert ctx.event.tenant_id == "tenant-a"
        assert record.attempt == 1
        calls.append(effect.idempotency_key)

    registry = EffectHandlerRegistry()
    registry.register("run", "plugin", handler)
    relay = MessageEffectIntentRelay(
        store,
        registry,
        worker_id="relay-a",
        handler_timeout_seconds=1,
    )

    assert await relay.drain_once() == 1
    assert await relay.drain_once() == 0
    assert calls == ["plugin:effect:1"]
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert row.status == "completed"
    assert row.attempts == 1
    assert row.claim_owner == ""
    assert row.claim_token == ""


@pytest.mark.asyncio
async def test_effect_relay_completes_denied_owner_without_calling_handler(factory) -> None:
    store = MessageReliabilityStore(factory)
    await _commit_prepared_intent(store, factory)
    calls: list[str] = []

    async def handler(effect, ctx, record) -> None:
        _ = ctx, record
        calls.append(effect.idempotency_key)

    async def deny(owner: str, ctx: PipelineContext) -> bool:
        assert owner == "plugin"
        assert ctx.event.tenant_id == "tenant-a"
        return False

    registry = EffectHandlerRegistry()
    registry.register("run", "plugin", handler)
    relay = MessageEffectIntentRelay(
        store,
        registry,
        worker_id="relay-a",
        owner_gate=deny,
    )

    assert await relay.drain_once() == 1
    assert calls == []
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert row.status == "completed"
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_legacy_member_erasure_bypasses_disabled_plugin_gate_and_uses_core_handler(
    factory,
) -> None:
    store = MessageReliabilityStore(factory)
    await _commit_prepared_intent(
        store,
        factory,
        owner="memory",
        producer_owner="memory",
        effect_type="forget_member",
        idempotency_key="member-memory-delete:legacy-1",
    )
    calls: list[str] = []
    gated: list[str] = []

    async def handler(effect, ctx, record) -> None:
        _ = ctx, record
        calls.append(effect.idempotency_key)

    async def deny_plugins(owner: str, ctx: PipelineContext) -> bool:
        _ = ctx
        gated.append(owner)
        return False

    registry = EffectHandlerRegistry()
    registry.register("forget_member", "core", handler)
    relay = MessageEffectIntentRelay(
        store,
        registry,
        worker_id="privacy-relay",
        owner_gate=deny_plugins,
    )

    assert await relay.drain_once() == 1
    assert calls == ["member-memory-delete:legacy-1"]
    # evaluate_owner_execution handles core locally, so the plugin gate is
    # never asked to authorize a mandatory privacy compensation.
    assert gated == []
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert row.status == "completed"


@pytest.mark.asyncio
async def test_effect_relay_gates_producer_before_handler_owner(factory) -> None:
    store = MessageReliabilityStore(factory)
    await _commit_prepared_intent(
        store,
        factory,
        owner="wxbot",
        producer_owner="draw",
    )
    calls: list[str] = []
    gated: list[str] = []

    async def handler(effect, ctx, record) -> None:
        _ = effect, ctx, record
        calls.append("called")

    async def gate(owner: str, ctx: PipelineContext) -> bool:
        _ = ctx
        gated.append(owner)
        return owner != "draw"

    registry = EffectHandlerRegistry()
    registry.register("run", "wxbot", handler)
    relay = MessageEffectIntentRelay(
        store,
        registry,
        worker_id="relay-a",
        owner_gate=gate,
    )

    assert await relay.drain_once() == 1
    assert gated == ["draw"]
    assert calls == []
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert row.status == "completed"
    assert row.producer_owner == "draw"


@pytest.mark.asyncio
async def test_flow_runner_db_relay_preserves_and_gates_stamped_producer(factory) -> None:
    store = MessageReliabilityStore(factory)
    ctx = PipelineContext(event=_event(), trace_id="trace-1")
    flow = CompiledFlow(
        name="producer-audit-flow",
        version=1,
        status=FLOW_STATUS_ACTIVE,
        steps=[
            CompiledStep(
                id="draw-effect",
                kind="plugin.draw.emit",
                owner="draw",
                name="draw effect",
                permissions=[],
                inputs=set(),
                outputs=set(),
            )
        ],
    )
    runner = FlowRunner(
        {"plugin.draw.emit": _DeferredEffectStep()},
        effect_intent_recorder=store,
        deferred_effect_handlers={("wxbot", "run")},
    )

    with store.stage():
        result = await runner.run(flow, ctx)
        async with factory() as db:
            async with db.begin():
                await store.flush_stage(db)

    assert result.status == FLOW_RUN_COMPLETED
    assert ctx.effects[0].producer_owner == "draw"
    assert result.effect_commits[0]["producer_owner"] == "draw"
    async with factory() as db:
        prepared = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert prepared.status == "prepared"
    assert prepared.producer_owner == "draw"

    gated: list[str] = []
    received: list[tuple[MessageEffect, str]] = []

    async def gate(owner: str, relay_ctx: PipelineContext) -> bool:
        assert relay_ctx.event.tenant_id == "tenant-a"
        gated.append(owner)
        return True

    async def handler(effect, relay_ctx, record) -> None:
        assert relay_ctx.event.tenant_id == "tenant-a"
        received.append((effect, record.producer_owner))

    registry = EffectHandlerRegistry()
    registry.register("run", "wxbot", handler)
    relay = MessageEffectIntentRelay(
        store,
        registry,
        worker_id="relay-a",
        owner_gate=gate,
    )

    assert await relay.drain_once() == 1
    assert gated == ["draw", "wxbot"]
    assert len(received) == 1
    assert received[0][0].producer_owner == "draw"
    assert received[0][1] == "draw"
    async with factory() as db:
        completed = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert completed.status == "completed"


@pytest.mark.asyncio
async def test_effect_relay_retries_transient_owner_gate_failure(factory) -> None:
    store = MessageReliabilityStore(factory)
    await _commit_prepared_intent(store, factory)

    async def unavailable(owner: str, ctx: PipelineContext) -> bool:
        _ = owner, ctx
        raise RuntimeError("state store unavailable")

    registry = EffectHandlerRegistry()
    registry.register("run", "plugin", lambda *_args: None)
    relay = MessageEffectIntentRelay(
        store,
        registry,
        worker_id="relay-a",
        owner_gate=unavailable,
    )

    assert await relay.drain_once() == 0
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert row.status == "failed"
    assert row.available_at is not None
    assert row.last_error == "RuntimeError:owner_gate_error"


@pytest.mark.asyncio
async def test_effect_relay_completes_nested_binding_owner_denial(factory) -> None:
    store = MessageReliabilityStore(factory)
    await _commit_prepared_intent(store, factory)

    async def denied_binding_handler(effect, ctx, record) -> None:
        _ = effect, ctx, record
        raise EffectOwnerExecutionDenied("channel-plugin")

    registry = EffectHandlerRegistry()
    registry.register("run", "plugin", denied_binding_handler)
    relay = MessageEffectIntentRelay(store, registry, worker_id="relay-a")

    assert await relay.drain_once() == 1
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert row.status == "completed"
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_expired_effect_claim_is_recovered_and_stale_fence_rejected(factory) -> None:
    store = MessageReliabilityStore(factory)
    await _commit_prepared_intent(store, factory)
    registry = EffectHandlerRegistry()
    first = MessageEffectIntentRelay(store, registry, worker_id="relay-a")
    second = MessageEffectIntentRelay(store, registry, worker_id="relay-b")

    first_claim = (await first._claim_batch())[0]
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
        row.claim_until = datetime(2000, 1, 1, tzinfo=UTC)
        await db.commit()

    second_claim = (await second._claim_batch())[0]
    assert second_claim.attempts == 2
    assert await first._mark_completed(first_claim) is False
    assert await second._mark_completed(second_claim) is True


@pytest.mark.asyncio
async def test_missing_effect_handler_becomes_terminal_failed_intent(factory) -> None:
    store = MessageReliabilityStore(factory)
    await _commit_prepared_intent(store, factory)
    relay = MessageEffectIntentRelay(
        store,
        EffectHandlerRegistry(),
        worker_id="relay-a",
        max_attempts=3,
    )

    assert await relay.drain_once() == 0
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert row.status == "failed"
    assert row.available_at is None
    assert row.last_error.startswith("LookupError:missing effect handler")


@pytest.mark.asyncio
async def test_expired_last_attempt_is_reconciled_to_terminal_failed(factory) -> None:
    store = MessageReliabilityStore(factory)
    await _commit_prepared_intent(store, factory)
    relay = MessageEffectIntentRelay(
        store,
        EffectHandlerRegistry(),
        worker_id="relay-a",
        max_attempts=1,
    )
    claim = (await relay._claim_batch())[0]
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
        row.claim_until = datetime(2000, 1, 1, tzinfo=UTC)
        await db.commit()

    assert claim.attempts == 1
    assert await relay._claim_batch() == []
    async with factory() as db:
        row = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert row.status == "failed"
    assert row.available_at is None
    assert row.last_error == "effect_claim_expired_after_max_attempts"
