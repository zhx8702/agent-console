"""OpenAI-compatible Qwen3Guard scanner and strict response normalization."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from app.common.safe_url import (
    OutboundURLPolicy,
    UnsafeOutboundResponseError,
    UnsafeOutboundURLError,
    normalize_origin,
)
from app.egress.safe_http import safe_http_request
from plugins.prompt_audit.config import EndpointConfig, PromptAuditConfig
from plugins.prompt_audit.contracts import (
    AuditDecisionKind,
    AuditRisk,
    RiskCategory,
    SafetyLabel,
    ScanResult,
)
from plugins.prompt_audit.snapshot import AuditChunk

MAX_RESPONSE_BYTES = 256 * 1024


class GuardUnavailable(RuntimeError):
    def __init__(self, code: str = "prompt_guard_unavailable", *, retryable: bool = True):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class GuardInvalidResponse(RuntimeError):
    def __init__(self, code: str = "prompt_guard_invalid_response"):
        self.code = code
        super().__init__(code)


class PromptScanner(Protocol):
    async def scan(self, chunk: AuditChunk, config: PromptAuditConfig) -> ScanResult: ...


_CATEGORY_LABELS: dict[str, RiskCategory] = {
    "Violent": RiskCategory.VIOLENT,
    "Non-violent Illegal Acts": RiskCategory.NON_VIOLENT_ILLEGAL_ACTS,
    "Sexual Content or Sexual Acts": RiskCategory.SEXUAL_CONTENT,
    "PII": RiskCategory.PII,
    "Suicide & Self-Harm": RiskCategory.SELF_HARM,
    "Unethical Acts": RiskCategory.UNETHICAL_ACTS,
    "Politically Sensitive Topics": RiskCategory.POLITICALLY_SENSITIVE,
    "Copyright Violations": RiskCategory.COPYRIGHT,
    "Jailbreak": RiskCategory.JAILBREAK,
}
_FIELD_RE = re.compile(r"^(Safety|Categories): (.*?)$")


def parse_qwen3_guard_output(value: str) -> ScanResult:
    text = str(value or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 2:
        raise GuardInvalidResponse()
    if not lines[0].startswith("Safety: ") or not lines[1].startswith("Categories: "):
        raise GuardInvalidResponse()
    fields: dict[str, str] = {}
    for line in lines:
        match = _FIELD_RE.fullmatch(line)
        if match is None:
            raise GuardInvalidResponse()
        key = match.group(1)
        if key in fields:
            raise GuardInvalidResponse()
        fields[key] = match.group(2).strip()
    safety = fields.get("Safety", "")
    if safety not in {label.value for label in SafetyLabel}:
        raise GuardInvalidResponse()
    raw_categories = fields.get("Categories", "")
    category_names = tuple(
        item.strip()
        for item in raw_categories.split(",")
        if item.strip() and item.strip() != "None"
    )
    if set(fields) != {"Safety", "Categories"}:
        raise GuardInvalidResponse()
    if safety == SafetyLabel.SAFE.value and raw_categories != "None":
        raise GuardInvalidResponse()
    if safety != SafetyLabel.SAFE.value and not category_names:
        raise GuardInvalidResponse()
    categories: set[RiskCategory] = set()
    for name in category_names:
        category = _CATEGORY_LABELS.get(name)
        if category is None:
            raise GuardInvalidResponse()
        categories.add(category)
    if safety == SafetyLabel.SAFE.value:
        kind = AuditDecisionKind.ALLOW
        risk = AuditRisk.LOW
        safety_label = SafetyLabel.SAFE
    elif safety == SafetyLabel.CONTROVERSIAL.value:
        kind = AuditDecisionKind.FLAG
        risk = AuditRisk.MEDIUM
        safety_label = SafetyLabel.CONTROVERSIAL
    else:
        kind = AuditDecisionKind.BLOCK
        risk = AuditRisk.HIGH
        safety_label = SafetyLabel.UNSAFE
    return ScanResult(
        kind=kind,
        risk=risk,
        safety=safety_label,
        categories=tuple(sorted(categories, key=lambda item: item.value)),
    )


@dataclass(slots=True)
class Qwen3GuardScanner:
    client: httpx.AsyncClient | None = None
    global_limit: int = 64
    per_endpoint_limit: int = 16
    _global_tokens: asyncio.Queue[None] = field(init=False, repr=False)
    _endpoint_tokens: dict[str, asyncio.Queue[None]] = field(
        init=False,
        repr=False,
        default_factory=dict,
    )

    def __post_init__(self) -> None:
        if self.global_limit < 1 or self.per_endpoint_limit < 1:
            raise ValueError("prompt-audit scanner concurrency limits must be positive")
        self._global_tokens = _token_queue(self.global_limit)

    async def scan(self, chunk: AuditChunk, config: PromptAuditConfig) -> ScanResult:
        try:
            self._global_tokens.get_nowait()
        except asyncio.QueueEmpty as exc:
            raise GuardUnavailable("prompt_guard_bulkhead_full", retryable=True) from exc
        try:
            return await self._scan_with_failover(chunk, config)
        finally:
            self._global_tokens.put_nowait(None)

    async def _scan_with_failover(
        self,
        chunk: AuditChunk,
        config: PromptAuditConfig,
    ) -> ScanResult:
        endpoints = config.active_endpoints
        if not endpoints:
            raise GuardUnavailable()
        last_error: GuardUnavailable | None = None
        for endpoint in endpoints:
            try:
                return await self._scan_endpoint(chunk, endpoint)
            except GuardInvalidResponse:
                raise
            except GuardUnavailable as exc:
                last_error = exc
                if not exc.retryable:
                    raise
        raise last_error or GuardUnavailable()

    async def _scan_endpoint(self, chunk: AuditChunk, endpoint: EndpointConfig) -> ScanResult:
        tokens = self._endpoint_tokens.setdefault(
            endpoint.id,
            _token_queue(self.per_endpoint_limit),
        )
        try:
            tokens.get_nowait()
        except asyncio.QueueEmpty as exc:
            raise GuardUnavailable("prompt_guard_bulkhead_full", retryable=True) from exc
        try:
            return await self._scan_endpoint_with_token(chunk, endpoint)
        finally:
            tokens.put_nowait(None)

    async def _scan_endpoint_with_token(
        self,
        chunk: AuditChunk,
        endpoint: EndpointConfig,
    ) -> ScanResult:
        started = time.perf_counter()
        client = self.client
        owns_client = False
        try:
            url = _chat_completions_url(endpoint.base_url)
            parsed_url = urlsplit(url)
            exact_host = str(parsed_url.hostname or "").strip().lower()
            allowed_hosts = frozenset(
                endpoint.allowed_hosts or ((exact_host,) if exact_host else ())
            )
            endpoint_origin = normalize_origin(url)
            configured_private_origins = {
                origin
                for value in endpoint.allowed_private_origins
                if (origin := normalize_origin(value))
            }
            policy = OutboundURLPolicy(
                allowed_hosts=allowed_hosts,
                allowed_private_origins=frozenset(
                    {endpoint_origin}
                    if endpoint_origin in configured_private_origins
                    else set()
                ),
                max_redirects=0,
                max_response_bytes=MAX_RESPONSE_BYTES,
                timeout_seconds=endpoint.timeout_seconds,
                # Error responses are inspected only for their status. A 2xx
                # response must still be application/json below.
                allowed_response_content_types=(
                    "application/json",
                    "application/problem+json",
                    "text/plain",
                ),
            )
            if client is None:
                client = httpx.AsyncClient(timeout=endpoint.timeout_seconds, trust_env=False)
                owns_client = True
            headers = {"accept": "application/json", "content-type": "application/json"}
            if endpoint.api_key:
                headers["authorization"] = f"Bearer {endpoint.api_key}"
            payload = {
                "model": endpoint.model,
                "messages": [{"role": "user", "content": chunk.text}],
                "temperature": 0,
                "stream": False,
            }
            response = await safe_http_request(
                client,
                "POST",
                url,
                json=payload,
                headers=headers,
                policy=policy,
            )
            if response.status_code in {401, 403}:
                raise GuardUnavailable("prompt_guard_unauthorized", retryable=False)
            if response.status_code == 429 or response.status_code >= 500:
                raise GuardUnavailable(retryable=True)
            if response.status_code < 200 or response.status_code >= 300:
                raise GuardUnavailable("prompt_guard_request_rejected", retryable=False)
            media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if media_type != "application/json":
                raise GuardInvalidResponse()
            try:
                decoded = json.loads(response.content)
                content = decoded["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                raise GuardInvalidResponse() from exc
            if not isinstance(content, str):
                raise GuardInvalidResponse()
            parsed = parse_qwen3_guard_output(content)
            return ScanResult(
                kind=parsed.kind,
                risk=parsed.risk,
                safety=parsed.safety,
                categories=parsed.categories,
                unknown_categories=parsed.unknown_categories,
                endpoint_id=endpoint.id,
                latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                chunk_count=1,
            )
        except UnsafeOutboundResponseError as exc:
            raise GuardInvalidResponse() from exc
        except UnsafeOutboundURLError as exc:
            code = (
                "prompt_guard_redirect_rejected"
                if "redirect" in str(exc).lower()
                else "prompt_guard_endpoint_invalid"
            )
            raise GuardUnavailable(code, retryable=False) from exc
        except httpx.TimeoutException as exc:
            raise GuardUnavailable("prompt_guard_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise GuardUnavailable(retryable=True) from exc
        except (TypeError, ValueError) as exc:
            raise GuardUnavailable("prompt_guard_endpoint_invalid", retryable=False) from exc
        finally:
            if owns_client and client is not None:
                await client.aclose()

    async def close(self) -> None:
        # An injected client remains owned by its caller. Per-request clients
        # created by this scanner are closed in ``_scan_endpoint``.
        return None


def _token_queue(size: int) -> asyncio.Queue[None]:
    queue: asyncio.Queue[None] = asyncio.Queue(maxsize=size)
    for _ in range(size):
        queue.put_nowait(None)
    return queue


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return urljoin(base + "/", "v1/chat/completions")
