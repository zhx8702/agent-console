from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.admin.authorization import AdminRole, Principal
from app.admin.route_permissions import RoutePermissionRegistry
from app.models.base import Base
from app.models.reliability import MessageEffectIntentRow
from app.models.social import (
    AuditEventRow,
    SocialGroupPolicyHistoryRow,
    SocialGroupPolicyRow,
    SocialMemberPolicyHistoryRow,
    SocialMemberPolicyRow,
    SocialParticipationEventRow,
    SocialPolicyIdempotencyRow,
    SocialScopeControlHistoryRow,
    SocialScopeControlRow,
    SocialTenantMemberControlRow,
    VoiceProfileHistoryRow,
    VoiceProfileRow,
)
from app.orchestrator.effect_handlers import EffectHandlerRegistry
from app.reliability import MessageEffectIntentRelay, MessageReliabilityStore
from app.social.effects import MemberMemoryForgetEffectHandler
from app.social.router import build_social_admin_router
from app.social.store import SocialPolicyStore


def _principal(*, tenants: tuple[str, ...] = ("tenant-a",)) -> Principal:
    return Principal(
        subject="operator-1",
        roles=(AdminRole.PLATFORM_ADMIN.value,),
        tenant_ids=tenants,
        auth_kind="session",
    )


async def _authorize(request: Request) -> Principal:
    principal = _principal(
        tenants=("*",) if request.headers.get("X-Test-Global") == "1" else ("tenant-a",)
    )
    request.state.admin_principal = principal
    return principal


@pytest.fixture
async def social_api() -> AsyncIterator[tuple[httpx.AsyncClient, async_sessionmaker, AsyncEngine]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        social_tables = [
            SocialGroupPolicyRow.__table__,
            SocialGroupPolicyHistoryRow.__table__,
            SocialMemberPolicyRow.__table__,
            SocialMemberPolicyHistoryRow.__table__,
            SocialParticipationEventRow.__table__,
            VoiceProfileRow.__table__,
            VoiceProfileHistoryRow.__table__,
            AuditEventRow.__table__,
            SocialPolicyIdempotencyRow.__table__,
            SocialScopeControlRow.__table__,
            SocialScopeControlHistoryRow.__table__,
            SocialTenantMemberControlRow.__table__,
            MessageEffectIntentRow.__table__,
        ]
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=social_tables,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(
        build_social_admin_router(
            SocialPolicyStore(factory),
            authorization_dependency=_authorize,
        )
    )
    # The isolated registry has no manifest entries: all six routes must carry
    # their own exact declarations or this startup validation fails closed.
    RoutePermissionRegistry(()).bind_and_validate(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client, factory, engine
    await engine.dispose()


def _group_payload(
    *,
    threshold: int = 60,
    group_enabled: bool = True,
    mention_sender_strategy: str = "never",
    prompt_context_retention_seconds: int = 3600,
) -> dict:
    return {
        "kill_switches": {
            "global_enabled": True,
            "tenant_enabled": True,
            "group_enabled": group_enabled,
        },
        "policy": {
            "threshold": threshold,
            "proactive_enabled": False,
            "mention_sender_strategy": mention_sender_strategy,
            "prompt_context_retention_seconds": prompt_context_retention_seconds,
        },
        "voice_profile": {
            "profile_id": "natural-v1",
            "enabled": True,
            "sample_source": "manual",
            "sample_scope": "none",
            "authorized_sample_session_ids": [],
            "authorization_reference": "manual-style-controls",
            "tone": "natural",
            "verbosity": "concise",
            "identity_disclosure": "contextual",
        },
        "change_reason": "unit test",
    }


@pytest.mark.asyncio
async def test_group_policy_requires_precondition_and_round_trips_etag(
    social_api,
) -> None:
    client, _factory, _engine = social_api
    path = "/v1/admin/tenants/tenant-a/groups/room%40chatroom/participation-policy"

    initial = await client.get(path)
    missing_precondition = await client.put(path, json=_group_payload())
    updated = await client.put(
        path,
        headers={
            "If-Match": initial.headers["etag"],
            "Idempotency-Key": "group-policy-create",
            "X-Trace-ID": "trace-social-1",
        },
        json=_group_payload(
            threshold=72,
            mention_sender_strategy="reply_or_ambiguous",
            prompt_context_retention_seconds=7200,
        ),
    )

    assert initial.status_code == 200
    assert initial.headers["etag"] == '"0"'
    assert initial.headers["cache-control"] == "no-store"
    assert initial.json()["version"] == 0
    assert initial.json()["kill_switches"]["group_enabled"] is True
    assert initial.json()["effective_enabled"] is False
    assert initial.json()["policy"]["proactive_enabled"] is False
    assert initial.json()["policy"]["mention_sender_strategy"] == "never"
    assert initial.json()["policy"]["prompt_context_retention_seconds"] == 3600
    assert missing_precondition.status_code == 428
    assert missing_precondition.json()["detail"] == "if_match_required"
    assert updated.status_code == 200
    assert updated.headers["etag"] == '"1"'
    assert updated.json()["version"] == 1
    assert updated.json()["policy"]["threshold"] == 72
    assert updated.json()["policy"]["mention_sender_strategy"] == "reply_or_ambiguous"
    assert updated.json()["policy"]["prompt_context_retention_seconds"] == 7200
    assert updated.json()["voice_profile"]["version"] == 1
    assert updated.json()["voice_profile"]["enabled"] is True


@pytest.mark.asyncio
async def test_group_policy_rejects_cross_group_voice_sample_scope_without_writing(
    social_api,
) -> None:
    client, factory, _engine = social_api
    path = "/v1/admin/tenants/tenant-a/groups/room%40chatroom/participation-policy"
    payload = _group_payload()
    payload["voice_profile"].update(
        {
            "sample_source": "authorized_group_samples",
            "sample_scope": "current_group",
            "authorized_sample_session_ids": ["other@chatroom"],
            "authorization_reference": "approval-do-not-echo",
        }
    )

    rejected = await client.put(
        path,
        headers={"If-Match": '"0"', "Idempotency-Key": "cross-scope-reject"},
        json=payload,
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"] == {"code": "voice_profile_sample_scope_invalid"}
    assert "other@chatroom" not in rejected.text
    assert "approval-do-not-echo" not in rejected.text
    async with factory() as db:
        assert await db.scalar(select(func.count(SocialGroupPolicyRow.version))) == 0
        assert await db.scalar(select(func.count(VoiceProfileHistoryRow.id))) == 0


@pytest.mark.asyncio
async def test_authorized_voice_profile_round_trips_history_and_rollback(
    social_api,
) -> None:
    client, factory, _engine = social_api
    base = "/v1/admin/tenants/tenant-a/groups/governed-room%40chatroom/participation-policy"
    governed = _group_payload()
    governed["voice_profile"].update(
        {
            "profile_id": "governed-voice",
            "sample_source": "authorized_group_samples",
            "sample_scope": "current_group",
            "authorized_sample_session_ids": ["governed-room@chatroom"],
            "authorization_reference": "approval-voice-42",
            "valid_from": "2026-07-18T00:00:00+00:00",
            "expires_at": "2026-08-18T00:00:00+00:00",
        }
    )
    created = await client.put(
        base,
        headers={"If-Match": '"0"', "Idempotency-Key": "governed-create"},
        json=governed,
    )

    replacement = _group_payload()
    replacement["voice_profile"].update(
        {
            "profile_id": "governed-voice",
            "enabled": False,
            "authorization_reference": "",
        }
    )
    replaced = await client.put(
        base,
        headers={"If-Match": '"1"', "Idempotency-Key": "governed-disable"},
        json=replacement,
    )
    rolled_back = await client.put(
        base,
        headers={"If-Match": '"2"', "Idempotency-Key": "governed-rollback"},
        json={"rollback_to_version": 1, "change_reason": "restore authorization"},
    )
    history = await client.get(f"{base}/history")

    assert created.status_code == 200
    assert replaced.status_code == 200
    assert rolled_back.status_code == 200
    restored = rolled_back.json()["voice_profile"]
    assert restored["version"] == 3
    assert restored["enabled"] is True
    assert restored["sample_source"] == "authorized_group_samples"
    assert restored["sample_scope"] == "current_group"
    assert restored["authorized_sample_session_ids"] == ["governed-room@chatroom"]
    assert restored["authorization_reference"] == "approval-voice-42"
    assert restored["valid_from"] == "2026-07-18T00:00:00Z"
    assert restored["expires_at"] == "2026-08-18T00:00:00Z"
    assert [item["version"] for item in history.json()["items"]] == [3, 2, 1]

    async with factory() as db:
        first_history = await db.scalar(
            select(VoiceProfileHistoryRow).where(
                VoiceProfileHistoryRow.session_id == "governed-room@chatroom",
                VoiceProfileHistoryRow.version == 1,
            )
        )
    assert first_history is not None
    assert first_history.snapshot_json["authorization_reference"] == ("approval-voice-42")
    assert "chat_text" not in first_history.snapshot_json
    assert "sample_text" not in first_history.snapshot_json


@pytest.mark.asyncio
async def test_voice_profile_history_records_an_immutable_removal_tombstone(
    social_api,
) -> None:
    client, _factory, _engine = social_api
    base = "/v1/admin/tenants/tenant-a/groups/voice-history-room"
    created = await client.put(
        f"{base}/participation-policy",
        headers={"If-Match": '"0"', "Idempotency-Key": "voice-create"},
        json=_group_payload(),
    )
    removal_payload = _group_payload()
    removal_payload["voice_profile"] = None
    removed = await client.put(
        f"{base}/participation-policy",
        headers={"If-Match": created.headers["etag"], "Idempotency-Key": "voice-remove"},
        json=removal_payload,
    )
    history = await client.get(f"{base}/voice-profile/history")

    assert removed.status_code == 200
    assert removed.json()["voice_profile"] is None
    assert history.status_code == 200
    assert [item["version"] for item in history.json()["items"]] == [2, 1]
    assert history.json()["items"][0]["change_summary"] == ["voice_profile:removed"]


@pytest.mark.asyncio
async def test_group_policy_idempotency_version_conflict_history_and_audit(
    social_api,
) -> None:
    client, factory, _engine = social_api
    path = "/v1/admin/tenants/tenant-a/groups/room-1/participation-policy"
    headers = {"If-Match": '"0"', "Idempotency-Key": "create-1"}

    created = await client.put(path, headers=headers, json=_group_payload(threshold=65))
    replay = await client.put(path, headers=headers, json=_group_payload(threshold=65))
    reused_key = await client.put(path, headers=headers, json=_group_payload(threshold=66))
    stale = await client.put(
        path,
        headers={"If-Match": '"0"'},
        json=_group_payload(threshold=67),
    )
    second = await client.put(
        path,
        headers={"If-Match": '"1"'},
        json=_group_payload(threshold=90),
    )
    rolled_back = await client.put(
        path,
        headers={"If-Match": '"2"'},
        json={"rollback_to_version": 1, "change_reason": "restore safe policy"},
    )

    assert created.status_code == 200
    assert replay.status_code == 200
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json() == created.json()
    assert reused_key.status_code == 409
    assert reused_key.json()["detail"]["code"] == "idempotency_conflict"
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "version_conflict",
        "expected_version": 0,
        "current_version": 1,
    }
    assert second.json()["version"] == 2
    assert rolled_back.status_code == 200
    assert rolled_back.json()["version"] == 3
    assert rolled_back.json()["policy"]["threshold"] == 65

    async with factory() as db:
        history_count = await db.scalar(select(func.count(SocialGroupPolicyHistoryRow.id)))
        audit_count = await db.scalar(select(func.count(AuditEventRow.id)))
        rollback_audit = await db.scalar(
            select(AuditEventRow).where(AuditEventRow.action == "participation_policy.rollback")
        )
    assert history_count == 3
    assert audit_count == 3
    assert rollback_audit is not None
    assert rollback_audit.policy_version == 3
    assert rollback_audit.before_state_json["version"] == 2
    assert rollback_audit.after_state_json["version"] == 3


@pytest.mark.asyncio
async def test_preview_uses_service_and_events_never_persist_chat_text(social_api) -> None:
    client, factory, _engine = social_api
    base = "/v1/admin/tenants/tenant-a/groups/room-1"

    rejected_text = await client.post(
        f"{base}/participation-preview",
        json={"mentioned_me": True, "chat_text": "do not persist me"},
    )
    preview = await client.post(
        f"{base}/participation-preview",
        headers={"X-Trace-ID": "trace-preview"},
        json={"mentioned_me": True, "message_id": "preview-message-1"},
    )
    events = await client.get(f"{base}/participation-events")

    assert rejected_text.status_code == 422
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    # A fresh deployment is shadow-only until both independent release and
    # tenant controls are explicitly enabled.
    assert preview.json()["status"] == "observe_only"
    assert preview.json()["policy_version"] == 0
    assert events.status_code == 200
    assert len(events.json()["items"]) == 1
    item = events.json()["items"][0]
    assert item["trace_id"] == "trace-preview"
    assert item["signal_summary"]["mentioned_me"] is True
    assert "message_id" not in item["signal_summary"]

    async with factory() as db:
        row = await db.scalar(select(SocialParticipationEventRow))
    assert row is not None
    assert not hasattr(row, "chat_text")
    assert not hasattr(row, "message_text")
    assert "preview-message-1" not in str(row.signal_summary_json)


@pytest.mark.asyncio
async def test_voice_profile_preview_reuses_guard_without_sending_or_persisting_text(
    social_api,
) -> None:
    client, factory, _engine = social_api
    base = "/v1/admin/tenants/tenant-a/groups/room-1"
    reply_text = "这波可以，先看这个思路🙂🙂。后面还有一段补充。"
    source_text = "群里刚才的临时问题，不应进入任何事件或档案"

    preview = await client.post(
        f"{base}/voice-profile/preview",
        json={
            "voice_profile": {
                **_group_payload()["voice_profile"],
                "phrase_preferences": ["这波可以", "这波可以"],
                "emoji_frequency": 0.15,
                "identity_disclosure": "always",
            },
            "reply_text": reply_text,
            "source_text": source_text,
        },
    )

    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    payload = preview.json()
    assert payload["applied"] is True
    assert payload["runtime_reason"] == "voice_profile_active"
    assert payload["output_text"].startswith("我是 AI 助手。")
    assert payload["identity_disclosed"] is True
    assert "identity_prefix_added" in payload["reason_codes"]
    assert "reply_text" not in payload
    assert "source_text" not in payload

    async with factory() as db:
        assert await db.scalar(select(func.count(SocialParticipationEventRow.id))) == 0
        assert await db.scalar(select(func.count(VoiceProfileRow.profile_id))) == 0
        assert await db.scalar(select(func.count(VoiceProfileHistoryRow.id))) == 0
        assert await db.scalar(select(func.count(AuditEventRow.id))) == 0


@pytest.mark.asyncio
async def test_member_privacy_defaults_are_conservative_and_support_rollback(
    social_api,
) -> None:
    client, _factory, _engine = social_api
    path = "/v1/admin/tenants/tenant-a/groups/room-1/members/member-1/privacy-policy"
    initial = await client.get(path)
    first_policy = {
        "memory_enabled": True,
        "allow_group_recall": False,
        "allow_private_recall": True,
        "proactive_participation_enabled": False,
        "soft_reply_opt_out": False,
        "no_group_mentions": False,
        "retention_days": 14,
        "audience_scope": "explicit",
        "allowed_session_ids": ["room-1"],
        "sensitive_memory_enabled": False,
        "correction_enabled": True,
        "deletion_enabled": True,
    }
    first = await client.put(
        path,
        headers={"If-Match": '"0"'},
        json={"policy": first_policy},
    )
    second_policy = {**first_policy, "memory_enabled": False, "retention_days": 7}
    second = await client.put(
        path,
        headers={"If-Match": '"1"'},
        json={"policy": second_policy},
    )
    rollback = await client.put(
        path,
        headers={"If-Match": '"2"'},
        json={"rollback_to_version": 1},
    )

    assert initial.headers["etag"] == '"0"'
    assert initial.json()["policy"]["memory_enabled"] is False
    assert initial.json()["policy"]["audience_scope"] == "private"
    assert first.status_code == 200
    assert second.json()["version"] == 2
    assert rollback.status_code == 200
    assert rollback.json()["version"] == 3
    assert rollback.json()["policy"] == first_policy


@pytest.mark.asyncio
async def test_tenant_member_opt_out_does_not_overwrite_group_configured_policy(
    social_api,
) -> None:
    client, _factory, _engine = social_api
    member_path = (
        "/v1/admin/tenants/tenant-a/groups/room-1/members/member-independent/privacy-policy"
    )
    tenant_member_path = "/v1/admin/tenants/tenant-a/members/member-independent/control"
    configured = {
        "memory_enabled": True,
        "allow_group_recall": True,
        "allow_private_recall": True,
        "proactive_participation_enabled": True,
        "soft_reply_opt_out": False,
        "no_group_mentions": False,
        "retention_days": 90,
        "audience_scope": "session",
        "allowed_session_ids": [],
        "sensitive_memory_enabled": False,
        "correction_enabled": True,
        "deletion_enabled": True,
    }
    await client.put(
        member_path,
        headers={"If-Match": '"0"', "Idempotency-Key": "member-configured"},
        json={"policy": configured},
    )
    opted_out = await client.put(
        tenant_member_path,
        headers={"If-Match": '"0"', "Idempotency-Key": "member-opt-out"},
        json={
            "control": {
                "memory_opt_out": True,
                "participation_opt_out": True,
                "no_group_mentions": True,
            }
        },
    )
    effective = await client.get(member_path)

    assert opted_out.status_code == 200
    assert effective.json()["configured_policy"] == configured
    assert effective.json()["policy"]["memory_enabled"] is False
    assert effective.json()["policy"]["proactive_participation_enabled"] is False
    assert effective.json()["policy"]["no_group_mentions"] is True

    cleared = await client.put(
        tenant_member_path,
        headers={"If-Match": opted_out.headers["etag"], "Idempotency-Key": "member-opt-in"},
        json={
            "control": {
                "memory_opt_out": False,
                "participation_opt_out": False,
                "no_group_mentions": False,
            }
        },
    )
    restored = await client.get(member_path)

    assert cleared.status_code == 200
    assert restored.json()["policy"] == configured
    assert restored.json()["configured_policy"] == configured


@pytest.mark.asyncio
async def test_exact_social_routes_enforce_principal_tenant_scope(social_api) -> None:
    client, _factory, _engine = social_api

    forbidden = await client.get("/v1/admin/tenants/other/groups/room-1/participation-policy")

    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "tenant_scope_forbidden"


@pytest.mark.asyncio
async def test_release_tenant_and_group_controls_are_independent_with_groups_open_by_default(
    social_api,
) -> None:
    client, _factory, _engine = social_api
    group_path = "/v1/admin/tenants/tenant-a/groups/room-1/participation-policy"
    release_path = "/v1/admin/social/release-control"
    tenant_path = "/v1/admin/tenants/tenant-a/participation-control"
    global_headers = {"X-Test-Global": "1"}

    initial = await client.get(group_path)
    assert initial.json()["effective_enabled"] is False
    assert initial.json()["kill_switches"]["group_enabled"] is True
    assert initial.json()["policy"]["rollout_stage"] == "shadow"

    release = await client.put(
        release_path,
        headers={
            **global_headers,
            "If-Match": '"0"',
            "Idempotency-Key": "enable-global-release",
        },
        json={
            "control": {"enabled": True, "rollout_stage": "contextual"},
            "change_reason": "start release",
        },
    )
    after_release = await client.get(group_path)
    assert release.status_code == 200
    assert after_release.json()["kill_switches"]["global_enabled"] is True
    assert after_release.json()["effective_enabled"] is False
    assert after_release.json()["policy"]["rollout_stage"] == "shadow"

    tenant = await client.put(
        tenant_path,
        headers={"If-Match": '"0"', "Idempotency-Key": "enable-tenant-release"},
        json={
            "control": {"enabled": True, "rollout_stage": "privacy_5"},
            "change_reason": "tenant canary",
        },
    )
    enabled_by_broad_controls = await client.get(group_path)
    assert tenant.status_code == 200
    assert enabled_by_broad_controls.json()["effective_enabled"] is True
    assert enabled_by_broad_controls.json()["kill_switches"] == {
        "global_enabled": True,
        "tenant_enabled": True,
        "group_enabled": True,
    }
    assert enabled_by_broad_controls.json()["policy"]["rollout_stage"] == "privacy_5"

    another_group = await client.get(
        "/v1/admin/tenants/tenant-a/groups/room-2/participation-policy"
    )
    assert another_group.json()["effective_enabled"] is True
    assert another_group.json()["kill_switches"]["group_enabled"] is True

    group_update = _group_payload(group_enabled=False)
    explicitly_disabled = await client.put(
        group_path,
        headers={"If-Match": '"0"', "Idempotency-Key": "disable-group-only"},
        json=group_update,
    )
    assert explicitly_disabled.status_code == 200
    assert explicitly_disabled.json()["effective_enabled"] is False
    assert explicitly_disabled.json()["kill_switches"] == {
        "global_enabled": True,
        "tenant_enabled": True,
        "group_enabled": False,
    }

    group_update = _group_payload()
    group_update["kill_switches"]["global_enabled"] = False
    group_update["kill_switches"]["tenant_enabled"] = False
    saved_group = await client.put(
        group_path,
        headers={"If-Match": '"1"', "Idempotency-Key": "enable-group-only"},
        json=group_update,
    )
    assert saved_group.status_code == 200
    assert saved_group.json()["effective_enabled"] is True
    assert saved_group.json()["kill_switches"] == {
        "global_enabled": True,
        "tenant_enabled": True,
        "group_enabled": True,
    }


@pytest.mark.asyncio
async def test_event_api_filters_exact_reason_and_paginates_with_cursor(social_api) -> None:
    client, factory, _engine = social_api
    async with factory() as db:
        async with db.begin():
            db.add_all(
                [
                    SocialParticipationEventRow(
                        id="event-1",
                        tenant_id="tenant-a",
                        session_id="room-1",
                        policy_version=2,
                        event_kind="runtime",
                        runtime_stage="revalidation",
                        delivery_stage="cancelled",
                        status="cancel",
                        score=65,
                        reason_codes_json=["quiet_hours_at_send"],
                        signal_summary_json={"quiet": True},
                    ),
                    SocialParticipationEventRow(
                        id="event-2",
                        tenant_id="tenant-a",
                        session_id="room-1",
                        policy_version=2,
                        event_kind="runtime",
                        runtime_stage="decision",
                        delivery_stage="not_applicable",
                        status="defer",
                        score=65,
                        reason_codes_json=["quiet_hours"],
                        signal_summary_json={"quiet": True},
                    ),
                    SocialParticipationEventRow(
                        id="event-3",
                        tenant_id="tenant-a",
                        session_id="room-1",
                        policy_version=1,
                        event_kind="preview",
                        status="observe_only",
                        score=0,
                        reason_codes_json=["participation_disabled"],
                        signal_summary_json={},
                    ),
                ]
            )
    base = "/v1/admin/tenants/tenant-a/groups/room-1/participation-events"
    exact = await client.get(base, params={"reason": "quiet_hours"})
    stage = await client.get(
        base,
        params={"runtime_stage": "revalidation", "delivery_stage": "cancelled"},
    )
    first = await client.get(base, params={"limit": 1})

    assert [item["event_id"] for item in exact.json()["items"]] == ["event-2"]
    assert [item["event_id"] for item in stage.json()["items"]] == ["event-1"]
    assert len(first.json()["items"]) == 1
    assert first.json()["next_cursor"]
    second = await client.get(
        base,
        params={"limit": 1, "cursor": first.json()["next_cursor"]},
    )
    assert len(second.json()["items"]) == 1
    assert second.json()["items"][0]["event_id"] != first.json()["items"][0]["event_id"]


@pytest.mark.asyncio
async def test_member_erasure_intent_runs_through_relay_once_and_stays_fail_closed(
    social_api,
) -> None:
    client, factory, _engine = social_api
    path = "/v1/admin/tenants/tenant-a/members/member-1/control"
    requested = await client.put(
        path,
        headers={
            "If-Match": '"0"',
            "Idempotency-Key": "erase-member-1",
            "X-Trace-ID": "trace-member-erasure",
        },
        json={
            "control": {
                "memory_opt_out": False,
                "participation_opt_out": False,
                "no_group_mentions": False,
            },
            "request_memory_deletion": True,
            "change_reason": "member request",
        },
    )
    assert requested.status_code == 200
    assert requested.json()["control"]["memory_opt_out"] is True
    assert requested.json()["deletion_state"] == "requested"

    async with factory() as db:
        intent = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
    assert intent.status == "prepared"
    assert intent.owner == "core"
    assert intent.producer_owner == "core"
    assert intent.effect_type == "forget_member"
    assert intent.context["event"]["message"] == {"type": "event", "content": ""}
    assert "content" not in intent.payload

    # A follow-up update cannot reopen recall while the durable erasure is in flight.
    still_closed = await client.put(
        path,
        headers={"If-Match": '"1"', "Idempotency-Key": "edit-during-erasure"},
        json={
            "control": {
                "memory_opt_out": False,
                "participation_opt_out": False,
                "no_group_mentions": False,
            },
            "request_memory_deletion": False,
        },
    )
    assert still_closed.status_code == 200
    assert still_closed.json()["control"]["memory_opt_out"] is True
    assert still_closed.json()["deletion_state"] == "requested"

    calls: list[dict[str, str]] = []

    class _MemoryStore:
        async def forget_member(self, **kwargs: str) -> int:
            calls.append(dict(kwargs))
            return 3

    policy_store = SocialPolicyStore(factory)
    registry = EffectHandlerRegistry()
    registry.register(
        "forget_member",
        "core",
        MemberMemoryForgetEffectHandler(_MemoryStore(), policy_store),
    )
    relay = MessageEffectIntentRelay(
        MessageReliabilityStore(factory),
        registry,
        worker_id="member-erasure-test",
        handler_timeout_seconds=1,
    )

    assert await relay.drain_once() == 1
    assert await relay.drain_once() == 0
    assert calls == [
        {
            "tenant_id": "tenant-a",
            "session_id": intent.session_id,
            "user_id": "member-1",
            "idempotency_key": "member-memory-delete:erase-member-1",
        }
    ]
    completed = await client.get(path)
    assert completed.json()["deletion_state"] == "completed"
    assert completed.json()["control"]["memory_opt_out"] is True
    assert completed.json()["version"] == 3
    assert completed.headers["etag"] == '"3"'
    assert completed.json()["updated_by"] == "effect-intent-relay"
    async with factory() as db:
        final_intent = (await db.execute(select(MessageEffectIntentRow))).scalar_one()
        completed_audit = await db.scalar(
            select(AuditEventRow).where(
                AuditEventRow.action == "tenant_member_memory_deletion.completed"
            )
        )
    assert final_intent.status == "completed"
    assert completed_audit is not None
    assert completed_audit.before_state_json == {"deletion_state": "requested"}
    assert completed_audit.after_state_json == {"deletion_state": "completed"}
