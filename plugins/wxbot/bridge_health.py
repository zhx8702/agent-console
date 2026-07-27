"""Leader health, cursor fencing, and self-healing for the SDK bridge."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from app.common.logging import get_logger
from plugins.wxbot.bridge_contract import (
    _STREAM_TASK_NAMES,
    CURSOR_LAG_THRESHOLD,
    CURSOR_RECONCILE_INTERVAL_SECONDS,
    CURSOR_STALL_CHECKS,
    INBOUND_DEDUPE_KEY_PREFIX,
    SELF_HEAL_COOLDOWN_SECONDS,
    SELF_HEAL_RECURRENCE_THRESHOLD,
    _cursor_diagnostics,
    _key_part,
    _utcnow_iso,
)
from plugins.wxbot.bridge_state import WxbotBridgeState

log = get_logger(__name__)


class WxbotBridgeHealthMixin(WxbotBridgeState):
    async def _get_cursor(self) -> int:
        val = await self._redis.get(self.cursor_key)
        return int(val) if val else 0

    async def _set_cursor(self, cursor: int) -> None:
        await self._redis.set(self.cursor_key, str(cursor))

    async def _get_legacy_cursor(self) -> int:
        val = await self._redis.get(self.legacy_cursor_key)
        return int(val) if val else 0

    async def _set_legacy_cursor(self, cursor: int) -> None:
        await self._redis.set(self.legacy_cursor_key, str(cursor))

    async def _get_event_cursor(self) -> int:
        val = await self._redis.get(self.event_cursor_key)
        return int(val) if val else 0

    async def _set_event_cursor(self, cursor: int) -> None:
        await self._redis.set(self.event_cursor_key, str(cursor))

    def _inbound_dedupe_key(self, message_id: str) -> str:
        if self._connection_id and self._connection_id != "legacy-wechat-default":
            return (
                f"{INBOUND_DEDUPE_KEY_PREFIX}:{_key_part(self._tenant_id)}:"
                f"{_key_part(self._connection_id)}:{_key_part(message_id)}"
            )
        return f"{INBOUND_DEDUPE_KEY_PREFIX}:{self._tenant_id}:{message_id}"

    async def _mark_inbound_seen(self, message_id: str) -> bool:
        cleaned = str(message_id or "").strip()
        if not cleaned:
            return True
        ttl = int(getattr(self._settings, "inbound_idempotency_ttl_seconds", 86_400) or 86_400)
        ok = await self._redis.set(
            self._inbound_dedupe_key(cleaned),
            "1",
            nx=True,
            ex=max(60, ttl),
        )
        return bool(ok)

    async def _release_inbound_seen(self, message_id: str) -> None:
        cleaned = str(message_id or "").strip()
        if cleaned:
            await self._redis.delete(self._inbound_dedupe_key(cleaned))

    def _max_inbound_message_age_seconds(self) -> int:
        return max(0, int(getattr(self._settings, "wxbot_bridge_max_message_age_seconds", 0) or 0))

    @staticmethod
    def _parse_sdk_timestamp(*values: Any) -> datetime | None:
        for value in values:
            if value is None or value == "":
                continue
            if isinstance(value, (int, float)):
                if value <= 0:
                    continue
                return datetime.fromtimestamp(float(value), UTC)
            text = str(value).strip()
            if not text:
                continue
            try:
                numeric = float(text)
            except ValueError:
                numeric = 0
            if numeric > 0:
                return datetime.fromtimestamp(numeric, UTC)
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        return None

    def _is_stale_inbound_message(self, occurred_at: datetime | None) -> bool:
        max_age = self._max_inbound_message_age_seconds()
        if max_age <= 0 or occurred_at is None:
            return False
        age = (datetime.now(UTC) - occurred_at).total_seconds()
        return age > max_age

    async def _sdk_queue_bounds(self) -> dict[str, int]:
        try:
            payload = await self.sdk_request("GET", "/status")
        except Exception:
            return {"max_inbound_id": 0, "max_event_id": 0, "max_stream_id": 0}
        queue = payload.get("queue") if isinstance(payload, dict) else {}
        if not isinstance(queue, dict):
            queue = {}
        return {
            "max_inbound_id": int(queue.get("max_inbound_id") or 0),
            "max_event_id": int(queue.get("max_event_id") or 0),
            "max_stream_id": int(queue.get("max_stream_id") or queue.get("max_unified_id") or 0),
        }

    async def _reconcile_ingest_cursor(
        self, cursor: int, bounds: dict[str, int] | None = None
    ) -> int:
        bounds = bounds if bounds is not None else await self._sdk_queue_bounds()
        max_cursor = int(bounds.get("max_stream_id") or bounds.get("max_inbound_id") or 0)
        if max_cursor <= 0:
            return cursor
        if cursor > max_cursor:
            await self._set_cursor(0)
            log.warning(
                "wxbot.bridge.ingest_cursor_reset_for_rebuilt_sdk_queue",
                stored_cursor=cursor,
                sdk_max_cursor=max_cursor,
            )
            return 0
        return cursor

    async def _reconcile_legacy_ingest_cursor(
        self, cursor: int, bounds: dict[str, int] | None = None
    ) -> int:
        bounds = bounds if bounds is not None else await self._sdk_queue_bounds()
        max_cursor = int(bounds.get("max_inbound_id") or 0)
        if max_cursor <= 0:
            return cursor
        if cursor > max_cursor:
            await self._set_legacy_cursor(0)
            log.warning(
                "wxbot.bridge.legacy_ingest_cursor_reset_for_rebuilt_sdk_queue",
                stored_cursor=cursor,
                sdk_max_inbound_id=max_cursor,
            )
            return 0
        return cursor

    async def _reconcile_event_cursor(
        self, cursor: int, bounds: dict[str, int] | None = None
    ) -> int:
        bounds = bounds if bounds is not None else await self._sdk_queue_bounds()
        max_event_id = int(bounds.get("max_event_id") or 0)
        max_stream_id = int(bounds.get("max_stream_id") or 0)
        if max_event_id < cursor <= max_stream_id:
            return cursor
        if max_event_id <= 0:
            return cursor
        if cursor > max_event_id:
            await self._set_event_cursor(0)
            log.warning(
                "wxbot.bridge.event_cursor_reset_for_rebuilt_sdk_queue",
                stored_cursor=cursor,
                sdk_max_event_id=max_event_id,
            )
            return 0
        return cursor

    def _request_stream_reconnect(self, *, reason: str) -> None:
        self._cursor_reset_generation += 1
        cancelled: list[str] = []
        for task in list(self._tasks):
            if task.done():
                continue
            if task.get_name() not in _STREAM_TASK_NAMES:
                continue
            task.cancel()
            cancelled.append(task.get_name())
        log.warning(
            "wxbot.bridge.stream_reconnect_requested",
            reason=reason,
            tenant_id=self._tenant_id,
            generation=self._cursor_reset_generation,
            cancelled_tasks=cancelled,
        )

    def _can_self_heal(self, reason: str) -> bool:
        now = asyncio.get_running_loop().time()
        if now - self._last_self_heal_by_reason.get(reason, 0.0) < SELF_HEAL_COOLDOWN_SECONDS:
            return False
        self._last_self_heal_by_reason[reason] = now
        self._last_self_heal_at = now
        return True

    def _record_health_recurrence(self, reason: str, active: bool) -> int:
        if not active:
            self._health_recurrences[reason] = 0
            return 0
        count = self._health_recurrences.get(reason, 0) + 1
        self._health_recurrences[reason] = count
        return count

    def _fail_fast(self, reason: str) -> None:
        log.critical(
            "wxbot.bridge.fail_fast",
            reason=reason,
            tenant_id=self._tenant_id,
            instance_id=self._instance_id,
        )
        os._exit(1)

    async def _self_heal_bridge(self, *, reason: str) -> bool:
        if not self._sdk_online or self._sdk_auth_state != "ok":
            return False
        if not self._can_self_heal(reason):
            self._diagnostics["self_heal_cooldown_reason"] = reason
            return False
        self._diagnostics["last_self_heal_reason"] = reason
        self._diagnostics["last_self_heal_at"] = _utcnow_iso()
        log.warning(
            "wxbot.bridge.self_heal_triggered",
            reason=reason,
            tenant_id=self._tenant_id,
        )
        if reason == "leader_missing":
            restored = await self._restore_leader_key()
            if not restored:
                self._fail_fast("leader_missing_restore_failed")
                return True
            if self._is_leader:
                await self._activate_leader()
                if not await self._leader_present():
                    self._fail_fast("leader_missing_after_restore")
            return True
        if reason in {"task_missing", "running_false"}:
            created = self._ensure_bridge_tasks()
            log.warning(
                "wxbot.bridge.task_missing",
                tenant_id=self._tenant_id,
                created_tasks=created,
            )
            if self._task_snapshot()["missing"]:
                self._fail_fast("required_tasks_missing_after_self_heal")
            return True
        if reason in {"cursor_lag", "bridge_ingest_stalled"}:
            self._request_stream_reconnect(reason=reason)
            log.warning(
                "bridge_ingest_stalled",
                tenant_id=self._tenant_id,
                reason=reason,
            )
            return True
        return False

    async def _check_leader_health(self) -> bool:
        if not self._is_leader:
            self._record_health_recurrence("leader_missing", False)
            return False
        leader_missing = not await self._leader_present()
        count = self._record_health_recurrence("leader_missing", leader_missing)
        if not leader_missing or count < SELF_HEAL_RECURRENCE_THRESHOLD:
            return False
        return await self._self_heal_bridge(reason="leader_missing")

    async def _check_task_health(self) -> bool:
        if not self._leader_claimed():
            self._record_health_recurrence("task_missing", False)
            return False
        snapshot = self._task_snapshot()
        missing = list(snapshot.get("missing") or [])
        done = list(snapshot.get("done") or [])
        self._diagnostics["tasks_missing"] = missing
        self._diagnostics["tasks_done"] = done
        count = self._record_health_recurrence("task_missing", bool(missing))
        if missing and count >= SELF_HEAL_RECURRENCE_THRESHOLD:
            return await self._self_heal_bridge(reason="task_missing")
        return False

    def _cursor_lag_snapshot(
        self, bounds: dict[str, int], cursors: tuple[int, int, int]
    ) -> dict[str, int]:
        cursor, legacy_cursor, event_cursor = cursors
        max_stream_id = int(bounds.get("max_stream_id") or 0)
        max_inbound_id = int(bounds.get("max_inbound_id") or 0)
        max_event_id = int(bounds.get("max_event_id") or 0)
        stream_lag = max(0, max_stream_id - cursor)
        legacy_lag = max(0, max_inbound_id - legacy_cursor)
        event_lag = max(0, max_event_id - event_cursor)
        active_lags = [stream_lag, legacy_lag, event_lag]
        lag_mode = "all"
        if self._stream_mode == "unified" or (max_stream_id > 0 and cursor >= max_stream_id):
            active_lags = [stream_lag]
            lag_mode = "unified"
        elif self._stream_mode in {"legacy", "legacy-sse"} or self._ingest_mode == "polling":
            active_lags = [legacy_lag, event_lag]
            lag_mode = "legacy"
        return {
            "cursor": cursor,
            "legacy_cursor": legacy_cursor,
            "event_cursor": event_cursor,
            "max_stream_id": max_stream_id,
            "max_inbound_id": max_inbound_id,
            "max_event_id": max_event_id,
            "stream_lag": stream_lag,
            "legacy_lag": legacy_lag,
            "event_lag": event_lag,
            "lag_mode": lag_mode,
            "bounds_known": bool(max_stream_id or max_inbound_id or max_event_id),
            "max_lag": max(active_lags),
        }

    async def _refresh_cursor_diagnostics(self) -> bool:
        bounds = await self._sdk_queue_bounds()
        cursors = await asyncio.gather(
            self._get_cursor(),
            self._get_legacy_cursor(),
            self._get_event_cursor(),
        )
        if not any(
            int(bounds.get(key) or 0) for key in ("max_stream_id", "max_inbound_id", "max_event_id")
        ):
            existing = self._diagnostics.get("cursor")
            existing_payload = existing if isinstance(existing, dict) else {}
            self._diagnostics["cursor"] = _cursor_diagnostics(
                {**existing_payload, "bounds_known": False},
                stream_mode=self._stream_mode,
                cursor=int(cursors[0]),
                legacy_cursor=int(cursors[1]),
                event_cursor=int(cursors[2]),
            )
            return False
        return await self._check_cursor_lag_health(bounds, cursors)

    async def _check_cursor_lag_health(
        self,
        bounds: dict[str, int],
        cursors: tuple[int, int, int],
    ) -> bool:
        snapshot = self._cursor_lag_snapshot(bounds, cursors)
        self._diagnostics["cursor"] = snapshot
        if snapshot["max_lag"] < CURSOR_LAG_THRESHOLD:
            self._cursor_stall_count = 0
            self._last_cursor_observation = snapshot
            return False

        previous = self._last_cursor_observation
        if previous and all(
            int(previous.get(key) or 0) == snapshot[key]
            for key in ("cursor", "legacy_cursor", "event_cursor")
        ):
            self._cursor_stall_count += 1
        else:
            self._cursor_stall_count = 1
        self._last_cursor_observation = snapshot
        if self._cursor_stall_count < CURSOR_STALL_CHECKS:
            log.warning(
                "cursor_lag",
                tenant_id=self._tenant_id,
                **snapshot,
            )
            return False
        healed = await self._self_heal_bridge(reason="bridge_ingest_stalled")
        if healed:
            log.warning(
                "cursor_lag",
                tenant_id=self._tenant_id,
                stalled_checks=self._cursor_stall_count,
                **snapshot,
            )
        return healed

    async def _reconcile_all_cursors(self) -> bool:
        bounds = await self._sdk_queue_bounds()
        cursors = await asyncio.gather(
            self._get_cursor(),
            self._get_legacy_cursor(),
            self._get_event_cursor(),
        )
        cursor, legacy_cursor, event_cursor = cursors
        new_cursor = await self._reconcile_ingest_cursor(cursor, bounds)
        new_legacy_cursor = await self._reconcile_legacy_ingest_cursor(legacy_cursor, bounds)
        new_event_cursor = await self._reconcile_event_cursor(event_cursor, bounds)
        reset = (
            new_cursor != cursor
            or new_legacy_cursor != legacy_cursor
            or new_event_cursor != event_cursor
        )
        if reset:
            self._request_stream_reconnect(reason="sdk_queue_rebuilt")
            self._cursor_stall_count = 0
        else:
            if any(
                int(bounds.get(key) or 0)
                for key in ("max_stream_id", "max_inbound_id", "max_event_id")
            ):
                await self._check_cursor_lag_health(bounds, (cursor, legacy_cursor, event_cursor))
            else:
                self._diagnostics["cursor"] = _cursor_diagnostics(
                    {"bounds_known": False},
                    stream_mode=self._stream_mode,
                    cursor=cursor,
                    legacy_cursor=legacy_cursor,
                    event_cursor=event_cursor,
                )
        return reset

    async def _status_watchdog(self) -> None:
        if not self._sdk_online or self._sdk_auth_state != "ok":
            return
        if await self._check_leader_health():
            return
        task_snapshot = self._task_snapshot()
        missing = list(task_snapshot.get("missing") or [])
        if not self.is_running and missing:
            await self._check_task_health()
        if "wxbot-bridge-cursor-reconcile" in missing:
            await self._refresh_cursor_diagnostics()

    async def _cursor_reconcile_loop(self) -> None:
        interval = max(CURSOR_RECONCILE_INTERVAL_SECONDS, self._poll_interval)
        while not self._stop.is_set():
            try:
                await self._reconcile_all_cursors()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("wxbot.bridge.cursor_reconcile_failed")
            await asyncio.sleep(interval)
