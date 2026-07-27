"""Explicit lifecycle wrapper for embedding the engine outside the app registry."""

from __future__ import annotations

import inspect

from plugins.prompt_audit.config import ConfigSnapshot, PromptAuditConfig
from plugins.prompt_audit.contracts import AuditDecision, AuditRequest
from plugins.prompt_audit.scanner import PromptScanner
from plugins.prompt_audit.service import AuditEventSink, ObserveQueue, PromptAuditService


class PromptAuditComponent:
    def __init__(
        self,
        config: PromptAuditConfig | None = None,
        *,
        scanner: PromptScanner | None = None,
        observe_queue: ObserveQueue | None = None,
        event_sink: AuditEventSink | None = None,
        close_scanner: bool = False,
    ) -> None:
        self.config = ConfigSnapshot(config)
        self.service = PromptAuditService(
            self.config,
            scanner=scanner,
            observe_queue=observe_queue,
            event_sink=event_sink,
        )
        self._closed = False
        self._close_scanner = close_scanner

    @property
    def closed(self) -> bool:
        return self._closed

    def runtime_status(self) -> dict[str, object]:
        return {"closed": self._closed, **self.service.runtime_status()}

    async def evaluate(self, request: AuditRequest) -> AuditDecision:
        if self._closed:
            raise RuntimeError("prompt_audit_component_closed")
        return await self.service.evaluate(request)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._close_scanner:
            return
        close = getattr(self.service.scanner, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
