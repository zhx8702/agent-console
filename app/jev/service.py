from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.common.logging import get_logger
from app.jev.models import JevPolicy, answer, confidence, fingerprint, memory_fingerprint, questions
from app.jev.store import JevStore

log = get_logger(__name__)


class JevService:
    def __init__(self, settings: Any, *, client: Any = None, store: Any = None) -> None:
        self.settings = settings
        self.store = store or JevStore()
        self.client = client
        self.memory_store: Any = None
        self.registry: Any = None
        self._task: asyncio.Task | None = None
        self._policies: dict[str, tuple[float, JevPolicy]] = {}
        self._circuit_until = 0.0
        self._failures = 0
        self._online_slots = asyncio.Semaphore(4)
        self._maintenance_at = 0.0
        self._reconcile_cursor = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1), 0)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "typesafe_enabled", False) and
                    (self.client is not None or getattr(self.settings, "typesafe_api_key", None)))

    def defaults(self) -> JevPolicy:
        return JevPolicy(
            shadow_only=getattr(self.settings, "typesafe_shadow_only", True),
            min_confidence=getattr(self.settings, "typesafe_min_confidence", 0.8),
            **{d: getattr(self.settings, f"typesafe_{d}_enabled", True)
               for d in ("relationship", "memory", "intent", "moderation", "participation")},
        )

    async def policy(self, tenant_id: str) -> JevPolicy:
        cached = self._policies.get(tenant_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            policy, _ = await asyncio.wait_for(self.store.policy(tenant_id, self.defaults()), 1)
        except Exception:
            # Configuration outages must not silently enable active decisions.
            disabled = JevPolicy(enabled=False)
            self._policies[tenant_id] = (time.monotonic() + 5, disabled)
            return disabled
        if len(self._policies) > 1000:
            self._policies.clear()
        self._policies[tenant_id] = (time.monotonic() + 5, policy)
        return policy

    async def allowed(self, tenant_id: str, session_id: str, domain: str) -> bool:
        if self.registry is None:
            return False
        owner = "wxbot" if domain == "participation" else "moderation" if domain == "moderation" else "memory" if domain in {"memory", "relationship"} else None
        if owner is None:
            return True
        if await self.registry.scope_execution_allowed(owner, tenant_id=tenant_id, session_id=session_id) is not True:
            return False
        if domain == "relationship" and session_id.endswith("@chatroom"):
            return await self.registry.scope_execution_allowed("wxbot", tenant_id=tenant_id, session_id=session_id) is True
        return True

    async def enqueue_memory(self, item: dict, *, run: Any) -> bool:
        if not self.enabled or item.get("source_type") in {"manual", "explicit_user"}:
            return False
        value = item.get("value") or {}
        domain = "relationship" if value.get("relation") or item.get("source_kind") == "graph" else "memory"
        policy = await self.policy(item["tenant_id"])
        if not policy.enabled or not getattr(policy, domain):
            return False
        digest = memory_fingerprint(item)
        if (value.get("jev") or {}).get("content_hash") == digest:
            return False
        key = fingerprint([item["id"], digest, getattr(self.settings, "typesafe_model", "jev-latest"), "jev-v1"])
        if int(key[:8], 16) / 0x100000000 >= policy.sample_rate:
            return False
        return bool(await self.store.enqueue(
            tenant_id=item["tenant_id"], session_id=item.get("session_id") or "", domain=domain,
            fingerprint=key, input_hash=digest, target_id=int(item["id"]), run=run))

    async def evaluate(self, domain: str, state: dict, *, timeout: float) -> dict:
        if time.monotonic() < self._circuit_until:
            raise RuntimeError("circuit_open")
        if self.client is None:
            from app.typesafe import TypeSafeClient
            self.client = TypeSafeClient(self.settings)
        try:
            response = await asyncio.wait_for(self.client.evaluate(state=state, questions=questions(domain)), timeout)
            if response is None:
                raise ValueError("empty_result")
            result = response.as_dict() if hasattr(response, "as_dict") else response
            if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                raise ValueError("invalid_result")
            self._failures = 0
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failures += 1
            if self._failures >= 3:
                self._circuit_until = time.monotonic() + 30
            raise

    async def online(self, *, tenant_id: str, session_id: str, domain: str,
                     state: dict, trace_id: str) -> tuple[dict | None, bool]:
        if not self.enabled:
            return None, False
        try:
            policy = await self.policy(tenant_id)
            if not policy.enabled or not getattr(policy, domain) or not await self.allowed(tenant_id, session_id, domain):
                return None, False
            key = fingerprint([trace_id, state, getattr(self.settings, "typesafe_model", "jev-latest")])
            if int(key[:8], 16) / 0x100000000 >= policy.sample_rate:
                return None, False
            if domain == "participation" and session_id not in policy.help_sessions:
                return None, False
            shadow_only = policy.participation_shadow_only if domain == "participation" else policy.shadow_only
            if shadow_only:
                await asyncio.wait_for(self.store.enqueue(
                    tenant_id=tenant_id, session_id=session_id, domain=domain,
                    fingerprint=key, input_hash=key, state=state), 1)
                return None, False
            start = time.monotonic()
            result = None
            error = ""
            try:
                async def request():
                    async with self._online_slots:
                        return await self.evaluate(domain, state, timeout=self.settings.typesafe_online_timeout)
                result = await asyncio.wait_for(request(), self.settings.typesafe_online_timeout)
            except Exception as exc:
                error = type(exc).__name__
            self._policies.pop(tenant_id, None)
            policy = await self.policy(tenant_id)
            shadow_only = policy.participation_shadow_only if domain == "participation" else policy.shadow_only
            active = bool(result and policy.enabled and not shadow_only
                          and (domain != "participation" or session_id in policy.help_sessions)
                          and getattr(policy, domain) and confidence(result) >= policy.min_confidence)
            # The caller only receives the active flag after current owner policy is rechecked.
            active = active and await self.allowed(tenant_id, session_id, domain)
            active = active and answer(result, "decision") == ({"intent": "abstain", "moderation": "flag", "participation": "reply"}.get(domain))
            await asyncio.wait_for(self.store.record_online(
                tenant_id=tenant_id, session_id=session_id, domain=domain, key=key,
                result=result, duration_ms=int((time.monotonic()-start)*1000), error=error,
                applied=active), 1)
            return result, active
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("jev.online_fallback", domain=domain, error_type=type(exc).__name__)
            return None, False

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="jev-evaluations")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_batch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("jev.worker_failed", error_type=type(exc).__name__)
            await asyncio.sleep(2)

    async def run_batch(self) -> int:
        if not self.enabled or time.monotonic() < self._circuit_until:
            return 0
        if self.memory_store is not None and time.monotonic() >= self._maintenance_at:
            self._maintenance_at = time.monotonic() + 60
            await self.store.maintain()
            rows = await self.store.memory_changes(*self._reconcile_cursor)
            for row in rows:
                item = await self.memory_store.get_memory_item(row["id"])
                if item is not None:
                    from app.jev.store import execute
                    await self.enqueue_memory(item, run=execute)
                self._reconcile_cursor = (row["updated_at"], row["id"])
        jobs = await self.store.claim(limit=self.settings.typesafe_worker_concurrency,
                                      lease_seconds=max(60, int(self.settings.typesafe_timeout) + 30),
                                      max_attempts=self.settings.typesafe_job_max_attempts)
        await asyncio.gather(*(self._process(job) for job in jobs))
        return len(jobs)

    async def _process(self, job: dict) -> None:
        started = time.monotonic()
        try:
            policy = await self.policy(job["tenant_id"])
            if not policy.enabled or not getattr(policy, job["domain"]) or not await self.allowed(job["tenant_id"], job["session_id"], job["domain"]):
                await self.store.finish(job, status="skipped", error="disabled")
                return
            state = job["state"]
            if job["target_id"] is not None:
                state = await self.memory_store.jev_state(job)
                if state is None:
                    await self.store.finish(job, status="skipped", error="stale_or_protected")
                    return
            result = await self.evaluate(job["domain"], state, timeout=self.settings.typesafe_timeout)
            duration = int((time.monotonic()-started)*1000)
            if job["target_id"] is not None:
                # Re-read tenant policy after I/O, including administrator mode changes.
                self._policies.pop(job["tenant_id"], None)
                policy = await self.policy(job["tenant_id"])
                await self.memory_store.apply_jev_result(job, result, policy=policy, duration_ms=duration)
            else:
                await self.store.finish(job, status="completed", result=result, duration_ms=duration)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retry = int(job["attempts"]) < self.settings.typesafe_job_max_attempts
            await self.store.finish(job, status="pending" if retry else "failed", error=type(exc).__name__,
                                    duration_ms=int((time.monotonic()-started)*1000),
                                    retry_seconds=min(300, 10 * 2 ** int(job["attempts"])))
            log.warning("jev.evaluation_failed", domain=job["domain"], error_type=type(exc).__name__)
