from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.admin.authorization import AdminRole, Principal
from app.agent.engine import AgentCapabilityEngine
from app.agent.registry import AgentToolDefinition, AgentToolRegistry
from app.social.contracts import (
    GroupParticipationPolicyDocument,
    KillSwitches,
    ParticipationPolicyValues,
)
from plugins.repeater.store import RepeaterConfigMutation
from plugins.wxbot import router as wxbot_router
from plugins.wxbot.media_ids import issue_media_id
from plugins.wxbot.router import build_wxbot_router
from plugins.wxbot.store import (
    ReplyPolicyIdempotencyConflictError,
    WxbotPolicyMutation,
    WxbotPolicyVersionConflictError,
    compose_reply_policy_aggregate,
)


class _FakeStore:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            admin_bearer_token="token",
            admin_allow_bearer_fallback=True,
            admin_session_cookie_name="agent_console_admin",
            wxbot_default_tenant_id="default",
            wxbot_sdk_url="http://127.0.0.1:5080",
            channel_connection_id="",
            bus_inbound_stream="cs:inbound",
            wxbot_bridge_poll_interval=3.0,
            wxbot_bridge_send_interval=2.0,
            wxbot_report_stage_timeout_seconds=30.0,
            wxbot_daily_report_footer="",
        )
        self.policies: dict[tuple[str, str], dict[str, object]] = {}
        self.global_policies: dict[str, dict[str, object]] = {}
        self.report_subscriptions: dict[tuple[str, str], dict[str, object]] = {
            ("default", "room@chatroom"): {
                "tenant_id": "default",
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "daily_enabled": True,
                "weekly_enabled": True,
                "monthly_enabled": False,
                "daily_hour": 9,
                "weekly_day": 1,
                "weekly_hour": 9,
                "monthly_day": 1,
                "tz": "Asia/Shanghai",
            }
        }
        self.report_jobs: dict[int, dict[str, object]] = {}
        self.report_job_scope_index: dict[tuple[str, str, str, str], int] = {}
        self.next_report_job_id = 1
        self.self_review_subscriptions: dict[tuple[str, str], dict[str, object]] = {
            ("default", "room@chatroom"): {
                "tenant_id": "default",
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "enabled": True,
                "daily_hour": 23,
                "tz": "Asia/Shanghai",
                "focus_mode": "bot_interactions",
                "auto_create_kb_doc": True,
            }
        }
        self.self_review_jobs: dict[int, dict[str, object]] = {}
        self.self_review_job_scope_index: dict[tuple[str, str, str], int] = {}
        self.next_self_review_job_id = 1
        self.member_event_connections: list[str] = []
        self.media_event_connections: list[str] = []

    async def list_reply_queue(
        self,
        tenant_id: str,
        *,
        status: str = "",
        session_id: str = "",
        trace_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, object]]:
        assert tenant_id == "demo"
        assert status == "pending"
        assert session_id == "room@chatroom"
        assert trace_id == ""
        assert limit == 2
        return [
            {
                "id": 11,
                "tenant_id": "demo",
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "sender_name": "群友A",
                "mention_sender": True,
                "reply_text": "您好，这里是自动回复。",
                "msg_type": "text",
                "image_path": "",
                "trace_id": "trace-11",
                "status": "pending",
                "attempt_count": 0,
                "error": "",
                "created_at": "2026-04-20T20:00:00",
                "sent_at": None,
            }
        ]

    async def reply_queue_stats(self, tenant_id: str) -> dict[str, int]:
        assert tenant_id == "demo"
        return {"pending": 1, "sent": 2, "failed": 0}

    async def clear_reply_queue(
        self,
        tenant_id: str,
        *,
        status: str = "pending",
        session_id: str = "",
    ) -> dict[str, object]:
        assert tenant_id == "demo"
        assert status == "pending"
        assert session_id == "room@chatroom"
        return {
            "tenant_id": tenant_id,
            "status": status,
            "session_id": session_id,
            "cleared": 2,
            "ids": [11, 12],
        }

    async def member_event_stats(
        self, tenant_id: str, *, connection_id: str = ""
    ) -> dict[str, int]:
        assert tenant_id == "default"
        _ = connection_id
        return {"group.member.joined": 3, "group.member.left": 1}

    async def media_ready_stats(
        self, tenant_id: str, *, connection_id: str = ""
    ) -> dict[str, int]:
        assert tenant_id == "default"
        _ = connection_id
        return {"message.media.ready": 2}

    async def list_member_events(
        self,
        tenant_id: str,
        limit: int = 50,
        *,
        connection_id: str = "",
    ) -> list[dict[str, object]]:
        assert tenant_id == "demo"
        assert limit == 2
        self.member_event_connections.append(connection_id)
        return [
            {
                "sdk_event_id": 101,
                "event_type": "group.member.joined",
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "entity_wxid": "wxid_new",
                "entity_name": "新群友",
                "payload": {"inviter_wxid": "wxid_admin"},
                "created_ts": 1710000000,
            },
            {
                "sdk_event_id": 100,
                "event_type": "group.member.left",
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "entity_wxid": "wxid_old",
                "entity_name": "老群友",
                "payload": {"operator_wxid": "wxid_admin"},
                "created_ts": 1709999999,
            },
        ]

    async def list_media_ready_events(
        self,
        tenant_id: str,
        limit: int = 50,
        *,
        connection_id: str = "",
    ) -> list[dict[str, object]]:
        assert tenant_id == "demo"
        assert limit == 2
        self.media_event_connections.append(connection_id)
        return [
            {
                "sdk_event_id": 201,
                "event_type": "message.media.ready",
                "stream_event_id": "stream:201",
                "message_id": "msg-201",
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "sender_wxid": "wxid_group_user",
                "sender_name": "群友A",
                "msg_type": "image",
                "media_type": "image",
                "media_path": "images/ready-201.png",
                "media_url": "http://127.0.0.1:5080/images/images/ready-201.png",
                "payload": {"meta": {"stage": "ready"}},
                "created_ts": 1710000010,
            },
            {
                "sdk_event_id": 200,
                "event_type": "message.media.ready",
                "stream_event_id": "stream:200",
                "message_id": "msg-200",
                "session_id": "wx-user",
                "session_name": "私聊",
                "sender_wxid": "wxid_user",
                "sender_name": "用户A",
                "msg_type": "image",
                "media_type": "image",
                "media_path": "images/ready-200.png",
                "media_url": "http://127.0.0.1:5080/images/images/ready-200.png",
                "payload": {"meta": {"stage": "ready"}},
                "created_ts": 1710000009,
            },
        ]

    async def get_global_policy(self, tenant_id: str) -> dict[str, object]:
        return self.global_policies.get(
            tenant_id,
            {
                "tenant_id": tenant_id,
                "private_reply_mode": "all",
                "group_reply_mode": "off",
                "group_reply_mention_sender": False,
                "trigger_keywords_text": "",
                "trigger_keywords": [],
                "version": 0,
                "updated_at": None,
            },
        )

    async def set_global_policy(
        self,
        tenant_id: str,
        *,
        expected_version: int,
        private_reply_mode: str | None = None,
        group_reply_mode: str | None = None,
        group_reply_mention_sender: bool | None = None,
        trigger_keywords_text: str | None = None,
    ) -> WxbotPolicyMutation:
        before = await self.get_global_policy(tenant_id)
        if int(before["version"]) != expected_version:
            raise WxbotPolicyVersionConflictError(
                expected=expected_version,
                current=int(before["version"]),
            )
        policy = {
            "tenant_id": tenant_id,
            "private_reply_mode": private_reply_mode or before["private_reply_mode"],
            "group_reply_mode": group_reply_mode or before["group_reply_mode"],
            "group_reply_mention_sender": (
                bool(group_reply_mention_sender)
                if group_reply_mention_sender is not None
                else bool(before["group_reply_mention_sender"])
            ),
            "trigger_keywords_text": (
                trigger_keywords_text
                if trigger_keywords_text is not None
                else before["trigger_keywords_text"]
            ),
            "version": expected_version + 1,
            "updated_at": "2026-04-20T20:00:00",
        }
        policy["trigger_keywords"] = [
            line.strip()
            for line in str(policy["trigger_keywords_text"] or "").splitlines()
            if line.strip()
        ]
        self.global_policies[tenant_id] = policy
        return WxbotPolicyMutation(before=before, after=policy)

    async def upsert_global_policy_in_transaction(
        self,
        _db,
        *,
        tenant_id: str,
        private_reply_mode: str,
        group_reply_mode: str,
        group_reply_mention_sender: bool,
        trigger_keywords_text: str,
    ) -> dict[str, object]:
        current = await self.get_global_policy(tenant_id)
        mutation = await self.set_global_policy(
            tenant_id,
            expected_version=int(current["version"]),
            private_reply_mode=private_reply_mode,
            group_reply_mode=group_reply_mode,
            group_reply_mention_sender=group_reply_mention_sender,
            trigger_keywords_text=trigger_keywords_text,
        )
        return mutation.after

    async def get_session_policy(self, tenant_id: str, session_id: str) -> dict[str, object]:
        global_policy = await self.get_global_policy(tenant_id)
        default_mode = (
            global_policy["group_reply_mode"]
            if session_id.endswith("@chatroom")
            else global_policy["private_reply_mode"]
        )
        return self.policies.get(
            (tenant_id, session_id),
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "reply_mode": "inherit",
                "mention_sender_mode": "inherit",
                "trigger_keywords_text": "",
                "default_mode": default_mode,
                "effective_mode": default_mode,
                "default_mention_sender": bool(global_policy["group_reply_mention_sender"])
                if session_id.endswith("@chatroom")
                else False,
                "effective_mention_sender": bool(global_policy["group_reply_mention_sender"])
                if session_id.endswith("@chatroom")
                else False,
                "global_policy": global_policy,
                "inherits_global_keywords": True,
                "effective_trigger_keywords_text": global_policy["trigger_keywords_text"],
                "trigger_keywords": global_policy["trigger_keywords"],
                "participation_policy": {
                    "threshold": 60,
                    "quiet_start_hour": 23,
                    "quiet_end_hour": 8,
                    "timezone": "Asia/Shanghai",
                    "max_soft_replies_10m": 2,
                    "max_soft_replies_hour": 6,
                    "max_bot_ratio_last_40": 0.15,
                    "max_consecutive_bot_messages": 2,
                },
                "version": 0,
                "updated_at": None,
            },
        )

    async def set_session_policy(
        self,
        tenant_id: str,
        session_id: str,
        *,
        expected_version: int,
        reply_mode: str | None = None,
        mention_sender_mode: str | None = None,
        trigger_keywords_text: str | None = None,
        participation_policy: dict[str, object] | None = None,
    ) -> WxbotPolicyMutation:
        global_policy = await self.get_global_policy(tenant_id)
        before = await self.get_session_policy(tenant_id, session_id)
        if int(before["version"]) != expected_version:
            raise WxbotPolicyVersionConflictError(
                expected=expected_version,
                current=int(before["version"]),
            )
        effective_mode = reply_mode or str(before["reply_mode"])
        effective_mention_mode = mention_sender_mode or str(before["mention_sender_mode"])
        default_mode = (
            global_policy["group_reply_mode"]
            if session_id.endswith("@chatroom")
            else global_policy["private_reply_mode"]
        )
        default_mention_sender = (
            bool(global_policy["group_reply_mention_sender"])
            if session_id.endswith("@chatroom")
            else False
        )
        effective_keywords_text = str(
            trigger_keywords_text
            if trigger_keywords_text is not None
            else before["trigger_keywords_text"]
        ).strip() or str(global_policy["trigger_keywords_text"] or "")
        policy = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "reply_mode": effective_mode,
            "mention_sender_mode": effective_mention_mode,
            "trigger_keywords_text": (
                trigger_keywords_text
                if trigger_keywords_text is not None
                else before["trigger_keywords_text"]
            ),
            "default_mode": default_mode,
            "effective_mode": effective_mode if effective_mode != "inherit" else default_mode,
            "default_mention_sender": default_mention_sender,
            "effective_mention_sender": (
                default_mention_sender
                if effective_mention_mode == "inherit"
                else effective_mention_mode == "on"
            ),
            "global_policy": global_policy,
            "inherits_global_keywords": not bool(str(trigger_keywords_text or "").strip()),
            "effective_trigger_keywords_text": effective_keywords_text,
            "trigger_keywords": [
                line.strip() for line in effective_keywords_text.splitlines() if line.strip()
            ],
            "participation_policy": {
                "threshold": 60,
                "quiet_start_hour": 23,
                "quiet_end_hour": 8,
                "timezone": "Asia/Shanghai",
                "max_soft_replies_10m": 2,
                "max_soft_replies_hour": 6,
                "max_bot_ratio_last_40": 0.15,
                "max_consecutive_bot_messages": 2,
                **dict(before.get("participation_policy") or {}),
                **(participation_policy or {}),
            },
            "version": expected_version + 1,
            "updated_at": "2026-04-20T20:00:00",
        }
        self.policies[(tenant_id, session_id)] = policy
        return WxbotPolicyMutation(before=before, after=policy)

    async def list_report_subscriptions(self, tenant_id: str) -> list[dict[str, object]]:
        return [
            dict(item)
            for (tid, _), item in sorted(
                self.report_subscriptions.items(), key=lambda entry: entry[1]["session_id"]
            )
            if tid == tenant_id
        ]

    async def upsert_report_subscription(
        self,
        tenant_id: str,
        *,
        session_id: str,
        session_name: str,
        daily_enabled: bool = False,
        weekly_enabled: bool = True,
        monthly_enabled: bool = False,
        daily_hour: int = 9,
        weekly_day: int = 1,
        weekly_hour: int = 9,
        monthly_day: int = 1,
        tz: str = "Asia/Shanghai",
    ) -> dict[str, object]:
        row = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "session_name": session_name,
            "daily_enabled": bool(daily_enabled),
            "weekly_enabled": bool(weekly_enabled),
            "monthly_enabled": bool(monthly_enabled),
            "daily_hour": int(daily_hour),
            "weekly_day": int(weekly_day),
            "weekly_hour": int(weekly_hour),
            "monthly_day": int(monthly_day),
            "tz": tz,
        }
        self.report_subscriptions[(tenant_id, session_id)] = row
        return dict(row)

    async def delete_report_subscription(self, tenant_id: str, session_id: str) -> bool:
        return self.report_subscriptions.pop((tenant_id, session_id), None) is not None

    async def list_enabled_report_subscriptions(self, tenant_id: str) -> list[dict[str, object]]:
        return [
            dict(item)
            for (tid, _), item in self.report_subscriptions.items()
            if tid == tenant_id
            and (
                bool(item["daily_enabled"])
                or bool(item.get("weekly_enabled", True))
                or bool(item["monthly_enabled"])
            )
        ]

    async def get_or_create_report_job(
        self,
        *,
        tenant_id: str,
        session_id: str,
        session_name: str,
        report_type: str,
        period_key: str,
        period_label: str,
    ) -> dict[str, object]:
        scope = (tenant_id, session_id, report_type, period_key)
        job_id = self.report_job_scope_index.get(scope)
        if job_id is None:
            job_id = self.next_report_job_id
            self.next_report_job_id += 1
            self.report_job_scope_index[scope] = job_id
            self.report_jobs[job_id] = {
                "id": job_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "session_name": session_name,
                "report_type": report_type,
                "period_key": period_key,
                "period_label": period_label,
                "status": "pending",
                "current_stage": "queued",
                "msg_count": 0,
                "result_text": "",
                "report_payload": {},
                "run_attempt": 0,
                "delivery_status": "pending",
                "delivery_attempt": 0,
                "sdk_outbound_id": None,
                "delivery_error": "",
                "error": "",
            }
        else:
            self.report_jobs[job_id]["session_name"] = session_name
            self.report_jobs[job_id]["period_label"] = period_label
        return dict(self.report_jobs[job_id])

    async def get_report_job(self, job_id: int) -> dict[str, object] | None:
        job = self.report_jobs.get(job_id)
        return dict(job) if job else None

    async def get_report_job_by_scope(
        self,
        *,
        tenant_id: str,
        session_id: str,
        report_type: str,
        period_key: str,
    ) -> dict[str, object] | None:
        job_id = self.report_job_scope_index.get((tenant_id, session_id, report_type, period_key))
        if job_id is None:
            return None
        return dict(self.report_jobs[job_id])

    async def update_report_job(
        self,
        job_id: int,
        *,
        status: str,
        current_stage: str,
        msg_count: int | None = None,
        result_text: str | None = None,
        report_payload: dict[str, object] | None = None,
        error: str | None = None,
        expected_run_attempt: int | None = None,
        expected_status: str | None = None,
    ) -> bool:
        job = self.report_jobs[job_id]
        if expected_run_attempt is not None and int(job.get("run_attempt") or 0) != expected_run_attempt:
            return False
        if expected_status is not None and job.get("status") != expected_status:
            return False
        job["status"] = status
        job["current_stage"] = current_stage
        if msg_count is not None:
            job["msg_count"] = msg_count
        if result_text is not None:
            job["result_text"] = result_text
        if report_payload is not None:
            job["report_payload"] = report_payload
        if error is not None:
            job["error"] = error
        return True

    async def try_start_report_job(self, job_id: int) -> int | None:
        job = self.report_jobs[job_id]
        if str(job["status"]) not in {"pending", "failed"}:
            return None
        job["status"] = "running"
        job["run_attempt"] = int(job.get("run_attempt") or 0) + 1
        job["current_stage"] = "collect_messages"
        job["error"] = ""
        return int(job["run_attempt"])

    async def try_start_report_delivery(self, job_id: int) -> int | None:
        job = self.report_jobs[job_id]
        if str(job.get("delivery_status") or "pending") not in {"pending", "failed"}:
            return None
        job["delivery_status"] = "sending"
        job["delivery_attempt"] = int(job.get("delivery_attempt") or 0) + 1
        job["delivery_error"] = ""
        return int(job["delivery_attempt"])

    async def mark_report_delivery_sent(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
    ) -> bool:
        job = self.report_jobs[job_id]
        if (
            job.get("delivery_status") != "sending"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
        ):
            return False
        job["delivery_status"] = "sent"
        job["delivery_error"] = ""
        return True

    async def mark_report_delivery_queued(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
    ) -> bool:
        job = self.report_jobs[job_id]
        if (
            job.get("delivery_status") != "sending"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
        ):
            return False
        job["delivery_status"] = "queued"
        job["sdk_outbound_id"] = int(sdk_outbound_id)
        job["delivery_error"] = ""
        return True

    async def touch_report_delivery_check(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
        error: str = "",
    ) -> bool:
        job = self.report_jobs[job_id]
        if (
            job.get("delivery_status") != "queued"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
            or int(job.get("sdk_outbound_id") or 0) != sdk_outbound_id
        ):
            return False
        job["delivery_error"] = error
        return True

    async def mark_report_delivery_terminal(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
        status: str,
        error: str = "",
    ) -> bool:
        job = self.report_jobs[job_id]
        if (
            job.get("delivery_status") != "queued"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
            or int(job.get("sdk_outbound_id") or 0) != sdk_outbound_id
        ):
            return False
        job["delivery_status"] = status
        job["delivery_error"] = error
        return True

    async def list_report_deliveries_to_reconcile(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        return [
            dict(job)
            for job in self.report_jobs.values()
            if job.get("tenant_id") == tenant_id
            and job.get("delivery_status") == "queued"
        ][:limit]

    async def mark_report_delivery_failed(
        self,
        job_id: int,
        error: str,
        *,
        delivery_attempt: int,
    ) -> bool:
        job = self.report_jobs[job_id]
        if (
            job.get("delivery_status") != "sending"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
        ):
            return False
        job["delivery_status"] = "failed"
        job["delivery_error"] = error
        return True

    async def mark_report_delivery_indeterminate(
        self,
        job_id: int,
        error: str,
        *,
        delivery_attempt: int,
    ) -> bool:
        job = self.report_jobs[job_id]
        if (
            job.get("delivery_status") != "sending"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
        ):
            return False
        job["delivery_status"] = "indeterminate"
        job["delivery_error"] = error
        return True

    async def release_report_delivery(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        reason: str,
    ) -> bool:
        job = self.report_jobs[job_id]
        if (
            job.get("delivery_status") != "sending"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
        ):
            return False
        job["delivery_status"] = "pending"
        job["delivery_error"] = reason
        return True

    async def list_self_review_subscriptions(self, tenant_id: str) -> list[dict[str, object]]:
        return [
            dict(item)
            for (tid, _), item in sorted(
                self.self_review_subscriptions.items(), key=lambda entry: entry[1]["session_id"]
            )
            if tid == tenant_id
        ]

    async def upsert_self_review_subscription(
        self,
        tenant_id: str,
        *,
        session_id: str,
        session_name: str,
        enabled: bool = False,
        daily_hour: int = 23,
        tz: str = "Asia/Shanghai",
        focus_mode: str = "bot_interactions",
        auto_create_kb_doc: bool = False,
    ) -> dict[str, object]:
        row = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "session_name": session_name,
            "enabled": bool(enabled),
            "daily_hour": int(daily_hour),
            "tz": tz,
            "focus_mode": focus_mode,
            "auto_create_kb_doc": bool(auto_create_kb_doc),
        }
        self.self_review_subscriptions[(tenant_id, session_id)] = row
        return dict(row)

    async def delete_self_review_subscription(self, tenant_id: str, session_id: str) -> bool:
        return self.self_review_subscriptions.pop((tenant_id, session_id), None) is not None

    async def get_self_review_subscription(
        self, tenant_id: str, session_id: str
    ) -> dict[str, object] | None:
        row = self.self_review_subscriptions.get((tenant_id, session_id))
        return dict(row) if row else None

    async def list_self_review_jobs(
        self,
        tenant_id: str,
        *,
        session_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, object]]:
        items = [
            dict(item)
            for item in self.self_review_jobs.values()
            if item["tenant_id"] == tenant_id
            and (not session_id or item["session_id"] == session_id)
        ]
        return items[:limit]

    async def get_or_create_self_review_job(
        self,
        *,
        tenant_id: str,
        session_id: str,
        session_name: str,
        period_key: str,
        period_label: str,
    ) -> dict[str, object]:
        scope = (tenant_id, session_id, period_key)
        job_id = self.self_review_job_scope_index.get(scope)
        if job_id is None:
            job_id = self.next_self_review_job_id
            self.next_self_review_job_id += 1
            self.self_review_job_scope_index[scope] = job_id
            self.self_review_jobs[job_id] = {
                "id": job_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "session_name": session_name,
                "period_key": period_key,
                "period_label": period_label,
                "status": "pending",
                "current_stage": "queued",
                "msg_count": 0,
                "result_text": "",
                "review_payload": {},
                "run_attempt": 0,
                "kb_doc_id": None,
                "kb_doc_title": "",
                "error": "",
            }
        else:
            self.self_review_jobs[job_id]["session_name"] = session_name
            self.self_review_jobs[job_id]["period_label"] = period_label
        return dict(self.self_review_jobs[job_id])

    async def get_self_review_job(self, job_id: int) -> dict[str, object] | None:
        job = self.self_review_jobs.get(job_id)
        return dict(job) if job else None

    async def update_self_review_job(
        self,
        job_id: int,
        *,
        status: str,
        current_stage: str,
        msg_count: int | None = None,
        result_text: str | None = None,
        review_payload: dict[str, object] | None = None,
        kb_doc_id: int | None = None,
        kb_doc_title: str | None = None,
        error: str | None = None,
        expected_run_attempt: int | None = None,
        expected_status: str | None = None,
    ) -> bool:
        job = self.self_review_jobs[job_id]
        if expected_run_attempt is not None and int(job.get("run_attempt") or 0) != expected_run_attempt:
            return False
        if expected_status is not None and job.get("status") != expected_status:
            return False
        job["status"] = status
        job["current_stage"] = current_stage
        if msg_count is not None:
            job["msg_count"] = msg_count
        if result_text is not None:
            job["result_text"] = result_text
        if review_payload is not None:
            job["review_payload"] = review_payload
        if kb_doc_id is not None:
            job["kb_doc_id"] = kb_doc_id
        if kb_doc_title is not None:
            job["kb_doc_title"] = kb_doc_title
        if error is not None:
            job["error"] = error
        return True

    async def try_start_self_review_job(self, job_id: int) -> int | None:
        job = self.self_review_jobs[job_id]
        if str(job["status"]) not in {"pending", "failed"}:
            return None
        job["status"] = "running"
        job["run_attempt"] = int(job.get("run_attempt") or 0) + 1
        job["current_stage"] = "collect_messages"
        job["error"] = ""
        return int(job["run_attempt"])


class _FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.request_headers: list[dict[str, str] | None] = []

    async def status(self) -> dict[str, object]:
        return {
            "running": True,
            "sdk_online": True,
            "sdk_url": "http://127.0.0.1:5080",
            "event_mode": "sse",
            "member_event_stats": {
                "group.member.joined": 3,
                "group.member.left": 1,
            },
            "media_ready_stats": {
                "message.media.ready": 2,
            },
        }

    async def sdk_request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self.request_headers.append(request_headers)
        self.calls.append((method, path, params, json_body))
        if path == "/sessions":
            return {"sessions": [{"session_id": "wx-1", "session_name": "测试会话"}]}
        if path == "/queue/stats":
            return {"pending": 3}
        if path == "/status":
            return {
                "status": "running",
                "config": {
                    "my_names": ["bot", "机器人"],
                },
            }
        if path == "/queue/messages":
            return {
                "items": [
                    {
                        "id": 7,
                        "session_id": "room@chatroom",
                        "session_name": "测试群",
                        "sender_name": "客服",
                        "mention_sender": True,
                        "reply_text": "SDK 待发送消息",
                        "msg_type": "text",
                        "image_path": "",
                        "status": "pending",
                        "attempt_count": 0,
                        "error": "",
                        "created_ts": 1710000000,
                        "sent_ts": None,
                    }
                ],
                "count": 1,
            }
        if path == "/queue/messages/7" and method == "GET":
            return {
                "id": 7,
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "status": "uncertain",
                "command_id": "sdk-row-7",
            }
        if path == "/queue/clear" and method == "POST":
            return {
                "status": str(json_body.get("status") or "pending"),
                "session_id": str(json_body.get("session_id") or ""),
                "cleared": 1,
                "ids": [7],
            }
        if path == "/debug/trigger-config" and method == "GET":
            return {
                "group_require_at_me": True,
                "group_capture_mode": "mention_only",
                "my_names": ["bot", "机器人"],
            }
        if path == "/debug/trigger-config" and method == "POST":
            return {
                "group_require_at_me": bool(json_body.get("group_require_at_me")),
                "group_capture_mode": (
                    "mention_only" if json_body.get("group_require_at_me") else "all_group_messages"
                ),
                "my_names": ["bot", "机器人"],
                "saved": True,
            }
        if path == "/ext/roster/groups" and method == "GET":
            return {
                "ok": True,
                "sessions": [
                    {
                        "session_id": "room@chatroom",
                        "session_name": "测试群",
                        "kind": "group",
                        "count": 0,
                        "last_ts": None,
                    },
                    {
                        "session_id": "new-room@chatroom",
                        "session_name": "新群",
                        "kind": "group",
                        "count": 0,
                        "last_ts": None,
                    },
                ],
                "count": 2,
            }
        if path == "/queue/messages/7/reconcile" and method == "POST":
            return {
                "id": 7,
                "action": str(json_body.get("action") or ""),
                "status": "sent" if json_body.get("action") == "confirm_sent" else "pending",
                "replayed": False,
            }
        if path == "/send":
            return {"queued": True, "id": 7}
        if path == "/send/envelope":
            return {
                "queued": True,
                "id": 17,
                "protocol": "envelope",
                "normalized": {
                    "session_id": "room@chatroom",
                    "session_kind": "group",
                    "msg_type": "text",
                },
            }
        if path == "/send/envelope/batch":
            return {
                "results": [
                    {
                        "queued": True,
                        "id": 18,
                        "protocol": "envelope",
                        "normalized": {
                            "session_id": "room@chatroom",
                            "session_kind": "group",
                            "msg_type": "text",
                        },
                    }
                ],
                "count": 1,
            }
        if path == "/send/batch":
            return {"results": [{"queued": True, "id": 8}], "count": 1}
        if path == "/event-subscriptions" and method == "GET":
            return {
                "items": [
                    {
                        "id": 9,
                        "event_type": params.get("event_type") if params else "group.member.joined",
                        "target_url": "https://example.com/member-events",
                        "session_id": (
                            params.get("session_id")
                            if params
                            else "room@chatroom"
                        ),
                        "enabled": True,
                    }
                ],
                "count": 1,
            }
        if path == "/event-subscriptions" and method == "POST":
            return {"subscription": json_body, "saved": True}
        if path == "/event-subscriptions/9" and method == "DELETE":
            return {"deleted": True, "id": 9}
        if path == "/group-members/settings/room@chatroom" and method == "GET":
            return {
                "session_id": "room@chatroom",
                "welcome_enabled": True,
                "welcome_template": "欢迎 {{member_name}}",
                "welcome_mention": False,
            }
        if path == "/group-members/settings/room@chatroom" and method == "POST":
            return {"session_id": "room@chatroom", **(json_body or {}), "saved": True}
        if path == "/ext/roster/groups/room@chatroom/members" and method == "GET":
            return {
                "ok": True,
                "session_id": "room@chatroom",
                "candidates": [
                    {
                        "wxid": "wxid_member_1",
                        "name": "群友一",
                        "msg_count": 18,
                        "has_history": True,
                    }
                ],
            }
        if path == "/ext/reports/messages/room@chatroom" and method == "GET":
            return {
                "ok": True,
                "session_id": "room@chatroom",
                "session_name": params.get("session_name") if params else "测试群",
                "report_type": params.get("report_type") if params else "daily",
                "period": (params.get("date") or params.get("year_month") or "2026-04-20")
                if params
                else "2026-04-20",
                "count": 2,
                "messages": [
                    {
                        "timestamp": "2026-04-20 10:00:00",
                        "sender_wxid": "wxid_a",
                        "sender_name": "张三",
                        "msg_type": "text",
                        "text": "第一个议题",
                        "is_self_sent": False,
                    },
                    {
                        "timestamp": "2026-04-20 10:05:00",
                        "sender_wxid": "wxid_self",
                        "sender_name": "机器人",
                        "msg_type": "text",
                        "text": "这条应该被过滤",
                        "is_self_sent": True,
                    },
                ],
            }
        if path == "/ext/query/read" and method == "POST":
            return {
                "ok": True,
                "database": json_body.get("database"),
                "rows": [{"session_id": "room@chatroom"}],
                "count": 1,
                "limit": json_body.get("limit", 100),
            }
        raise AssertionError(path)


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[dict[str, object]] = []

    async def publish(
        self,
        stream: str,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        self.published.append(
            {
                "stream": stream,
                "payload": payload,
                "headers": headers or {},
                "partition_key": partition_key,
            }
        )
        return f"msg-{len(self.published)}"


class _FakeLlmService:
    async def chat(self, request):
        content = str(request.messages[0].content)
        if "合并成一份完整" in content:
            return SimpleNamespace(content="# 测试群日报\n\n## 整体概览\n- 今天有讨论\n")
        if "原始聊天记录片段" in content:
            return SimpleNamespace(content="## 关键事件\n- 完成一轮讨论")
        return SimpleNamespace(content="# 默认报告\n")


class _FakeScheduler:
    def __init__(self) -> None:
        self.notified = 0
        self.scheduled_keys: list[str] = []

    async def schedule_background(self, key: str, coro_factory):
        self.scheduled_keys.append(key)
        await coro_factory()
        return True

    def notify_report_scheduler(self) -> None:
        self.notified += 1

    def notify_self_review_scheduler(self) -> None:
        self.notified += 1


class _FakeAgentStore:
    def __init__(self) -> None:
        self.policies: dict[tuple[str, str], dict[str, object]] = {}
        self.audit_rows: list[dict[str, object]] = [
            {
                "id": 1,
                "tenant_id": "demo",
                "session_id": "room@chatroom",
                "user_id": "wxid_user",
                "channel": "wechat",
                "scope": "group_info",
                "tool_name": "search_group_messages",
                "tool_args_json": json.dumps({"query": "draw"}, ensure_ascii=False),
                "tool_result_json": json.dumps({"total": 1}, ensure_ascii=False),
                "tool_error": "",
                "latency_ms": 12,
                "trace_id": "trace-agent-1",
                "final_reply_text": "刚才提到 draw 的是张三。",
                "created_at": "2026-04-23T10:00:00",
                "tool_args": {"query": "draw"},
                "tool_result": {"total": 1},
            },
            {
                "id": 2,
                "tenant_id": "demo",
                "session_id": "room@chatroom",
                "user_id": "wxid_user",
                "channel": "wechat",
                "scope": "group_plugin_status",
                "tool_name": "get_group_credits_status",
                "tool_args_json": json.dumps({}, ensure_ascii=False),
                "tool_result_json": json.dumps(
                    {"enabled": True, "credit_name": "积分"}, ensure_ascii=False
                ),
                "tool_error": "",
                "latency_ms": 7,
                "trace_id": "trace-agent-2",
                "final_reply_text": "这个群积分功能开着。",
                "created_at": "2026-04-23T10:01:00",
                "tool_args": {},
                "tool_result": {"enabled": True, "credit_name": "积分"},
            },
        ]

    async def get_session_policy(
        self,
        tenant_id: str,
        session_id: str,
        *,
        scope: str = "group_info",
        available_tools: list[str] | None = None,
    ) -> dict[str, object]:
        available = list(available_tools or [])
        row = self.policies.get(
            (tenant_id, session_id, scope),
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "scope": scope,
                "enabled": True,
                "allowed_tools": [],
                "available_tools": available,
                "effective_tools": available,
                "inherits_default_tools": True,
                "updated_at": None,
            },
        )
        return dict(row)

    async def set_session_policy(
        self,
        tenant_id: str,
        session_id: str,
        *,
        scope: str = "group_info",
        enabled: bool | None = None,
        allowed_tools: list[str] | None = None,
        available_tools: list[str] | None = None,
    ) -> dict[str, object]:
        available = list(available_tools or [])
        normalized_allowed = list(allowed_tools or [])
        next_enabled = True if enabled is None else bool(enabled)
        if not next_enabled:
            effective_tools: list[str] = []
        elif normalized_allowed:
            effective_tools = [item for item in available if item in normalized_allowed]
        else:
            effective_tools = available
        row = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "scope": scope,
            "enabled": next_enabled,
            "allowed_tools": normalized_allowed,
            "available_tools": available,
            "effective_tools": effective_tools,
            "inherits_default_tools": not bool(normalized_allowed),
            "updated_at": "2026-04-23T10:00:00",
        }
        self.policies[(tenant_id, session_id, scope)] = row
        return dict(row)

    async def list_tool_audits(
        self,
        tenant_id: str,
        *,
        session_id: str = "",
        scope: str = "",
        tool_name: str = "",
        trace_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, object]]:
        assert tenant_id == "demo"
        items = list(self.audit_rows)
        if session_id:
            items = [item for item in items if item["session_id"] == session_id]
        if scope:
            items = [item for item in items if item["scope"] == scope]
        if tool_name:
            items = [item for item in items if item["tool_name"] == tool_name]
        if trace_id:
            items = [item for item in items if item["trace_id"] == trace_id]
        return items[:limit]


class _CaptureSelfReviewService:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store
        self.run_job_ids: list[int] = []

    async def run_self_review_job(self, job_id: int) -> None:
        self.run_job_ids.append(job_id)
        job = self.store.self_review_jobs[job_id]
        job["status"] = "completed"
        job["current_stage"] = "completed"
        job["result_text"] = "# 测试群自我迭代复盘"
        job["review_payload"] = {
            **job["review_payload"],
            "period": job["period_label"],
            "auto_create_kb_doc": False,
            "kb_doc_id": None,
            "kb_doc_title": "[测试群] 自我迭代复盘 · 2026-04-21",
            "kb_publish_status": "pending_review",
        }
        job["kb_doc_title"] = "[测试群] 自我迭代复盘 · 2026-04-21"


class _FakeKbService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def add_document(self, **kwargs) -> int:
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return 99


async def _allow_wxbot_scope(_tenant_id: str, _session_id: str) -> bool:
    return True


class _FakeGroupFilePolicyStore:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled

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
            policy=ParticipationPolicyValues(file_send_enabled=self.enabled),
        )


def _seed_completed_self_review_job(
    store: _FakeStore,
    *,
    tenant_id: str = "default",
    status: str = "completed",
    result_text: str = "# 测试群自我迭代复盘",
) -> None:
    store.self_review_jobs[1] = {
        "id": 1,
        "tenant_id": tenant_id,
        "session_id": "room@chatroom",
        "session_name": "测试群",
        "period_key": "2026-04-21",
        "period_label": "2026-04-21",
        "status": status,
        "current_stage": "completed" if status == "completed" else "queued",
        "msg_count": 8,
        "result_text": result_text,
        "review_payload": {
            "focus_mode": "bot_interactions",
            "focused_message_count": 5,
            "focused_thread_count": 2,
            "auto_create_kb_doc": False,
            "kb_doc_id": None,
            "kb_doc_title": "[测试群] 自我迭代复盘 · 2026-04-21",
            "kb_publish_status": "pending_review",
        },
        "kb_doc_id": None,
        "kb_doc_title": "[测试群] 自我迭代复盘 · 2026-04-21",
        "error": "",
    }


def _build_self_review_publish_client(
    kb_service: _FakeKbService,
) -> tuple[TestClient, _FakeStore]:
    app = FastAPI()
    store = _FakeStore()
    container = SimpleNamespace(kb_service=kb_service, llm_service=None)
    service = wxbot_router.WxbotSelfReviewService(
        store,
        container,
        scope_execution_allowed=_allow_wxbot_scope,
    )
    app.include_router(
        build_wxbot_router(
            store,
            container=container,
            bridge=_FakeBridge(),
            self_review_service=service,
        )
    )
    return TestClient(app), store


def _build_client(
    *,
    group_file_send_enabled: bool = False,
) -> tuple[TestClient, _FakeStore, _FakeBridge, _FakeScheduler, _FakeAgentStore]:
    app = FastAPI()
    store = _FakeStore()
    bridge = _FakeBridge()
    scheduler = _FakeScheduler()
    agent_store = _FakeAgentStore()
    registry = AgentToolRegistry()
    wxbot_agent_tool_metadata = {"channels": ["wechat"], "session_kinds": ["group"]}
    for item in AgentCapabilityEngine.tool_catalog("group_info"):
        registry.register(
            AgentToolDefinition(
                scope="group_info",
                name=item["name"],
                description=item["description"],
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda *_args, **_kwargs: None,
                metadata=wxbot_agent_tool_metadata,
            ),
            owner="wxbot",
        )
    for item in AgentCapabilityEngine.tool_catalog("group_plugin_status"):
        registry.register(
            AgentToolDefinition(
                scope="group_plugin_status",
                name=item["name"],
                description=item["description"],
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda *_args, **_kwargs: None,
                metadata=wxbot_agent_tool_metadata,
            ),
            owner="wxbot",
        )
    registry.register(
        AgentToolDefinition(
            scope="group_draw_generation",
            name="generate_group_image",
            description="draw tool",
            parameters={
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "additionalProperties": False,
            },
            handler=lambda *_args, **_kwargs: None,
        ),
        owner="draw",
    )
    container = SimpleNamespace(
        llm_service=_FakeLlmService(),
        agent_tool_registry=registry,
        social_policy_store=_FakeGroupFilePolicyStore(
            enabled=group_file_send_enabled,
        ),
    )
    report_service = wxbot_router.WxbotReportService(
        store,
        container,
        bridge=bridge,
        scope_execution_allowed=_allow_wxbot_scope,
    )
    self_review_service = wxbot_router.WxbotSelfReviewService(
        store,
        container,
        bridge=bridge,
        scope_execution_allowed=_allow_wxbot_scope,
    )
    app.include_router(
        build_wxbot_router(
            store,
            container=container,
            bridge=bridge,
            scheduler=scheduler,
            report_service=report_service,
            self_review_service=self_review_service,
            agent_store=agent_store,
            scope_execution_allowed=_allow_wxbot_scope,
        )
    )
    return TestClient(app), store, bridge, scheduler, agent_store


def test_event_subscription_listing_omits_empty_sdk_filters() -> None:
    client, _store, bridge, _scheduler, _agent_store = _build_client()

    response = client.get(
        "/admin/event-subscriptions",
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["session_id"] == "room@chatroom"
    subscription_calls = [
        call
        for call in bridge.calls
        if call[0] == "GET" and call[1] == "/event-subscriptions"
    ]
    assert subscription_calls == [("GET", "/event-subscriptions", None, None)]


def test_admin_inbound_simulator_uses_verified_roster_and_server_side_bus() -> None:
    app = FastAPI()
    store = _FakeStore()
    bridge = _FakeBridge()
    bus = _FakeBus()
    app.include_router(
        build_wxbot_router(
            store,
            container=SimpleNamespace(bus=bus, llm_service=None),
            bridge=bridge,
        )
    )
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer token",
        "Idempotency-Key": "playground-test-message-1",
    }

    accepted = client.post(
        "/admin/tenants/default/groups/room@chatroom/simulate-inbound",
        headers=headers,
        json={"message": "@机器人 帮我看看今天的安排"},
    )
    repeated = client.post(
        "/admin/tenants/default/groups/room@chatroom/simulate-inbound",
        headers=headers,
        json={"message": "@机器人 帮我看看今天的安排"},
    )
    conflicting = client.post(
        "/admin/tenants/default/groups/room@chatroom/simulate-inbound",
        headers=headers,
        json={"message": "同一个键不能换成另一条消息"},
    )
    unknown = client.post(
        "/admin/tenants/default/groups/unknown@chatroom/simulate-inbound",
        headers={**headers, "Idempotency-Key": "playground-test-message-2"},
        json={"message": "测试"},
    )
    missing_key = client.post(
        "/admin/tenants/default/groups/room@chatroom/simulate-inbound",
        headers={"Authorization": "Bearer token"},
        json={"message": "测试"},
    )

    assert accepted.status_code == 200
    assert repeated.status_code == 200
    assert accepted.json()["message_id"] == repeated.json()["message_id"]
    assert "帮我看看" not in accepted.text
    assert conflicting.status_code == 409
    assert len(bus.published) == 1
    published = bus.published[0]
    assert published["stream"] == "cs:inbound"
    assert published["partition_key"] == "default:room@chatroom"
    assert published["headers"]["tenant_id"] == "default"
    assert published["payload"]["session_id"] == "room@chatroom"
    assert published["payload"]["metadata"]["admin_simulation"] is True
    assert published["payload"]["metadata"]["source"] == "admin_console_simulator"
    assert published["payload"]["metadata"]["profile_source_key"] == "wxbot"
    assert unknown.status_code == 404
    assert missing_key.status_code == 400


def test_sdk_uncertain_reconciliation_is_explicit_and_exactly_replayed() -> None:
    client, _store, bridge, _scheduler, _agent_store = _build_client()
    headers = {
        "Authorization": "Bearer token",
        "Idempotency-Key": "sdk-reconcile-row-7-confirm-1",
    }

    with client:
        first = client.post(
            "/admin/sdk/queue/messages/7/reconcile",
            headers=headers,
            json={"action": "confirm_sent"},
        )
        replay = client.post(
            "/admin/sdk/queue/messages/7/reconcile",
            headers=headers,
            json={"action": "confirm_sent"},
        )
        conflict = client.post(
            "/admin/sdk/queue/messages/7/reconcile",
            headers=headers,
            json={"action": "retry"},
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_key_conflict"
    assert sum(call[1] == "/queue/messages/7/reconcile" for call in bridge.calls) == 1


def test_sdk_reconcile_resolves_exact_row_and_denies_disabled_session() -> None:
    calls: list[tuple[str, str]] = []

    async def deny_scope(tenant_id: str, session_id: str) -> bool:
        calls.append((tenant_id, session_id))
        return False

    app = FastAPI()
    store = _FakeStore()
    bridge = _FakeBridge()
    app.include_router(
        build_wxbot_router(
            store,
            container=None,
            bridge=bridge,
            scope_execution_allowed=deny_scope,
        )
    )

    response = TestClient(app).post(
        "/admin/sdk/queue/messages/7/reconcile",
        headers={
            "Authorization": "Bearer token",
            "Idempotency-Key": "sdk-reconcile-disabled-row-7",
        },
        json={"action": "confirm_sent"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "plugin_runtime_disabled"
    assert calls == [("default", "room@chatroom")]
    assert ("GET", "/queue/messages/7", None, None) in bridge.calls
    assert all(call[1] != "/queue/messages/7/reconcile" for call in bridge.calls)


def test_event_subscription_delete_denies_resolved_disabled_session() -> None:
    async def deny_scope(_tenant_id: str, _session_id: str) -> bool:
        return False

    app = FastAPI()
    store = _FakeStore()
    bridge = _FakeBridge()
    app.include_router(
        build_wxbot_router(
            store,
            container=None,
            bridge=bridge,
            scope_execution_allowed=deny_scope,
        )
    )
    client = TestClient(app)
    observed = client.get(
        "/admin/event-subscriptions",
        headers={"Authorization": "Bearer token"},
    )

    response = client.delete(
        "/admin/event-subscriptions/9",
        headers={
            "Authorization": "Bearer token",
            "If-Match": observed.headers["etag"],
            "Idempotency-Key": "event-delete-disabled-9",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "plugin_runtime_disabled"
    assert all(call[1] != "/event-subscriptions/9" for call in bridge.calls)


def test_raw_admin_send_fails_closed_without_scope_gate() -> None:
    app = FastAPI()
    store = _FakeStore()
    bridge = _FakeBridge()
    app.include_router(build_wxbot_router(store, container=None, bridge=bridge))

    response = TestClient(app).post(
        "/admin/send",
        headers={
            "Authorization": "Bearer token",
            "Idempotency-Key": "direct-send-missing-scope-gate",
        },
        json={"session_id": "wx-1", "msg_type": "text", "text": "hello"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "plugin_scope_unavailable"
    assert all(call[1] != "/send" for call in bridge.calls)


def test_raw_admin_batch_send_gates_every_exact_target() -> None:
    calls: list[tuple[str, str]] = []

    async def scope_gate(tenant_id: str, session_id: str) -> bool:
        calls.append((tenant_id, session_id))
        return session_id != "wx-2"

    app = FastAPI()
    store = _FakeStore()
    bridge = _FakeBridge()
    app.include_router(
        build_wxbot_router(
            store,
            container=None,
            bridge=bridge,
            scope_execution_allowed=scope_gate,
        )
    )

    response = TestClient(app).post(
        "/admin/send/batch",
        headers={
            "Authorization": "Bearer token",
            "Idempotency-Key": "batch-send-disabled-target",
        },
        json={
            "messages": [
                {"session_id": "wx-1", "msg_type": "text", "text": "one"},
                {"session_id": "wx-2", "msg_type": "text", "text": "two"},
            ]
        },
    )

    assert response.status_code == 503
    assert calls == [("default", "wx-1"), ("default", "wx-2")]
    assert all(call[1] != "/send/batch" for call in bridge.calls)


def test_sdk_reconciliation_preserves_downstream_idempotency_conflict() -> None:
    class _SdkConflict(RuntimeError):
        status_code = 409
        error_code = "idempotency_key_conflict"

    class _ConflictBridge(_FakeBridge):
        async def sdk_request(self, method: str, path: str, **kwargs):
            if path == "/queue/messages/7/reconcile":
                self.calls.append((method, path, kwargs.get("params"), kwargs.get("json_body")))
                raise _SdkConflict("conflict")
            return await super().sdk_request(method, path, **kwargs)

    app = FastAPI()
    store = _FakeStore()
    bridge = _ConflictBridge()
    app.include_router(
        build_wxbot_router(
            store,
            container=None,
            bridge=bridge,
            scope_execution_allowed=_allow_wxbot_scope,
        )
    )
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer token",
        "Idempotency-Key": "sdk-reconcile-downstream-conflict-1",
    }

    with client:
        first = client.post(
            "/admin/sdk/queue/messages/7/reconcile",
            headers=headers,
            json={"action": "retry"},
        )
        replay = client.post(
            "/admin/sdk/queue/messages/7/reconcile",
            headers=headers,
            json={"action": "retry"},
        )

    assert first.status_code == 409
    assert first.json()["detail"] == {
        "code": "wxbot_sdk_error",
        "sdk_error": "idempotency_key_conflict",
    }
    assert replay.json() == first.json()
    assert sum(call[1] == "/queue/messages/7/reconcile" for call in bridge.calls) == 1


def test_sdk_queue_message_status_filter_rejects_unknown_values() -> None:
    client, _store, bridge, _scheduler, _agent_store = _build_client()

    with client:
        invalid = client.get(
            "/admin/sdk/queue/messages?status=surprise",
            headers={"Authorization": "Bearer token"},
        )
        all_items = client.get(
            "/admin/sdk/queue/messages?status=all&limit=2",
            headers={"Authorization": "Bearer token"},
        )

    assert invalid.status_code == 400
    assert all_items.status_code == 200
    assert ("GET", "/queue/messages", {"status": "", "limit": 2}, None) in bridge.calls


def test_session_payload_filter_hides_unassigned_groups() -> None:
    principal = Principal(
        subject="group-operator",
        roles=(AdminRole.GROUP_OPERATOR.value,),
        tenant_ids=("default",),
        group_ids=("default:room@chatroom",),
        auth_kind="test",
    )
    payload = {
        "sessions": [
            {"session_id": "room@chatroom", "session_name": "允许群"},
            {"session_id": "other@chatroom", "session_name": "其他群"},
        ],
        "count": 2,
    }

    filtered = wxbot_router._filter_session_payload_for_principal(
        payload,
        principal=principal,
        tenant_id="default",
    )

    assert filtered == {
        "sessions": [{"session_id": "room@chatroom", "session_name": "允许群"}],
        "count": 1,
    }


def test_wxbot_router_exposes_bridge_status_and_admin_endpoints() -> None:
    client, store, bridge, scheduler, agent_store = _build_client()

    with client:
        status = client.get("/bridge/status")
        queue = client.get(
            "/admin/reply-queue/stats?tenant_id=demo",
            headers={"Authorization": "Bearer token"},
        )
        member_events = client.get(
            "/admin/member-events?tenant_id=demo&limit=2",
            headers={"Authorization": "Bearer token"},
        )
        media_ready_events = client.get(
            "/admin/media-ready-events?tenant_id=demo&limit=2&connection_id=wechat-main",
            headers={"Authorization": "Bearer token"},
        )
        reply_policy = client.get(
            "/admin/reply-policy/demo/room@chatroom",
            headers={"Authorization": "Bearer token"},
        )
        global_policy = client.get(
            "/admin/reply-policy/global/demo",
            headers={"Authorization": "Bearer token"},
        )
        update_global_policy = client.post(
            "/admin/reply-policy/global/demo",
            headers={
                "Authorization": "Bearer token",
                "If-Match": global_policy.headers["etag"],
            },
            json={"group_reply_mode": "contains", "trigger_keywords_text": "报价\n人工"},
        )
        update_policy = client.post(
            "/admin/reply-policy/demo/room@chatroom",
            headers={
                "Authorization": "Bearer token",
                "If-Match": reply_policy.headers["etag"],
            },
            json={
                "reply_mode": "contains",
                "mention_sender_mode": "off",
                "trigger_keywords_text": "报价\n退款",
                "participation_policy": {
                    "threshold": 75,
                    "max_soft_replies_10m": 1,
                    "max_bot_ratio_last_40": 0.1,
                },
            },
        )
        agent_tool_catalog = client.get(
            "/admin/agent-tools/catalog",
            headers={"Authorization": "Bearer token"},
        )
        agent_tool_policy = client.get(
            "/admin/agent-tools/policy/demo/room@chatroom",
            headers={"Authorization": "Bearer token"},
        )
        save_agent_tool_policy = client.post(
            "/admin/agent-tools/policy/demo/room@chatroom",
            headers={
                "Authorization": "Bearer token",
                "If-Match": agent_tool_policy.headers["etag"],
                "Idempotency-Key": "agent-policy-save-1",
            },
            json={
                "enabled": True,
                "allowed_tools": ["search_group_messages", "get_group_public_facts"],
            },
        )
        agent_tool_audit = client.get(
            "/admin/agent-tools/audit?tenant_id=demo&session_id=room@chatroom&scope=group_info&limit=5",
            headers={"Authorization": "Bearer token"},
        )
        subscriptions = client.get(
            "/admin/event-subscriptions?event_type=group.member.joined&session_id=room@chatroom",
            headers={"Authorization": "Bearer token"},
        )
        save_subscription = client.post(
            "/admin/event-subscriptions",
            headers={
                "Authorization": "Bearer token",
                "If-Match": subscriptions.headers["etag"],
                "Idempotency-Key": "event-subscription-save-1",
            },
            json={
                "event_type": "group.member.left",
                "target_url": "https://example.com/member-events",
                "session_id": "room@chatroom",
                "enabled": True,
            },
        )
        delete_subscription = client.delete(
            "/admin/event-subscriptions/9",
            headers={
                "Authorization": "Bearer token",
                "If-Match": save_subscription.headers["etag"],
                "Idempotency-Key": "event-subscription-delete-1",
            },
        )
        group_settings = client.get(
            "/admin/group-members/settings/room@chatroom",
            headers={"Authorization": "Bearer token"},
        )
        roster_groups = client.get(
            "/admin/roster/groups",
            headers={"Authorization": "Bearer token"},
        )
        roster_members = client.get(
            "/admin/roster/groups/room@chatroom/members",
            headers={"Authorization": "Bearer token"},
        )
        save_group_settings = client.post(
            "/admin/group-members/settings/room@chatroom",
            headers={
                "Authorization": "Bearer token",
                "If-Match": group_settings.headers["etag"],
                "Idempotency-Key": "group-member-settings-save-1",
            },
            json={
                "welcome_enabled": True,
                "welcome_template": "欢迎 {{member_name}}",
                "welcome_mention": False,
            },
        )
        report_subscriptions = client.get(
            "/admin/reports/subscriptions",
            headers={"Authorization": "Bearer token"},
        )
        save_report_subscription = client.post(
            "/admin/reports/subscriptions",
            headers={
                "Authorization": "Bearer token",
                "If-Match": report_subscriptions.headers["etag"],
                "Idempotency-Key": "report-subscription-save-1",
            },
            json={
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "daily_enabled": True,
                "weekly_enabled": True,
                "monthly_enabled": False,
                "daily_hour": 9,
                "weekly_day": 1,
                "weekly_hour": 9,
                "monthly_day": 1,
                "tz": "Asia/Shanghai",
            },
        )
        preview_report = client.get(
            "/admin/reports/preview/room@chatroom?report_type=daily&session_name=%E6%B5%8B%E8%AF%95%E7%BE%A4",
            headers={"Authorization": "Bearer token"},
        )
        send_report = client.post(
            "/admin/reports/send",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "report-send-1",
            },
            json={
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "report_type": "daily",
            },
        )
        delete_report_subscription = client.delete(
            "/admin/reports/subscriptions/room@chatroom",
            headers={
                "Authorization": "Bearer token",
                "If-Match": save_report_subscription.headers["etag"],
                "Idempotency-Key": "report-subscription-delete-1",
            },
        )
        sdk_query = client.post(
            "/admin/sdk/query/read",
            headers={"Authorization": "Bearer token"},
            json={"database": "message", "sql": "SELECT 1 AS n", "limit": 10},
        )
        sessions = client.get(
            "/admin/sessions",
            headers={"Authorization": "Bearer token"},
        )
        sdk_queue = client.get(
            "/admin/sdk/queue/stats",
            headers={"Authorization": "Bearer token"},
        )
        queue_messages = client.get(
            "/admin/reply-queue/messages?tenant_id=demo&status=pending&session_id=room@chatroom&limit=2",
            headers={"Authorization": "Bearer token"},
        )
        clear_queue = client.post(
            "/admin/reply-queue/clear",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "reply-queue-clear-1",
            },
            json={
                "tenant_id": "demo",
                "status": "pending",
                "session_id": "room@chatroom",
            },
        )
        sdk_queue_messages = client.get(
            "/admin/sdk/queue/messages?status=pending&limit=2",
            headers={"Authorization": "Bearer token"},
        )
        clear_sdk_queue = client.post(
            "/admin/sdk/queue/clear",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "sdk-queue-clear-1",
            },
            json={
                "status": "pending",
                "session_id": "room@chatroom",
            },
        )
        sdk_trigger_debug = client.get(
            "/admin/sdk/debug/trigger-config",
            headers={"Authorization": "Bearer token"},
        )
        save_sdk_trigger_debug = client.post(
            "/admin/sdk/debug/trigger-config",
            headers={"Authorization": "Bearer token"},
            json={"group_require_at_me": False},
        )
        send = client.post(
            "/admin/send",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "direct-send-1",
            },
            json={"session_id": "wx-1", "msg_type": "text", "text": "hello"},
        )
        send_envelope = client.post(
            "/admin/send/envelope",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "envelope-send-1",
            },
            json={
                "target": {
                    "session_id": "room@chatroom",
                    "session_name": "测试群",
                    "session_kind": "group",
                },
                "sender": {
                    "wxid": "wxid_customer",
                    "name": "张三",
                },
                "content": {
                    "msg_type": "text",
                    "text": "结构化消息",
                },
                "reply": {
                    "mention_sender": True,
                    "reply_to_msg_svr_id": "123456",
                },
                "delivery": {
                    "channel": "wechat",
                    "protocol": "envelope",
                },
            },
        )
        send_envelope_batch = client.post(
            "/admin/send/envelope/batch",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "envelope-batch-send-1",
            },
            json={
                "messages": [
                    {
                        "target": {
                            "session_id": "room@chatroom",
                            "session_name": "测试群",
                            "session_kind": "group",
                        },
                        "sender": {
                            "wxid": "wxid_customer",
                            "name": "张三",
                        },
                        "content": {
                            "msg_type": "text",
                            "text": "结构化消息",
                        },
                        "reply": {
                            "mention_sender": True,
                            "reply_to_msg_svr_id": "123456",
                        },
                        "delivery": {
                            "channel": "wechat",
                            "protocol": "envelope",
                        },
                    }
                ]
            },
        )
        send_batch = client.post(
            "/admin/send/batch",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "batch-send-1",
            },
            json={"messages": [{"session_id": "wx-1", "msg_type": "text", "text": "hello"}]},
        )

    assert status.status_code == 200
    assert status.json()["event_mode"] == "sse"
    assert status.json()["media_ready_stats"] == {"message.media.ready": 2}

    assert queue.status_code == 200
    assert queue.json()["pending"] == 1

    assert member_events.status_code == 200
    assert member_events.json()["count"] == 2
    assert member_events.json()["events"][0]["event_type"] == "group.member.joined"
    assert store.member_event_connections == ["legacy-wechat-default"]

    assert media_ready_events.status_code == 200
    assert media_ready_events.json()["count"] == 2
    assert media_ready_events.json()["events"][0]["event_type"] == "message.media.ready"
    assert media_ready_events.json()["events"][0]["media_id"].startswith("mid1.")
    assert "media_url" not in media_ready_events.json()["events"][0]
    assert store.media_event_connections == ["wechat-main"]

    assert reply_policy.status_code == 200
    assert reply_policy.json()["effective_mode"] == "off"
    assert reply_policy.headers["etag"] == '"0"'
    assert reply_policy.headers["cache-control"] == "no-store"

    assert global_policy.status_code == 200
    assert global_policy.json()["group_reply_mode"] == "off"
    assert global_policy.json()["group_reply_mention_sender"] is False
    assert global_policy.headers["etag"] == '"0"'

    assert update_global_policy.status_code == 200
    assert update_global_policy.json()["group_reply_mode"] == "contains"
    assert update_global_policy.json()["group_reply_mention_sender"] is False
    assert update_global_policy.json()["trigger_keywords"] == ["报价", "人工"]
    assert update_global_policy.headers["etag"] == '"1"'

    assert update_policy.status_code == 200
    assert update_policy.json()["effective_mode"] == "contains"
    assert update_policy.json()["effective_mention_sender"] is False
    assert update_policy.json()["trigger_keywords"] == ["报价", "退款"]
    assert update_policy.json()["participation_policy"]["threshold"] == 75
    assert update_policy.json()["participation_policy"]["max_soft_replies_10m"] == 1
    assert update_policy.json()["participation_policy"]["max_bot_ratio_last_40"] == 0.1
    assert update_policy.headers["etag"] == '"1"'
    assert store.policies[("demo", "room@chatroom")]["reply_mode"] == "contains"

    assert agent_tool_catalog.status_code == 200
    assert agent_tool_catalog.json()["count"] == 16
    assert agent_tool_catalog.json()["items"][0]["name"] == "get_group_info"
    assert agent_tool_catalog.json()["items"][0]["owner"] == "wxbot"
    assert agent_tool_catalog.json()["items"][0]["channels"] == ["wechat"]
    assert agent_tool_catalog.json()["items"][0]["session_kinds"] == ["group"]
    assert "group_info" in agent_tool_catalog.json()["scopes"]
    assert "group_draw_generation" in agent_tool_catalog.json()["scopes"]

    assert agent_tool_policy.status_code == 200
    assert agent_tool_policy.json()["enabled"] is True
    assert agent_tool_policy.json()["effective_tools"] == [
        "get_group_info",
        "list_group_members",
        "get_group_member_avatar",
        "search_group_messages",
        "research_group_messages",
        "get_group_public_facts",
        "get_group_reply_policy",
        "get_group_credits_status",
        "get_group_credits_member",
        "get_group_moderation_status",
        "get_group_repeater_status",
        "get_group_welcome_status",
        "get_group_report_status",
        "get_group_credits_leaderboard",
        "get_group_recent_moderation_events",
        "get_group_activity_ranking",
    ]

    assert save_agent_tool_policy.status_code == 200
    assert save_agent_tool_policy.json()["allowed_tools"] == [
        "search_group_messages",
        "get_group_public_facts",
    ]
    assert save_agent_tool_policy.json()["effective_tools"] == [
        "search_group_messages",
        "get_group_public_facts",
    ]
    assert agent_store.policies[("demo", "room@chatroom", "group_info")]["enabled"] is True

    assert agent_tool_audit.status_code == 200
    assert agent_tool_audit.json()["count"] == 1
    assert agent_tool_audit.json()["items"][0]["tool_name"] == "search_group_messages"
    assert agent_tool_audit.json()["items"][0]["tool_args"] == {"query": "draw"}

    assert subscriptions.status_code == 200
    assert subscriptions.json()["items"][0]["session_id"] == "room@chatroom"

    assert save_subscription.status_code == 200
    assert save_subscription.json()["saved"] is True

    assert delete_subscription.status_code == 200
    assert delete_subscription.json()["deleted"] is True

    assert group_settings.status_code == 200
    assert group_settings.json()["welcome_enabled"] is True

    assert send_envelope.status_code == 200
    assert send_envelope.json()["protocol"] == "envelope"
    assert send_envelope.json()["normalized"]["session_id"] == "room@chatroom"

    assert send_envelope_batch.status_code == 200
    assert send_envelope_batch.json()["count"] == 1
    assert send_envelope_batch.json()["results"][0]["protocol"] == "envelope"

    assert roster_groups.status_code == 200
    assert roster_groups.json()["sessions"][0]["session_id"] == "room@chatroom"

    assert roster_members.status_code == 200
    assert roster_members.json()["candidates"][0]["wxid"] == "wxid_member_1"

    assert save_group_settings.status_code == 200
    assert save_group_settings.json()["welcome_enabled"] is True
    assert save_group_settings.json()["version"] == 1

    assert report_subscriptions.status_code == 200
    assert report_subscriptions.json()["subscriptions"][0]["session_id"] == "room@chatroom"

    assert save_report_subscription.status_code == 200
    assert save_report_subscription.json()["subscription"]["daily_enabled"] is True
    assert save_report_subscription.json()["subscription"]["weekly_enabled"] is True
    assert scheduler.notified == 2

    assert preview_report.status_code == 200
    assert preview_report.json()["report_type"] == "daily"
    assert preview_report.json()["status"] == "completed"
    assert "测试群" in preview_report.json()["report"]

    assert send_report.status_code == 200
    assert send_report.json()["queued_count"] == 1

    assert delete_report_subscription.status_code == 200
    assert delete_report_subscription.json()["deleted"] == "room@chatroom"

    assert sdk_query.status_code == 200
    assert sdk_query.json()["rows"][0]["session_id"] == "room@chatroom"

    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["session_id"] == "wx-1"

    assert sdk_queue.status_code == 200
    assert sdk_queue.json()["pending"] == 3

    assert queue_messages.status_code == 200
    assert queue_messages.json()["count"] == 1
    assert queue_messages.json()["items"][0]["trace_id"] == "trace-11"

    assert clear_queue.status_code == 200
    assert clear_queue.json()["cleared"] == 2
    assert clear_queue.json()["ids"] == [11, 12]

    assert sdk_queue_messages.status_code == 200
    assert sdk_queue_messages.json()["count"] == 1
    assert sdk_queue_messages.json()["items"][0]["reply_text"] == "SDK 待发送消息"

    assert clear_sdk_queue.status_code == 200
    assert clear_sdk_queue.json()["cleared"] == 1
    assert clear_sdk_queue.json()["ids"] == [7]

    assert sdk_trigger_debug.status_code == 200
    assert sdk_trigger_debug.json()["group_require_at_me"] is True

    assert save_sdk_trigger_debug.status_code == 409
    assert "durable reply-policy aggregate" in save_sdk_trigger_debug.json()["detail"]

    assert send.status_code == 200
    assert send.json()["queued"] is True

    assert send_batch.status_code == 200
    assert send_batch.json()["count"] == 1

    report_messages_call = next(
        call for call in bridge.calls if call[1] == "/ext/reports/messages/room@chatroom"
    )
    report_send_call = next(
        call
        for call in bridge.calls
        if call[1] == "/send" and (call[3] or {}).get("session_id") == "room@chatroom"
    )
    expected_bridge_calls = [
        (
            "GET",
            "/event-subscriptions",
            {"event_type": "group.member.joined", "session_id": "room@chatroom"},
            None,
        ),
        (
            "POST",
            "/event-subscriptions",
            None,
            {
                "event_type": "group.member.left",
                "target_url": "https://example.com/member-events",
                "session_id": "room@chatroom",
                "enabled": True,
            },
        ),
        ("DELETE", "/event-subscriptions/9", None, None),
        ("GET", "/group-members/settings/room@chatroom", None, None),
        ("GET", "/ext/roster/groups", None, None),
        ("GET", "/ext/roster/groups/room@chatroom/members", None, None),
        (
            "POST",
            "/group-members/settings/room@chatroom",
            None,
            {
                "welcome_enabled": True,
                "welcome_template": "欢迎 {{member_name}}",
                "welcome_mention": False,
            },
        ),
        (
            "GET",
            "/ext/reports/messages/room@chatroom",
            {
                "report_type": "daily",
                "session_name": "测试群",
                "date": report_messages_call[2]["date"],
                "year_month": "",
            },
            None,
        ),
        (
            "POST",
            "/send",
            None,
            {
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "sender_name": "",
                "text": "# 测试群日报\n\n## 整体概览\n- 今天有讨论",
                "msg_type": "text",
            },
        ),
        (
            "POST",
            "/ext/query/read",
            None,
            {
                "database": "message",
                "sql": "SELECT 1 AS n",
                "limit": 10,
            },
        ),
        ("GET", "/sessions", None, None),
        ("GET", "/queue/stats", None, None),
        ("GET", "/queue/messages", {"status": "pending", "limit": 2}, None),
        (
            "POST",
            "/queue/clear",
            None,
            {
                "status": "pending",
                "session_id": "room@chatroom",
            },
        ),
        ("GET", "/debug/trigger-config", None, None),
        (
            "POST",
            "/send",
            None,
            {
                "session_id": "wx-1",
                "session_name": "",
                "sender_name": "",
                "sender_wxid": "",
                "mention_sender": False,
                "reply_to_msg_svr_id": "",
                "session_kind": "",
                "text": "hello",
                "msg_type": "text",
                "source_message": {},
                "delivery": {},
            },
        ),
        (
            "POST",
            "/send/envelope",
            None,
            {
                "target": {
                    "session_id": "room@chatroom",
                    "session_name": "测试群",
                    "session_kind": "group",
                },
                "content": {
                    "msg_type": "text",
                    "text": "结构化消息",
                },
                "sender": {
                    "wxid": "wxid_customer",
                    "name": "张三",
                },
                "reply": {
                    "mention_sender": True,
                    "reply_to_msg_svr_id": "123456",
                },
                "source_message": {},
                "delivery": {
                    "channel": "wechat",
                    "protocol": "envelope",
                },
                "metadata": {},
            },
        ),
        (
            "POST",
            "/send/envelope/batch",
            None,
            {
                "messages": [
                    {
                        "target": {
                            "session_id": "room@chatroom",
                            "session_name": "测试群",
                            "session_kind": "group",
                        },
                        "content": {
                            "msg_type": "text",
                            "text": "结构化消息",
                        },
                        "sender": {
                            "wxid": "wxid_customer",
                            "name": "张三",
                        },
                        "reply": {
                            "mention_sender": True,
                            "reply_to_msg_svr_id": "123456",
                        },
                        "source_message": {},
                        "delivery": {
                            "channel": "wechat",
                            "protocol": "envelope",
                        },
                        "metadata": {},
                    }
                ]
            },
        ),
        (
            "POST",
            "/send/batch",
            None,
            {
                "messages": [
                    {
                        "session_id": "wx-1",
                        "session_name": "",
                        "sender_name": "",
                        "sender_wxid": "",
                        "mention_sender": False,
                        "reply_to_msg_svr_id": "",
                        "session_kind": "",
                        "text": "hello",
                        "msg_type": "text",
                        "source_message": {},
                        "delivery": {},
                    }
                ]
            },
        ),
    ]
    expected_bridge_calls[8] = (
        "POST",
        "/send",
        None,
        {
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "sender_name": "",
            "text": report_send_call[3]["text"],
            "msg_type": "text",
        },
    )
    for expected_call in expected_bridge_calls:
        assert expected_call in bridge.calls


def test_wxbot_admin_event_queries_reject_invalid_connection_scope() -> None:
    client, store, _bridge, _scheduler, _agent_store = _build_client()

    with client:
        member_events = client.get(
            "/admin/member-events?tenant_id=demo&limit=2&connection_id=bad%20scope",
            headers={"Authorization": "Bearer token"},
        )
        media_events = client.get(
            "/admin/media-ready-events?tenant_id=demo&limit=2&connection_id=bad%20scope",
            headers={"Authorization": "Bearer token"},
        )

    assert member_events.status_code == 422
    assert media_events.status_code == 422
    assert store.member_event_connections == []
    assert store.media_event_connections == []


def test_wxbot_report_preview_returns_backoff_without_resetting_failed_job() -> None:
    client, store, _bridge, _scheduler, _agent_store = _build_client()
    retry_after = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
    job = {
        "id": 1,
        "tenant_id": "default",
        "session_id": "room@chatroom",
        "session_name": "测试群",
        "report_type": "daily",
        "period_key": "2026-05-13",
        "period_label": "2026-05-13",
        "status": "failed",
        "current_stage": "summarize_chunk_1",
        "msg_count": 3,
        "result_text": "",
        "report_payload": {
            "transient_error": True,
            "retry_after": retry_after,
            "last_error": "upstream timeout",
            "last_failed_stage": "summarize_chunk_1",
        },
        "delivery_status": "pending",
        "delivery_error": "",
        "error": "upstream timeout",
    }
    store.report_jobs[1] = dict(job)
    store.report_job_scope_index[("default", "room@chatroom", "daily", "2026-05-13")] = 1

    with client:
        resp = client.get(
            "/admin/reports/preview/room@chatroom?report_type=daily&date=2026-05-13&session_name=%E6%B5%8B%E8%AF%95%E7%BE%A4",
            headers={"Authorization": "Bearer token"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["current_stage"] == "summarize_chunk_1"
    assert body["error"] == "upstream timeout"
    assert body["retry_after"] == retry_after
    assert store.report_jobs[1] == job


def test_wxbot_report_manual_send_appends_configured_footer_to_cached_result() -> None:
    client, store, bridge, _scheduler, _agent_store = _build_client()
    footer = "Project: https://example.invalid/project"
    store.settings.wxbot_daily_report_footer = footer
    store.report_jobs[1] = {
        "id": 1,
        "tenant_id": "default",
        "session_id": "room@chatroom",
        "session_name": "测试群",
        "report_type": "daily",
        "period_key": "2026-05-13",
        "period_label": "2026-05-13",
        "status": "completed",
        "current_stage": "completed",
        "msg_count": 3,
        "result_text": "[测试群] 日报 · 2026-05-13\n\n缓存正文",
        "report_payload": {},
        "delivery_status": "pending",
        "delivery_error": "",
        "error": "",
    }
    store.report_job_scope_index[("default", "room@chatroom", "daily", "2026-05-13")] = 1

    with client:
        resp = client.post(
            "/admin/reports/send",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "report-manual-send-1",
            },
            json={
                "session_id": "room@chatroom",
                "session_name": "过期群名",
                "report_type": "daily",
                "date": "2026-05-13",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["delivery_status"] == "queued"
    assert resp.json()["sdk_outbound_id"] == 7
    assert store.report_jobs[1]["delivery_status"] == "queued"
    assert store.report_jobs[1]["sdk_outbound_id"] == 7
    assert bridge.request_headers[-1] == {"Idempotency-Key": "wxbot-report:1"}
    text = bridge.calls[-1][3]["text"]
    assert bridge.calls[-1][3]["session_name"] == "测试群"
    assert text == f"[测试群] 日报 · 2026-05-13\n\n缓存正文\n\n{footer}"
    assert text.count(footer) == 1


def test_wxbot_self_review_subscription_defaults_to_manual_review() -> None:
    client, store, _bridge, _scheduler, _agent_store = _build_client()
    _seed_completed_self_review_job(store)
    store.self_review_jobs[1]["review_payload"]["auto_create_kb_doc"] = True

    with client:
        legacy = client.get(
            "/admin/self-review/subscriptions",
            headers={"Authorization": "Bearer token"},
        )
        created = client.post(
            "/admin/self-review/subscriptions",
            headers={
                "Authorization": "Bearer token",
                "If-Match": legacy.headers["etag"],
                "Idempotency-Key": "self-review-subscription-create-1",
            },
            json={
                "session_id": "new-room@chatroom",
                "session_name": "新群",
                "enabled": True,
            },
        )
        legacy_true = client.post(
            "/admin/self-review/subscriptions",
            headers={
                "Authorization": "Bearer token",
                "If-Match": created.headers["etag"],
                "Idempotency-Key": "self-review-subscription-update-1",
            },
            json={
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "enabled": True,
                "auto_create_kb_doc": True,
            },
        )
        jobs = client.get(
            "/admin/self-review/jobs",
            headers={"Authorization": "Bearer token"},
        )

    assert legacy.status_code == 200
    assert legacy.json()["subscriptions"][0]["auto_create_kb_doc"] is False
    assert created.status_code == 200
    assert created.json()["subscription"]["auto_create_kb_doc"] is False
    assert legacy_true.status_code == 200
    assert legacy_true.json()["subscription"]["auto_create_kb_doc"] is False
    assert jobs.status_code == 200
    assert jobs.json()["items"][0]["review_payload"]["auto_create_kb_doc"] is False
    assert jobs.json()["items"][0]["review_payload"]["kb_publish_status"] == "pending_review"
    assert (
        store.self_review_subscriptions[("default", "room@chatroom")]["auto_create_kb_doc"] is False
    )


def test_wxbot_self_review_preview_ignores_legacy_auto_publish_flag() -> None:
    app = FastAPI()
    store = _FakeStore()
    service = _CaptureSelfReviewService(store)
    scheduler = _FakeScheduler()
    app.include_router(
        build_wxbot_router(
            store,
            container=SimpleNamespace(),
            bridge=_FakeBridge(),
            scheduler=scheduler,
            self_review_service=service,
        )
    )

    with TestClient(app) as client:
        resp = client.get(
            "/admin/self-review/preview/room@chatroom"
            "?session_name=%E6%B5%8B%E8%AF%95%E7%BE%A4"
            "&date=2026-04-21"
            "&auto_create_kb_doc=true",
            headers={"Authorization": "Bearer token"},
        )
        generated_payload = dict(store.self_review_jobs[1]["review_payload"])
        store.self_review_jobs[1]["review_payload"]["auto_create_kb_doc"] = True
        cached = client.get(
            "/admin/self-review/preview/room@chatroom"
            "?session_name=%E6%B5%8B%E8%AF%95%E7%BE%A4"
            "&date=2026-04-21",
            headers={"Authorization": "Bearer token"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert resp.json()["kb_doc_id"] is None
    assert resp.json()["kb_publish_status"] == "pending_review"
    assert cached.status_code == 200
    assert cached.json()["cached"] is True
    assert cached.json()["auto_create_kb_doc"] is False
    assert generated_payload["requested_auto_create_kb_doc"] is False
    assert generated_payload["auto_create_kb_doc"] is False
    assert store.self_review_jobs[1]["review_payload"]["auto_create_kb_doc"] is True
    assert store.self_review_jobs[1]["review_payload"]["kb_publish_status"] == "pending_review"
    assert service.run_job_ids == [1]
    assert scheduler.scheduled_keys == ["self-review-job-1"]


def test_wxbot_self_review_publish_endpoint_is_explicit_and_idempotent() -> None:
    kb_service = _FakeKbService()
    client, store = _build_self_review_publish_client(kb_service)
    _seed_completed_self_review_job(store)

    with client:
        first = client.post(
            "/admin/self-review/jobs/1/publish",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "self-review-publish-1",
            },
        )
        second = client.post(
            "/admin/self-review/jobs/1/publish",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "self-review-publish-1",
            },
        )

    assert first.status_code == 200
    assert first.json()["kb_doc_id"] == 99
    assert first.json()["kb_publish_status"] == "published"
    assert first.json()["idempotent"] is False
    assert second.status_code == 200
    assert second.json()["kb_doc_id"] == 99
    assert second.json() == first.json()
    assert len(kb_service.calls) == 1
    metadata = kb_service.calls[0]["metadata"]
    assert metadata["reviewed"] is True
    assert metadata["reviewed_by"] == "admin"
    assert metadata["reviewed_request_id"] == first.json()["request_id"]
    assert metadata["published_by"] == "admin"
    assert metadata["published_request_id"] == first.json()["request_id"]
    assert store.self_review_jobs[1]["kb_doc_id"] == 99
    assert store.self_review_jobs[1]["review_payload"]["kb_publish_status"] == "published"


def test_wxbot_self_review_publish_rejects_cross_tenant_and_unready_jobs() -> None:
    kb_service = _FakeKbService()
    client, store = _build_self_review_publish_client(kb_service)
    _seed_completed_self_review_job(store, tenant_id="another-tenant")

    with client:
        cross_tenant = client.post(
            "/admin/self-review/jobs/1/publish",
            headers={"Authorization": "Bearer token"},
        )
        _seed_completed_self_review_job(
            store,
            status="pending",
            result_text="# 尚未完成的草稿",
        )
        pending = client.post(
            "/admin/self-review/jobs/1/publish",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "self-review-publish-pending-1",
            },
        )
        _seed_completed_self_review_job(store, result_text="")
        empty = client.post(
            "/admin/self-review/jobs/1/publish",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "self-review-publish-empty-1",
            },
        )

    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["detail"] == "self_review_job_not_found"
    assert pending.status_code == 409
    assert pending.json()["detail"] == "self_review_job_not_ready"
    assert empty.status_code == 409
    assert empty.json()["detail"] == "self_review_job_not_ready"
    assert kb_service.calls == []


def test_wxbot_self_review_publish_failure_is_not_reported_as_success() -> None:
    kb_service = _FakeKbService(error=RuntimeError("vector index unavailable"))
    client, store = _build_self_review_publish_client(kb_service)
    _seed_completed_self_review_job(store)

    with client:
        response = client.post(
            "/admin/self-review/jobs/1/publish",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "self-review-publish-failure-1",
            },
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "self_review_publish_failed"
    assert len(kb_service.calls) == 1
    assert store.self_review_jobs[1]["kb_doc_id"] is None
    assert store.self_review_jobs[1]["review_payload"]["kb_publish_status"] == "pending_review"
    assert store.self_review_jobs[1]["review_payload"]["kb_doc_error"] == "vector index unavailable"


def test_wxbot_router_bridge_status_reads_shared_bridge_runtime(monkeypatch) -> None:
    class _FakeRedis:
        async def get(self, key: str):
            if key == "wxbot:bridge:status:default":
                return json.dumps(
                    {
                        "running": True,
                        "sdk_url": "http://192.0.2.94:5080",
                        "sdk_online": True,
                        "tenant_id": "default",
                        "ingest_mode": "unified-sse",
                        "event_mode": "sse",
                        "stream_mode": "unified",
                        "bridge_leader": True,
                        "poll_interval": 3.0,
                        "send_interval": 2.0,
                        "instance_id": "bridge-1",
                        "process_role": "wxbot_bridge",
                        "host": "srv-1",
                        "pid": 1234,
                        "updated_at": "2026-04-22T00:00:00Z",
                        "started_at": "2026-04-22T00:00:00Z",
                        "leader_token": "tok-1",
                    },
                    ensure_ascii=False,
                )
            if key == "wxbot:bridge:ingest_cursor:default":
                return "42"
            if key == "wxbot:bridge:event_cursor:default":
                return "40"
            if key == "wxbot:bridge:leader:default":
                return json.dumps(
                    {
                        "token": "tok-1",
                        "instance_id": "bridge-1",
                        "process_role": "wxbot_bridge",
                        "host": "srv-1",
                        "pid": 1234,
                        "updated_at": "2026-04-22T00:00:01Z",
                    },
                    ensure_ascii=False,
                )
            return None

        async def ttl(self, key: str):
            assert key == "wxbot:bridge:leader:default"
            return 25

    monkeypatch.setattr(wxbot_router, "get_redis", lambda: _FakeRedis())

    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_wxbot_router(store, container=None, bridge=None))

    with TestClient(app) as client:
        resp = client.get("/bridge/status")

    assert resp.status_code == 200
    assert resp.json()["running"] is True
    assert resp.json()["bridge_leader"] is True
    assert resp.json()["cursor"] == 42
    assert resp.json()["event_cursor"] == 42
    assert resp.json()["leader"]["ttl"] == 25
    assert resp.json()["process_role"] == "wxbot_bridge"
    assert resp.json()["media_ready_stats"] == {"message.media.ready": 2}


def test_wxbot_router_bridge_status_uses_managed_connection_scope(monkeypatch) -> None:
    connection_id = "8f410eee-a701-552e-bfd6-55905c88acfc"

    class _FakeRedis:
        async def get(self, key: str):
            suffix = f":default:{connection_id}"
            if key == f"wxbot:bridge:status{suffix}":
                return json.dumps(
                    {
                        "running": True,
                        "sdk_online": True,
                        "sdk_auth_state": "ok",
                        "connection_id": connection_id,
                    }
                )
            if key == f"wxbot:bridge:leader{suffix}":
                return json.dumps({"token": "managed-token"})
            return None

        async def ttl(self, key: str):
            assert key == f"wxbot:bridge:leader:default:{connection_id}"
            return 20

    monkeypatch.setattr(wxbot_router, "get_redis", lambda: _FakeRedis())
    store = _FakeStore()
    store.settings.channel_connection_id = connection_id
    app = FastAPI()
    app.include_router(build_wxbot_router(store, container=None, bridge=None))

    with TestClient(app) as client:
        response = client.get("/bridge/status")

    assert response.status_code == 200
    assert response.json()["running"] is True
    assert response.json()["connection_id"] == connection_id


def test_wxbot_router_exposes_media_ready_admin_endpoint() -> None:
    client, _, _, _, _ = _build_client()

    with client:
        status = client.get("/bridge/status")
        resp = client.get(
            "/admin/media-ready-events?tenant_id=demo&limit=2",
            headers={"Authorization": "Bearer token"},
        )

    assert status.status_code == 200
    assert status.json()["media_ready_stats"] == {"message.media.ready": 2}
    assert resp.status_code == 200
    assert resp.json()["count"] == 2
    assert resp.json()["events"][0]["event_type"] == "message.media.ready"
    assert resp.json()["events"][0]["message_id"] == "msg-201"
    assert resp.json()["events"][0]["media_id"].startswith("mid1.")
    assert "media_url" not in resp.json()["events"][0]


def test_wxbot_router_proxies_and_converts_admin_bmp_preview(monkeypatch) -> None:
    client, store, _, _, _ = _build_client()
    source = BytesIO()
    Image.new("RGB", (3, 2), (36, 123, 220)).save(source, format="BMP")
    requested_urls: list[str] = []

    class _ImageTransport(wxbot_router.httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            requested_urls.append(str(request.url))
            return wxbot_router.httpx.Response(
                200,
                content=source.getvalue(),
                headers={"Content-Type": "image/bmp"},
                request=request,
            )

    class _ImageClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            self._transport = _ImageTransport()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(wxbot_router.httpx, "AsyncClient", _ImageClient)
    media_id = issue_media_id(
        "c2c98d555ee48e52bb9fcaab6e2e49c7/3618_preview.bmp",
        store.settings,
        tenant_id="default",
    )

    with client:
        response = client.get(
            f"/admin/images/{media_id}",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert requested_urls == [
        "http://127.0.0.1:5080/images/c2c98d555ee48e52bb9fcaab6e2e49c7/3618_preview.bmp"
    ]


def test_wxbot_router_admin_image_requires_auth_and_rejects_traversal() -> None:
    client, store, _, _, _ = _build_client()
    media_id = issue_media_id(
        "example/preview.bmp",
        store.settings,
        tenant_id="default",
    )

    with client:
        missing_auth = client.get(f"/admin/images/{media_id}")
        traversal = client.get(
            "/admin/images/not-a-media-id",
            headers={"Authorization": "Bearer token"},
        )

    assert missing_auth.status_code == 401
    assert traversal.status_code == 400
    assert traversal.json()["detail"] == "invalid media id"


def test_wxbot_router_admin_file_streams_authenticated_sdk_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _, _, _ = _build_client()
    requested_urls: list[str] = []
    real_async_client = wxbot_router.httpx.AsyncClient

    async def handler(request):
        requested_urls.append(str(request.url))
        return wxbot_router.httpx.Response(
            200,
            content=b"report-bytes",
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": 'attachment; filename="report.pdf"',
            },
            request=request,
        )

    def client_factory(**kwargs):
        kwargs.pop("follow_redirects", None)
        return real_async_client(
            **kwargs,
            transport=wxbot_router.httpx.MockTransport(handler),
        )

    monkeypatch.setattr(wxbot_router.httpx, "AsyncClient", client_factory)
    media_id = issue_media_id(
        "incoming/report.pdf",
        store.settings,
        tenant_id="default",
        resource_type="file",
    )

    with client:
        response = client.get(
            f"/admin/files/{media_id}",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 200
    assert response.content == b"report-bytes"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="report.pdf"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert requested_urls == ["http://127.0.0.1:5080/files/incoming/report.pdf"]


def test_wxbot_router_rejects_file_media_id_on_image_route() -> None:
    client, store, _, _, _ = _build_client()
    media_id = issue_media_id(
        "incoming/report.pdf",
        store.settings,
        tenant_id="default",
        resource_type="file",
    )

    with client:
        response = client.get(
            f"/admin/images/{media_id}",
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "media id is not an image"


def test_wxbot_admin_image_send_accepts_only_tenant_scoped_media_id() -> None:
    client, store, bridge, _, _ = _build_client()
    media_id = issue_media_id(
        "images/hash-1/generated.png",
        store.settings,
        tenant_id="default",
    )

    with client:
        accepted = client.post(
            "/admin/send",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "image-send-1",
            },
            json={
                "tenant_id": "default",
                "session_id": "room@chatroom",
                "msg_type": "image",
                "media_id": media_id,
            },
        )
        legacy_path = client.post(
            "/admin/send",
            headers={"Authorization": "Bearer token"},
            json={
                "tenant_id": "default",
                "session_id": "room@chatroom",
                "msg_type": "image",
                "image_path": "images/hash-1/generated.png",
            },
        )
        tenant_mismatch = client.post(
            "/admin/send",
            headers={"Authorization": "Bearer token"},
            json={
                "tenant_id": "other",
                "session_id": "room@chatroom",
                "msg_type": "image",
                "media_id": media_id,
            },
        )

    assert accepted.status_code == 200
    assert bridge.calls[-1][3]["image_path"] == "images/hash-1/generated.png"
    assert "media_id" not in bridge.calls[-1][3]
    assert legacy_path.status_code == 422
    assert tenant_mismatch.status_code == 400
    assert tenant_mismatch.json()["detail"] == "media id tenant mismatch"


def test_wxbot_admin_file_send_forwards_sdk_local_file_contract() -> None:
    client, _store, bridge, _, _ = _build_client()

    with client:
        response = client.post(
            "/admin/send",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "file-send-1",
            },
            json={
                "tenant_id": "default",
                "session_id": "wx-1",
                "msg_type": "file",
                "file_path": "E:\\wxbot-share\\report.pdf",
                "file_name": "report.pdf",
                "file_size": 123,
                "file_md5": "9e107d9d372bb6826bd81d3542a419d6",
                "file_sha256": (
                    "1d3c43633f2b30c61186f81bb9d635327d0485094d65619745c0bf44f42996ae"
                ),
            },
        )

    assert response.status_code == 200
    assert bridge.calls[-1][1] == "/send"
    payload = bridge.calls[-1][3]
    assert payload is not None
    assert payload["msg_type"] == "file"
    assert payload["file_path"] == "E:\\wxbot-share\\report.pdf"
    assert payload["file_name"] == "report.pdf"
    assert payload["file_size"] == 123
    assert payload["file_md5"] == "9e107d9d372bb6826bd81d3542a419d6"
    assert payload["file_sha256"].startswith("1d3c4363")


@pytest.mark.parametrize(
    ("path", "body", "sdk_path"),
    [
        (
            "/admin/send",
            {
                "tenant_id": "default",
                "session_id": "room@chatroom",
                "msg_type": "file",
                "file_path": "E:\\wxbot-share\\report.pdf",
            },
            "/send",
        ),
        (
            "/admin/send/envelope",
            {
                "target": {
                    "tenant_id": "default",
                    "session_id": "room@chatroom",
                },
                "content": {
                    "msg_type": "file",
                    "file_path": "E:\\wxbot-share\\report.pdf",
                },
            },
            "/send/envelope",
        ),
        (
            "/admin/send/batch",
            {
                "messages": [
                    {
                        "tenant_id": "default",
                        "session_id": "room@chatroom",
                        "msg_type": "file",
                        "file_path": "E:\\wxbot-share\\report.pdf",
                    }
                ]
            },
            "/send/batch",
        ),
        (
            "/admin/send/envelope/batch",
            {
                "messages": [
                    {
                        "target": {
                            "tenant_id": "default",
                            "session_id": "room@chatroom",
                        },
                        "content": {
                            "msg_type": "file",
                            "file_path": "E:\\wxbot-share\\report.pdf",
                        },
                    }
                ]
            },
            "/send/envelope/batch",
        ),
    ],
)
def test_wxbot_admin_cannot_bypass_disabled_group_file_switch(
    path: str,
    body: dict[str, object],
    sdk_path: str,
) -> None:
    client, _store, bridge, _, _ = _build_client(group_file_send_enabled=False)

    with client:
        response = client.post(
            path,
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": f"group-file-disabled:{sdk_path}",
            },
            json=body,
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "group_file_send_disabled"
    assert all(
        not (method == "POST" and called_path == sdk_path)
        for method, called_path, _params, _json in bridge.calls
    )


def test_wxbot_admin_group_file_send_succeeds_after_explicit_enable() -> None:
    client, _store, bridge, _, _ = _build_client(group_file_send_enabled=True)

    with client:
        response = client.post(
            "/admin/send",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": "group-file-enabled",
            },
            json={
                "tenant_id": "default",
                "session_id": "room@chatroom",
                "msg_type": "file",
                "file_path": "E:\\wxbot-share\\report.pdf",
            },
        )

    assert response.status_code == 200
    assert any(
        method == "POST" and called_path == "/send"
        for method, called_path, _params, _json in bridge.calls
    )


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        (
            {"msg_type": "file", "file_path": "relative/report.pdf"},
            "file_path must be absolute on the SDK host",
        ),
        (
            {
                "msg_type": "file",
                "file_path": "/srv/wxbot/report.pdf",
                "file_url": "https://example.com/report.pdf",
            },
            "file_url is not supported for outbound file messages",
        ),
        (
            {
                "msg_type": "file",
                "file_path": "/srv/wxbot/report.pdf",
                "file_sha256": "not-a-digest",
            },
            "file_sha256 must be a 64-character hexadecimal digest",
        ),
    ],
)
def test_wxbot_admin_file_send_rejects_unsafe_contract(
    payload: dict[str, object],
    detail: str,
) -> None:
    client, _store, bridge, _, _ = _build_client()

    with client:
        response = client.post(
            "/admin/send/envelope",
            headers={
                "Authorization": "Bearer token",
                "Idempotency-Key": f"file-invalid-{detail}",
            },
            json={
                "target": {"tenant_id": "default", "session_id": "wx-1"},
                "content": payload,
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == detail
    assert all(call[1] != "/send/envelope" for call in bridge.calls)


def test_client_safe_media_payload_signs_file_url_without_exposing_sdk_locator() -> None:
    store = _FakeStore()

    safe = wxbot_router._client_safe_media_payload(
        {
            "type": "file",
            "file_name": "report.pdf",
            "file_path": "E:\\wxbot-files\\incoming\\report.pdf",
            "file_url": "/files/incoming/report.pdf?name=report.pdf",
        },
        store,
        tenant_id="default",
    )

    assert safe["file_name"] == "report.pdf"
    assert "file_path" not in safe
    assert "file_url" not in safe
    file_media_id = safe["file_media_id"]
    locator = wxbot_router.resolve_media_id(file_media_id, store.settings)
    assert locator.resource_type == "file"
    assert locator.value == "incoming/report.pdf"


class _FakeMessageStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail = fail

    async def enqueue_effect_intent(self, _db, **kwargs):
        self.calls.append(dict(kwargs))
        if self.fail:
            raise RuntimeError("injected_effect_failure")
        return SimpleNamespace(
            status="prepared",
            idempotency_key=kwargs["idempotency_key"],
        )

    def snapshot(self):
        return deepcopy(self.calls)

    def restore(self, snapshot) -> None:
        self.calls = snapshot


class _FakeAggregateRepeaterStore:
    def __init__(self) -> None:
        self.configs: dict[tuple[str, str], dict[str, object]] = {}
        self.read_calls = 0
        self.mutation_calls = 0

    async def get_config(self, tenant_id: str, session_id: str):
        self.read_calls += 1
        return deepcopy(
            self.configs.get(
                (tenant_id, session_id),
                {
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "enabled": False,
                    "cooldown_seconds": 300,
                    "version": 0,
                    "updated_at": None,
                },
            )
        )

    async def get_config_in_transaction(
        self,
        _db,
        tenant_id: str,
        session_id: str,
        *,
        for_update: bool = False,
    ):
        _ = for_update
        return await self.get_config(tenant_id, session_id)

    async def set_config_in_transaction(
        self,
        _db,
        tenant_id: str,
        session_id: str,
        *,
        expected_version: int,
        enabled: bool | None = None,
        cooldown_seconds: int | None = None,
    ) -> RepeaterConfigMutation:
        self.mutation_calls += 1
        before = await self.get_config(tenant_id, session_id)
        if int(before["version"]) != expected_version:
            raise AssertionError("unexpected repeater version")
        after = {
            **before,
            "enabled": bool(enabled),
            "cooldown_seconds": int(cooldown_seconds or 300),
            "version": expected_version + 1,
            "updated_at": "2026-07-18T00:00:00+00:00",
        }
        self.configs[(tenant_id, session_id)] = after
        return RepeaterConfigMutation(before=before, after=after)

    def snapshot(self):
        return deepcopy(self.configs)

    def restore(self, snapshot) -> None:
        self.configs = snapshot


class _FakeAggregateStore(_FakeStore):
    def __init__(self, repeater_store: _FakeAggregateRepeaterStore) -> None:
        super().__init__()
        self.repeater_store = repeater_store
        self.idempotency: dict[tuple[str, str], dict[str, object]] = {}
        self.aggregate_states: dict[tuple[str, str], dict[str, object]] = {}

    async def get_global_policy_in_transaction(
        self,
        _db,
        tenant_id: str,
        *,
        for_update: bool = False,
    ):
        _ = for_update
        return await self.get_global_policy(tenant_id)

    async def set_global_policy_in_transaction(self, _db, **kwargs):
        tenant_id = str(kwargs.pop("tenant_id"))
        return await self.set_global_policy(tenant_id, **kwargs)

    async def get_session_policy_in_transaction(
        self,
        _db,
        tenant_id: str,
        session_id: str,
        *,
        global_policy=None,
        for_update: bool = False,
    ):
        _ = global_policy, for_update
        return await self.get_session_policy(tenant_id, session_id)

    async def set_session_policy_in_transaction(self, _db, **kwargs):
        tenant_id = str(kwargs.pop("tenant_id"))
        session_id = str(kwargs.pop("session_id"))
        return await self.set_session_policy(tenant_id, session_id, **kwargs)

    async def begin_reply_policy_idempotency(
        self,
        _db,
        *,
        tenant_id: str,
        session_id: str,
        idempotency_key: str,
        request_hash: str,
    ):
        identity = (tenant_id, idempotency_key)
        guard = self.idempotency.get(identity)
        if guard is None:
            guard = {
                "session_id": session_id,
                "request_hash": request_hash,
                "response_json": {},
                "response_etag": "",
                "completed": False,
            }
            self.idempotency[identity] = guard
        elif guard["session_id"] != session_id or guard["request_hash"] != request_hash:
            raise ReplyPolicyIdempotencyConflictError("different request")
        return deepcopy(guard)

    async def complete_reply_policy_idempotency(
        self,
        _db,
        *,
        tenant_id: str,
        idempotency_key: str,
        response_payload: dict[str, object],
        response_etag: str,
    ) -> None:
        guard = self.idempotency[(tenant_id, idempotency_key)]
        guard.update(
            response_json=deepcopy(response_payload),
            response_etag=response_etag,
            completed=True,
        )

    async def lock_reply_policy_aggregate_state(
        self,
        _db,
        tenant_id: str,
        session_id: str,
    ):
        return deepcopy(
            self.aggregate_states.setdefault(
                (tenant_id, session_id),
                {
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "sdk_group_require_at_me": True,
                    "effect_idempotency_key": "",
                    "version": 0,
                    "updated_at": None,
                },
            )
        )

    async def update_reply_policy_aggregate_state(
        self,
        _db,
        *,
        tenant_id: str,
        session_id: str,
        expected_version: int,
        sdk_group_require_at_me: bool,
        effect_idempotency_key: str,
    ):
        current = await self.lock_reply_policy_aggregate_state(
            _db,
            tenant_id,
            session_id,
        )
        if int(current["version"]) != expected_version:
            raise AssertionError("unexpected aggregate version")
        after = {
            **current,
            "sdk_group_require_at_me": sdk_group_require_at_me,
            "effect_idempotency_key": effect_idempotency_key,
            "version": expected_version + 1,
            "updated_at": "2026-07-18T00:00:00+00:00",
        }
        self.aggregate_states[(tenant_id, session_id)] = after
        return deepcopy(after)

    async def get_reply_policy_effect_status(
        self,
        _db,
        tenant_id: str,
        effect_idempotency_key: str,
    ) -> str:
        _ = tenant_id
        return "prepared" if effect_idempotency_key else "not_requested"

    async def get_reply_policy_aggregate(
        self,
        tenant_id: str,
        session_id: str,
    ):
        global_policy = await self.get_global_policy(tenant_id)
        session_policy = await self.get_session_policy(tenant_id, session_id)
        repeater_config = await self.repeater_store.get_config(
            tenant_id,
            session_id,
        )
        state = await self.lock_reply_policy_aggregate_state(
            None,
            tenant_id,
            session_id,
        )
        return compose_reply_policy_aggregate(
            tenant_id=tenant_id,
            session_id=session_id,
            global_policy=global_policy,
            session_policy=session_policy,
            repeater_config=repeater_config,
            aggregate_state=state,
            effect_status=await self.get_reply_policy_effect_status(
                None,
                tenant_id,
                str(state["effect_idempotency_key"]),
            ),
        )

    def snapshot(self):
        return (
            deepcopy(self.global_policies),
            deepcopy(self.policies),
            deepcopy(self.idempotency),
            deepcopy(self.aggregate_states),
        )

    def restore(self, snapshot) -> None:
        (
            self.global_policies,
            self.policies,
            self.idempotency,
            self.aggregate_states,
        ) = snapshot


class _FakeAtomicTransaction:
    def __init__(self, participants) -> None:
        self.participants = participants
        self.snapshots = []

    async def __aenter__(self):
        self.snapshots = [item.snapshot() for item in self.participants]
        return self

    async def __aexit__(self, exc_type, *_args: object) -> None:
        if exc_type is not None:
            for item, snapshot in zip(
                self.participants,
                self.snapshots,
                strict=True,
            ):
                item.restore(snapshot)


class _FakeAtomicDb:
    def __init__(self, participants) -> None:
        self.participants = participants

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _FakeAtomicTransaction:
        return _FakeAtomicTransaction(self.participants)


def _aggregate_request() -> dict[str, object]:
    return {
        "tenant_id": "default",
        "session_id": "room@chatroom",
        "private_reply_mode": "all",
        "group_reply_mode": "contains",
        "group_reply_mention_sender": False,
        "trigger_keywords_text": "报价\n人工",
        "session_reply_mode": "inherit",
        "session_mention_sender_mode": "inherit",
        "session_trigger_keywords_text": "",
        "participation_policy": {"threshold": 75},
        "repeater_enabled": True,
        "repeater_cooldown_seconds": 120,
        "sdk_group_require_at_me": False,
    }


async def _allow_aggregate_owners_scope(
    owners: tuple[str, ...],
    tenant_id: str,
    session_id: str,
) -> bool:
    assert owners == ("wxbot", "repeater")
    assert tenant_id == "default"
    assert session_id.endswith("@chatroom")
    return True


def _build_aggregate_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: bool = False,
    owners_scope_execution_allowed=_allow_aggregate_owners_scope,
):
    app = FastAPI()
    repeater_store = _FakeAggregateRepeaterStore()
    store = _FakeAggregateStore(repeater_store)
    message_store = _FakeMessageStore(fail=fail)
    participants = [store, repeater_store, message_store]
    container = SimpleNamespace(
        message_store=message_store,
        repeater_store=repeater_store,
    )
    app.include_router(
        build_wxbot_router(
            store,
            container=container,
            owners_scope_execution_allowed=owners_scope_execution_allowed,
        )
    )
    monkeypatch.setattr(
        wxbot_router,
        "get_session_factory",
        lambda: lambda: _FakeAtomicDb(participants),
    )
    return (
        TestClient(app, raise_server_exceptions=False),
        store,
        repeater_store,
        message_store,
    )


def test_wxbot_reply_policy_aggregate_requires_repeater_owner_at_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], str, str]] = []

    async def deny(owners: tuple[str, ...], tenant_id: str, session_id: str) -> bool:
        calls.append((owners, tenant_id, session_id))
        return False

    client, _store, repeater_store, _message_store = _build_aggregate_client(
        monkeypatch,
        owners_scope_execution_allowed=deny,
    )

    response = client.get(
        "/admin/reply-policy/aggregate?tenant_id=default&session_id=room%40chatroom",
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "plugin_owner_runtime_disabled"
    assert calls == [(('wxbot', 'repeater'), "default", "room@chatroom")]
    assert repeater_store.read_calls == 0


def test_wxbot_reply_policy_aggregate_rechecks_repeater_immediately_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = iter((True, True, True, True, False))
    calls: list[tuple[tuple[str, ...], str, str]] = []

    async def change_during_request(
        owners: tuple[str, ...],
        tenant_id: str,
        session_id: str,
    ) -> bool:
        calls.append((owners, tenant_id, session_id))
        return next(decisions)

    client, store, repeater_store, message_store = _build_aggregate_client(
        monkeypatch,
        owners_scope_execution_allowed=change_during_request,
    )
    auth = {"Authorization": "Bearer token"}
    initial = client.get(
        "/admin/reply-policy/aggregate?tenant_id=default&session_id=room%40chatroom",
        headers=auth,
    )

    response = client.post(
        "/admin/reply-policy/aggregate",
        headers={
            **auth,
            "If-Match": initial.headers["etag"],
            "Idempotency-Key": "agent-console:repeater-owner-race",
        },
        json=_aggregate_request(),
    )

    assert initial.status_code == 200
    assert response.status_code == 503
    assert response.json()["detail"] == "plugin_owner_runtime_disabled"
    assert calls == [(('wxbot', 'repeater'), "default", "room@chatroom")] * 5
    assert repeater_store.mutation_calls == 0
    assert repeater_store.configs == {}
    assert store.global_policies == {}
    assert store.policies == {}
    assert message_store.calls == []


def test_wxbot_reply_policy_aggregate_stages_one_durable_sdk_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _store, _repeater_store, message_store = _build_aggregate_client(monkeypatch)
    headers = {"Authorization": "Bearer token"}
    initial = client.get(
        "/admin/reply-policy/aggregate?tenant_id=default&session_id=room%40chatroom",
        headers=headers,
    )
    response = client.post(
        "/admin/reply-policy/aggregate",
        headers={
            **headers,
            "If-Match": initial.headers["etag"],
            "Idempotency-Key": "agent-console:test-preset",
        },
        json=_aggregate_request(),
    )

    assert initial.status_code == 200
    assert initial.headers["etag"] == '"reply-policy-g0-s0-r0-a0"'
    assert response.status_code == 200
    assert response.json()["global_policy"]["group_reply_mode"] == "contains"
    assert response.json()["sdk_trigger"]["status"] == "prepared"
    assert response.json()["repeater_config"]["enabled"] is True
    assert response.headers["etag"] == '"reply-policy-g1-s1-r1-a1"'
    assert len(message_store.calls) == 1
    assert message_store.calls[0]["owner"] == "wxbot"
    assert message_store.calls[0]["effect_type"] == "sdk_trigger_config"
    assert message_store.calls[0]["payload"] == {"group_require_at_me": False}

    replay = client.post(
        "/admin/reply-policy/aggregate",
        headers={
            **headers,
            "If-Match": initial.headers["etag"],
            "Idempotency-Key": "agent-console:test-preset",
        },
        json=_aggregate_request(),
    )
    assert replay.status_code == 200
    assert replay.json() == response.json()
    assert replay.headers["etag"] == response.headers["etag"]
    assert len(message_store.calls) == 1


def test_wxbot_reply_policy_aggregate_conflicts_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _store, _repeater_store, message_store = _build_aggregate_client(monkeypatch)
    initial_etag = '"reply-policy-g0-s0-r0-a0"'
    headers = {
        "Authorization": "Bearer token",
        "If-Match": initial_etag,
        "Idempotency-Key": "agent-console:conflict",
    }
    missing_if_match = client.post(
        "/admin/reply-policy/aggregate",
        headers={
            "Authorization": "Bearer token",
            "Idempotency-Key": "agent-console:missing-version",
        },
        json=_aggregate_request(),
    )
    missing_idempotency_key = client.post(
        "/admin/reply-policy/aggregate",
        headers={
            "Authorization": "Bearer token",
            "If-Match": initial_etag,
        },
        json=_aggregate_request(),
    )
    first = client.post(
        "/admin/reply-policy/aggregate",
        headers=headers,
        json=_aggregate_request(),
    )
    changed = {**_aggregate_request(), "repeater_cooldown_seconds": 180}
    reused_key = client.post(
        "/admin/reply-policy/aggregate",
        headers=headers,
        json=changed,
    )
    stale_writer = client.post(
        "/admin/reply-policy/aggregate",
        headers={
            **headers,
            "Idempotency-Key": "agent-console:second-writer",
        },
        json=changed,
    )

    assert missing_if_match.status_code == 428
    assert missing_idempotency_key.status_code == 400
    assert first.status_code == 200
    assert reused_key.status_code == 409
    assert reused_key.json()["detail"]["code"] == "idempotency_key_conflict"
    assert stale_writer.status_code == 409
    assert stale_writer.headers["etag"] == '"reply-policy-g1-s1-r1-a1"'
    assert len(message_store.calls) == 1


def test_wxbot_reply_policy_aggregate_enforces_group_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _store, _repeater_store, _message_store = _build_aggregate_client(monkeypatch)
    principal = Principal(
        subject="group-operator",
        roles=(AdminRole.GROUP_OPERATOR.value,),
        tenant_ids=("default",),
        group_ids=("default:room@chatroom",),
        auth_kind="test",
    )
    monkeypatch.setattr(
        wxbot_router,
        "authenticate_admin_request",
        lambda _request, _settings: principal,
    )

    allowed = client.get(
        "/admin/reply-policy/aggregate?tenant_id=default&session_id=room%40chatroom"
    )
    crossed = client.get(
        "/admin/reply-policy/aggregate?tenant_id=default&session_id=other%40chatroom"
    )
    tenant_wide = client.get("/admin/reply-policy/global/default")
    private_session = client.get(
        "/admin/reply-policy/aggregate?tenant_id=default&session_id=private-user"
    )

    assert allowed.status_code == 200
    assert crossed.status_code == 403
    assert tenant_wide.status_code == 403
    assert private_session.status_code == 400


def test_wxbot_reply_policy_aggregate_rolls_back_and_retries_after_effect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, repeater_store, message_store = _build_aggregate_client(
        monkeypatch,
        fail=True,
    )
    headers = {
        "Authorization": "Bearer token",
        "If-Match": '"reply-policy-g0-s0-r0-a0"',
        "Idempotency-Key": "agent-console:fault-retry",
    }
    failed = client.post(
        "/admin/reply-policy/aggregate",
        headers=headers,
        json=_aggregate_request(),
    )

    assert failed.status_code == 500
    assert store.global_policies == {}
    assert store.policies == {}
    assert store.idempotency == {}
    assert repeater_store.configs == {}
    assert message_store.calls == []

    message_store.fail = False
    retried = client.post(
        "/admin/reply-policy/aggregate",
        headers=headers,
        json=_aggregate_request(),
    )
    assert retried.status_code == 200
    assert len(message_store.calls) == 1


def test_wxbot_router_rejects_invalid_reply_mode_and_missing_admin() -> None:
    client, _, _, _, _ = _build_client()

    with client:
        forbidden = client.get("/admin/reply-queue/stats?tenant_id=demo")
        invalid_mode = client.post(
            "/admin/reply-policy/demo/room@chatroom",
            headers={"Authorization": "Bearer token", "If-Match": '"0"'},
            json={"reply_mode": "maybe"},
        )
        invalid_mention_mode = client.post(
            "/admin/reply-policy/demo/room@chatroom",
            headers={"Authorization": "Bearer token", "If-Match": '"0"'},
            json={"mention_sender_mode": "maybe"},
        )
        invalid_global_group_mode = client.post(
            "/admin/reply-policy/global/demo",
            headers={"Authorization": "Bearer token", "If-Match": '"0"'},
            json={"group_reply_mode": "all"},
        )
        invalid_group_session_mode = client.post(
            "/admin/reply-policy/demo/room@chatroom",
            headers={"Authorization": "Bearer token", "If-Match": '"0"'},
            json={"reply_mode": "all"},
        )

    assert forbidden.status_code == 401
    assert invalid_mode.status_code == 400
    assert "reply_mode must be one of" in invalid_mode.json()["detail"]
    assert invalid_mention_mode.status_code == 400
    assert "mention_sender_mode must be one of" in invalid_mention_mode.json()["detail"]
    assert invalid_global_group_mode.status_code == 400
    assert (
        invalid_global_group_mode.json()["detail"]
        == "group_reply_mode does not support all for group chats"
    )
    assert invalid_group_session_mode.status_code == 400
    assert (
        invalid_group_session_mode.json()["detail"]
        == "reply_mode does not support all for group chats"
    )


def test_wxbot_reply_policy_direct_writes_require_current_versions() -> None:
    client, _store, _bridge, _scheduler, _agent_store = _build_client()
    auth = {"Authorization": "Bearer token"}

    with client:
        missing_global = client.post(
            "/admin/reply-policy/global/demo",
            headers=auth,
            json={"group_reply_mode": "contains"},
        )
        first_global = client.post(
            "/admin/reply-policy/global/demo",
            headers={**auth, "If-Match": '"0"'},
            json={"group_reply_mode": "contains"},
        )
        stale_global = client.post(
            "/admin/reply-policy/global/demo",
            headers={**auth, "If-Match": '"0"'},
            json={"group_reply_mode": "off"},
        )
        missing_session = client.post(
            "/admin/reply-policy/demo/room@chatroom",
            headers=auth,
            json={"reply_mode": "contains"},
        )
        first_session = client.post(
            "/admin/reply-policy/demo/room@chatroom",
            headers={**auth, "If-Match": '"0"'},
            json={"reply_mode": "contains"},
        )
        stale_session = client.post(
            "/admin/reply-policy/demo/room@chatroom",
            headers={**auth, "If-Match": '"0"'},
            json={"reply_mode": "off"},
        )

    assert missing_global.status_code == 428
    assert first_global.status_code == 200
    assert first_global.headers["etag"] == '"1"'
    assert stale_global.status_code == 409
    assert stale_global.headers["etag"] == '"1"'
    assert missing_session.status_code == 428
    assert first_session.status_code == 200
    assert first_session.headers["etag"] == '"1"'
    assert stale_session.status_code == 409
    assert stale_session.headers["etag"] == '"1"'


def test_wxbot_reply_policy_audit_summaries_redact_keyword_values() -> None:
    secret_keyword = "customer-secret-keyword"
    aggregate = {
        "versions": {
            "global": 1,
            "session": 2,
            "repeater": 3,
            "aggregate": 4,
        },
        "global_policy": {
            "version": 1,
            "private_reply_mode": "all",
            "group_reply_mode": "contains",
            "trigger_keywords_text": secret_keyword,
        },
        "session_policy": {
            "version": 2,
            "reply_mode": "inherit",
            "mention_sender_mode": "off",
            "trigger_keywords_text": secret_keyword,
        },
        "repeater_config": {"enabled": True, "cooldown_seconds": 120},
        "sdk_gate": {"group_require_at_me": False, "status": "prepared"},
    }

    summary = wxbot_router._aggregate_audit_summary(aggregate)

    assert secret_keyword not in json.dumps(summary)
    assert summary["global_policy"]["trigger_keyword_count"] == 1
    assert summary["session_policy"]["trigger_keyword_count"] == 1


def test_wxbot_router_gracefully_handles_unsupported_sdk_debug_endpoints() -> None:
    app = FastAPI()
    store = _FakeStore()

    class _LegacyBridge(_FakeBridge):
        async def sdk_request(
            self,
            method: str,
            path: str,
            *,
            params: dict | None = None,
            json_body: dict | None = None,
            request_headers: dict[str, str] | None = None,
        ) -> dict[str, object]:
            if path in {"/queue/messages", "/debug/trigger-config"}:
                from fastapi import HTTPException

                raise HTTPException(404, "not found")
            return await super().sdk_request(
                method,
                path,
                params=params,
                json_body=json_body,
                request_headers=request_headers,
            )

    bridge = _LegacyBridge()
    app.include_router(build_wxbot_router(store, container=None, bridge=bridge))
    client = TestClient(app)

    with client:
        sdk_queue_messages = client.get(
            "/admin/sdk/queue/messages?status=pending&limit=2",
            headers={"Authorization": "Bearer token"},
        )
        sdk_trigger_debug = client.get(
            "/admin/sdk/debug/trigger-config",
            headers={"Authorization": "Bearer token"},
        )
        save_sdk_trigger_debug = client.post(
            "/admin/sdk/debug/trigger-config",
            headers={"Authorization": "Bearer token"},
            json={"group_require_at_me": False},
        )

    assert sdk_queue_messages.status_code == 200
    assert sdk_queue_messages.json()["unsupported"] is True
    assert sdk_queue_messages.json()["count"] == 0

    assert sdk_trigger_debug.status_code == 200
    assert sdk_trigger_debug.json()["unsupported"] is True
    assert sdk_trigger_debug.json()["group_require_at_me"] is True

    assert save_sdk_trigger_debug.status_code == 409
    assert "durable reply-policy aggregate" in save_sdk_trigger_debug.json()["detail"]


def test_wxbot_router_keeps_agent_policy_and_audit_isolated_by_scope() -> None:
    client, _store, _bridge, _scheduler, agent_store = _build_client()

    with client:
        initial_group_info = client.get(
            "/admin/agent-tools/policy/demo/room@chatroom?scope=group_info",
            headers={"Authorization": "Bearer token"},
        )
        initial_plugin_status = client.get(
            "/admin/agent-tools/policy/demo/room@chatroom?scope=group_plugin_status",
            headers={"Authorization": "Bearer token"},
        )
        save_group_info = client.post(
            "/admin/agent-tools/policy/demo/room@chatroom?scope=group_info",
            headers={
                "Authorization": "Bearer token",
                "If-Match": initial_group_info.headers["etag"],
                "Idempotency-Key": "agent-policy-group-info-1",
            },
            json={"enabled": True, "allowed_tools": ["get_group_info"]},
        )
        save_plugin_status = client.post(
            "/admin/agent-tools/policy/demo/room@chatroom?scope=group_plugin_status",
            headers={
                "Authorization": "Bearer token",
                "If-Match": initial_plugin_status.headers["etag"],
                "Idempotency-Key": "agent-policy-plugin-status-1",
            },
            json={"enabled": True, "allowed_tools": ["get_group_credits_status"]},
        )
        read_group_info = client.get(
            "/admin/agent-tools/policy/demo/room@chatroom?scope=group_info",
            headers={"Authorization": "Bearer token"},
        )
        read_plugin_status = client.get(
            "/admin/agent-tools/policy/demo/room@chatroom?scope=group_plugin_status",
            headers={"Authorization": "Bearer token"},
        )
        audit_group_info = client.get(
            "/admin/agent-tools/audit?tenant_id=demo&session_id=room@chatroom&scope=group_info",
            headers={"Authorization": "Bearer token"},
        )
        audit_plugin_status = client.get(
            "/admin/agent-tools/audit?tenant_id=demo&session_id=room@chatroom&scope=group_plugin_status",
            headers={"Authorization": "Bearer token"},
        )

    assert save_group_info.status_code == 200
    assert save_plugin_status.status_code == 200
    assert read_group_info.json()["allowed_tools"] == ["get_group_info"]
    assert read_plugin_status.json()["allowed_tools"] == ["get_group_credits_status"]
    assert agent_store.policies[("demo", "room@chatroom", "group_info")]["allowed_tools"] == [
        "get_group_info"
    ]
    assert agent_store.policies[("demo", "room@chatroom", "group_plugin_status")][
        "allowed_tools"
    ] == ["get_group_credits_status"]

    assert audit_group_info.status_code == 200
    assert audit_group_info.json()["count"] == 1
    assert audit_group_info.json()["items"][0]["scope"] == "group_info"
    assert audit_group_info.json()["items"][0]["tool_result"] == {"total": 1}

    assert audit_plugin_status.status_code == 200
    assert audit_plugin_status.json()["count"] == 1
    assert audit_plugin_status.json()["items"][0]["scope"] == "group_plugin_status"
    assert audit_plugin_status.json()["items"][0]["tool_result"] == {
        "enabled": True,
        "credit_name": "积分",
    }
