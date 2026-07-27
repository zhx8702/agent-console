from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.plugin.registry import PluginRegistry
from plugins.prompt_audit import (
    AuditDecisionKind,
    AuditMode,
    AuditRequest,
    AuditRisk,
    ConfigSnapshot,
    EndpointConfig,
    GuardInvalidResponse,
    PromptAuditComponent,
    PromptAuditConfig,
    PromptAuditService,
    Qwen3GuardScanner,
    RiskCategory,
    SafetyLabel,
    ScanResult,
    parse_qwen3_guard_output,
)
from plugins.prompt_audit.config import ConfigConflictError
from plugins.prompt_audit.scanner import GuardUnavailable
from plugins.prompt_audit.snapshot import (
    aggregate_scan_results,
    build_snapshot,
    chunk_snapshot,
)

ROOT = Path(__file__).resolve().parents[2]


def _endpoint(**changes: object) -> EndpointConfig:
    values: dict[str, object] = {
        "id": "guard-a",
        "base_url": "https://guard.example.com",
        "model": "qwen3guard-test",
        "api_key": "sk-super-secret",
        "allowed_hosts": ("guard.example.com",),
        "input_limit": 16,
    }
    values.update(changes)
    return EndpointConfig(**values)  # type: ignore[arg-type]


def _config(mode: AuditMode, **changes: object) -> PromptAuditConfig:
    values: dict[str, object] = {
        "enabled": mode != AuditMode.OFF,
        "mode": mode,
        "version": 2,
        "endpoints": (_endpoint(),),
        "total_timeout_seconds": 1.0,
    }
    values.update(changes)
    return PromptAuditConfig(**values)  # type: ignore[arg-type]


class _Scanner:
    def __init__(
        self,
        results: list[ScanResult] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.results = list(results or [])
        self.exc = exc
        self.calls: list[str] = []
        self.closed = 0

    async def scan(self, chunk, config):
        self.calls.append(chunk.text)
        if self.exc is not None:
            raise self.exc
        if not self.results:
            raise AssertionError("unexpected scanner call")
        return self.results.pop(0)

    async def close(self) -> None:
        self.closed += 1


class _Queue:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[tuple[AuditRequest, object, int]] = []

    async def enqueue(self, request, snapshot, config_version):
        self.calls.append((request, snapshot, config_version))
        if self.exc is not None:
            raise self.exc


class _HangingQueue(_Queue):
    async def enqueue(self, request, snapshot, config_version):
        self.calls.append((request, snapshot, config_version))
        await asyncio.Event().wait()


class _Sink:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[tuple[object, object, int]] = []

    async def record(self, decision, snapshot, config_version):
        self.calls.append((decision, snapshot, config_version))
        if self.exc is not None:
            raise self.exc


def _scan_result(
    kind: AuditDecisionKind,
    *,
    risk: AuditRisk = AuditRisk.LOW,
    category: RiskCategory | None = None,
) -> ScanResult:
    return ScanResult(
        kind=kind,
        risk=risk,
        safety={
            AuditDecisionKind.ALLOW: SafetyLabel.SAFE,
            AuditDecisionKind.FLAG: SafetyLabel.CONTROVERSIAL,
            AuditDecisionKind.BLOCK: SafetyLabel.UNSAFE,
        }[kind],
        categories=(category,) if category else (),
        endpoint_id="guard-a",
    )


@pytest.mark.asyncio
async def test_default_off_has_no_scanner_queue_or_event_side_effects() -> None:
    scanner = _Scanner()
    queue = _Queue()
    sink = _Sink()
    service = PromptAuditService(scanner=scanner, observe_queue=queue, event_sink=sink)

    decision = await service.evaluate(AuditRequest(request_id="m1", text="hello"))

    assert decision.kind == AuditDecisionKind.ALLOW
    assert decision.mode == AuditMode.OFF
    assert decision.allow_next_stage is True
    assert scanner.calls == []
    assert queue.calls == []
    assert sink.calls == []


@pytest.mark.asyncio
async def test_observe_mode_is_best_effort_and_never_changes_the_gate() -> None:
    queue = _Queue(exc=RuntimeError("queue unavailable"))
    service = PromptAuditService(ConfigSnapshot(_config(AuditMode.OBSERVE)), observe_queue=queue)

    decision = await service.evaluate(
        AuditRequest(request_id="m2", text="contact me at user@example.com")
    )

    assert decision.kind == AuditDecisionKind.ALLOW
    assert decision.mode == AuditMode.OBSERVE
    assert decision.allow_next_stage is True
    assert decision.redacted_preview == "contact me at <EMAIL>"
    assert len(queue.calls) == 1
    assert queue.calls[0][2] == 2
    assert service.counters.observe_dropped == 1


@pytest.mark.asyncio
async def test_blocking_mode_scans_all_safe_chunks_and_aggregates_flag() -> None:
    scanner = _Scanner(
        [
            _scan_result(AuditDecisionKind.ALLOW),
            _scan_result(
                AuditDecisionKind.FLAG,
                risk=AuditRisk.MEDIUM,
                category=RiskCategory.POLITICALLY_SENSITIVE,
            ),
        ]
    )
    config = _config(
        AuditMode.BLOCKING,
        endpoints=(_endpoint(input_limit=2),),
    )
    service = PromptAuditService(ConfigSnapshot(config), scanner=scanner)

    decision = await service.evaluate(AuditRequest(request_id="m3", text="abcd"))

    assert scanner.calls == ["ab", "cd"]
    assert decision.kind == AuditDecisionKind.FLAG
    assert decision.categories == (RiskCategory.POLITICALLY_SENSITIVE,)
    assert decision.chunk_count == 2
    assert decision.allow_next_stage is True


@pytest.mark.asyncio
async def test_blocking_mode_stops_on_block_and_records_only_redacted_snapshot() -> None:
    scanner = _Scanner(
        [
            _scan_result(
                AuditDecisionKind.BLOCK,
                risk=AuditRisk.HIGH,
                category=RiskCategory.JAILBREAK,
            )
        ]
    )
    sink = _Sink()
    service = PromptAuditService(
        ConfigSnapshot(
            _config(AuditMode.BLOCKING, endpoints=(_endpoint(input_limit=2),))
        ),
        scanner=scanner,
        event_sink=sink,
    )

    decision = await service.evaluate(
        AuditRequest(request_id="m4", text="sk-abcdefgh-secret")
    )

    assert decision.kind == AuditDecisionKind.BLOCK
    assert decision.allow_next_stage is False
    assert scanner.calls == ["sk"]
    assert decision.redacted_preview == "<SECRET>"
    assert len(sink.calls) == 1
    _recorded_decision, recorded_snapshot, config_version = sink.calls[0]
    assert recorded_snapshot.scan_text == ""
    assert recorded_snapshot.redacted_preview == "<SECRET>"
    assert config_version == 2


@pytest.mark.asyncio
async def test_blocking_errors_fail_closed_without_becoming_allow() -> None:
    unavailable = PromptAuditService(
        ConfigSnapshot(_config(AuditMode.BLOCKING)),
        scanner=_Scanner(exc=GuardUnavailable("prompt_guard_timeout")),
    )
    invalid = PromptAuditService(
        ConfigSnapshot(_config(AuditMode.BLOCKING)),
        scanner=_Scanner(exc=GuardInvalidResponse()),
    )

    unavailable_decision = await unavailable.evaluate(
        AuditRequest(request_id="m5", text="hello")
    )
    invalid_decision = await invalid.evaluate(AuditRequest(request_id="m6", text="hello"))

    assert unavailable_decision.kind == AuditDecisionKind.UNAVAILABLE
    assert unavailable_decision.allow_next_stage is False
    assert unavailable_decision.error_code == "prompt_guard_timeout"
    assert invalid_decision.kind == AuditDecisionKind.INVALID
    assert invalid_decision.allow_next_stage is False


def test_config_snapshot_is_versioned_and_compare_and_swap() -> None:
    snapshot = ConfigSnapshot()
    replacement = _config(AuditMode.OBSERVE)

    assert snapshot.replace(replacement, expected_version=1) is replacement
    with pytest.raises(ConfigConflictError, match="prompt_audit_config_conflict"):
        snapshot.replace(replace(replacement, version=3), expected_version=1)


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": "false"},
        {"mode": "blocking"},
        {"total_timeout_seconds": 0},
        {"preview_chars": -1},
        {"max_input_chars": 0},
        {"store_pass_events": 1},
    ],
)
def test_config_rejects_ambiguous_or_unsafe_types(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "enabled": False,
        "mode": AuditMode.OFF,
    }
    values.update(changes)

    with pytest.raises((TypeError, ValueError)):
        PromptAuditConfig(**values)  # type: ignore[arg-type]


def test_endpoint_repr_does_not_expose_api_key() -> None:
    assert "sk-super-secret" not in repr(_endpoint())


def test_snapshot_redacts_secrets_and_chunks_unicode_graphemes() -> None:
    request = AuditRequest(
        request_id="m7",
        text="A👨\u200d👩\u200d👧\u200d👦e\u0301",
        prior_text=("13800138000",),
    )
    snapshot = build_snapshot(request, preview_chars=100)
    chunks = chunk_snapshot(snapshot, 1)

    assert [chunk.text for chunk in chunks[:3]] == ["A", "👨\u200d👩\u200d👧\u200d👦", "e\u0301"]
    assert chunks[-1].text == "0"
    assert "13800138000" not in snapshot.redacted_preview
    assert "<PHONE>" in snapshot.redacted_preview


def test_aggregate_uses_worst_result_and_stable_category_order() -> None:
    result = aggregate_scan_results(
        [
            _scan_result(
                AuditDecisionKind.FLAG,
                risk=AuditRisk.MEDIUM,
                category=RiskCategory.PII,
            ),
            _scan_result(
                AuditDecisionKind.BLOCK,
                risk=AuditRisk.HIGH,
                category=RiskCategory.JAILBREAK,
            ),
        ]
    )

    assert result.kind == AuditDecisionKind.BLOCK
    assert result.categories == (RiskCategory.JAILBREAK, RiskCategory.PII)
    assert result.chunk_count == 2


@pytest.mark.parametrize(
    ("content", "kind", "categories"),
    [
        ("Safety: Safe\nCategories: None", AuditDecisionKind.ALLOW, ()),
        (
            "Safety: Controversial\nCategories: PII",
            AuditDecisionKind.FLAG,
            (RiskCategory.PII,),
        ),
        (
            "Safety: Unsafe\nCategories: Jailbreak, Violent",
            AuditDecisionKind.BLOCK,
            (RiskCategory.JAILBREAK, RiskCategory.VIOLENT),
        ),
    ],
)
def test_qwen_parser_accepts_only_canonical_complete_output(
    content: str,
    kind: AuditDecisionKind,
    categories: tuple[RiskCategory, ...],
) -> None:
    result = parse_qwen3_guard_output(content)

    assert result.kind == kind
    assert result.categories == categories


@pytest.mark.parametrize(
    "content",
    [
        "",
        "Safety: Safe",
        "Unsafe",
        "Safety: Safe\nCategories: Jailbreak",
        "Safety: Unsafe\nCategories: Jailbreak\nexplanation: no",
        "```text\nSafety: Safe\nCategories: None\n```",
        "Safety: Safe\nSafety: Unsafe",
        "Safety: Unsafe\nCategories: None",
        "Safety: Unsafe\nCategories: Unknown Future Category",
        "Categories: Jailbreak\nSafety: Unsafe",
    ],
)
def test_qwen_parser_rejects_partial_or_ambiguous_output(content: str) -> None:
    with pytest.raises(GuardInvalidResponse):
        parse_qwen3_guard_output(content)


@pytest.mark.asyncio
async def test_http_scanner_uses_openai_envelope_auth_and_safe_url_policy() -> None:
    seen: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "choices": [
                    {"message": {"content": "Safety: Safe\nCategories: None"}}
                ]
            },
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(responder),
        trust_env=False,
    ) as client:
        scanner = Qwen3GuardScanner(client)
        config = _config(AuditMode.BLOCKING)
        chunk = chunk_snapshot(
            build_snapshot(AuditRequest(request_id="m8", text="hello")),
            100,
        )[0]
        result = await scanner.scan(chunk, config)

    assert result.kind == AuditDecisionKind.ALLOW
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://guard.example.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-super-secret"
    body = json.loads(request.content)
    assert body["model"] == "qwen3guard-test"
    assert body["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_http_scanner_does_not_duplicate_v1_in_base_url() -> None:
    seen: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "choices": [
                    {"message": {"content": "Safety: Safe\nCategories: None"}}
                ]
            },
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(responder),
        trust_env=False,
    ) as client:
        scanner = Qwen3GuardScanner(client)
        chunk = chunk_snapshot(
            build_snapshot(AuditRequest(request_id="m-v1", text="hello")),
            100,
        )[0]
        await scanner.scan(
            chunk,
            _config(
                AuditMode.BLOCKING,
                endpoints=(_endpoint(base_url="https://guard.example.com/v1"),),
            ),
        )

    assert str(seen[0].url) == "https://guard.example.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_http_scanner_rejects_malformed_response() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"choices": []},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(responder),
        trust_env=False,
    ) as client:
        scanner = Qwen3GuardScanner(client)
        chunk = chunk_snapshot(
            build_snapshot(AuditRequest(request_id="m9", text="hello")),
            100,
        )[0]
        with pytest.raises(GuardInvalidResponse):
            await scanner.scan(chunk, _config(AuditMode.BLOCKING))


@pytest.mark.asyncio
async def test_http_scanner_rejects_post_redirect_without_forwarding_auth() -> None:
    seen: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            307,
            headers={"location": "https://attacker.example/collect"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(responder),
        trust_env=False,
    ) as client:
        scanner = Qwen3GuardScanner(client)
        chunk = chunk_snapshot(
            build_snapshot(AuditRequest(request_id="m-redirect", text="hello")),
            100,
        )[0]
        with pytest.raises(GuardUnavailable) as excinfo:
            await scanner.scan(chunk, _config(AuditMode.BLOCKING))

    assert excinfo.value.code == "prompt_guard_redirect_rejected"
    assert excinfo.value.retryable is False
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer sk-super-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "content"),
    [
        ({"content-type": "image/png"}, b"not-json"),
        (
            {"content-type": "application/json"},
            b"x" * (256 * 1024 + 1),
        ),
    ],
    ids=("wrong-content-type", "oversized"),
)
async def test_http_scanner_rejects_wrong_type_and_oversized_responses(
    headers: dict[str, str],
    content: bytes,
) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=headers,
            content=content,
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(responder),
        trust_env=False,
    ) as client:
        scanner = Qwen3GuardScanner(client)
        chunk = chunk_snapshot(
            build_snapshot(AuditRequest(request_id="m-bounds", text="hello")),
            100,
        )[0]
        with pytest.raises(GuardInvalidResponse):
            await scanner.scan(chunk, _config(AuditMode.BLOCKING))


@pytest.mark.asyncio
async def test_scanner_bulkhead_fails_fast_without_network() -> None:
    seen: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise AssertionError("network must not be reached")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(responder),
        trust_env=False,
    ) as client:
        scanner = Qwen3GuardScanner(client, global_limit=1)
        scanner._global_tokens.get_nowait()
        chunk = chunk_snapshot(
            build_snapshot(AuditRequest(request_id="m-bulkhead", text="hello")),
            100,
        )[0]
        with pytest.raises(GuardUnavailable, match="prompt_guard_bulkhead_full"):
            await scanner.scan(chunk, _config(AuditMode.BLOCKING))

    assert seen == []


@pytest.mark.asyncio
async def test_observe_enqueue_is_bounded_and_remains_allow() -> None:
    config = _config(
        AuditMode.OBSERVE,
        observe_enqueue_timeout_seconds=0.01,
    )
    queue = _HangingQueue()
    service = PromptAuditService(ConfigSnapshot(config), observe_queue=queue)

    decision = await service.evaluate(AuditRequest(request_id="m-timeout", text="hello"))

    assert decision.kind == AuditDecisionKind.ALLOW
    assert service.counters.observe_dropped == 1


@pytest.mark.asyncio
async def test_oversized_input_is_observed_as_drop_and_blocking_is_invalid() -> None:
    request = AuditRequest(request_id="m-large", text="12345")
    observe = PromptAuditService(
        ConfigSnapshot(_config(AuditMode.OBSERVE, max_input_chars=4)),
        observe_queue=_Queue(),
    )
    blocking_scanner = _Scanner([_scan_result(AuditDecisionKind.ALLOW)])
    blocking = PromptAuditService(
        ConfigSnapshot(_config(AuditMode.BLOCKING, max_input_chars=4)),
        scanner=blocking_scanner,
    )

    observe_decision = await observe.evaluate(request)
    blocking_decision = await blocking.evaluate(request)

    assert observe_decision.kind == AuditDecisionKind.ALLOW
    assert observe_decision.error_code == "prompt_audit_input_too_large"
    assert observe.counters.observe_dropped == 1
    assert blocking_decision.kind == AuditDecisionKind.INVALID
    assert blocking_decision.allow_next_stage is False
    assert blocking_scanner.calls == []


@pytest.mark.asyncio
async def test_observe_worker_uses_the_queued_config_version() -> None:
    initial = _config(AuditMode.OBSERVE)
    snapshot = ConfigSnapshot(initial)
    snapshot.replace(_config(AuditMode.BLOCKING, version=3), expected_version=2)
    scanner = _Scanner(
        [
            _scan_result(
                AuditDecisionKind.BLOCK,
                risk=AuditRisk.HIGH,
                category=RiskCategory.JAILBREAK,
            )
        ]
    )
    service = PromptAuditService(snapshot, scanner=scanner)

    decision = await service.process_observed(
        AuditRequest(request_id="m-observed", text="unsafe"),
        config_version=2,
    )

    assert decision.mode == AuditMode.OBSERVE
    assert decision.kind == AuditDecisionKind.BLOCK
    assert scanner.calls == ["unsafe"]


@pytest.mark.asyncio
async def test_component_close_is_idempotent_and_owned_scanner_is_closed_once() -> None:
    scanner = _Scanner()
    component = PromptAuditComponent(scanner=scanner, close_scanner=True)

    await component.close()
    await component.close()

    assert component.closed is True
    assert scanner.closed == 1
    with pytest.raises(RuntimeError, match="prompt_audit_component_closed"):
        await component.evaluate(AuditRequest(request_id="m10", text="hello"))


def test_prompt_audit_remains_undiscovered_until_an_adapter_is_added() -> None:
    package_dir = ROOT / "plugins" / "prompt_audit"
    registry = PluginRegistry()

    assert package_dir.is_dir()
    assert not (package_dir / "plugin.py").exists()
    registry.discover_directory(ROOT / "plugins")
    assert "prompt_audit" not in registry.loaded_plugins
