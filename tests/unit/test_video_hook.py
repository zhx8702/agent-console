from __future__ import annotations

from pathlib import Path

import pytest

from app.billing import BillingCoordinator, BillingQuote, BillingReservation
from app.billing.models import BillingResource, BillingSubject
from app.channel import ChannelRegistry, ChannelSendResult
from app.commands import CommandRegistryService
from app.common.types import Channel, InboundEvent, Message, Role, Session, Turn
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort
from plugins.commands.hooks import CommandCenterHook
from plugins.video.agent import (
    VIDEO_ACCEPTED_TEXT,
    VIDEO_DELIVERY_SUPPRESSED_TEXT,
    VideoAgentToolService,
)
from plugins.video.hooks import (
    VIDEO_HELP_TEXT,
    _parse_video_command_args,
    build_video_command_definitions,
)
from plugins.video.store import VideoApiError, VideoResult


class _FakeCommandStore:
    async def get_config(
        self,
        tenant_id: str,
        *,
        catalog: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "tenant_id": tenant_id,
            "admin_user_ids": [],
            "user_commands": ["/video", "/视频"],
            "admin_commands": [],
            "catalog": catalog,
        }


class _FakeVideoService:
    def __init__(self, *, accepted_reply_enqueued: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.accepted_reply_enqueued = accepted_reply_enqueued

    async def accept_video_command(self, session: Session, **kwargs: object) -> dict[str, object]:
        self.calls.append({"session": session, **kwargs})
        return {
            "message": VIDEO_ACCEPTED_TEXT,
            "accepted_reply_enqueued": self.accepted_reply_enqueued,
        }


class _FakeBillingProvider:
    name = "credits"

    def __init__(self) -> None:
        self.reservations: list[BillingReservation] = []
        self.captures: list[BillingReservation] = []
        self.releases: list[BillingReservation] = []

    async def quote(
        self,
        subject: BillingSubject,
        resource: BillingResource,
    ) -> BillingQuote:
        return BillingQuote(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=10,
            currency="积分",
        )

    async def reserve(
        self,
        subject: BillingSubject,
        resource: BillingResource,
    ) -> BillingReservation:
        reservation = BillingReservation(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=10,
            currency="积分",
            reservation_id=f"reservation-{len(self.reservations) + 1}",
        )
        self.reservations.append(reservation)
        return reservation

    async def capture(self, reservation: BillingReservation, *, amount: int | None = None):
        _ = amount
        self.captures.append(reservation)
        return None

    async def release(self, reservation: BillingReservation) -> None:
        self.releases.append(reservation)


class _FakeVideoStore:
    def __init__(self, tmp_path: Path, error: Exception | None = None) -> None:
        self.path = tmp_path / "video.mp4"
        self.path.write_bytes(b"video")
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def generate_video(self, prompt: str, *, duration: int, resolution: str):
        self.calls.append(
            {"prompt": prompt, "duration": duration, "resolution": resolution}
        )
        if self.error is not None:
            raise self.error
        return VideoResult(
            video_id="video-1",
            prompt=prompt,
            local_path=str(self.path),
            media_type="video/mp4",
        )


class _FakeVideoOutbound:
    def __init__(self, *, file_result: ChannelSendResult | None = None) -> None:
        self.files: list[dict[str, object]] = []
        self.texts: list[str] = []
        self.text_options: list[object] = []
        self.file_result = file_result or ChannelSendResult(provider="fake")

    async def send_file(self, target, file, options=None):
        self.files.append({"target": target, "file": file, "options": options})
        return self.file_result

    async def send_text(self, target, text, options=None):
        _ = target
        self.texts.append(text)
        self.text_options.append(options)
        return ChannelSendResult(provider="fake")


def _ctx(message: str) -> PipelineContext:
    event = InboundEvent(
        message_id="msg-video-1",
        tenant_id="tenant-a",
        channel=Channel.WECHAT,
        user_id="user-a",
        session_id="room@chatroom",
        message=Message(content=message),
        trace_id="trace-video-1",
        metadata={
            "session_kind": "group",
            "session_name": "测试群",
            "sender_name": "测试用户",
        },
    )
    session = Session(
        tenant_id="tenant-a",
        session_id="room@chatroom",
        user_id="user-a",
        channel=Channel.WECHAT,
        adapter_id="wechat-sdk",
        connection_id="connection-video-test",
        metadata={
            "session_kind": "group",
            "session_name": "测试群",
            "sender_name": "测试用户",
        },
    )
    return PipelineContext(event=event, trace_id="trace-video-1", session=session)


def test_video_command_parser_uses_defaults_and_extracts_parameters() -> None:
    parsed = _parse_video_command_args(
        ["duration=10", "resolution=480p", "海边日落", "镜头推进"]
    )

    assert parsed.duration == 10
    assert parsed.resolution == "480p"
    assert parsed.args == ["海边日落", "镜头推进"]


def test_video_command_parser_rejects_invalid_duration() -> None:
    with pytest.raises(ValueError, match="1 到 15"):
        _parse_video_command_args(["--duration", "16", "海边日落"])


@pytest.mark.asyncio
async def test_video_command_passes_parameters_to_service_and_reserves_credits() -> None:
    service = _FakeVideoService()
    registry = CommandRegistryService()
    registry.register(build_video_command_definitions(service))
    billing = BillingCoordinator()
    provider = _FakeBillingProvider()
    billing.register_provider(provider)
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)

    ctx = _ctx("/video duration=8 resolution=1080p 海边日落")
    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == VIDEO_ACCEPTED_TEXT
    assert service.calls[0]["prompt"] == "海边日落"
    assert service.calls[0]["duration"] == 8
    assert service.calls[0]["resolution"] == "1080p"
    reservation = service.calls[0]["reservation"]
    assert isinstance(reservation, BillingReservation)
    assert reservation.resource.operation == "/video"
    assert reservation.resource.metadata["duration"] == 8
    assert reservation.resource.metadata["resolution"] == "1080p"
    assert ctx.extras["_billing_command_deferred"] is True


@pytest.mark.asyncio
async def test_video_command_does_not_publish_duplicate_acceptance() -> None:
    service = _FakeVideoService(accepted_reply_enqueued=True)
    registry = CommandRegistryService()
    registry.register(build_video_command_definitions(service))
    hook = CommandCenterHook(_FakeCommandStore(), registry)

    ctx = _ctx("/video 海边日落")
    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == ""
    assert ctx.extras["suppress_outbound"] is True
    assert ctx.extras["skip_assistant_turn"] is True


@pytest.mark.asyncio
async def test_video_command_without_prompt_returns_help_without_reserving() -> None:
    service = _FakeVideoService()
    registry = CommandRegistryService()
    registry.register(build_video_command_definitions(service))
    billing = BillingCoordinator()
    provider = _FakeBillingProvider()
    billing.register_provider(provider)
    hook = CommandCenterHook(_FakeCommandStore(), registry, billing)

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(_ctx("/video duration=8"))

    assert excinfo.value.reply_text == VIDEO_HELP_TEXT
    assert len(provider.reservations) == 1
    assert provider.releases == provider.reservations
    assert service.calls == []


@pytest.mark.asyncio
async def test_video_command_job_captures_reserved_credits_after_generation(
    tmp_path: Path,
) -> None:
    store = _FakeVideoStore(tmp_path)
    outbound = _FakeVideoOutbound()
    channel_registry = ChannelRegistry()
    channel_registry.register_outbound(
        "wechat",
        outbound,
        tenant_id="tenant-a",
        connection_id="connection-video-test",
        adapter_id="wechat-sdk",
    )
    billing = BillingCoordinator()
    provider = _FakeBillingProvider()
    billing.register_provider(provider)
    reservation = await billing.provider("credits").reserve(  # type: ignore[union-attr]
        BillingSubject(
            tenant_id="tenant-a",
            session_id="room@chatroom",
            user_id="user-a",
            display_name="测试用户",
        ),
        BillingResource(
            kind="command",
            operation="/video",
            reference="trace-video-command",
            metadata={"command": "/video", "duration": 8, "resolution": "480p"},
        ),
    )
    spawned: list[object] = []

    def track(task) -> None:
        spawned.append(task)

    async def scope_allowed(tenant_id: str, session_id: str) -> bool:
        _ = (tenant_id, session_id)
        return True

    service = VideoAgentToolService(
        store=store,  # type: ignore[arg-type]
        channel_registry=channel_registry,
        billing=billing,
        register_background_task=track,
        scope_execution_allowed=scope_allowed,
    )
    session = _ctx("/video").session
    assert session is not None

    accepted = await service.accept_video_command(
        session,
        prompt="海边日落",
        duration=8,
        resolution="480p",
        reservation=reservation,
        trace_id="trace-video-command",
    )
    assert accepted["duration"] == 8
    assert accepted["accepted_reply_enqueued"] is True
    assert accepted["suppress_final_reply"] is True
    assert outbound.texts == [VIDEO_ACCEPTED_TEXT]
    assert outbound.text_options[0].idempotency_key.endswith("-accepted")
    assert len(spawned) == 1
    await spawned[0]

    assert store.calls == [
        {"prompt": "海边日落", "duration": 8, "resolution": "480p"}
    ]
    assert provider.captures == [reservation]
    assert provider.releases == []
    assert len(outbound.files) == 1
    assert outbound.files[0]["target"].connection_id == "connection-video-test"


@pytest.mark.asyncio
async def test_video_job_reports_file_delivery_suppression_instead_of_success(
    tmp_path: Path,
) -> None:
    store = _FakeVideoStore(tmp_path)
    outbound = _FakeVideoOutbound(
        file_result=ChannelSendResult(
            provider="wxbot",
            metadata={
                "suppressed": True,
                "reason": "group_file_send_disabled",
            },
        )
    )
    channel_registry = ChannelRegistry()
    channel_registry.register_outbound(
        "wechat",
        outbound,
        tenant_id="tenant-a",
        connection_id="connection-video-test",
        adapter_id="wechat-sdk",
    )
    spawned: list[object] = []

    async def scope_allowed(tenant_id: str, session_id: str) -> bool:
        _ = (tenant_id, session_id)
        return True

    service = VideoAgentToolService(
        store=store,  # type: ignore[arg-type]
        channel_registry=channel_registry,
        register_background_task=spawned.append,
        scope_execution_allowed=scope_allowed,
    )
    session = _ctx("/video").session
    assert session is not None

    accepted = await service.accept_video_command(
        session,
        prompt="海边日落",
        duration=8,
        resolution="480p",
        reservation=None,
        trace_id="trace-video-delivery-suppressed",
    )
    assert accepted["accepted_reply_enqueued"] is True
    await spawned[0]

    assert len(outbound.files) == 1
    assert outbound.texts == [VIDEO_ACCEPTED_TEXT, VIDEO_DELIVERY_SUPPRESSED_TEXT]


@pytest.mark.asyncio
async def test_agent_video_uses_inbound_prompt_without_llm_rewriting(
    tmp_path: Path,
) -> None:
    store = _FakeVideoStore(tmp_path)
    outbound = _FakeVideoOutbound()
    channel_registry = ChannelRegistry()
    channel_registry.register_outbound(
        "wechat",
        outbound,
        tenant_id="tenant-a",
        connection_id="connection-video-test",
        adapter_id="wechat-sdk",
    )

    async def scope_allowed(tenant_id: str, session_id: str) -> bool:
        _ = (tenant_id, session_id)
        return True

    spawned: list[object] = []
    service = VideoAgentToolService(
        store=store,  # type: ignore[arg-type]
        channel_registry=channel_registry,
        register_background_task=spawned.append,
        scope_execution_allowed=scope_allowed,
    )
    session = _ctx("帮我生成一段海边日落视频").session
    assert session is not None
    session.turns.append(
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="清理后的内容",
            metadata={"wxbot_normalized_content": "帮我生成一段海边日落视频"},
        )
    )

    accepted = await service.generate_group_video(
        session,
        {
            "prompt": "LLM 包装后的主体、动作、镜头和风格",
            "duration": 6,
            "resolution": "720p",
        },
    )
    await spawned[0]

    assert accepted["prompt"] == "帮我生成一段海边日落视频"
    assert store.calls == [
        {
            "prompt": "帮我生成一段海边日落视频",
            "duration": 6,
            "resolution": "720p",
        }
    ]


@pytest.mark.asyncio
async def test_video_job_releases_reserved_credits_when_generation_fails(
    tmp_path: Path,
) -> None:
    store = _FakeVideoStore(tmp_path, VideoApiError("upstream failed"))
    outbound = _FakeVideoOutbound()
    channel_registry = ChannelRegistry()
    channel_registry.register_outbound(
        "wechat",
        outbound,
        tenant_id="tenant-a",
        connection_id="connection-video-test",
        adapter_id="wechat-sdk",
    )
    billing = BillingCoordinator()
    provider = _FakeBillingProvider()
    billing.register_provider(provider)
    reservation = BillingReservation(
        provider="credits",
        subject=BillingSubject(
            tenant_id="tenant-a",
            session_id="room@chatroom",
            user_id="user-a",
            display_name="测试用户",
        ),
        resource=BillingResource(
            kind="command",
            operation="/video",
            reference="trace-video-failure",
            metadata={"command": "/video"},
        ),
        amount=10,
        currency="积分",
        reservation_id="reservation-failure",
    )
    spawned: list[object] = []

    async def scope_allowed(tenant_id: str, session_id: str) -> bool:
        _ = (tenant_id, session_id)
        return True

    service = VideoAgentToolService(
        store=store,  # type: ignore[arg-type]
        channel_registry=channel_registry,
        billing=billing,
        register_background_task=spawned.append,
        scope_execution_allowed=scope_allowed,
    )
    session = _ctx("/video").session
    assert session is not None

    await service.accept_video_command(
        session,
        prompt="海边日落",
        duration=8,
        resolution="480p",
        reservation=reservation,
        trace_id="trace-video-failure",
    )
    assert outbound.texts == [VIDEO_ACCEPTED_TEXT]
    await spawned[0]

    assert provider.captures == []
    assert provider.releases == [reservation]
    assert outbound.texts == [VIDEO_ACCEPTED_TEXT, "视频生成失败了，请稍后再试。"]
    assert outbound.text_options[0].idempotency_key.endswith("-accepted")
    assert outbound.text_options[1].idempotency_key.endswith("-failure")
