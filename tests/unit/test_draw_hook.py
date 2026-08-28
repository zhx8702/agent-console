from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from app.admin.mutation_ledger import (
    MutationIdempotencyConflictError,
    MutationOutcome,
    fingerprint,
)
from app.billing import BillingCapture, BillingCoordinator, BillingQuote, BillingReservation
from app.billing.models import BillingResource, BillingSubject
from app.channel import (
    ChannelMedia,
    ChannelRegistry,
    ChannelSendOptions,
    ChannelSendResult,
    ChannelTarget,
)
from app.commands import CommandRegistryService
from app.common.types import (
    CapabilityResult,
    Channel,
    InboundEvent,
    Message,
    PreprocessedMessage,
    Role,
    RouteType,
    Session,
    Turn,
)
from app.orchestrator.effect_handlers import EffectDispatcher, EffectHandlerRegistry
from app.orchestrator.effects import (
    EFFECT_STATUS_DUPLICATE,
    EFFECT_STATUS_RECORDED,
    InMemoryEffectCommitter,
)
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort
from plugins.commands.hooks import CommandCenterHook
from plugins.draw.agent import DrawAgentToolService
from plugins.draw.avatar import DrawAvatarReference
from plugins.draw.hooks import (
    DRAW_API_ERROR_TEXT,
    DRAW_CONFIG_ERROR_TEXT,
    DRAW_EMPTY_RESPONSE_ERROR_TEXT,
    DRAW_HELP_TEXT,
    DRAW_IMAGE_ID_ERROR_TEXT,
    DRAW_SUCCESS_TEXT,
    DRAW_TIMEOUT_ERROR_TEXT,
    REDRAW_HELP_TEXT,
    DrawPostprocessResultStep,
    DrawPublishMediaEffectHandler,
    DrawReplyHook,
    build_draw_command_definitions,
    drain_queued_draw_tasks,
    recover_stale_draw_tasks,
)
from plugins.draw.router import build_draw_router
from plugins.draw.store import (
    DRAW_TASK_INTERRUPTED_ERROR_MESSAGE,
    DrawApiError,
    DrawConfigError,
    DrawResult,
    DrawTaskRecord,
)


class _FakeDrawStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.settings = type(
            "Settings",
            (),
            {
                "admin_bearer_token": "admin_token",
                "wxbot_media_base_url": "",
                "draw_task_max_retries": 2,
                "draw_task_retry_backoff_seconds": 0.0,
                "draw_task_stale_seconds": 60.0,
                "draw_task_auto_retry_enabled": False,
            },
        )()
        self.records: dict[str, object] = {"img_demo": object()}
        self.draw_tasks: dict[str, DrawTaskRecord] = {}
        self.task_creates: list[object] = []
        self.task_statuses: list[tuple[str, str]] = []
        self.task_results: list[tuple[str, str]] = []
        self.task_failures: list[tuple[str, str, str]] = []
        self.callback_claims: list[str] = []
        self.callback_marks: list[tuple[str, str]] = []
        self.callback_errors: list[tuple[str, str]] = []
        self.callback_releases: list[tuple[str, str]] = []
        self.callback_already_sent = False
        self.callback_sent_task_ids: set[str] = set()
        self.reserved_retries: list[tuple[str, int]] = []
        self.deferred_claims: list[tuple[str, str]] = []
        self._task_counter = 0

    async def run_admin_mutation(self, *, identity, audit, mutate):
        _ = (identity, audit)
        change = await mutate()
        return MutationOutcome(
            response=change.response,
            status_code=change.status_code,
            replayed=False,
            mutation_id="mutation-test",
        )

    async def generate_image(
        self,
        prompt: str,
        *,
        trace_id: str,
        quality: str = "low",
    ) -> DrawResult:
        call = {"prompt": prompt, "trace_id": trace_id}
        if quality != "low":
            call["quality"] = quality
        self.calls.append(call)
        return DrawResult(
            image_id="img_demo",
            prompt=prompt,
            local_path="/mnt/e/cs-system-draw/demo.png",
            file_name="demo.png",
            media_type="image/png",
            public_path="/plugins/draw/files/demo.png",
            source_url="http://127.0.0.1:18080/p/img/task/0",
        )

    async def edit_image(
        self,
        image_id: str,
        prompt: str,
        *,
        trace_id: str,
        quality: str = "low",
    ) -> DrawResult:
        call = {"image_id": image_id, "prompt": prompt, "trace_id": trace_id}
        if quality != "low":
            call["quality"] = quality
        self.calls.append(call)
        return DrawResult(
            image_id="img_redraw",
            prompt=prompt,
            local_path="/mnt/e/cs-system-draw/redraw.png",
            file_name="redraw.png",
            media_type="image/png",
            public_path="/plugins/draw/files/redraw.png",
            source_url="http://127.0.0.1:18080/p/img/task/1",
            source_image_id=image_id,
        )

    async def edit_reference_image(
        self,
        *,
        image_url: str = "",
        image_path: str = "",
        prompt: str,
        trace_id: str,
        quality: str = "low",
        source_label: str = "reference",
    ) -> DrawResult:
        call = {
            "image_url": image_url,
            "image_path": image_path,
            "prompt": prompt,
            "trace_id": trace_id,
            "source_label": source_label,
        }
        if quality != "low":
            call["quality"] = quality
        self.calls.append(call)
        return DrawResult(
            image_id="img_redraw_ref",
            prompt=prompt,
            local_path="/mnt/e/cs-system-draw/redraw-ref.png",
            file_name="redraw-ref.png",
            media_type="image/png",
            public_path="/plugins/draw/files/redraw-ref.png",
            source_url="http://127.0.0.1:18080/p/img/task/2",
            source_image_id=source_label,
        )

    def resolve_image_id(self, image_id: str) -> object | None:
        return self.records.get(str(image_id or "").strip())

    async def create_draw_task(self, task: object) -> DrawTaskRecord:
        task_id = str(getattr(task, "task_id", "") or "").strip()
        if task_id and task_id in self.draw_tasks:
            return self.draw_tasks[task_id]
        if not task_id:
            self._task_counter += 1
            task_id = f"task-{self._task_counter}"
        self.task_creates.append(task)
        target = dict(getattr(task, "callback_target", {}) or {})
        source_message = dict(getattr(task, "source_message", {}) or {})
        source_image = dict(getattr(task, "source_image", {}) or {})
        record = DrawTaskRecord(
            task_id=task_id,
            request_id=str(getattr(task, "request_id", "") or ""),
            trace_id=str(getattr(task, "trace_id", "") or ""),
            command_type=str(getattr(task, "command_type", "") or ""),
            status="queued",
            tenant_id=str(getattr(task, "tenant_id", "") or ""),
            channel=str(getattr(task, "channel", "") or ""),
            session_id=str(getattr(task, "session_id", "") or ""),
            user_id=str(getattr(task, "user_id", "") or ""),
            requester=str(getattr(task, "requester", "") or ""),
            requester_display_name=str(getattr(task, "requester_display_name", "") or ""),
            original_message_id=str(getattr(task, "original_message_id", "") or ""),
            callback_target=target,
            source_message=source_message,
            prompt=str(getattr(task, "prompt", "") or ""),
            quality=str(getattr(task, "quality", "") or "low"),
            source_image=source_image,
            retry_count=int(getattr(task, "retry_count", 0) or 0),
            next_run_at=str(getattr(task, "next_run_at", "") or ""),
        )
        self.draw_tasks[task_id] = record
        return record

    async def get_draw_task(self, task_id: str) -> DrawTaskRecord | None:
        return self.draw_tasks.get(task_id)

    async def reserve_draw_task_retry(
        self,
        task_id: str,
        *,
        max_retries: int,
    ) -> DrawTaskRecord | None:
        record = self.draw_tasks.get(task_id)
        if record is None or record.status not in {"failed", "interrupted"}:
            return None
        if record.retry_count >= max_retries:
            return None
        updated = DrawTaskRecord(**{**record.__dict__, "retry_count": record.retry_count + 1})
        self.draw_tasks[task_id] = updated
        self.reserved_retries.append((task_id, updated.retry_count))
        return updated

    async def create_retry_draw_task(
        self,
        parent: DrawTaskRecord,
        *,
        retry_count: int,
        next_run_at: str = "",
    ) -> DrawTaskRecord:
        task = type(
            "Task",
            (),
            {
                "request_id": parent.request_id,
                "trace_id": f"{parent.trace_id}:retry{retry_count}" if parent.trace_id else "",
                "command_type": parent.command_type,
                "tenant_id": parent.tenant_id,
                "channel": parent.channel,
                "session_id": parent.session_id,
                "user_id": parent.user_id,
                "requester": parent.requester,
                "requester_display_name": parent.requester_display_name,
                "original_message_id": parent.original_message_id,
                "callback_target": dict(parent.callback_target or {}),
                "source_message": {
                    **dict(parent.source_message or {}),
                    "draw_retry_parent_task_id": parent.task_id,
                },
                "prompt": parent.prompt,
                "quality": parent.quality,
                "source_image": dict(parent.source_image or {}),
                "retry_count": retry_count,
                "next_run_at": next_run_at,
            },
        )()
        return await self.create_draw_task(task)

    async def claim_due_draw_tasks(
        self,
        *,
        limit: int = 5,
        lock_ttl_seconds: float = 900.0,
        worker_id: str = "",
    ) -> list[DrawTaskRecord]:
        _ = (lock_ttl_seconds, worker_id)
        claimed: list[DrawTaskRecord] = []
        for task in list(self.draw_tasks.values()):
            if task.status != "queued":
                continue
            if str(task.next_run_at or "") > datetime.now(UTC).isoformat():
                continue
            updated = DrawTaskRecord(
                **{
                    **task.__dict__,
                    "status": "running",
                    "locked_by": worker_id,
                }
            )
            self.draw_tasks[task.task_id] = updated
            claimed.append(updated)
            if len(claimed) >= limit:
                break
        return claimed

    async def claim_draw_task_for_execution(
        self,
        task_id: str,
        *,
        worker_id: str = "",
        lock_ttl_seconds: float = 900.0,
    ) -> DrawTaskRecord | None:
        _ = (worker_id, lock_ttl_seconds)
        record = self.draw_tasks.get(task_id)
        if record is None:
            return None
        if record.status == "queued":
            self.task_statuses.append((task_id, "running"))
            record = DrawTaskRecord(
                **{
                    **record.__dict__,
                    "status": "running",
                    "locked_by": worker_id,
                    "started_at": datetime.now(UTC).isoformat(),
                }
            )
            self.draw_tasks[task_id] = record
        return record

    async def defer_draw_task_claim(
        self,
        task_id: str,
        *,
        worker_id: str,
        defer_seconds: float = 30.0,
    ) -> bool:
        _ = defer_seconds
        record = self.draw_tasks.get(task_id)
        if record is None or record.status != "running" or record.locked_by != worker_id:
            return False
        self.deferred_claims.append((task_id, worker_id))
        self.draw_tasks[task_id] = DrawTaskRecord(
            **{
                **record.__dict__,
                "status": "queued",
                "locked_by": "",
            }
        )
        return True

    async def mark_draw_task_running(self, task_id: str) -> None:
        self.task_statuses.append((task_id, "running"))
        record = self.draw_tasks.get(task_id)
        if record is not None:
            self.draw_tasks[task_id] = DrawTaskRecord(**{**record.__dict__, "status": "running"})

    async def complete_draw_task(
        self,
        task_id: str,
        result: DrawResult,
    ) -> None:
        self.task_statuses.append((task_id, "completed"))
        self.task_results.append((task_id, result.image_id))
        record = self.draw_tasks.get(task_id)
        if record is not None:
            self.draw_tasks[task_id] = DrawTaskRecord(
                **{
                    **record.__dict__,
                    "status": "completed",
                    "result_image_id": result.image_id,
                    "result_local_path": result.local_path,
                    "result_public_path": result.public_path,
                    "result_source_url": result.source_url,
                }
            )

    async def fail_draw_task(
        self,
        task_id: str,
        *,
        status: str = "failed",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        _ = error_message
        self.task_statuses.append((task_id, status))
        self.task_failures.append((task_id, status, error_code))
        record = self.draw_tasks.get(task_id)
        if record is not None:
            self.draw_tasks[task_id] = DrawTaskRecord(
                **{
                    **record.__dict__,
                    "status": status,
                    "error_code": error_code,
                    "error_message": error_message,
                }
            )

    async def claim_draw_task_callback(self, task_id: str) -> bool:
        self.callback_claims.append(task_id)
        return not self.callback_already_sent and task_id not in self.callback_sent_task_ids

    async def mark_draw_task_callback_sent(
        self,
        task_id: str,
        *,
        callback_error: str = "",
    ) -> None:
        self.callback_marks.append((task_id, callback_error))
        self.callback_sent_task_ids.add(task_id)

    async def release_draw_task_callback_claim(
        self,
        task_id: str,
        *,
        reason: str = "scope_execution_denied",
    ) -> bool:
        self.callback_releases.append((task_id, reason))
        return True

    async def mark_draw_task_callback_error(
        self,
        task_id: str,
        *,
        callback_error: str,
        force: bool = False,
    ) -> None:
        _ = force
        self.callback_errors.append((task_id, callback_error))


class _ReplayDrawStore(_FakeDrawStore):
    def __init__(self) -> None:
        super().__init__()
        self._admin_mutations: dict[tuple[str, str], tuple[str, MutationOutcome]] = {}

    async def run_admin_mutation(self, *, identity, audit, mutate):
        _ = audit
        key = (identity.operation, identity.idempotency_key)
        request_hash = fingerprint(
            {
                "resource_key": identity.resource_key,
                "request_payload": identity.request_payload,
            }
        )
        existing = self._admin_mutations.get(key)
        if existing is not None:
            if existing[0] != request_hash:
                raise MutationIdempotencyConflictError("key reused")
            previous = existing[1]
            return MutationOutcome(
                response=previous.response,
                status_code=previous.status_code,
                replayed=True,
                mutation_id=previous.mutation_id,
            )
        change = await mutate()
        outcome = MutationOutcome(
            response=change.response,
            status_code=change.status_code,
            replayed=False,
            mutation_id="mutation-replay-test",
        )
        self._admin_mutations[key] = (request_hash, outcome)
        return outcome


class _FailingDrawStore(_FakeDrawStore):
    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error or DrawApiError("upstream returned 500 with verbose provider details")

    async def generate_image(
        self,
        prompt: str,
        *,
        trace_id: str,
        quality: str = "low",
    ) -> DrawResult:
        call = {"prompt": prompt, "trace_id": trace_id}
        if quality != "low":
            call["quality"] = quality
        self.calls.append(call)
        raise self.error


class _BlockingDrawStore(_FakeDrawStore):
    def __init__(self) -> None:
        super().__init__()
        self.generation_started = asyncio.Event()

    async def generate_image(
        self,
        prompt: str,
        *,
        trace_id: str,
        quality: str = "low",
    ) -> DrawResult:
        self.calls.append(
            {"prompt": prompt, "trace_id": trace_id, "quality": quality}
        )
        self.generation_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _ScopeToggleDrawStore(_FakeDrawStore):
    def __init__(self) -> None:
        super().__init__()
        self.generation_started = asyncio.Event()
        self.finish_generation = asyncio.Event()

    async def generate_image(
        self,
        prompt: str,
        *,
        trace_id: str,
        quality: str = "low",
    ) -> DrawResult:
        self.generation_started.set()
        await self.finish_generation.wait()
        return await super().generate_image(
            prompt,
            trace_id=trace_id,
            quality=quality,
        )


class _BlockingCompletionDrawStore(_FakeDrawStore):
    def __init__(self) -> None:
        super().__init__()
        self.completion_committed = asyncio.Event()
        self.finish_completion = asyncio.Event()

    async def complete_draw_task(
        self,
        task_id: str,
        result: DrawResult,
    ) -> DrawTaskRecord | None:
        await super().complete_draw_task(task_id, result)
        self.completion_committed.set()
        await self.finish_completion.wait()
        return self.draw_tasks.get(task_id)


class _StorageUnavailableDrawStore(_FakeDrawStore):
    def __init__(self) -> None:
        super().__init__()
        self.storage_checks = 0

    def _ensure_storage_dir(self) -> None:
        self.storage_checks += 1
        raise DrawConfigError("DRAW_STORAGE_DIR 不可写")


class _FakeCommandStore:
    def __init__(self) -> None:
        self.config = {
            "admin_user_ids": [],
            "user_commands": ["/draw", "/画图", "/redraw", "/重绘"],
            "admin_commands": [],
        }

    async def get_config(self, tenant_id: str, *, catalog: list[dict[str, object]]) -> dict:
        return dict(self.config, catalog=catalog, tenant_id=tenant_id)


class _FakeBillingProvider:
    name = "credits"

    def __init__(self, amount: int = 10) -> None:
        self.amount = amount
        self.reservations: list[BillingReservation] = []
        self.captures: list[BillingReservation] = []
        self.releases: list[BillingReservation] = []

    async def quote(self, subject: BillingSubject, resource: BillingResource) -> BillingQuote:
        return BillingQuote(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=self.amount,
            currency="credits",
        )

    async def reserve(self, subject: BillingSubject, resource: BillingResource) -> BillingReservation:
        reservation = BillingReservation(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=self.amount,
            currency="credits",
            reservation_id=f"reservation-{len(self.reservations) + 1}",
        )
        self.reservations.append(reservation)
        return reservation

    async def capture(
        self,
        reservation: BillingReservation,
        *,
        amount: int | None = None,
    ) -> BillingCapture:
        self.captures.append(reservation)
        return BillingCapture(
            provider=self.name,
            subject=reservation.subject,
            resource=reservation.resource,
            amount=amount or reservation.amount,
            currency=reservation.currency,
        )

    async def release(self, reservation: BillingReservation) -> None:
        self.releases.append(reservation)


class _BlockingReserveBillingProvider(_FakeBillingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.reserve_committed = asyncio.Event()
        self.finish_reserve = asyncio.Event()

    async def reserve(
        self,
        subject: BillingSubject,
        resource: BillingResource,
    ) -> BillingReservation:
        reservation = await super().reserve(subject, resource)
        self.reserve_committed.set()
        await self.finish_reserve.wait()
        return reservation


class _BlockingCaptureBillingProvider(_FakeBillingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.capture_committed = asyncio.Event()
        self.finish_capture = asyncio.Event()

    async def capture(
        self,
        reservation: BillingReservation,
        *,
        amount: int | None = None,
    ) -> BillingCapture:
        capture = await super().capture(reservation, amount=amount)
        self.capture_committed.set()
        await self.finish_capture.wait()
        return capture


def _fake_billing(provider: _FakeBillingProvider | None = None) -> tuple[BillingCoordinator, _FakeBillingProvider]:
    billing = BillingCoordinator()
    provider = provider or _FakeBillingProvider()
    billing.register_provider(provider)
    return billing, provider


def _ctx(
    text: str,
    *,
    session_id: str = "room@chatroom",
    session_kind: str = "group",
    channel: Channel = Channel.WECHAT,
    user_id: str = "wxid_user_1",
    metadata: dict[str, object] | None = None,
) -> PipelineContext:
    event_metadata = {"session_kind": session_kind}
    if metadata:
        event_metadata.update(metadata)
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=channel,
        user_id=user_id,
        session_id=session_id,
        message=Message(content=text),
        trace_id="trace-1",
        metadata=event_metadata,
    )
    pre = PreprocessedMessage(original_text=text, cleaned_text=text)
    return PipelineContext(event=event, trace_id="trace-1", pre=pre)


class _FakeChannelOutbound:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_send_text = False
        self.fail_send_image = False

    async def get_session_policy(self, target: ChannelTarget) -> dict[str, object]:
        return {
            "tenant_id": target.tenant_id,
            "session_id": target.session_id,
            "effective_mention_sender": True,
        }

    async def capture_group_delivery_contract(
        self,
        target: ChannelTarget,
        *,
        source_message_id: str,
        response_kind: str = "tool_result",
    ) -> dict[str, object]:
        assert target.channel == "wechat"
        return {
            "participation_status": "must_reply",
            "source_message_id": source_message_id,
            "participation_policy_version": 23,
            "send_revalidation_enabled": True,
            "response_kind": response_kind,
            "speech_class": "obligation",
        }

    async def send_text(
        self,
        target: ChannelTarget,
        text: str,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        if self.fail_send_text:
            raise RuntimeError("send text failed")
        self.calls.append(
            {
                "msg_type": "text",
                "reply_text": text,
                "target": target,
                "options": options,
            }
        )
        return ChannelSendResult(provider="fake")

    async def send_image(
        self,
        target: ChannelTarget,
        media: ChannelMedia,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        if self.fail_send_image:
            raise RuntimeError("send image failed")
        self.calls.append(
            {
                "msg_type": "image",
                "image_path": media.image_path,
                "image_url": media.image_url,
                "target": target,
                "options": options,
            }
        )
        return ChannelSendResult(provider="fake")


class _BlockingImageAckOutbound(_FakeChannelOutbound):
    def __init__(self) -> None:
        super().__init__()
        self.image_sent = asyncio.Event()
        self.finish_ack = asyncio.Event()

    async def send_image(
        self,
        target: ChannelTarget,
        media: ChannelMedia,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        result = await super().send_image(target, media, options)
        self.image_sent.set()
        await self.finish_ack.wait()
        return result


async def _allow_channel_owner(_owner: str, _target: ChannelTarget) -> bool:
    return True


def _fake_channel_registry(
    outbound: _FakeChannelOutbound,
    *,
    channel: Channel = Channel.WECHAT,
) -> ChannelRegistry:
    registry = ChannelRegistry(owner_gate=_allow_channel_owner)
    registry.register_outbound(channel.value, outbound, owner="test")
    return registry


def _agent_session() -> Session:
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_user_1",
        channel=Channel.WECHAT,
        metadata={"session_name": "测试群"},
    )
    session.turns = [
        Turn(
            session_id="room@chatroom",
            role=Role.USER,
            content="@zzz 帮我画一只橘猫",
            trace_id="trace-agent-draw",
            metadata={
                "session_name": "测试群",
                "sender_name": "群友A",
                "sender_wxid": "wxid_user_a",
                "msg_svr_id": "123456",
            },
        )
    ]
    return session


def _private_agent_session() -> Session:
    session = Session(
        session_id="wxid_private",
        tenant_id="demo",
        user_id="wxid_private",
        channel=Channel.WECHAT,
        metadata={"session_kind": "private", "session_name": "Z"},
    )
    session.turns = [
        Turn(
            session_id="wxid_private",
            role=Role.USER,
            content="画个海边日落的图片",
            trace_id="trace-agent-draw-private",
            metadata={
                "session_kind": "private",
                "session_name": "Z",
                "sender_name": "Z",
                "sender_wxid": "wxid_private",
                "msg_svr_id": "private-msg-1",
            },
        )
    ]
    return session


def _discord_agent_session() -> Session:
    session = Session(
        session_id="discord-channel-1",
        tenant_id="demo",
        user_id="discord-user-1",
        channel=Channel.DISCORD,
        metadata={"session_kind": "group", "session_name": "设计频道"},
    )
    session.turns = [
        Turn(
            session_id="discord-channel-1",
            role=Role.USER,
            content="/draw 一只戴墨镜的橘猫",
            trace_id="trace-agent-draw-discord",
            metadata={
                "session_kind": "group",
                "session_name": "设计频道",
                "sender_name": "Alice",
                "sender_id": "discord-user-1",
                "reply_to_message_id": "discord-msg-1",
            },
        )
    ]
    return session


@pytest.mark.asyncio
async def test_draw_command_center_hook_triggers_on_group_draw_command() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只戴墨镜的橘猫")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [{"prompt": "一只戴墨镜的橘猫", "trace_id": "trace-1"}]
    assert ctx.extras["draw_result"]["local_path"] == "/mnt/e/cs-system-draw/demo.png"
    assert ctx.extras["draw_result"]["image_id"] == "img_demo"
    assert ctx.extras["draw_result"]["quality"] == "low"
    assert excinfo.value.reply_text == "画好了, 图片ID: img_demo"


@pytest.mark.asyncio
async def test_draw_command_center_hook_parses_quality_before_prompt_and_billing() -> None:
    store = _FakeDrawStore()
    billing, provider = _fake_billing()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store, billing=billing))
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)
    ctx = _ctx("/draw quality=medium 一只戴墨镜的橘猫")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [
        {"prompt": "一只戴墨镜的橘猫", "trace_id": "trace-1", "quality": "medium"}
    ]
    assert ctx.extras["draw_result"]["quality"] == "medium"
    assert len(provider.reservations) == 1
    assert provider.reservations[0].resource.metadata["quality"] == "medium"


@pytest.mark.asyncio
async def test_draw_command_center_hook_rejects_invalid_quality_before_billing() -> None:
    store = _FakeDrawStore()
    billing, provider = _fake_billing()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store, billing=billing))
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)
    ctx = _ctx("/draw quality=ultra 一只戴墨镜的橘猫")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert "quality" in excinfo.value.reply_text
    assert "low" in excinfo.value.reply_text
    assert store.calls == []
    assert provider.reservations == []


@pytest.mark.asyncio
async def test_redraw_command_center_hook_parses_quality_flag() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/redraw --quality high img_demo 改成水彩")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [
        {
            "image_id": "img_demo",
            "prompt": "改成水彩",
            "trace_id": "trace-1",
            "quality": "high",
        }
    ]


@pytest.mark.asyncio
async def test_draw_command_center_hook_includes_quoted_text_in_prompt() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/draw 画成国风插画",
        metadata={"quote_text": "一个穿红色斗篷、站在雪地里的女孩"},
    )

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [
        {
            "prompt": "画成国风插画\n\n引用文本：一个穿红色斗篷、站在雪地里的女孩",
            "trace_id": "trace-1",
        }
    ]


@pytest.mark.asyncio
async def test_draw_command_center_hook_uses_quoted_text_when_prompt_missing() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/draw",
        metadata={
            "quote": {
                "msg_svr_id": "quoted-text",
                "message": {"text": "一座赛博朋克风格的雨夜城市"},
            }
        },
    )

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [
        {"prompt": "一座赛博朋克风格的雨夜城市", "trace_id": "trace-1"}
    ]


@pytest.mark.asyncio
async def test_draw_command_center_hook_returns_help_when_prompt_missing() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/画图")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == DRAW_HELP_TEXT
    assert store.calls == []


@pytest.mark.asyncio
async def test_redraw_command_center_hook_uses_image_id_and_prompt() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/redraw img_demo 把这张图变成梵高星空风格的油画")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [
        {
            "image_id": "img_demo",
            "prompt": "把这张图变成梵高星空风格的油画",
            "trace_id": "trace-1",
        }
    ]
    assert ctx.extras["draw_result"]["image_id"] == "img_redraw"
    assert ctx.extras["draw_result"]["source_image_id"] == "img_demo"
    assert excinfo.value.reply_text == "画好了, 图片ID: img_redraw"


@pytest.mark.asyncio
async def test_redraw_command_center_hook_uses_quoted_image_when_no_image_id() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/重绘 把这张图变成梵高星空风格的油画",
        metadata={
            "quote_image_path": r"C:\Users\Example\AppData\Local\Programs\wx-bot-client\data\images\hash-601\601.png",
            "quote_image_url": "http://127.0.0.1:5080/images/hash-601/601.png",
            "quote": {"msg_svr_id": "quoted-image"},
        },
    )

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [
        {
            "image_url": "http://127.0.0.1:5080/images/hash-601/601.png",
            "image_path": r"C:\Users\Example\AppData\Local\Programs\wx-bot-client\data\images\hash-601\601.png",
            "prompt": "把这张图变成梵高星空风格的油画",
            "trace_id": "trace-1",
            "source_label": "quote:quoted-image",
        }
    ]
    assert ctx.extras["draw_result"]["image_id"] == "img_redraw_ref"
    assert ctx.extras["draw_result"]["source_image_id"] == "quote:quoted-image"
    assert excinfo.value.reply_text == "画好了, 图片ID: img_redraw_ref"


@pytest.mark.asyncio
async def test_redraw_command_center_hook_prefers_quoted_preview_image() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/重绘 改成水彩",
        metadata={
            "quote_image_url": "http://127.0.0.1:5080/images/hash-601/601_thumbnail.jpg",
            "quote_image_preview_url": "http://127.0.0.1:5080/images/hash-601/601_preview.jpg",
            "quote_image_thumbnail_url": "http://127.0.0.1:5080/images/hash-601/601_thumbnail.jpg",
            "quote": {"msg_svr_id": "quoted-image"},
        },
    )

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [
        {
            "image_url": "http://127.0.0.1:5080/images/hash-601/601_preview.jpg",
            "image_path": "",
            "prompt": "改成水彩",
            "trace_id": "trace-1",
            "source_label": "quote:quoted-image",
        }
    ]


@pytest.mark.asyncio
async def test_redraw_command_center_hook_resolves_referenced_turn_image_same_session() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/重绘 改成水彩",
        metadata={
            "quote": {
                "msg_svr_id": "quote-wrapper",
                "refer_msg_svr_id": "refer-image",
                "type": "text",
            },
        },
    )
    ctx.session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_user_1",
        channel=Channel.WECHAT,
    )
    ctx.session.turns = [
        Turn(
            session_id="room@chatroom",
            role=Role.USER,
            content="[图片]",
            metadata={
                "msg_svr_id": "refer-image",
                "image_url": "http://127.0.0.1:5080/images/hash-602/602_preview.jpg",
                "image_path": "images/hash-602/602.png",
            },
        )
    ]

    with pytest.raises(HookAbort):
        await hook.run(ctx)

    assert store.calls == [
        {
            "image_url": "http://127.0.0.1:5080/images/hash-602/602_preview.jpg",
            "image_path": "images/hash-602/602.png",
            "prompt": "改成水彩",
            "trace_id": "trace-1",
            "source_label": "quote:refer-image",
        }
    ]


@pytest.mark.asyncio
async def test_redraw_command_center_hook_does_not_resolve_cross_session_reference() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/重绘 改成水彩",
        metadata={"quote": {"refer_msg_svr_id": "refer-image", "type": "text"}},
    )
    ctx.session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_user_1",
        channel=Channel.WECHAT,
    )
    ctx.session.turns = [
        Turn(
            session_id="other@chatroom",
            role=Role.USER,
            content="[图片]",
            metadata={
                "msg_svr_id": "refer-image",
                "image_url": "http://127.0.0.1:5080/images/hash-other/other.png",
            },
        )
    ]

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == REDRAW_HELP_TEXT
    assert store.calls == []


@pytest.mark.asyncio
async def test_redraw_command_center_hook_resolves_nested_quoted_image_from_referenced_turn() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/重绘 改成水彩",
        metadata={"quote": {"refer_msg_svr_id": "refer-text", "type": "text"}},
    )
    ctx.session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_user_1",
        channel=Channel.WECHAT,
    )
    ctx.session.turns = [
        Turn(
            session_id="room@chatroom",
            role=Role.USER,
            content="这张图改成梵高风格",
            metadata={
                "msg_svr_id": "refer-text",
                "quote_image_url": "http://127.0.0.1:5080/images/hash-603/603_preview.jpg",
                "quote_image_path": "images/hash-603/603.png",
            },
        )
    ]

    with pytest.raises(HookAbort):
        await hook.run(ctx)

    assert store.calls == [
        {
            "image_url": "http://127.0.0.1:5080/images/hash-603/603_preview.jpg",
            "image_path": "images/hash-603/603.png",
            "prompt": "改成水彩",
            "trace_id": "trace-1",
            "source_label": "quote:refer-text",
        }
    ]


@pytest.mark.asyncio
async def test_redraw_command_center_hook_keeps_explicit_image_id_with_quote() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/redraw img_demo 改成水彩",
        metadata={
            "quote_image_url": "http://127.0.0.1:5080/images/hash-601/601.png",
        },
    )

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [
        {
            "image_id": "img_demo",
            "prompt": "改成水彩",
            "trace_id": "trace-1",
        }
    ]
    assert excinfo.value.reply_text == "画好了, 图片ID: img_redraw"


@pytest.mark.asyncio
async def test_redraw_command_center_hook_returns_help_when_args_missing() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/重绘 img_demo")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == REDRAW_HELP_TEXT
    assert store.calls == []


@pytest.mark.asyncio
async def test_redraw_command_center_hook_rejects_unknown_image_id_before_store_call() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/redraw img_missing 改成水彩")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == DRAW_IMAGE_ID_ERROR_TEXT
    assert store.calls == []


@pytest.mark.asyncio
async def test_async_redraw_rejects_unknown_image_id_without_scheduling_task() -> None:
    store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/redraw img_missing 改成水彩")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == DRAW_IMAGE_ID_ERROR_TEXT
    assert store.calls == []
    assert store.task_creates == []
    assert spawned == []
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_async_draw_storage_error_fails_without_ack_or_scheduling_task() -> None:
    store = _StorageUnavailableDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == DRAW_CONFIG_ERROR_TEXT
    assert store.storage_checks == 1
    assert store.calls == []
    assert store.task_creates == []
    assert spawned == []
    assert outbound.calls == []
    assert "draw_result" not in ctx.extras


@pytest.mark.asyncio
async def test_async_draw_storage_ok_acks_and_schedules_once_before_store_call() -> None:
    store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == "收到, 正在画。"
    assert len(spawned) == 1
    assert len(store.task_creates) == 1
    assert store.task_creates[0].prompt == "一只柴犬"
    assert store.task_creates[0].trace_id == "trace-1:draw"
    assert store.task_creates[0].command_type == "draw"
    assert ctx.extras["draw_task_id"] == "task-1"
    assert store.calls == []
    assert outbound.calls == []

    await spawned[0]

    assert store.calls == [{"prompt": "一只柴犬", "trace_id": "trace-1:draw"}]
    assert store.task_statuses == [("task-1", "running"), ("task-1", "completed")]
    assert store.task_results == [("task-1", "img_demo")]
    assert store.callback_claims == ["task-1"]
    assert store.callback_marks == [("task-1", "")]
    assert [call["msg_type"] for call in outbound.calls] == ["text", "image"]


@pytest.mark.asyncio
async def test_inline_draw_threads_scope_gate_through_long_running_job() -> None:
    store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    spawned: list[asyncio.Task[None]] = []
    decisions = iter((True, False))

    async def scope_allowed(_tenant_id: str, _session_id: str) -> bool:
        return next(decisions)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            store,
            channel_registry=_fake_channel_registry(outbound),
            register_background_task=spawned.append,
            scope_execution_allowed=scope_allowed,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)

    with pytest.raises(HookAbort):
        await hook.run(_ctx("/draw 一只柴犬"))
    await spawned[0]

    assert store.calls == [{"prompt": "一只柴犬", "trace_id": "trace-1:draw"}]
    assert store.task_results == []
    assert len(store.deferred_claims) == 1
    assert store.deferred_claims[0][0] == "task-1"
    assert store.deferred_claims[0][1].startswith("draw-inline-runner-")
    assert store.draw_tasks["task-1"].status == "queued"
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_async_draw_cancel_during_capture_finishes_completion_without_release() -> None:
    store = _FakeDrawStore()
    provider = _BlockingCaptureBillingProvider()
    billing, _ = _fake_billing(provider)
    outbound = _FakeChannelOutbound()
    spawned: list[asyncio.Task[None]] = []

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            store,
            billing=billing,
            channel_registry=_fake_channel_registry(outbound),
            register_background_task=spawned.append,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)

    with pytest.raises(HookAbort):
        await hook.run(_ctx("/draw 一只柴犬"))

    await asyncio.wait_for(provider.capture_committed.wait(), timeout=1)
    task = spawned[0]
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    provider.finish_capture.set()
    result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert store.task_statuses == [("task-1", "running"), ("task-1", "completed")]
    assert store.task_failures == []
    assert store.callback_marks == [("task-1", "")]
    assert [call["msg_type"] for call in outbound.calls] == ["text", "image"]
    assert len(provider.captures) == 1
    assert provider.releases == []


@pytest.mark.asyncio
async def test_async_draw_cancel_after_completion_commit_keeps_completed_callback() -> None:
    store = _BlockingCompletionDrawStore()
    billing, provider = _fake_billing()
    outbound = _FakeChannelOutbound()
    spawned: list[asyncio.Task[None]] = []

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            store,
            billing=billing,
            channel_registry=_fake_channel_registry(outbound),
            register_background_task=spawned.append,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)

    with pytest.raises(HookAbort):
        await hook.run(_ctx("/draw 一只柴犬"))
    await asyncio.wait_for(store.completion_committed.wait(), timeout=1)

    task = spawned[0]
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    store.finish_completion.set()
    result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert store.draw_tasks["task-1"].status == "completed"
    assert store.task_failures == []
    assert store.callback_marks == [("task-1", "")]
    assert outbound.calls[0]["reply_text"] == "画好了, 图片ID: img_demo"
    assert len(provider.captures) == 1
    assert provider.releases == []


@pytest.mark.asyncio
async def test_async_draw_cancel_before_capture_releases_and_marks_interrupted() -> None:
    store = _BlockingDrawStore()
    billing, provider = _fake_billing()
    outbound = _FakeChannelOutbound()
    spawned: list[asyncio.Task[None]] = []

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            store,
            billing=billing,
            channel_registry=_fake_channel_registry(outbound),
            register_background_task=spawned.append,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)

    with pytest.raises(HookAbort):
        await hook.run(_ctx("/draw 一只柴犬"))
    await asyncio.wait_for(store.generation_started.wait(), timeout=1)

    spawned[0].cancel()
    result = await asyncio.gather(spawned[0], return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert store.task_statuses == [("task-1", "running"), ("task-1", "interrupted")]
    assert store.task_failures == [("task-1", "interrupted", "cancelled")]
    assert store.callback_marks == [("task-1", "")]
    assert outbound.calls[0]["reply_text"] == "画图任务中断，请重试。"
    assert provider.captures == []
    assert provider.releases == provider.reservations


@pytest.mark.asyncio
async def test_async_draw_cancel_during_send_waits_for_durable_callback_ack() -> None:
    store = _FakeDrawStore()
    billing, provider = _fake_billing()
    outbound = _BlockingImageAckOutbound()
    spawned: list[asyncio.Task[None]] = []

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            store,
            billing=billing,
            channel_registry=_fake_channel_registry(outbound),
            register_background_task=spawned.append,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)

    with pytest.raises(HookAbort):
        await hook.run(_ctx("/draw 一只柴犬"))

    await asyncio.wait_for(outbound.image_sent.wait(), timeout=1)
    task = spawned[0]
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    assert store.callback_marks == []

    outbound.finish_ack.set()
    result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert store.draw_tasks["task-1"].status == "completed"
    assert store.task_failures == []
    assert store.callback_marks == [("task-1", "")]
    assert store.callback_errors == []
    assert len(provider.captures) == 1
    assert provider.releases == []


@pytest.mark.asyncio
async def test_async_redraw_storage_error_fails_without_ack_or_scheduling_task() -> None:
    store = _StorageUnavailableDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/redraw 改成水彩",
        metadata={"quote_image_url": "http://media.test/source.png"},
    )

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == DRAW_CONFIG_ERROR_TEXT
    assert store.storage_checks == 1
    assert store.calls == []
    assert store.task_creates == []
    assert spawned == []
    assert outbound.calls == []
    assert "draw_result" not in ctx.extras


@pytest.mark.asyncio
async def test_draw_command_center_hook_supports_private_session() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只柴犬", session_id="wxid_private", session_kind="private")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [{"prompt": "一只柴犬", "trace_id": "trace-1"}]
    assert ctx.extras["draw_result"]["file_name"] == "demo.png"


@pytest.mark.asyncio
async def test_draw_command_center_hook_handles_leading_quote_noise() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("'/draw 一只橘猫")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [{"prompt": "一只橘猫", "trace_id": "trace-1"}]


@pytest.mark.asyncio
async def test_draw_command_center_hook_triggers_registered_draw_command() -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只会打字的海豹")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [{"prompt": "一只会打字的海豹", "trace_id": "trace-1"}]
    assert ctx.extras["draw_result"]["file_name"] == "demo.png"


@pytest.mark.asyncio
async def test_draw_command_center_hook_uses_group_avatar_reference(monkeypatch) -> None:
    store = _FakeDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 基于群里千羽头像生成一个微信聊天记录")

    async def _fake_resolve(*args, **kwargs) -> DrawAvatarReference:
        return DrawAvatarReference(
            query="千羽",
            display_name="千羽",
            wxid="wxid_qianyu",
            avatar_url="http://127.0.0.1:5080/ext/roster/avatars/wxid_qianyu",
            image_path="/tmp/wxbot-avatars/qianyu.jpg",
            source_label="avatar:wxid_qianyu",
        )

    monkeypatch.setattr("plugins.draw.hooks.resolve_prompt_avatar_reference", _fake_resolve)

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert store.calls == [
        {
            "image_url": "",
            "image_path": "/tmp/wxbot-avatars/qianyu.jpg",
            "prompt": "基于群里千羽头像生成一个微信聊天记录",
            "trace_id": "trace-1",
            "source_label": "avatar:wxid_qianyu",
        }
    ]
    assert ctx.extras["draw_result"]["source_image_id"] == "avatar:wxid_qianyu"


@pytest.mark.asyncio
async def test_draw_command_center_hook_hides_verbose_api_error() -> None:
    store = _FailingDrawStore()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == DRAW_API_ERROR_TEXT
    assert "upstream" not in excinfo.value.reply_text
    assert store.calls == [{"prompt": "一只柴犬", "trace_id": "trace-1"}]


@pytest.mark.asyncio
async def test_draw_command_center_hook_reports_config_error() -> None:
    store = _FailingDrawStore(DrawConfigError("未配置 DRAW_API_URL"))
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == DRAW_CONFIG_ERROR_TEXT
    assert store.calls == [{"prompt": "一只柴犬", "trace_id": "trace-1"}]


@pytest.mark.asyncio
async def test_draw_command_center_hook_reports_empty_upstream_response() -> None:
    store = _FailingDrawStore(DrawApiError("绘图接口响应中未找到图片数据"))
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store))
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == DRAW_EMPTY_RESPONSE_ERROR_TEXT


@pytest.mark.asyncio
async def test_draw_command_failure_releases_billing_reservation() -> None:
    store = _FailingDrawStore()
    billing, provider = _fake_billing()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store, billing=billing))
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == DRAW_API_ERROR_TEXT
    assert len(provider.reservations) == 1
    assert provider.captures == []
    assert len(provider.releases) == 1


@pytest.mark.asyncio
async def test_draw_command_cancel_during_admission_releases_billing_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeDrawStore()
    billing, provider = _fake_billing()
    registry = CommandRegistryService()
    registry.register(build_draw_command_definitions(store, billing=billing))
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)
    resolver_started = asyncio.Event()

    async def blocking_resolver(*args, **kwargs) -> None:
        _ = args, kwargs
        resolver_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "plugins.draw.hooks.resolve_prompt_avatar_reference",
        blocking_resolver,
    )
    task = asyncio.create_task(hook.run(_ctx("/draw 一只柴犬")))
    await asyncio.wait_for(resolver_started.wait(), timeout=1)

    task.cancel()
    cancelled = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(cancelled[0], asyncio.CancelledError)
    assert len(provider.reservations) == 1
    assert provider.captures == []
    assert provider.releases == provider.reservations
    assert store.task_creates == []


@pytest.mark.asyncio
async def test_async_draw_command_failure_releases_billing_reservation() -> None:
    draw_store = _FailingDrawStore()
    billing, provider = _fake_billing()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            draw_store,
            billing=billing,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort):
        await hook.run(ctx)

    assert len(spawned) == 1
    await spawned[0]

    assert len(provider.reservations) == 1
    assert provider.captures == []
    assert len(provider.releases) == 1
    assert draw_store.task_statuses == [("task-1", "running"), ("task-1", "failed")]
    assert draw_store.task_failures == [
        ("task-1", "failed", "upstream_request_failed")
    ]
    assert draw_store.callback_claims == ["task-1"]
    assert draw_store.callback_marks == [("task-1", "")]
    assert len(outbound.calls) == 1
    assert outbound.calls[0]["reply_text"] == DRAW_API_ERROR_TEXT


@pytest.mark.asyncio
async def test_async_draw_timeout_sends_categorized_failure() -> None:
    draw_store = _FailingDrawStore(DrawApiError("绘图接口请求失败: timed out"))
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            draw_store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == "收到, 正在画。"
    assert len(spawned) == 1
    await spawned[0]

    assert outbound.calls[0]["reply_text"] == DRAW_TIMEOUT_ERROR_TEXT


@pytest.mark.asyncio
async def test_async_draw_callback_sent_idempotency_skips_duplicate_send() -> None:
    draw_store = _FakeDrawStore()
    draw_store.callback_already_sent = True
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            draw_store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort):
        await hook.run(ctx)

    await spawned[0]

    assert draw_store.task_statuses == [("task-1", "running"), ("task-1", "completed")]
    assert draw_store.callback_claims == ["task-1"]
    assert draw_store.callback_marks == []
    assert draw_store.callback_errors == []
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_async_draw_callback_failure_does_not_mark_sent() -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    outbound.fail_send_text = True
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            draw_store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort):
        await hook.run(ctx)

    await spawned[0]

    assert draw_store.callback_claims == ["task-1"]
    assert draw_store.callback_marks == []
    assert draw_store.callback_errors == [("task-1", "send text failed")]
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_stale_draw_recovery_callback_failure_records_error_without_marking_sent() -> None:
    draw_store = _FakeDrawStore()
    record = DrawTaskRecord(
        task_id="task-stale",
        request_id="req-stale",
        trace_id="trace-stale:draw",
        command_type="draw",
        status="interrupted",
        tenant_id="demo",
        channel=Channel.WECHAT.value,
        session_id="room@chatroom",
        requester="wxid_user_1",
        requester_display_name="群友A",
        original_message_id="m-stale",
        callback_target={
            "tenant_id": "demo",
            "channel": Channel.WECHAT.value,
            "session_id": "room@chatroom",
            "session_kind": "group",
            "user_id": "wxid_user_1",
            "reply_to_message_id": "m-stale",
            "metadata": {"session_name": "测试群"},
        },
        source_message={"message_id": "m-stale"},
        prompt="旧任务",
        quality="low",
    )

    async def recover_stale_tasks(**kwargs):
        _ = kwargs
        return [record]

    draw_store.recover_stale_tasks = recover_stale_tasks  # type: ignore[attr-defined]
    outbound = _FakeChannelOutbound()
    outbound.fail_send_text = True

    result = await recover_stale_draw_tasks(
        store=draw_store,  # type: ignore[arg-type]
        channel_registry=_fake_channel_registry(outbound),
        stale_seconds=60,
    )

    assert result == {"recovered": 1, "callbacks_sent": 0, "callback_failed": 1}
    assert draw_store.callback_claims == ["task-stale"]
    assert draw_store.callback_marks == []
    assert draw_store.callback_errors == [("task-stale", "send text failed")]
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_stale_draw_recovery_sends_compensation_callback_once() -> None:
    draw_store = _FakeDrawStore()
    record = DrawTaskRecord(
        task_id="task-stale",
        request_id="req-stale",
        trace_id="trace-stale:draw",
        command_type="draw",
        status="interrupted",
        tenant_id="demo",
        channel=Channel.WECHAT.value,
        session_id="room@chatroom",
        requester="wxid_user_1",
        original_message_id="m-stale",
        callback_target={
            "tenant_id": "demo",
            "channel": Channel.WECHAT.value,
            "session_id": "room@chatroom",
            "reply_to_message_id": "m-stale",
            "metadata": {},
        },
        source_message={"message_id": "m-stale"},
        prompt="旧任务",
        quality="low",
    )

    async def recover_stale_tasks(**kwargs):
        _ = kwargs
        return [record]

    draw_store.recover_stale_tasks = recover_stale_tasks  # type: ignore[attr-defined]
    outbound = _FakeChannelOutbound()

    result = await recover_stale_draw_tasks(
        store=draw_store,  # type: ignore[arg-type]
        channel_registry=_fake_channel_registry(outbound),
        stale_seconds=60,
    )

    assert result == {"recovered": 1, "callbacks_sent": 1, "callback_failed": 0}
    assert draw_store.callback_marks == [("task-stale", "")]
    assert outbound.calls[0]["reply_text"] == DRAW_TASK_INTERRUPTED_ERROR_MESSAGE


async def _recover_result(**kwargs):
    return {
        "recovered": 1,
        "callbacks_sent": 1,
        "callback_failed": 0,
        "stale_seconds": kwargs.get("stale_seconds"),
        "limit": kwargs.get("limit"),
    }


def _draw_router_client(
    draw_store: _FakeDrawStore,
    outbound: _FakeChannelOutbound,
    *,
    scope_execution_allowed=None,
):
    app = FastAPI()
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    app.include_router(
        build_draw_router(
            draw_store,  # type: ignore[arg-type]
            channel_registry=_fake_channel_registry(outbound),
            register_background_task=_track,
            recover_stale_tasks=_recover_result,
            scope_execution_allowed=(
                scope_execution_allowed or _allow_draw_scope
            ),
        )
    )
    transport = httpx.ASGITransport(app=app)
    return (
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Idempotency-Key": "draw-router-test"},
        ),
        spawned,
    )


async def _allow_draw_scope(_tenant_id: str, _session_id: str) -> bool:
    return True


def _terminal_task(
    *,
    task_id: str = "task-terminal",
    status: str = "failed",
    retry_count: int = 0,
    callback_sent: bool = False,
) -> DrawTaskRecord:
    return DrawTaskRecord(
        task_id=task_id,
        request_id="req-terminal",
        trace_id="trace-terminal:draw",
        command_type="draw",
        status=status,
        tenant_id="demo",
        channel=Channel.WECHAT.value,
        session_id="room@chatroom",
        requester="wxid_user_1",
        original_message_id="m-terminal",
        callback_target={
            "tenant_id": "demo",
            "channel": Channel.WECHAT.value,
            "session_id": "room@chatroom",
            "reply_to_message_id": "m-terminal",
            "metadata": {},
        },
        source_message={"message_id": "m-terminal"},
        prompt="一只柴犬",
        quality="low",
        result_image_id="img_done" if status == "completed" else "",
        result_local_path="/tmp/done.png" if status == "completed" else "",
        result_public_path="/plugins/draw/files/done.png" if status == "completed" else "",
        result_source_url="http://media.test/done.png" if status == "completed" else "",
        error_code="upstream_request_failed" if status == "failed" else "",
        error_message="provider failed" if status == "failed" else "",
        retry_count=retry_count,
        callback_sent=callback_sent,
    )


@pytest.mark.asyncio
async def test_draw_router_requires_admin_for_manual_task_actions() -> None:
    draw_store = _FakeDrawStore()
    draw_store.draw_tasks["task-failed"] = _terminal_task(task_id="task-failed")
    outbound = _FakeChannelOutbound()
    client, _ = _draw_router_client(draw_store, outbound)

    try:
        retry = await client.post("/tasks/task-failed/retry")
        resend = await client.post("/tasks/task-failed/resend-callback")
        recover = await client.post("/tasks/recover-stale")
    finally:
        await client.aclose()

    # Missing credentials are authentication failures (401). Authenticated
    # principals with insufficient privileges are covered separately as 403.
    assert retry.status_code == 401
    assert resend.status_code == 401
    assert recover.status_code == 401


@pytest.mark.asyncio
async def test_draw_router_requires_key_and_exactly_replays_retry() -> None:
    draw_store = _ReplayDrawStore()
    draw_store.draw_tasks["task-failed"] = _terminal_task(task_id="task-failed")
    draw_store.draw_tasks["task-other"] = _terminal_task(task_id="task-other")
    client, _ = _draw_router_client(draw_store, _FakeChannelOutbound())
    auth = {"Authorization": "Bearer admin_token"}
    intent_headers = {**auth, "Idempotency-Key": "retry-lost-response"}

    try:
        missing = await client.post(
            "/tasks/task-failed/retry",
            headers={**auth, "Idempotency-Key": ""},
        )
        first = await client.post(
            "/tasks/task-failed/retry",
            headers=intent_headers,
        )
        replay = await client.post(
            "/tasks/task-failed/retry",
            headers=intent_headers,
        )
        conflict = await client.post(
            "/tasks/task-other/retry",
            headers=intent_headers,
        )
    finally:
        await client.aclose()

    assert missing.status_code == 428
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert draw_store.reserved_retries == [("task-failed", 1)]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_conflict"


@pytest.mark.asyncio
async def test_draw_router_retries_failed_and_interrupted_tasks() -> None:
    draw_store = _FakeDrawStore()
    draw_store.draw_tasks["task-failed"] = _terminal_task(task_id="task-failed", status="failed")
    draw_store.draw_tasks["task-interrupted"] = _terminal_task(
        task_id="task-interrupted",
        status="interrupted",
    )
    outbound = _FakeChannelOutbound()
    client, spawned = _draw_router_client(draw_store, outbound)
    headers = {"Authorization": "Bearer admin_token"}

    try:
        failed_retry = await client.post("/tasks/task-failed/retry", headers=headers)
        interrupted_retry = await client.post("/tasks/task-interrupted/retry", headers=headers)
        await asyncio.gather(*spawned)
    finally:
        await client.aclose()

    assert failed_retry.status_code == 200
    assert failed_retry.json()["retry_queued"] is True
    assert failed_retry.json()["retry_count"] == 1
    assert interrupted_retry.status_code == 200
    assert draw_store.reserved_retries == [("task-failed", 1), ("task-interrupted", 1)]
    assert spawned == []
    assert draw_store.calls == []
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_draw_queue_worker_executes_manual_retry_child() -> None:
    draw_store = _FakeDrawStore()
    parent = _terminal_task(task_id="task-failed", status="failed")
    draw_store.draw_tasks[parent.task_id] = parent
    reserved = await draw_store.reserve_draw_task_retry(parent.task_id, max_retries=2)
    assert reserved is not None
    child = await draw_store.create_retry_draw_task(reserved, retry_count=reserved.retry_count)
    outbound = _FakeChannelOutbound()

    result = await drain_queued_draw_tasks(
        store=draw_store,  # type: ignore[arg-type]
        channel_registry=_fake_channel_registry(outbound),
        billing=None,
        worker_id="worker-a",
        batch_size=1,
        lock_ttl_seconds=60,
    )

    assert result == {"claimed": 1, "completed": 1, "failed": 0, "auto_retried": 0}
    assert draw_store.calls == [{"prompt": "一只柴犬", "trace_id": child.trace_id}]
    assert [call["msg_type"] for call in outbound.calls] == ["text", "image"]


@pytest.mark.asyncio
async def test_draw_queue_auto_retry_budget_and_backoff() -> None:
    draw_store = _FailingDrawStore(DrawApiError("upstream returned 500"))
    task = await draw_store.create_draw_task(
        type(
            "Task",
            (),
            {
                "request_id": "req-auto",
                "trace_id": "trace-auto:draw",
                "command_type": "draw",
                "tenant_id": "demo",
                "channel": Channel.WECHAT.value,
                "session_id": "room@chatroom",
                "requester": "wxid_user_1",
                "original_message_id": "m-auto",
                "callback_target": {
                    "tenant_id": "demo",
                    "channel": Channel.WECHAT.value,
                    "session_id": "room@chatroom",
                    "reply_to_message_id": "m-auto",
                    "metadata": {},
                },
                "source_message": {"message_id": "m-auto"},
                "prompt": "一只柴犬",
                "quality": "low",
                "source_image": {},
            },
        )()
    )
    outbound = _FakeChannelOutbound()

    result = await drain_queued_draw_tasks(
        store=draw_store,  # type: ignore[arg-type]
        channel_registry=_fake_channel_registry(outbound),
        billing=None,
        worker_id="worker-auto",
        batch_size=1,
        lock_ttl_seconds=60,
        auto_retry_enabled=True,
        max_retries=1,
        retry_backoff_seconds=30,
    )

    assert result == {"claimed": 1, "completed": 0, "failed": 1, "auto_retried": 1}
    assert draw_store.reserved_retries == [(task.task_id, 1)]
    children = [
        record
        for record in draw_store.draw_tasks.values()
        if record.source_message.get("draw_retry_parent_task_id") == task.task_id
    ]
    assert len(children) == 1
    assert children[0].retry_count == 1
    assert children[0].next_run_at
    assert children[0].next_run_at > datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_draw_queue_non_retryable_error_does_not_auto_retry() -> None:
    draw_store = _FailingDrawStore(DrawConfigError("未配置 DRAW_API_URL"))
    await draw_store.create_draw_task(
        type(
            "Task",
            (),
            {
                "request_id": "req-config",
                "trace_id": "trace-config:draw",
                "command_type": "draw",
                "tenant_id": "demo",
                "channel": Channel.WECHAT.value,
                "session_id": "room@chatroom",
                "requester": "wxid_user_1",
                "original_message_id": "m-config",
                "callback_target": {
                    "tenant_id": "demo",
                    "channel": Channel.WECHAT.value,
                    "session_id": "room@chatroom",
                    "reply_to_message_id": "m-config",
                    "metadata": {},
                },
                "source_message": {"message_id": "m-config"},
                "prompt": "一只柴犬",
                "quality": "low",
                "source_image": {},
            },
        )()
    )

    result = await drain_queued_draw_tasks(
        store=draw_store,  # type: ignore[arg-type]
        channel_registry=_fake_channel_registry(_FakeChannelOutbound()),
        billing=None,
        worker_id="worker-auto",
        batch_size=1,
        lock_ttl_seconds=60,
        auto_retry_enabled=True,
        max_retries=2,
        retry_backoff_seconds=30,
    )

    assert result == {"claimed": 1, "completed": 0, "failed": 1, "auto_retried": 0}
    assert draw_store.reserved_retries == []


@pytest.mark.asyncio
async def test_draw_router_rejects_running_completed_and_over_budget_retry() -> None:
    draw_store = _FakeDrawStore()
    draw_store.draw_tasks["task-running"] = _terminal_task(task_id="task-running", status="running")
    draw_store.draw_tasks["task-completed"] = _terminal_task(task_id="task-completed", status="completed")
    draw_store.draw_tasks["task-budget"] = _terminal_task(
        task_id="task-budget",
        status="failed",
        retry_count=2,
    )
    outbound = _FakeChannelOutbound()
    client, spawned = _draw_router_client(draw_store, outbound)
    headers = {"Authorization": "Bearer admin_token"}

    try:
        running = await client.post("/tasks/task-running/retry", headers=headers)
        completed = await client.post("/tasks/task-completed/retry", headers=headers)
        budget = await client.post("/tasks/task-budget/retry", headers=headers)
    finally:
        await client.aclose()

    assert running.status_code == 409
    assert completed.status_code == 409
    assert budget.status_code == 429
    assert spawned == []
    assert draw_store.calls == []


@pytest.mark.asyncio
async def test_draw_router_resend_callback_idempotent_and_force() -> None:
    draw_store = _FakeDrawStore()
    draw_store.draw_tasks["task-completed"] = _terminal_task(
        task_id="task-completed",
        status="completed",
        callback_sent=True,
    )
    outbound = _FakeChannelOutbound()
    client, _ = _draw_router_client(draw_store, outbound)
    headers = {"Authorization": "Bearer admin_token"}

    try:
        skipped = await client.post("/tasks/task-completed/resend-callback", headers=headers)
        assert skipped.status_code == 200
        assert skipped.json()["skipped"] is True
        assert outbound.calls == []
        forced = await client.post(
            "/tasks/task-completed/resend-callback?force=true",
            headers=headers,
        )
    finally:
        await client.aclose()

    assert forced.status_code == 200
    assert forced.json()["sent"] is True
    assert [call["msg_type"] for call in outbound.calls] == ["text", "image"]
    assert draw_store.callback_marks == [("task-completed", "")]


@pytest.mark.asyncio
async def test_draw_queue_defers_disabled_scope_and_executes_enabled_scope() -> None:
    draw_store = _FakeDrawStore()
    disabled = DrawTaskRecord(
        **{
            **_terminal_task(task_id="task-disabled", status="queued").__dict__,
            "tenant_id": "tenant-disabled",
            "session_id": "room-disabled@chatroom",
            "callback_target": {
                "tenant_id": "tenant-disabled",
                "channel": Channel.WECHAT.value,
                "session_id": "room-disabled@chatroom",
                "reply_to_message_id": "m-terminal",
                "metadata": {},
            },
        }
    )
    enabled = DrawTaskRecord(
        **{
            **_terminal_task(task_id="task-enabled", status="queued").__dict__,
            "tenant_id": "tenant-enabled",
            "session_id": "room-enabled@chatroom",
            "callback_target": {
                "tenant_id": "tenant-enabled",
                "channel": Channel.WECHAT.value,
                "session_id": "room-enabled@chatroom",
                "reply_to_message_id": "m-terminal",
                "metadata": {},
            },
        }
    )
    draw_store.draw_tasks = {
        disabled.task_id: disabled,
        enabled.task_id: enabled,
    }

    async def scope_allowed(tenant_id: str, _session_id: str) -> bool:
        return tenant_id == "tenant-enabled"

    result = await drain_queued_draw_tasks(
        store=draw_store,  # type: ignore[arg-type]
        channel_registry=_fake_channel_registry(_FakeChannelOutbound()),
        billing=None,
        worker_id="worker-scope",
        batch_size=2,
        lock_ttl_seconds=60,
        scope_execution_allowed=scope_allowed,
    )

    assert result == {"claimed": 1, "completed": 1, "failed": 0, "auto_retried": 0}
    assert draw_store.deferred_claims == [("task-disabled", "worker-scope")]
    assert draw_store.draw_tasks["task-disabled"].status == "queued"
    assert draw_store.draw_tasks["task-enabled"].status == "completed"
    assert draw_store.calls == [
        {"prompt": "一只柴犬", "trace_id": "trace-terminal:draw"}
    ]


@pytest.mark.asyncio
async def test_draw_queue_rechecks_scope_after_provider_before_completion() -> None:
    draw_store = _FakeDrawStore()
    task = _terminal_task(task_id="draw-toggle-before-complete", status="queued")
    draw_store.draw_tasks[task.task_id] = task
    decisions = iter((True, True, False))

    async def scope_allowed(_tenant_id: str, _session_id: str) -> bool:
        return next(decisions)

    outbound = _FakeChannelOutbound()
    result = await drain_queued_draw_tasks(
        store=draw_store,  # type: ignore[arg-type]
        channel_registry=_fake_channel_registry(outbound),
        billing=None,
        worker_id="worker-toggle",
        batch_size=1,
        scope_execution_allowed=scope_allowed,
    )

    assert result["completed"] == 0
    assert draw_store.calls == [
        {"prompt": "一只柴犬", "trace_id": "trace-terminal:draw"}
    ]
    assert draw_store.task_results == []
    assert draw_store.deferred_claims == [
        ("draw-toggle-before-complete", "worker-toggle")
    ]
    assert draw_store.draw_tasks[task.task_id].status == "queued"
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_draw_callback_rechecks_scope_after_claim_before_send() -> None:
    draw_store = _FakeDrawStore()
    task = _terminal_task(task_id="task-toggle-before-send", status="queued")
    draw_store.draw_tasks[task.task_id] = task
    # queue admission, pre-provider, post-provider, pre-claim, post-claim
    decisions = iter((True, True, True, True, False))

    async def scope_allowed(_tenant_id: str, _session_id: str) -> bool:
        return next(decisions)

    outbound = _FakeChannelOutbound()
    result = await drain_queued_draw_tasks(
        store=draw_store,  # type: ignore[arg-type]
        channel_registry=_fake_channel_registry(outbound),
        billing=None,
        worker_id="worker-toggle",
        batch_size=1,
        scope_execution_allowed=scope_allowed,
    )

    assert result["completed"] == 1
    assert draw_store.draw_tasks[task.task_id].status == "completed"
    assert draw_store.callback_claims == [task.task_id]
    assert draw_store.callback_releases == [
        (task.task_id, "scope_execution_denied")
    ]
    assert draw_store.callback_marks == []
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_draw_router_force_resend_exactly_replays_without_second_send() -> None:
    draw_store = _ReplayDrawStore()
    draw_store.draw_tasks["task-completed"] = _terminal_task(
        task_id="task-completed",
        status="completed",
        callback_sent=True,
    )
    outbound = _FakeChannelOutbound()
    client, _ = _draw_router_client(draw_store, outbound)
    headers = {
        "Authorization": "Bearer admin_token",
        "Idempotency-Key": "force-resend-lost-response",
    }

    try:
        first = await client.post(
            "/tasks/task-completed/resend-callback?force=true",
            headers=headers,
        )
        replay = await client.post(
            "/tasks/task-completed/resend-callback?force=true",
            headers=headers,
        )
        conflict = await client.post(
            "/tasks/task-completed/resend-callback?force=false",
            headers=headers,
        )
    finally:
        await client.aclose()

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert [call["msg_type"] for call in outbound.calls] == ["text", "image"]
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_conflict"


@pytest.mark.asyncio
async def test_draw_force_resend_reuses_external_command_ids_after_ledger_rollback() -> None:
    draw_store = _FakeDrawStore()
    draw_store.draw_tasks["task-completed"] = _terminal_task(
        task_id="task-completed",
        status="completed",
        callback_sent=True,
    )
    outbound = _FakeChannelOutbound()
    client, _ = _draw_router_client(draw_store, outbound)
    headers = {
        "Authorization": "Bearer admin_token",
        "Idempotency-Key": "force-resend-crash-window",
    }

    try:
        # This fake deliberately forgets the ledger outcome. Calling twice
        # models a process crash after enqueue but before the outer mutation
        # transaction commits.
        first = await client.post(
            "/tasks/task-completed/resend-callback?force=true",
            headers=headers,
        )
        retry = await client.post(
            "/tasks/task-completed/resend-callback?force=true",
            headers=headers,
        )
    finally:
        await client.aclose()

    assert first.status_code == retry.status_code == 200
    command_ids = [
        str(getattr(call["options"], "idempotency_key", ""))
        for call in outbound.calls
    ]
    assert command_ids[:2] == command_ids[2:]
    assert command_ids[0] != command_ids[1]
    assert all("force-resend-crash-window" not in item for item in command_ids)


@pytest.mark.asyncio
async def test_draw_router_forced_resend_records_callback_error() -> None:
    draw_store = _FakeDrawStore()
    draw_store.draw_tasks["task-completed"] = _terminal_task(
        task_id="task-completed",
        status="completed",
        callback_sent=True,
    )
    outbound = _FakeChannelOutbound()
    outbound.fail_send_text = True
    client, _ = _draw_router_client(draw_store, outbound)

    try:
        response = await client.post(
            "/tasks/task-completed/resend-callback?force=true",
            headers={"Authorization": "Bearer admin_token"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 409
    assert draw_store.callback_claims == []
    assert draw_store.callback_marks == []
    assert draw_store.callback_errors == [("task-completed", "send text failed")]


@pytest.mark.asyncio
async def test_draw_router_manual_recover_stale_calls_internal_recovery() -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    client, _ = _draw_router_client(draw_store, outbound)

    try:
        response = await client.post(
            "/tasks/recover-stale?stale_seconds=12&limit=3",
            headers={"Authorization": "Bearer admin_token"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.json() == {
        "recovered": 1,
        "callbacks_sent": 1,
        "callback_failed": 0,
        "stale_seconds": 12.0,
        "limit": 3,
    }


@pytest.mark.asyncio
async def test_async_redraw_creates_redraw_task_type() -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            draw_store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/redraw img_demo 改成水彩")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == "收到, 正在重绘。"
    assert len(draw_store.task_creates) == 1
    assert draw_store.task_creates[0].command_type == "redraw"
    assert draw_store.task_creates[0].source_image["image_id"] == "img_demo"
    await spawned[0]


@pytest.mark.asyncio
async def test_async_draw_config_error_sends_categorized_failure() -> None:
    draw_store = _FailingDrawStore(DrawConfigError("未配置 DRAW_API_URL"))
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            draw_store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx("/draw 一只柴犬")

    with pytest.raises(HookAbort):
        await hook.run(ctx)

    assert len(spawned) == 1
    await spawned[0]

    assert outbound.calls[0]["reply_text"] == DRAW_CONFIG_ERROR_TEXT


@pytest.mark.asyncio
async def test_async_draw_uses_bound_request_metadata_for_store_and_callback() -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            draw_store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/draw 一张未来城市海报",
        metadata={
            "session_name": "测试群",
            "sender_name": "群友A",
            "sender_wxid": "wxid_user_a",
            "msg_svr_id": "msg-original",
        },
    )

    with pytest.raises(HookAbort):
        await hook.run(ctx)

    ctx.event.metadata["sender_name"] = "被后续消息覆盖"
    ctx.event.metadata["sender_wxid"] = "wxid_later"
    await spawned[0]

    assert draw_store.calls == [{"prompt": "一张未来城市海报", "trace_id": "trace-1:draw"}]
    first_target = outbound.calls[0]["target"]
    assert isinstance(first_target, ChannelTarget)
    assert first_target.sender_id == "wxid_user_a"
    assert first_target.sender_name == "群友A"
    assert first_target.reply_to_message_id == "msg-original"
    first_options = outbound.calls[0]["options"]
    assert isinstance(first_options, ChannelSendOptions)
    assert first_options.trace_id == "trace-1:draw"
    assert first_options.source_message["metadata"]["sender_name"] == "群友A"
    delivery = first_options.delivery_metadata
    assert delivery["participation_status"] == "must_reply"
    assert delivery["source_message_id"] == "m-1"
    assert delivery["participation_policy_version"] == 23
    assert delivery["send_revalidation_enabled"] is True
    assert delivery["speech_class"] == "obligation"


@pytest.mark.asyncio
async def test_discord_group_draw_command_uses_channel_outbound() -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound, channel=Channel.DISCORD)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            draw_store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)
    ctx = _ctx(
        "/draw 一张未来城市海报",
        channel=Channel.DISCORD,
        user_id="discord-user-1",
        session_id="discord-channel-1",
        metadata={
            "session_kind": "group",
            "session_name": "设计频道",
            "sender_id": "discord-user-1",
            "sender_name": "Alice",
            "reply_to_message_id": "discord-msg-1",
        },
    )

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reason == "draw_command"
    assert excinfo.value.reply_text == "收到, 正在画。"
    assert len(spawned) == 1

    await spawned[0]

    assert draw_store.calls == [{"prompt": "一张未来城市海报", "trace_id": "trace-1:draw"}]
    assert len(outbound.calls) == 2
    first_target = outbound.calls[0]["target"]
    assert isinstance(first_target, ChannelTarget)
    assert first_target.channel == "discord"
    assert first_target.session_id == "discord-channel-1"
    assert first_target.session_kind == "group"
    assert first_target.sender_id == "discord-user-1"
    assert first_target.reply_to_message_id == "discord-msg-1"
    assert outbound.calls[0]["reply_text"] == "画好了, 图片ID: img_demo"
    assert outbound.calls[1]["msg_type"] == "image"


@pytest.mark.asyncio
async def test_discord_group_draw_command_dedupes_same_message_id() -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound, channel=Channel.DISCORD)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    registry = CommandRegistryService()
    registry.register(
        build_draw_command_definitions(
            draw_store,
            channel_registry=channel_registry,
            register_background_task=_track,
        )
    )
    hook = CommandCenterHook(_FakeCommandStore(), registry)

    def _discord_ctx() -> PipelineContext:
        return _ctx(
            "@zzz /draw 海总众筹 kyc 开启最后一舞",
            channel=Channel.DISCORD,
            user_id="discord-user-1",
            session_id="discord-channel-1",
            metadata={
                "session_kind": "group",
                "sender_id": "discord-user-1",
                "sender_name": "Alice",
                "reply_to_message_id": "discord-msg-1",
                "mentioned_me": True,
            },
        )

    for _ in range(2):
        with pytest.raises(HookAbort) as excinfo:
            await hook.run(_discord_ctx())
        assert excinfo.value.reason == "draw_command"
        assert excinfo.value.reply_text == "收到, 正在画。"

    assert len(draw_store.task_creates) == 1
    assert len(spawned) == 1
    assert len(draw_store.draw_tasks) == 1
    task = next(iter(draw_store.draw_tasks.values()))
    assert task.task_id.startswith("drawtask_msg_")
    assert task.original_message_id == "m-1"

    await spawned[0]

    assert draw_store.calls == [
        {"prompt": "海总众筹 kyc 开启最后一舞", "trace_id": "trace-1:draw"}
    ]
    assert len(outbound.calls) == 2


@pytest.mark.asyncio
async def test_draw_reply_hook_injects_text_and_image_segments() -> None:
    hook = DrawReplyHook()
    ctx = _ctx("/draw 一只狐狸")
    ctx.result = CapabilityResult(route=RouteType.CANNED, reply_text="已生成图片。")
    ctx.extras["draw_result"] = {
        "image_id": "img_fox",
        "prompt": "一只狐狸",
        "local_path": "/mnt/e/cs-system-draw/fox.png",
        "file_name": "fox.png",
        "media_type": "image/png",
        "public_path": "/plugins/draw/files/fox.png",
        "source_url": "http://127.0.0.1:18080/p/img/task/0",
        "source_image_id": "",
        "image_url": "http://198.51.100.94:8000/plugins/draw/files/fox.png",
        "text": DRAW_SUCCESS_TEXT,
    }

    await hook.run(ctx)

    assert ctx.result.reply_text == DRAW_SUCCESS_TEXT
    reply_segments = ctx.result.metadata["reply_segments"]
    assert reply_segments[0]["content"] == DRAW_SUCCESS_TEXT
    assert reply_segments[1]["metadata"]["wxbot_msg_type"] == "image"
    assert reply_segments[1]["metadata"]["image_path"] == "/mnt/e/cs-system-draw/fox.png"
    assert (
        reply_segments[1]["metadata"]["image_url"]
        == "http://198.51.100.94:8000/plugins/draw/files/fox.png"
    )
    assert (
        ctx.result.metadata["draw"]["image_url"]
        == "http://198.51.100.94:8000/plugins/draw/files/fox.png"
    )
    assert ctx.result.metadata["draw"]["image_id"] == "img_fox"
    assert reply_segments[1]["metadata"]["image_id"] == "img_fox"


@pytest.mark.asyncio
async def test_draw_postprocess_result_step_injects_segments_and_signal() -> None:
    step = DrawPostprocessResultStep()
    ctx = _ctx("/draw 一只狐狸")
    ctx.result = CapabilityResult(route=RouteType.CANNED, reply_text="已生成图片。")
    ctx.extras["draw_result"] = {
        "image_id": "img_fox",
        "prompt": "一只狐狸",
        "local_path": "/mnt/e/cs-system-draw/fox.png",
        "file_name": "fox.png",
        "media_type": "image/png",
        "public_path": "/plugins/draw/files/fox.png",
        "source_url": "http://127.0.0.1:18080/p/img/task/0",
        "source_image_id": "",
        "image_url": "http://198.51.100.94:8000/plugins/draw/files/fox.png",
        "text": DRAW_SUCCESS_TEXT,
    }

    result = await step.run(ctx)

    assert result.action == "continue"
    assert result.reason == "postprocessed"
    assert result.result is ctx.result
    assert len(result.effects) == 1
    assert result.effects[0].type == "publish_media"
    assert result.effects[0].owner == "draw"
    assert result.effects[0].payload["commit_semantics"] == "audit_after_side_effect"
    assert result.effects[0].payload["image_id"] == "img_fox"
    assert result.effects[0].payload["image_path"] == "/mnt/e/cs-system-draw/fox.png"
    assert result.effects[0].payload["file_name"] == "fox.png"
    assert result.effects[0].payload["image_url"] == (
        "http://198.51.100.94:8000/plugins/draw/files/fox.png"
    )
    assert result.effects[0].idempotency_key == (
        "draw:publish_media:demo:wechat:room@chatroom:trace-1"
    )
    assert ctx.signals["draw"]["result"]["image_id"] == "img_fox"
    assert ctx.result.reply_text == DRAW_SUCCESS_TEXT
    assert ctx.result.metadata["draw"]["image_id"] == "img_fox"
    assert ctx.result.metadata["reply_segments"][1]["metadata"]["image_path"].endswith(
        "fox.png"
    )


@pytest.mark.asyncio
async def test_draw_publish_media_effect_handler_audits_once_after_commit() -> None:
    step = DrawPostprocessResultStep()
    ctx = _ctx("/draw 一只狐狸")
    ctx.result = CapabilityResult(route=RouteType.CANNED, reply_text="已生成图片。")
    ctx.extras["draw_result"] = {
        "image_id": "img_fox",
        "prompt": "一只狐狸",
        "local_path": "/mnt/e/cs-system-draw/fox.png",
        "file_name": "fox.png",
        "media_type": "image/png",
        "public_path": "/plugins/draw/files/fox.png",
        "source_url": "http://127.0.0.1:18080/p/img/task/0",
        "source_image_id": "",
        "image_url": "http://198.51.100.94:8000/plugins/draw/files/fox.png",
        "text": DRAW_SUCCESS_TEXT,
    }
    registry = EffectHandlerRegistry()
    registry.register("publish_media", "draw", DrawPublishMediaEffectHandler())
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())

    step_result = await step.run(ctx)
    first = await dispatcher.dispatch(step_result.effects[0], ctx)
    second = await dispatcher.dispatch(step_result.effects[0], ctx)

    assert first.status == EFFECT_STATUS_RECORDED
    assert second.status == EFFECT_STATUS_DUPLICATE
    assert ctx.signals["effects"]["draw"] == [
        {
            "type": "publish_media",
            "owner": "draw",
            "idempotency_key": "draw:publish_media:demo:wechat:room@chatroom:trace-1",
            "channel": "wechat",
            "session_id": "room@chatroom",
            "image_id": "img_fox",
            "image_path": "/mnt/e/cs-system-draw/fox.png",
            "image_url": "http://198.51.100.94:8000/plugins/draw/files/fox.png",
            "status": "audited",
        }
    ]


@pytest.mark.asyncio
async def test_draw_postprocess_result_step_can_emit_channel_reply_effects() -> None:
    step = DrawPostprocessResultStep(channel_reply_effects_enabled=True)
    ctx = _ctx(
        "/draw 一只狐狸",
        metadata={
            "session_name": "测试群",
            "sender_name": "小石",
            "sender_wxid": "wxid_sender",
            "msg_svr_id": "msg-draw-1",
            "session_kind": "group",
        },
    )
    ctx.result = CapabilityResult(route=RouteType.CANNED, reply_text="已生成图片。")
    ctx.extras["draw_result"] = {
        "image_id": "img_fox",
        "prompt": "一只狐狸",
        "local_path": "/mnt/e/cs-system-draw/fox.png",
        "file_name": "fox.png",
        "media_type": "image/png",
        "public_path": "/plugins/draw/files/fox.png",
        "source_url": "http://127.0.0.1:18080/p/img/task/0",
        "source_image_id": "",
        "image_url": "http://198.51.100.94:8000/plugins/draw/files/fox.png",
        "text": DRAW_SUCCESS_TEXT,
    }

    result = await step.run(ctx)

    assert result.publish_outbound is False
    assert result.append_assistant_turn is False
    assert ctx.extras["suppress_outbound"] is True
    assert ctx.extras["skip_assistant_turn"] is True
    assert [effect.type for effect in result.effects] == [
        "publish_media",
        "enqueue_channel_reply",
        "enqueue_channel_reply",
    ]
    text_effect = result.effects[1]
    image_effect = result.effects[2]
    assert text_effect.owner == "wxbot"
    assert text_effect.payload["channel"] == "wechat"
    assert text_effect.payload["body"] == {"type": "text", "text": DRAW_SUCCESS_TEXT}
    assert text_effect.payload["reply_to_message_id"] == "msg-draw-1"
    assert text_effect.idempotency_key == "channel-reply:demo:m-1:draw-text"
    assert image_effect.owner == "wxbot"
    assert image_effect.payload["media"] == {
        "image_path": "/mnt/e/cs-system-draw/fox.png",
        "image_url": "http://198.51.100.94:8000/plugins/draw/files/fox.png",
    }
    assert image_effect.idempotency_key == "channel-reply:demo:m-1:draw-image"


@pytest.mark.asyncio
async def test_draw_postprocess_result_step_continues_without_draw_result() -> None:
    step = DrawPostprocessResultStep()
    ctx = _ctx("普通消息")
    ctx.result = CapabilityResult(route=RouteType.LLM, reply_text="普通回复")

    result = await step.run(ctx)

    assert result.action == "continue"
    assert result.reason == "no_draw_result"
    assert ctx.result.reply_text == "普通回复"
    assert ctx.signals["draw"]["result"] == {}


@pytest.mark.asyncio
async def test_draw_agent_cancel_during_reservation_releases_before_propagating() -> None:
    draw_store = _FakeDrawStore()
    provider = _BlockingReserveBillingProvider()
    billing, _ = _fake_billing(provider)
    outbound = _FakeChannelOutbound()
    spawned: list[asyncio.Task[None]] = []
    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=_fake_channel_registry(outbound),
        billing=billing,
        register_background_task=spawned.append,
        scope_execution_allowed=_allow_draw_scope,
    )

    task = asyncio.create_task(
        service.generate_group_image(
            _agent_session(),
            {"prompt": "一只戴墨镜的橘猫"},
        )
    )
    await asyncio.wait_for(provider.reserve_committed.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    provider.finish_reserve.set()
    result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert len(provider.reservations) == 1
    assert provider.captures == []
    assert provider.releases == provider.reservations
    assert spawned == []
    assert draw_store.calls == []


@pytest.mark.asyncio
async def test_draw_router_loads_task_and_rejects_disabled_scope_before_retry() -> None:
    draw_store = _FakeDrawStore()
    draw_store.draw_tasks["task-disabled"] = DrawTaskRecord(
        **{
            **_terminal_task(task_id="task-disabled", status="failed").__dict__,
            "tenant_id": "tenant-disabled",
            "session_id": "room-disabled@chatroom",
        }
    )
    gate_calls: list[tuple[str, str]] = []

    async def deny_scope(tenant_id: str, session_id: str) -> bool:
        gate_calls.append((tenant_id, session_id))
        return False

    outbound = _FakeChannelOutbound()
    client, spawned = _draw_router_client(
        draw_store,
        outbound,
        scope_execution_allowed=deny_scope,
    )
    try:
        response = await client.post(
            "/tasks/task-disabled/retry",
            headers={"Authorization": "Bearer admin_token"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 403
    assert gate_calls == [("tenant-disabled", "room-disabled@chatroom")]
    assert draw_store.reserved_retries == []
    assert spawned == []
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_draw_agent_cancel_during_generation_releases_reservation() -> None:
    draw_store = _BlockingDrawStore()
    billing, provider = _fake_billing()
    spawned: list[asyncio.Task[None]] = []
    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=_fake_channel_registry(_FakeChannelOutbound()),
        billing=billing,
        register_background_task=spawned.append,
        scope_execution_allowed=_allow_draw_scope,
    )

    result = await service.generate_group_image(
        _agent_session(),
        {"prompt": "一只戴墨镜的橘猫"},
    )
    assert result["accepted"] is True
    await asyncio.wait_for(draw_store.generation_started.wait(), timeout=1)

    task = spawned[0]
    task.cancel()
    cancelled = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(cancelled[0], asyncio.CancelledError)
    assert len(provider.reservations) == 1
    assert provider.captures == []
    assert provider.releases == provider.reservations


@pytest.mark.asyncio
async def test_draw_agent_tool_accepts_job_and_enqueues_async_result() -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=channel_registry,
        register_background_task=_track,
        scope_execution_allowed=_allow_draw_scope,
    )

    result = await service.generate_group_image(
        _agent_session(),
        {"prompt": "一只戴墨镜的橘猫"},
    )

    assert result["accepted"] is True
    assert result["async"] is True
    assert result["prompt"] == "一只戴墨镜的橘猫"
    assert len(spawned) == 1

    await spawned[0]

    assert draw_store.calls[0]["prompt"] == "一只戴墨镜的橘猫"
    assert len(outbound.calls) == 2
    assert outbound.calls[0]["msg_type"] == "text"
    assert outbound.calls[0]["reply_text"] == "画好了, 图片ID: img_demo"
    assert outbound.calls[1]["msg_type"] == "image"
    assert outbound.calls[1]["image_path"] == ""
    assert outbound.calls[1]["image_url"] == "http://127.0.0.1:18080/p/img/task/0"


@pytest.mark.asyncio
async def test_draw_agent_tool_accepts_private_session() -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    spawned: list[asyncio.Task[None]] = []

    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=_fake_channel_registry(outbound),
        register_background_task=spawned.append,
        scope_execution_allowed=_allow_draw_scope,
    )

    result = await service.generate_group_image(
        _private_agent_session(),
        {"prompt": "海边日落"},
    )

    assert result["accepted"] is True
    await spawned[0]
    assert len(outbound.calls) == 2
    assert outbound.calls[0]["target"].session_kind == "private"
    assert outbound.calls[1]["msg_type"] == "image"


@pytest.mark.asyncio
async def test_draw_agent_tool_rechecks_scope_after_provider_before_capture() -> None:
    draw_store = _ScopeToggleDrawStore()
    billing, provider = _fake_billing()
    outbound = _FakeChannelOutbound()
    spawned: list[asyncio.Task[None]] = []
    enabled = True
    gate_calls: list[tuple[str, str]] = []

    async def scope_allowed(tenant_id: str, session_id: str) -> bool:
        gate_calls.append((tenant_id, session_id))
        return enabled

    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=_fake_channel_registry(outbound),
        billing=billing,
        register_background_task=spawned.append,
        scope_execution_allowed=scope_allowed,
    )

    result = await service.generate_group_image(
        _agent_session(),
        {"prompt": "一只戴墨镜的橘猫"},
    )
    assert result["accepted"] is True
    await asyncio.wait_for(draw_store.generation_started.wait(), timeout=1)
    enabled = False
    draw_store.finish_generation.set()
    await spawned[0]

    assert len(gate_calls) >= 3
    assert len(provider.reservations) == 1
    assert provider.captures == []
    assert provider.releases == provider.reservations
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_draw_agent_tool_accepts_quality_and_bills_metadata() -> None:
    draw_store = _FakeDrawStore()
    billing, provider = _fake_billing()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=channel_registry,
        billing=billing,
        register_background_task=_track,
        scope_execution_allowed=_allow_draw_scope,
    )

    result = await service.generate_group_image(
        _agent_session(),
        {"prompt": "一只戴墨镜的橘猫", "quality": "high"},
    )

    assert result["quality"] == "high"
    await spawned[0]

    assert draw_store.calls[0]["prompt"] == "一只戴墨镜的橘猫"
    assert draw_store.calls[0]["quality"] == "high"
    assert draw_store.calls[0]["trace_id"].endswith(":draw-agent")
    assert provider.reservations[0].resource.metadata["quality"] == "high"


@pytest.mark.asyncio
async def test_draw_agent_tool_rejects_invalid_quality() -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=channel_registry,
        scope_execution_allowed=_allow_draw_scope,
    )

    with pytest.raises(ValueError, match="quality"):
        await service.generate_group_image(
            _agent_session(),
            {"prompt": "一只戴墨镜的橘猫", "quality": "ultra"},
        )

    assert draw_store.calls == []


@pytest.mark.asyncio
async def test_draw_agent_tool_storage_error_rejects_without_scheduling_task() -> None:
    draw_store = _StorageUnavailableDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=channel_registry,
        register_background_task=_track,
        scope_execution_allowed=_allow_draw_scope,
    )

    with pytest.raises(ValueError) as excinfo:
        await service.generate_group_image(
            _agent_session(),
            {"prompt": "一只戴墨镜的橘猫"},
        )

    assert str(excinfo.value) == DRAW_CONFIG_ERROR_TEXT
    assert draw_store.storage_checks == 1
    assert draw_store.calls == []
    assert spawned == []
    assert outbound.calls == []


@pytest.mark.asyncio
async def test_draw_agent_tool_accepts_discord_group_session() -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound, channel=Channel.DISCORD)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=channel_registry,
        register_background_task=_track,
        scope_execution_allowed=_allow_draw_scope,
    )

    result = await service.generate_group_image(
        _discord_agent_session(),
        {"prompt": "一张 Discord 频道活动海报"},
    )

    assert result["accepted"] is True
    assert result["async"] is True
    assert len(spawned) == 1

    await spawned[0]

    assert draw_store.calls[0]["prompt"] == "一张 Discord 频道活动海报"
    assert len(outbound.calls) == 2
    target = outbound.calls[0]["target"]
    assert isinstance(target, ChannelTarget)
    assert target.channel == "discord"
    assert target.session_id == "discord-channel-1"
    assert target.session_kind == "group"
    assert target.sender_id == "discord-user-1"
    assert target.reply_to_message_id == "discord-msg-1"
    assert outbound.calls[1]["image_path"] == ""
    assert outbound.calls[1]["image_url"] == "http://127.0.0.1:18080/p/img/task/0"


@pytest.mark.asyncio
async def test_draw_agent_tool_uses_group_avatar_reference(monkeypatch) -> None:
    draw_store = _FakeDrawStore()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    async def _fake_resolve(*args, **kwargs) -> DrawAvatarReference:
        return DrawAvatarReference(
            query="千羽",
            display_name="千羽",
            wxid="wxid_qianyu",
            avatar_url="http://127.0.0.1:5080/ext/roster/avatars/wxid_qianyu",
            image_path="/tmp/wxbot-avatars/qianyu.jpg",
            source_label="avatar:wxid_qianyu",
        )

    monkeypatch.setattr("plugins.draw.agent.resolve_prompt_avatar_reference", _fake_resolve)
    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=channel_registry,
        register_background_task=_track,
        scope_execution_allowed=_allow_draw_scope,
    )

    result = await service.generate_group_image(
        _agent_session(),
        {"prompt": "基于群里千羽头像生成一个微信聊天记录"},
    )

    assert result["accepted"] is True
    await spawned[0]

    assert draw_store.calls[0]["image_url"] == ""
    assert draw_store.calls[0]["image_path"] == "/tmp/wxbot-avatars/qianyu.jpg"
    assert draw_store.calls[0]["prompt"] == "基于群里千羽头像生成一个微信聊天记录"
    assert draw_store.calls[0]["trace_id"].endswith(":draw-agent")
    assert draw_store.calls[0]["source_label"] == "avatar:wxid_qianyu"
    assert outbound.calls[1]["image_path"] == ""
    assert outbound.calls[1]["image_url"] == "http://127.0.0.1:18080/p/img/task/2"


@pytest.mark.asyncio
async def test_draw_agent_tool_failure_releases_billing_reservation() -> None:
    draw_store = _FailingDrawStore()
    billing, provider = _fake_billing()
    outbound = _FakeChannelOutbound()
    channel_registry = _fake_channel_registry(outbound)
    spawned: list[asyncio.Task[None]] = []

    def _track(task: asyncio.Task[None]) -> None:
        spawned.append(task)

    service = DrawAgentToolService(
        store=draw_store,
        channel_registry=channel_registry,
        billing=billing,
        register_background_task=_track,
        scope_execution_allowed=_allow_draw_scope,
    )

    result = await service.generate_group_image(
        _agent_session(),
        {"prompt": "一只戴墨镜的橘猫"},
    )

    assert result["accepted"] is True
    assert len(spawned) == 1

    await spawned[0]

    assert len(provider.reservations) == 1
    assert provider.captures == []
    assert len(provider.releases) == 1
    assert len(outbound.calls) == 1
    assert outbound.calls[0]["msg_type"] == "text"
    assert outbound.calls[0]["reply_text"] == DRAW_API_ERROR_TEXT
