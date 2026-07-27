from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID
from app.common.logging import get_logger
from plugins.wxbot.bridge_contract import (
    _REQUIRED_TASK_NAMES,
    LEADER_TTL_SECONDS,
    STATUS_PUBLISH_INTERVAL_SECONDS,
    STATUS_TTL_SECONDS,
    _cursor_diagnostics,
    _cursor_key,
    _decode_leader_payload,
    _event_cursor_key,
    _leader_key,
    _legacy_cursor_key,
    _parse_json_dict,
    _status_key,
    _utcnow_iso,
)
from plugins.wxbot.bridge_state import WxbotBridgeState
from plugins.wxbot.store import WxbotStore, normalize_wxbot_event_connection_id

log = get_logger(__name__)


async def read_bridge_runtime_status(
    redis: Any,
    store: WxbotStore,
    settings: Any,
    tenant_id: str,
    connection_id: str = "",
) -> dict[str, Any]:
    scoped_connection_id = normalize_wxbot_event_connection_id(connection_id)
    (
        status_raw,
        cursor_raw,
        legacy_cursor_raw,
        event_cursor_raw,
        leader_raw,
        leader_ttl,
    ) = await asyncio.gather(
        redis.get(_status_key(tenant_id, scoped_connection_id)),
        redis.get(_cursor_key(tenant_id, scoped_connection_id)),
        redis.get(_legacy_cursor_key(tenant_id, scoped_connection_id)),
        redis.get(_event_cursor_key(tenant_id, scoped_connection_id)),
        redis.get(_leader_key(tenant_id, scoped_connection_id)),
        redis.ttl(_leader_key(tenant_id, scoped_connection_id)),
    )
    status_payload = _parse_json_dict(status_raw)
    leader_payload = _decode_leader_payload(leader_raw)
    tasks_payload = (
        status_payload.get("tasks") if isinstance(status_payload.get("tasks"), dict) else {}
    )
    diagnostics_payload = (
        status_payload.get("diagnostics")
        if isinstance(status_payload.get("diagnostics"), dict)
        else {}
    )
    event_stats, media_ready_stats = await asyncio.gather(
        store.member_event_stats(tenant_id, connection_id=scoped_connection_id),
        store.media_ready_stats(tenant_id, connection_id=scoped_connection_id),
    )

    sdk_url = str(status_payload.get("sdk_url") or getattr(settings, "wxbot_sdk_url", "")).rstrip(
        "/"
    )
    stream_mode = str(status_payload.get("stream_mode") or "unknown")
    cursor = int(cursor_raw or 0)
    legacy_cursor = int(legacy_cursor_raw or 0)
    event_cursor = cursor if stream_mode == "unified" else int(event_cursor_raw or 0)
    leader_ttl_int = int(leader_ttl or -1)
    status_leader_token = str(status_payload.get("leader_token") or "")
    bridge_leader = bool(
        status_payload
        and leader_ttl_int >= 0
        and status_leader_token
        and status_leader_token == str(leader_payload.get("token") or "")
    )
    leader_missing = bool(status_payload) and (
        (bool(status_payload.get("leader_token")) and (not leader_payload or leader_ttl_int < 0))
        or (
            bool(status_payload.get("sdk_online"))
            and str(status_payload.get("sdk_auth_state") or "unknown") == "ok"
            and not leader_payload
        )
    )
    task_missing = list(tasks_payload.get("missing") or [])
    diagnostics = {
        **diagnostics_payload,
        "leader_missing": leader_missing,
        "tasks_missing": task_missing,
        "tasks_done": list(tasks_payload.get("done") or []),
    }
    cursor_diagnostics_payload = diagnostics.get("cursor")
    if isinstance(cursor_diagnostics_payload, dict):
        diagnostics["cursor"] = _cursor_diagnostics(
            cursor_diagnostics_payload,
            stream_mode=stream_mode,
            cursor=cursor,
            legacy_cursor=legacy_cursor,
            event_cursor=event_cursor,
        )

    return {
        "running": bool(status_payload.get("running")),
        "sdk_url": sdk_url,
        "sdk_online": bool(status_payload.get("sdk_online")),
        "sdk_auth_state": str(status_payload.get("sdk_auth_state") or "unknown"),
        "sdk_auth_reason": str(status_payload.get("sdk_auth_reason") or ""),
        "tenant_id": tenant_id,
        "connection_id": scoped_connection_id,
        "cursor": cursor,
        "legacy_cursor": legacy_cursor,
        "event_cursor": event_cursor,
        "ingest_mode": str(status_payload.get("ingest_mode") or "stopped"),
        "event_mode": str(status_payload.get("event_mode") or "stopped"),
        "stream_mode": stream_mode,
        "bridge_leader": bridge_leader,
        "poll_interval": float(
            status_payload.get(
                "poll_interval",
                getattr(settings, "wxbot_bridge_poll_interval", 3.0),
            )
            or 3.0
        ),
        "send_interval": float(
            status_payload.get(
                "send_interval",
                getattr(settings, "wxbot_bridge_send_interval", 2.0),
            )
            or 2.0
        ),
        "member_event_stats": event_stats,
        "media_ready_stats": media_ready_stats,
        "instance_id": str(status_payload.get("instance_id") or ""),
        "process_role": str(status_payload.get("process_role") or ""),
        "host": str(status_payload.get("host") or ""),
        "pid": status_payload.get("pid"),
        "started_at": status_payload.get("started_at"),
        "updated_at": status_payload.get("updated_at"),
        "tasks": tasks_payload,
        "diagnostics": diagnostics,
        "leader": {
            "token": str(leader_payload.get("token") or ""),
            "instance_id": str(leader_payload.get("instance_id") or ""),
            "process_role": str(leader_payload.get("process_role") or ""),
            "host": str(leader_payload.get("host") or ""),
            "pid": leader_payload.get("pid"),
            "updated_at": leader_payload.get("updated_at"),
            "ttl": leader_ttl_int,
        },
    }


class WxbotBridgeRuntimeMixin(WxbotBridgeState):
    @property
    def is_running(self) -> bool:
        if self._stop.is_set() or not self._leader_claimed():
            return False
        return not self._task_snapshot()["missing"]

    @property
    def leader_key(self) -> str:
        return _leader_key(self._tenant_id, self._connection_id)

    @property
    def status_key(self) -> str:
        return _status_key(self._tenant_id, self._connection_id)

    @property
    def cursor_key(self) -> str:
        return _cursor_key(self._tenant_id, self._connection_id)

    @property
    def legacy_cursor_key(self) -> str:
        return _legacy_cursor_key(self._tenant_id, self._connection_id)

    @property
    def event_cursor_key(self) -> str:
        return _event_cursor_key(self._tenant_id, self._connection_id)

    def _leader_payload(self) -> dict[str, Any]:
        return {
            "token": self._leader_token,
            "instance_id": self._instance_id,
            "process_role": self._process_role,
            "host": self._host,
            "pid": self._pid,
            "updated_at": _utcnow_iso(),
        }

    def _leader_claimed(self) -> bool:
        return bool(self._is_leader and self._diagnostics.get("leader_present") is True)

    def _runtime_snapshot(self) -> dict[str, Any]:
        task_snapshot = self._task_snapshot()
        bridge_leader = self._leader_claimed()
        return {
            "running": self.is_running,
            "sdk_url": self._sdk_url,
            "sdk_online": self._sdk_online,
            "sdk_auth_state": self._sdk_auth_state,
            "sdk_auth_reason": self._sdk_auth_reason,
            "tenant_id": self._tenant_id,
            "connection_id": self._connection_id,
            "ingest_mode": self._ingest_mode,
            "event_mode": self._event_mode,
            "stream_mode": self._stream_mode,
            "bridge_leader": bridge_leader,
            "poll_interval": self._poll_interval,
            "send_interval": self._send_interval,
            "instance_id": self._instance_id,
            "process_role": self._process_role,
            "host": self._host,
            "pid": self._pid,
            "started_at": self._started_at,
            "updated_at": _utcnow_iso(),
            "leader_token": self._leader_token if bridge_leader else "",
            "tasks": task_snapshot,
            "diagnostics": self._diagnostics_snapshot(task_snapshot=task_snapshot),
        }

    def _task_snapshot(self) -> dict[str, Any]:
        names = {task.get_name() for task in self._tasks if not task.done()}
        missing = sorted(_REQUIRED_TASK_NAMES - names)
        done = sorted(task.get_name() for task in self._tasks if task.done())
        return {
            "active": sorted(names),
            "done": done,
            "missing": missing,
            "required": sorted(_REQUIRED_TASK_NAMES),
        }

    def _diagnostics_snapshot(
        self, *, task_snapshot: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        tasks = task_snapshot or self._task_snapshot()
        diagnostics = dict(self._diagnostics)
        diagnostics.update(
            {
                "tasks_missing": list(tasks.get("missing") or []),
                "tasks_done": list(tasks.get("done") or []),
                "leader_missing": bool(
                    self._is_leader and not diagnostics.get("leader_present", True)
                ),
                "cursor_stall_count": self._cursor_stall_count,
            }
        )
        return diagnostics

    async def _publish_runtime_status(self) -> None:
        if not self._stop.is_set():
            await self._status_watchdog()
        await self._redis.set(
            self.status_key,
            json.dumps(self._runtime_snapshot(), ensure_ascii=False),
            ex=STATUS_TTL_SECONDS,
        )

    async def _status_publish_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._publish_runtime_status()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("wxbot.bridge.status_publish_failed")
            await asyncio.sleep(STATUS_PUBLISH_INTERVAL_SECONDS)

    async def _try_acquire_leader(self) -> bool:
        if self._is_leader:
            if await self._leader_present():
                return True
            self._is_leader = False
        ok = await self._redis.set(
            self.leader_key,
            json.dumps(self._leader_payload(), ensure_ascii=False),
            nx=True,
            ex=LEADER_TTL_SECONDS,
        )
        self._is_leader = bool(ok)
        self._diagnostics["leader_present"] = self._is_leader
        self._diagnostics["leader_missing"] = False
        return self._is_leader

    async def _leader_present(self) -> bool:
        current = _decode_leader_payload(await self._redis.get(self.leader_key))
        present = bool(self._leader_token) and str(current.get("token") or "") == self._leader_token
        self._diagnostics["leader_present"] = present
        self._diagnostics["leader_missing"] = self._is_leader and not present
        return present

    async def _restore_leader_key(self) -> bool:
        current = _decode_leader_payload(await self._redis.get(self.leader_key))
        current_token = str(current.get("token") or "")
        if current_token == self._leader_token:
            await self._redis.set(
                self.leader_key,
                json.dumps(self._leader_payload(), ensure_ascii=False),
                ex=LEADER_TTL_SECONDS,
            )
            self._is_leader = True
            self._diagnostics["leader_present"] = True
            self._diagnostics["leader_missing"] = False
            return True
        if current_token:
            self._is_leader = False
            self._diagnostics["leader_present"] = False
            self._diagnostics["leader_missing"] = False
            return True
        ok = await self._redis.set(
            self.leader_key,
            json.dumps(self._leader_payload(), ensure_ascii=False),
            nx=True,
            ex=LEADER_TTL_SECONDS,
        )
        if ok:
            self._is_leader = True
            self._diagnostics["leader_present"] = True
            self._diagnostics["leader_missing"] = False
            return True
        return await self._leader_present()

    async def _refresh_leader_loop(self) -> None:
        while not self._stop.is_set() and self._is_leader:
            await asyncio.sleep(max(5, LEADER_TTL_SECONDS // 3))
            try:
                current = _decode_leader_payload(await self._redis.get(self.leader_key))
                if str(current.get("token") or "") != self._leader_token:
                    self._is_leader = False
                    log.warning("wxbot.bridge.leader_lost", tenant_id=self._tenant_id)
                    return
                await self._redis.set(
                    self.leader_key,
                    json.dumps(self._leader_payload(), ensure_ascii=False),
                    ex=LEADER_TTL_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._is_leader = False
                log.exception("wxbot.bridge.leader_refresh_failed")
                return

    async def _release_leader(self) -> None:
        if not self._is_leader:
            return
        current = _decode_leader_payload(await self._redis.get(self.leader_key))
        if str(current.get("token") or "") == self._leader_token:
            await self._redis.delete(self.leader_key)
        self._is_leader = False

    def _enter_standby(self) -> None:
        self._sdk_online = False
        self._ingest_mode = "standby"
        self._event_mode = "standby"
        self._stream_mode = "standby"
        if not self._standby_logged:
            log.info(
                "wxbot.bridge.standby",
                tenant_id=self._tenant_id,
                leader_key=self.leader_key,
            )
            self._standby_logged = True

    async def _activate_leader(self) -> None:
        self._standby_logged = False
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=10,
                trust_env=False,
            )
        if self._leader_refresh_task is None or self._leader_refresh_task.done():
            self._leader_refresh_task = asyncio.create_task(
                self._refresh_leader_loop(),
                name="wxbot-bridge-leader",
            )
        created = self._ensure_bridge_tasks()
        if created:
            log.info("wxbot.bridge.started", tenant_id=self._tenant_id)

    def _ensure_bridge_tasks(self) -> list[str]:
        active: dict[str, asyncio.Task] = {}
        duplicate_names: list[str] = []
        for task in self._tasks:
            if task.done():
                continue
            name = task.get_name()
            if name in active:
                task.cancel()
                duplicate_names.append(name)
                continue
            active[name] = task
        self._tasks = list(active.values())
        if duplicate_names:
            self._diagnostics["tasks_duplicate_cancelled"] = sorted(duplicate_names)
        task_factories = {
            "wxbot-bridge-ingest": self._ingest_loop,
            "wxbot-bridge-events": self._event_loop,
            "wxbot-bridge-send": self._send_loop,
            "wxbot-bridge-pending-media": self._pending_media_resolver_loop,
            "wxbot-bridge-cursor-reconcile": self._cursor_reconcile_loop,
        }
        created: list[str] = []
        for name, factory in task_factories.items():
            if name in active:
                continue
            self._tasks.append(asyncio.create_task(factory(), name=name))
            created.append(name)
        if created:
            self._diagnostics["tasks_created"] = created
            self._diagnostics["last_tasks_created_at"] = _utcnow_iso()
        return created

    async def _cancel_bridge_tasks(self) -> None:
        if self._leader_refresh_task is not None:
            self._leader_refresh_task.cancel()
            await asyncio.gather(self._leader_refresh_task, return_exceptions=True)
            self._leader_refresh_task = None
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._sdk_online = False

    async def _run_leader_supervisor(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._is_leader:
                    acquired = await self._try_acquire_leader()
                    if not acquired:
                        self._enter_standby()
                        await asyncio.sleep(self._leader_retry_interval)
                        continue
                    await self._activate_leader()

                if (
                    self._leader_refresh_task is not None
                    and self._leader_refresh_task.done()
                    and not self._stop.is_set()
                ):
                    await self._cancel_bridge_tasks()
                    self._enter_standby()
                    continue

                done_bridge_tasks = [task for task in self._tasks if task.done()]
                if done_bridge_tasks and not self._stop.is_set():
                    for task in done_bridge_tasks:
                        try:
                            exc = task.exception()
                        except asyncio.CancelledError:
                            exc = None
                        if exc is not None:
                            log.error(
                                "wxbot.bridge.task_exited",
                                task_name=task.get_name(),
                                error_class=exc.__class__.__name__,
                            )
                    await self._cancel_bridge_tasks()
                    if self._is_leader and not self._stop.is_set():
                        await self._activate_leader()
                    continue

                if await self._check_leader_health():
                    continue

                if await self._check_task_health():
                    continue

                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("wxbot.bridge.supervisor_error")
                await asyncio.sleep(self._leader_retry_interval)

    async def start(self) -> None:
        if self._leader_supervisor_task is not None and not self._leader_supervisor_task.done():
            return
        self._stop = asyncio.Event()
        if not await self._try_acquire_leader():
            self._enter_standby()
        else:
            await self._activate_leader()
        await self._publish_runtime_status()
        self._status_publish_task = asyncio.create_task(
            self._status_publish_loop(),
            name="wxbot-bridge-status",
        )
        self._leader_supervisor_task = asyncio.create_task(
            self._run_leader_supervisor(),
            name="wxbot-bridge-supervisor",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._status_publish_task is not None:
            self._status_publish_task.cancel()
            await asyncio.gather(self._status_publish_task, return_exceptions=True)
            self._status_publish_task = None
        if self._leader_supervisor_task is not None:
            self._leader_supervisor_task.cancel()
            await asyncio.gather(self._leader_supervisor_task, return_exceptions=True)
            self._leader_supervisor_task = None
        await self._cancel_bridge_tasks()
        try:
            await self._release_leader()
        except Exception:
            log.exception("wxbot.bridge.leader_release_failed")
        self._ingest_mode = "stopped"
        self._event_mode = "stopped"
        self._stream_mode = "stopped"
        await self._publish_runtime_status()
        log.info("wxbot.bridge.stopped")

    async def status(self) -> dict[str, Any]:
        await self._publish_runtime_status()
        return await read_bridge_runtime_status(
            self._redis,
            self._store,
            self._settings,
            self._tenant_id,
            connection_id=self._connection_id or LEGACY_WXBOT_CONNECTION_ID,
        )
