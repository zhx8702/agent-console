from __future__ import annotations

from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from app.channel.models import (
    ChannelFile,
    ChannelSendResult,
    ChannelTarget,
)
from app.channel.registry import ChannelRegistry
from app.common.types import ReplySegment
from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COLUMN_CONTRACTS,
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_REVISION,
)
from app.social.contracts import (
    GroupParticipationPolicyDocument,
    KillSwitches,
    ParticipationPolicyValues,
)
from plugins.wxbot.bridge_delivery import _sdk_content_payload
from plugins.wxbot.channel import WxbotChannelOutbound
from plugins.wxbot.reply_serialization import _segment_to_queue_payload
from plugins.wxbot.store import WxbotStore

_MD5 = "9e107d9d372bb6826bd81d3542a419d6"
_SHA256 = "1d3c43633f2b30c61186f81bb9d635327d0485094d65619745c0bf44f42996ae"
_WINDOWS_FILE = r"E:\wxbot-share\report.pdf"


def test_outbound_file_migration_updates_runtime_contract(monkeypatch) -> None:
    migration = import_module(
        "migrations.versions.20260729_0045_wxbot_outbound_files"
    )
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()
    rendered = output.getvalue()

    assert migration.revision == "0045_wxbot_outbound_files"
    assert migration.down_revision == "0044_persona_profile_catalog"
    assert RUNTIME_SCHEMA_COMPATIBILITY_LEVEL == 9
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [
        RUNTIME_SCHEMA_REVISION
    ]
    for name in ("file_path", "file_name", "file_size", "file_md5", "file_sha256"):
        assert f"ADD COLUMN {name}" in rendered
    assert "compatibility_level = 8" in rendered
    assert (
        "plugin_wxbot_reply_queue",
        "file_size",
        True,
    ) in RUNTIME_SCHEMA_COLUMN_CONTRACTS


def test_file_reply_segment_serializes_sdk_local_file_metadata() -> None:
    payload = _segment_to_queue_payload(
        ReplySegment(
            content="",
            metadata={
                "msg_type": "file",
                "file_path": _WINDOWS_FILE,
                "file_name": "report.pdf",
                "file_size": 0,
                "file_md5": _MD5.upper(),
                "file_sha256": _SHA256.upper(),
            },
        )
    )

    assert payload == {
        "msg_type": "file",
        "reply_text": "",
        "image_path": "",
        "image_url": "",
        "file_path": _WINDOWS_FILE,
        "file_name": "report.pdf",
        "file_size": 0,
        "file_md5": _MD5,
        "file_sha256": _SHA256,
    }


@pytest.mark.parametrize(
    "metadata",
    [
        {"msg_type": "file", "file_path": "relative/report.pdf"},
        {
            "msg_type": "file",
            "file_path": _WINDOWS_FILE,
            "file_url": "https://example.test/report.pdf",
        },
        {"msg_type": "file", "file_path": _WINDOWS_FILE, "file_size": -1},
    ],
)
def test_file_reply_segment_rejects_unsupported_locators(
    metadata: dict[str, Any],
) -> None:
    assert _segment_to_queue_payload(ReplySegment(metadata=metadata)) is None


@pytest.mark.asyncio
async def test_store_persists_nullable_file_assertions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        calls.append({"sql": sql, "params": dict(params or {})})
        if sql.startswith("INSERT INTO plugin_wxbot_reply_queue"):
            return [{"id": 45}]
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)

    reply_id = await WxbotStore(SimpleNamespace()).enqueue_reply(
        tenant_id="demo",
        session_id="wxid-recipient",
        session_name="收件人",
        sender_name="客服",
        reply_text="",
        msg_type="file",
        file_path=_WINDOWS_FILE,
        file_name="report.pdf",
        file_size=None,
        file_md5=_MD5,
        file_sha256=_SHA256,
    )

    assert reply_id == 45
    insert = calls[0]
    assert "file_path, file_name, file_size, file_md5, file_sha256" in insert["sql"]
    assert insert["params"]["file_path"] == _WINDOWS_FILE
    assert insert["params"]["file_size"] is None
    assert insert["params"]["file_md5"] == _MD5
    assert insert["params"]["file_sha256"] == _SHA256


@pytest.mark.asyncio
async def test_wxbot_channel_enqueues_file_without_remote_url() -> None:
    class QueueStore:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def enqueue_reply(self, **kwargs: Any) -> int:
            self.calls.append(dict(kwargs))
            return 81

    store = QueueStore()
    outbound = WxbotChannelOutbound(store)  # type: ignore[arg-type]
    result = await outbound.send_file(
        ChannelTarget(
            tenant_id="demo",
            channel="wechat",
            session_id="wxid-recipient",
        ),
        ChannelFile(
            file_path=_WINDOWS_FILE,
            file_name="report.pdf",
            file_size=0,
            file_sha256=_SHA256,
        ),
    )

    assert result.message_id == "81"
    assert store.calls[0]["msg_type"] == "file"
    assert store.calls[0]["file_path"] == _WINDOWS_FILE
    assert store.calls[0]["file_size"] == 0
    assert "file_url" not in store.calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_wxbot_group_file_send_requires_explicit_group_switch(
    enabled: bool,
) -> None:
    class QueueStore:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def enqueue_reply(self, **kwargs: Any) -> int:
            self.calls.append(dict(kwargs))
            return 82

    class PolicyStore:
        async def get_group_policy(
            self,
            tenant_id: str,
            session_id: str,
        ) -> GroupParticipationPolicyDocument:
            return GroupParticipationPolicyDocument(
                tenant_id=tenant_id,
                session_id=session_id,
                version=1,
                kill_switches=KillSwitches(),
                effective_enabled=True,
                policy=ParticipationPolicyValues(file_send_enabled=enabled),
            )

    store = QueueStore()
    outbound = WxbotChannelOutbound(  # type: ignore[arg-type]
        store,
        social_policy_store=PolicyStore(),
    )
    result = await outbound.send_file(
        ChannelTarget(
            tenant_id="demo",
            channel="wechat",
            session_id="room@chatroom",
            session_kind="group",
        ),
        ChannelFile(file_path=_WINDOWS_FILE, file_name="report.pdf"),
    )

    if enabled:
        assert result.message_id == "82"
        assert len(store.calls) == 1
    else:
        assert result.message_id == ""
        assert result.metadata == {
            "suppressed": True,
            "reason": "group_file_send_disabled",
        }
        assert store.calls == []


@pytest.mark.asyncio
async def test_owned_registry_facade_gates_file_send() -> None:
    class Provider:
        def __init__(self) -> None:
            self.files: list[ChannelFile] = []

        async def get_session_policy(self, target: ChannelTarget) -> dict:
            return {}

        async def send_text(self, target, text, options=None) -> ChannelSendResult:
            return ChannelSendResult()

        async def send_image(self, target, media, options=None) -> ChannelSendResult:
            return ChannelSendResult()

        async def send_file(
            self,
            target: ChannelTarget,
            file: ChannelFile,
            options=None,
        ) -> ChannelSendResult:
            self.files.append(file)
            return ChannelSendResult(message_id="sdk-file-1")

    checked: list[tuple[str, str]] = []

    async def owner_gate(owner: str, target: ChannelTarget) -> bool:
        checked.append((owner, target.session_id))
        return True

    provider = Provider()
    registry = ChannelRegistry(owner_gate=owner_gate)
    registry.register_outbound("wechat", provider, owner="wxbot")
    target = ChannelTarget(
        tenant_id="demo",
        channel="wechat",
        session_id="wxid-recipient",
    )
    result = await registry.require_outbound("wechat").send_file(
        target,
        ChannelFile(file_path=_WINDOWS_FILE),
    )

    assert result.message_id == "sdk-file-1"
    assert checked == [("wxbot", "wxid-recipient")]
    assert provider.files == [ChannelFile(file_path=_WINDOWS_FILE)]


def test_bridge_file_content_preserves_zero_and_omits_absent_assertions() -> None:
    assert _sdk_content_payload(
        {
            "reply_text": "",
            "file_path": _WINDOWS_FILE,
            "file_name": "empty.txt",
            "file_size": 0,
            "file_md5": "",
            "file_sha256": "",
        },
        msg_type="file",
    ) == {
        "msg_type": "file",
        "text": "",
        "file_path": _WINDOWS_FILE,
        "file_name": "empty.txt",
        "file_size": 0,
    }

    assert _sdk_content_payload(
        {
            "reply_text": "",
            "file_path": _WINDOWS_FILE,
            "file_size": None,
            "file_md5": _MD5,
            "file_sha256": _SHA256,
        },
        msg_type="file",
    ) == {
        "msg_type": "file",
        "text": "",
        "file_path": _WINDOWS_FILE,
        "file_md5": _MD5,
        "file_sha256": _SHA256,
    }
