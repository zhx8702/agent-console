"""Mode orchestration for the standalone prompt-audit engine."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

from plugins.prompt_audit.config import (
    ConfigSnapshot,
    ConfigVersionUnavailable,
    PromptAuditConfig,
)
from plugins.prompt_audit.contracts import (
    AuditDecision,
    AuditDecisionKind,
    AuditMode,
    AuditRequest,
    AuditRisk,
)
from plugins.prompt_audit.scanner import (
    GuardInvalidResponse,
    GuardUnavailable,
    PromptScanner,
)
from plugins.prompt_audit.snapshot import (
    AuditSnapshot,
    PromptTooLargeError,
    aggregate_scan_results,
    build_snapshot,
    chunk_snapshot,
)


class ObserveQueue(Protocol):
    async def enqueue(
        self,
        request: AuditRequest,
        snapshot: AuditSnapshot,
        config_version: int,
    ) -> None: ...


class AuditEventSink(Protocol):
    async def record(
        self,
        decision: AuditDecision,
        snapshot: AuditSnapshot,
        config_version: int,
    ) -> None: ...


@dataclass(slots=True)
class RuntimeCounters:
    total: int = 0
    allowed: int = 0
    flagged: int = 0
    blocked: int = 0
    unavailable: int = 0
    invalid: int = 0
    observe_enqueued: int = 0
    observe_dropped: int = 0
    event_record_failed: int = 0


class PromptAuditService:
    def __init__(
        self,
        config: ConfigSnapshot | None = None,
        *,
        scanner: PromptScanner | None = None,
        observe_queue: ObserveQueue | None = None,
        event_sink: AuditEventSink | None = None,
    ) -> None:
        self.config = config or ConfigSnapshot()
        self.scanner = scanner
        self.observe_queue = observe_queue
        self.event_sink = event_sink
        self.counters = RuntimeCounters()

    async def evaluate(self, request: AuditRequest) -> AuditDecision:
        started = time.perf_counter()
        config = self.config.get()
        mode = config.effective_mode
        self.counters.total += 1
        if mode == AuditMode.OFF:
            decision = AuditDecision(kind=AuditDecisionKind.ALLOW, mode=mode)
            self._observe(decision)
            return decision

        try:
            snapshot = self._snapshot(request, config)
        except Exception as exc:
            code = (
                str(exc)
                if isinstance(exc, PromptTooLargeError)
                else "prompt_audit_invalid_input"
            )
            kind = (
                AuditDecisionKind.ALLOW
                if mode == AuditMode.OBSERVE
                else AuditDecisionKind.INVALID
            )
            if mode == AuditMode.OBSERVE:
                self.counters.observe_dropped += 1
            decision = AuditDecision(kind=kind, mode=mode, error_code=code)
            self._observe(decision)
            return decision
        if mode == AuditMode.OBSERVE:
            if self.observe_queue is None:
                self.counters.observe_dropped += 1
            else:
                try:
                    async with asyncio.timeout(config.observe_enqueue_timeout_seconds):
                        await self.observe_queue.enqueue(request, snapshot, config.version)
                    self.counters.observe_enqueued += 1
                except Exception:
                    self.counters.observe_dropped += 1
            decision = AuditDecision(
                kind=AuditDecisionKind.ALLOW,
                mode=mode,
                prompt_hash=snapshot.prompt_hash,
                redacted_preview=snapshot.redacted_preview,
                latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            )
            self._observe(decision)
            return decision

        decision = await self._evaluate_blocking(snapshot, config, started=started)
        self._observe(decision)
        await self._record(decision, snapshot, config)
        return decision

    async def process_observed(
        self,
        request: AuditRequest,
        *,
        config_version: int,
    ) -> AuditDecision:
        """Scan one queued observe request using its original config version."""

        started = time.perf_counter()
        try:
            config = self.config.get(config_version)
            snapshot = self._snapshot(request, config)
        except ConfigVersionUnavailable:
            return AuditDecision(
                kind=AuditDecisionKind.INVALID,
                mode=AuditMode.OBSERVE,
                error_code="prompt_audit_config_version_unavailable",
            )
        except Exception as exc:
            return AuditDecision(
                kind=AuditDecisionKind.INVALID,
                mode=AuditMode.OBSERVE,
                error_code=(
                    str(exc)
                    if isinstance(exc, PromptTooLargeError)
                    else "prompt_audit_invalid_input"
                ),
            )
        decision = await self._evaluate_blocking(snapshot, config, started=started)
        await self._record(decision, snapshot, config)
        return decision

    @staticmethod
    def _snapshot(request: AuditRequest, config: PromptAuditConfig) -> AuditSnapshot:
        return build_snapshot(
            request,
            preview_chars=config.preview_chars,
            max_input_chars=config.max_input_chars,
            max_prior_segments=config.max_prior_segments,
        )

    async def _evaluate_blocking(
        self,
        snapshot: AuditSnapshot,
        config: PromptAuditConfig,
        *,
        started: float,
    ) -> AuditDecision:
        if self.scanner is None or not config.active_endpoints:
            return self._failure_decision(
                AuditDecisionKind.UNAVAILABLE,
                config,
                snapshot,
                "prompt_guard_unavailable",
                started,
            )
        limit = min(endpoint.input_limit for endpoint in config.active_endpoints)
        chunks = chunk_snapshot(snapshot, limit)
        if len(chunks) > config.max_chunks:
            return self._failure_decision(
                AuditDecisionKind.INVALID,
                config,
                snapshot,
                "prompt_audit_too_many_chunks",
                started,
            )
        if not chunks:
            return AuditDecision(
                kind=AuditDecisionKind.ALLOW,
                mode=config.effective_mode,
                prompt_hash=snapshot.prompt_hash,
                redacted_preview=snapshot.redacted_preview,
            )
        results = []
        try:
            async with asyncio.timeout(config.total_timeout_seconds):
                for chunk in chunks:
                    result = await self.scanner.scan(chunk, config)
                    results.append(result)
                    if result.kind == AuditDecisionKind.BLOCK:
                        break
        except GuardInvalidResponse as exc:
            return self._failure_decision(
                AuditDecisionKind.INVALID,
                config,
                snapshot,
                exc.code,
                started,
            )
        except (GuardUnavailable, TimeoutError) as exc:
            code = getattr(exc, "code", "prompt_guard_timeout")
            return self._failure_decision(
                AuditDecisionKind.UNAVAILABLE,
                config,
                snapshot,
                code,
                started,
            )
        except Exception:
            return self._failure_decision(
                AuditDecisionKind.UNAVAILABLE,
                config,
                snapshot,
                "prompt_guard_unavailable",
                started,
            )
        try:
            result = aggregate_scan_results(results)
        except ValueError:
            return self._failure_decision(
                AuditDecisionKind.INVALID,
                config,
                snapshot,
                "prompt_guard_invalid_response",
                started,
            )
        return AuditDecision(
            kind=result.kind,
            mode=config.effective_mode,
            risk=result.risk,
            categories=result.categories,
            unknown_categories=result.unknown_categories,
            endpoint_id=result.endpoint_id,
            prompt_hash=snapshot.prompt_hash,
            redacted_preview=snapshot.redacted_preview,
            chunk_count=result.chunk_count,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )

    @staticmethod
    def _failure_decision(
        kind: AuditDecisionKind,
        config: PromptAuditConfig,
        snapshot: AuditSnapshot,
        error_code: str,
        started: float,
    ) -> AuditDecision:
        return AuditDecision(
            kind=kind,
            mode=config.effective_mode,
            risk=AuditRisk.HIGH,
            error_code=error_code,
            prompt_hash=snapshot.prompt_hash,
            redacted_preview=snapshot.redacted_preview,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )

    async def _record(
        self,
        decision: AuditDecision,
        snapshot: AuditSnapshot,
        config: PromptAuditConfig,
    ) -> None:
        if self.event_sink is None:
            return
        if decision.kind == AuditDecisionKind.ALLOW and not config.store_pass_events:
            return
        try:
            async with asyncio.timeout(config.event_record_timeout_seconds):
                await self.event_sink.record(decision, snapshot.redacted(), config.version)
        except Exception:
            self.counters.event_record_failed += 1

    def _observe(self, decision: AuditDecision) -> None:
        if decision.kind == AuditDecisionKind.ALLOW:
            self.counters.allowed += 1
        elif decision.kind == AuditDecisionKind.FLAG:
            self.counters.flagged += 1
        elif decision.kind == AuditDecisionKind.BLOCK:
            self.counters.blocked += 1
        elif decision.kind == AuditDecisionKind.INVALID:
            self.counters.invalid += 1
        else:
            self.counters.unavailable += 1

    def runtime_status(self) -> dict[str, object]:
        config = self.config.get()
        return {
            "mode": config.effective_mode.value,
            "config_version": config.version,
            "configured_endpoints": len(config.active_endpoints),
            "scanner_configured": self.scanner is not None,
            "observe_queue_configured": self.observe_queue is not None,
            "event_sink_configured": self.event_sink is not None,
            "counters": {
                field: getattr(self.counters, field)
                for field in self.counters.__dataclass_fields__
            },
        }
