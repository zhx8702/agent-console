"""
Admin FastAPI router for FAQ + KB CRUD.

Endpoints (JSON in/out). Protected by the shared short-lived session / bearer
dependency sourced from settings.

Wiring: ``build_admin_router(faq_store, kb_service)`` returns a ready APIRouter.
Use ``app.include_router(build_admin_router(...))`` in main.py.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import Field, ValidationError

from app.admin.audit import set_admin_audit_context
from app.admin.authorization import Principal, build_admin_authorization_dependency
from app.admin.capabilities import build_tenant_capabilities, tenant_scope_allowed
from app.admin.dlq_service import DLQAdminService, DLQMessage
from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdempotencyConflictError,
    MutationIdentity,
    MutationLedgerError,
    MutationOutcome,
    hash_identifier,
    run_idempotent_mutation,
)
from app.admin.stream_service import StreamAdminService
from app.common.config import Settings, get_settings
from app.common.exceptions import CapabilityError
from app.common.request_models import StrictRequestModel
from app.common.runtime_llm_config import (
    RUNTIME_LLM_MUTABLE_FIELDS,
    ResolvedRuntimeLlmConfig,
    RuntimeLlmConfigIdempotencyConflict,
    RuntimeLlmConfigMutation,
    RuntimeLlmConfigSnapshot,
    RuntimeLlmConfigStore,
    RuntimeLlmConfigVersionConflict,
    externally_managed_runtime_llm_fields,
    load_runtime_llm_config,
    normalize_runtime_llm_idempotency_key,
    normalize_runtime_llm_overrides,
    resolve_runtime_llm_config,
    runtime_llm_request_hash,
    runtime_llm_secret_status,
)
from app.common.types import Channel, InboundEvent, Message, PreprocessedMessage, Session
from app.faq.store import FAQStore
from app.infra.db import get_engine
from app.kb.scope import normalize_scope_session_id, scope_payload
from app.kb.service import KnowledgeBaseService
from app.llm.service import validate_llm_settings
from app.orchestrator.effect_handlers import EffectDispatcher, effect_handler_registry_payload
from app.orchestrator.effect_log import PostgresEffectLog
from app.orchestrator.effects import (
    AuditedEffectCommitter,
    EffectIdempotencyConflictError,
    InMemoryEffectCommitter,
    RedisEffectCommitter,
)
from app.orchestrator.flow import (
    CompiledFlow,
    CompiledStep,
    FlowBinding,
    FlowResolveCandidate,
    FlowResolveRequest,
    FlowResolveResult,
    FlowStepDefinition,
    FlowStepRegistry,
    MessageEffect,
    build_default_flow_registry,
    compile_builtin_flows,
    normalize_flow_session_kind,
    resolve_builtin_flow,
)
from app.orchestrator.flow_runtime_config import build_flow_runtime_config_payload
from app.orchestrator.pipeline import PipelineContext
from app.orchestrator.runner import FlowRunner, FlowRunResult, FlowRunStepTrace
from app.orchestrator.trace_store import read_flow_trace_snapshots
from app.plugin.config_schema import (
    PluginConfigSchemaError,
    PluginConfigValidationError,
    validate_config_payload_bounds,
    validate_plugin_config,
)
from app.plugin.manager import PluginManager
from app.plugin.registry import PluginRegistry
from app.plugin.state import PluginScopeVersionConflictError


class FAQCreate(StrictRequestModel):
    tenant_id: str
    session_id: str | None = None
    question: str
    answer: str
    variants: list[str] | None = None
    tags: list[str] | None = None


class FAQUpdate(StrictRequestModel):
    question: str | None = None
    answer: str | None = None
    variants: list[str] | None = None
    tags: list[str] | None = None
    status: str | None = None


class FAQTestRequest(StrictRequestModel):
    tenant_id: str
    session_id: str | None = None
    query: str


class DocCreate(StrictRequestModel):
    tenant_id: str
    session_id: str | None = None
    title: str
    content: str
    source: str = "manual"
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocUpdate(StrictRequestModel):
    title: str
    content: str
    source: str = "manual"
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocSearchRequest(StrictRequestModel):
    tenant_id: str
    session_id: str | None = None
    query: str
    top_k: int = Field(default=5, ge=1, le=20)


class DocReindexRequest(StrictRequestModel):
    tenant_id: str
    session_id: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
    dry_run: bool = True


class DLQReplayRequest(StrictRequestModel):
    delete_after_replay: bool = True


def _required_dlq_idempotency_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=400,
            detail="valid_idempotency_key_required",
        )
    return normalized


def _admin_actor_audit_fields(request: Request) -> dict[str, object]:
    principal = getattr(request.state, "admin_principal", None)
    if not isinstance(principal, Principal):
        return {"actor_roles": [], "scope_type": "unknown"}
    if "*" in principal.tenant_ids:
        scope_type = "platform"
    elif principal.group_ids:
        scope_type = "group"
    else:
        scope_type = "tenant"
    return {
        "actor_roles": sorted({str(role) for role in principal.roles}),
        "scope_type": scope_type,
    }


def _required_admin_mutation_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=428,
            detail={"code": "idempotency_key_required"},
        )
    if len(normalized) > 128:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_idempotency_key"},
        )
    return normalized


def _admin_mutation_audit(
    request: Request,
    *,
    scope: dict[str, object],
    reason_code: str,
) -> MutationAudit:
    principal = getattr(request.state, "admin_principal", None)
    return MutationAudit(
        actor=str(getattr(principal, "subject", "") or "unknown")[:128],
        actor_kind=str(getattr(principal, "auth_kind", "") or "unknown")[:32],
        roles=tuple(str(role)[:64] for role in (getattr(principal, "roles", ()) or ())),
        scope=scope,
        reason_code=reason_code,
        trace_id=_runtime_llm_trace_id(request),
    )


async def _run_admin_mutation(
    *,
    identity: MutationIdentity,
    audit: MutationAudit,
    mutate: Callable[[], Awaitable[MutationChange]],
) -> MutationOutcome:
    async with get_engine().begin() as conn:
        return await run_idempotent_mutation(
            conn,
            identity=identity,
            audit=audit,
            mutate=mutate,
        )


def _set_admin_mutation_headers(response: Response, outcome: MutationOutcome) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Mutation-ID"] = outcome.mutation_id
    if outcome.replayed:
        response.headers["Idempotent-Replayed"] = "true"


def _raise_admin_mutation_error(exc: Exception) -> NoReturn:
    if isinstance(exc, MutationIdempotencyConflictError):
        raise HTTPException(
            status_code=409,
            detail={"code": "idempotency_key_conflict"},
        ) from exc
    if isinstance(exc, MutationLedgerError):
        raise HTTPException(
            status_code=503,
            detail={"code": "mutation_ledger_unavailable"},
        ) from exc
    raise exc


def _dlq_audit_state(
    request: Request,
    item: DLQMessage | None,
    *,
    operation: str,
    succeeded: bool,
    idempotent_replayed: bool = False,
    deleted: bool = False,
) -> dict[str, object]:
    return {
        **_admin_actor_audit_fields(request),
        "operation": operation,
        "entry_present": item is not None,
        "attempts": max(0, int(item.attempts)) if item is not None else 0,
        "origin_configured": bool(item and item.origin_stream),
        "succeeded": succeeded,
        "deleted": deleted,
        "idempotent_replayed": idempotent_replayed,
    }


def _plugin_lifecycle_audit_state(
    request: Request,
    state: dict[str, Any] | None,
    *,
    operation: str,
    succeeded: bool,
    idempotent_replayed: bool = False,
) -> dict[str, object]:
    safe_keys = (
        "plugin_name",
        "exists",
        "version",
        "source",
        "installed",
        "enabled",
        "system",
        "status",
        "restart_required",
        "runtime_active",
        "runtime_initialized",
    )
    safe_state = {key: state[key] for key in safe_keys if isinstance(state, dict) and key in state}
    return {
        **_admin_actor_audit_fields(request),
        **safe_state,
        "operation": operation,
        "succeeded": succeeded,
        "idempotent_replayed": idempotent_replayed,
    }


class RuntimeLlmConfigUpdate(StrictRequestModel):
    llm_provider: str | None = None
    openai_base_url: str | None = None
    openai_api_mode: str | None = None
    openai_web_search_enabled: bool | None = None
    openai_web_search_tool: str | None = None
    openai_web_search_live_enabled: bool | None = None
    llm_embed_provider: str | None = None
    knowledge_features_enabled: bool | None = None
    customer_service_prompt_enabled: bool | None = None
    llm_model_tier1: str | None = None
    llm_model_tier2: str | None = None
    llm_model_tier3: str | None = None
    llm_embed_model: str | None = None


class PluginInstallRequest(StrictRequestModel):
    name: str
    source: str | None = None
    version: str | None = None
    package_type: str | None = None
    uri: str | None = None
    checksum: str | None = None
    confirm_permissions: list[str] = Field(default_factory=list)
    confirm_restart_required: bool = False


class PluginUpgradePreviewRequest(StrictRequestModel):
    version: str | None = None
    confirm_permissions: list[str] = Field(default_factory=list)
    confirm_restart_required: bool = False


class PluginScopeStateRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(default="", max_length=256)
    enabled: bool
    config: dict[str, Any] = Field(default_factory=dict)


class FlowEffectProbeRequest(StrictRequestModel):
    owner: str = Field(default="memory", min_length=1, max_length=64)
    type: str = Field(default="save_memory", min_length=1, max_length=128)
    dry_run: bool = True
    repeat: int = Field(default=1, ge=1, le=3)
    tenant_id: str = Field(default="admin-probe", min_length=1, max_length=64)
    channel: Channel = Channel.WEB
    source_key: str = Field(default="admin_probe", min_length=1, max_length=128)
    user_id: str = Field(default="admin-probe-user", min_length=1, max_length=256)
    session_id: str = Field(default="admin-probe-session", min_length=1, max_length=256)
    user_text: str = Field(default="effect handler probe", min_length=1, max_length=500)
    assistant_text: str = Field(default="probe accepted", min_length=1, max_length=500)
    session_kind: str = Field(default="private", min_length=1, max_length=32)
    session_name: str = Field(default="admin-probe", min_length=1, max_length=256)
    sender_name: str = Field(default="admin-probe-user", min_length=1, max_length=256)
    sender_wxid: str = Field(default="admin-probe-user", min_length=1, max_length=256)
    reply_to_msg_svr_id: str = Field(default="admin-probe-message", min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


def _runtime_llm_etag(version: int) -> str:
    return f'"runtime-llm-config-{max(0, int(version))}"'


def _plugin_scope_etag(version: int) -> str:
    return f'"plugin-scope-{max(0, int(version))}"'


def _validated_plugin_scope_config(
    plugin_manager: PluginManager,
    plugin_registry: PluginRegistry | None,
    plugin_name: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Validate persisted scope config against the loaded plugin contract.

    Production ``PluginManager`` instances always expose their registry.  The
    bounded-payload-only branch keeps lightweight test/admin adapters usable,
    while still rejecting oversized or non-JSON values.
    """

    try:
        validate_config_payload_bounds(config)
    except PluginConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.as_detail()) from exc

    plugin: Any | None = None
    lookup = getattr(plugin_manager, "_plugin_or_404", None)
    if callable(lookup):
        plugin = lookup(plugin_name)
    else:
        registry = getattr(plugin_manager, "registry", None) or plugin_registry
        loaded_plugins = getattr(registry, "loaded_plugins", None)
        if isinstance(loaded_plugins, dict):
            plugin = loaded_plugins.get(plugin_name)
            if plugin is None:
                raise HTTPException(status_code=404, detail="plugin_not_found")

    if plugin is None:
        return config
    try:
        schema = plugin.get_config_schema()
        validate_plugin_config(config, schema)
    except PluginConfigSchemaError as exc:
        raise HTTPException(status_code=503, detail=exc.as_detail()) from exc
    except PluginConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.as_detail()) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "plugin_config_schema_unavailable",
                "path": "$",
                "message": "plugin config schema could not be loaded",
            },
        ) from exc
    return config


def _set_plugin_scope_headers(response: Response, version: int) -> None:
    response.headers["ETag"] = _plugin_scope_etag(version)
    response.headers["Cache-Control"] = "no-store"


def _required_plugin_scope_if_match(value: str | None) -> int:
    if value is None or not value.strip():
        raise HTTPException(status_code=428, detail="if_match_required")
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    if len(normalized) >= 2 and normalized[0] == '"' and normalized[-1] == '"':
        normalized = normalized[1:-1]
    prefix = "plugin-scope-"
    raw_version = normalized[len(prefix) :] if normalized.startswith(prefix) else ""
    if not raw_version.isdigit():
        raise HTTPException(status_code=400, detail="invalid_if_match")
    return int(raw_version)


def _set_runtime_llm_headers(response: Response, version: int) -> None:
    response.headers["ETag"] = _runtime_llm_etag(version)
    response.headers["Cache-Control"] = "no-store"


def _required_runtime_llm_if_match(value: str | None) -> int:
    if value is None or not value.strip():
        raise HTTPException(status_code=428, detail="if_match_required")
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    if len(normalized) >= 2 and normalized[0] == '"' and normalized[-1] == '"':
        normalized = normalized[1:-1]
    prefix = "runtime-llm-config-"
    raw_version = normalized[len(prefix) :] if normalized.startswith(prefix) else ""
    if not raw_version.isdigit():
        raise HTTPException(status_code=400, detail="invalid_if_match")
    return int(raw_version)


def _required_runtime_llm_idempotency_key(value: str | None) -> str:
    try:
        return normalize_runtime_llm_idempotency_key(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _runtime_llm_payload(
    resolved: ResolvedRuntimeLlmConfig,
    *,
    restart_required: bool = False,
) -> dict[str, Any]:
    settings = resolved.settings
    return {
        "loaded": True,
        "version": resolved.snapshot.version,
        "llm_provider": settings.llm_provider,
        "openai_base_url": settings.openai_base_url,
        "openai_api_mode": settings.openai_api_mode,
        "openai_web_search_enabled": settings.openai_web_search_enabled,
        "openai_web_search_tool": settings.openai_web_search_tool,
        "openai_web_search_live_enabled": settings.openai_web_search_live_enabled,
        "llm_embed_provider": settings.llm_embed_provider,
        "knowledge_features_enabled": settings.knowledge_features_enabled,
        "customer_service_prompt_enabled": settings.customer_service_prompt_enabled,
        "llm_model_tier1": settings.llm_model_tier1,
        "llm_model_tier2": settings.llm_model_tier2,
        "llm_model_tier3": settings.llm_model_tier3,
        "llm_embed_model": settings.llm_embed_model,
        "field_sources": resolved.field_sources,
        "secret_provider_status": {
            "openai_api_key": runtime_llm_secret_status(settings),
        },
        "validation_errors": _safe_runtime_llm_validation_errors(settings),
        "restart_required": restart_required,
        "apply_status": (
            "restart_required_or_unverified" if restart_required else "no_persisted_change"
        ),
        "affected_roles": ["api", "inbound", "scheduler"],
        "updated_at": (
            resolved.snapshot.updated_at.isoformat()
            if resolved.snapshot.updated_at is not None
            else None
        ),
    }


def _runtime_llm_conflict(expected: int, current: int) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "version_conflict",
            "expected_version": expected,
            "current_version": current,
        },
        headers={
            "ETag": _runtime_llm_etag(current),
            "Cache-Control": "no-store",
        },
    )


def _runtime_llm_idempotency_conflict(current: int) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "idempotency_conflict"},
        headers={
            "ETag": _runtime_llm_etag(current),
            "Cache-Control": "no-store",
        },
    )


def _safe_runtime_llm_validation_errors(settings: Settings) -> list[str]:
    errors: list[str] = []
    for error in validate_llm_settings(settings):
        if error.startswith("unsupported LLM_PROVIDER="):
            errors.append("unsupported LLM_PROVIDER")
        elif error.startswith("unsupported LLM_EMBED_PROVIDER="):
            errors.append("unsupported LLM_EMBED_PROVIDER")
        else:
            errors.append(error)
    return errors


def _runtime_llm_audit_summary(
    resolved: ResolvedRuntimeLlmConfig,
    *,
    changed_fields: list[str] | None = None,
) -> dict[str, object]:
    settings = resolved.settings
    return {
        "version": resolved.snapshot.version,
        "override_fields": sorted(resolved.snapshot.overrides),
        "changed_fields": sorted(changed_fields or []),
        "knowledge_features_enabled": settings.knowledge_features_enabled,
        "customer_service_prompt_enabled": settings.customer_service_prompt_enabled,
        "openai_web_search_enabled": settings.openai_web_search_enabled,
    }


def _runtime_llm_trace_id(request: Request) -> str:
    return str(
        request.headers.get("X-Trace-ID")
        or request.headers.get("X-Request-ID")
        or getattr(request.state, "admin_request_id", "")
    ).strip()[:128]


async def _execute_plugin_lifecycle_route(
    plugin_manager: PluginManager,
    *,
    operation: str,
    plugin_name: str,
    body: dict[str, Any],
    request: Request,
    response: Response,
    idempotency_key: str | None,
) -> dict[str, Any]:
    initial_state = _plugin_lifecycle_audit_state(
        request,
        None,
        operation=operation,
        succeeded=False,
    )
    set_admin_audit_context(
        request,
        target_type="plugin_lifecycle",
        before_state=initial_state,
        after_state=initial_state,
        trace_id=_runtime_llm_trace_id(request),
        reason=f"plugin_{operation}",
    )
    result = await plugin_manager.execute_lifecycle(
        operation,
        plugin_name,
        body,
        request,
        idempotency_key=_required_dlq_idempotency_key(idempotency_key),
    )
    if result.idempotent_replayed:
        response.headers["Idempotent-Replayed"] = "true"
    set_admin_audit_context(
        request,
        target_type="plugin_lifecycle",
        before_state=_plugin_lifecycle_audit_state(
            request,
            result.before_state,
            operation=operation,
            succeeded=False,
        ),
        after_state=_plugin_lifecycle_audit_state(
            request,
            result.after_state,
            operation=operation,
            succeeded=True,
            idempotent_replayed=result.idempotent_replayed,
        ),
        policy_version=result.policy_version,
        trace_id=_runtime_llm_trace_id(request),
        reason=f"plugin_{operation}",
    )
    return result.response


def build_admin_router(
    faq_store: FAQStore | None,
    kb_service: KnowledgeBaseService | None,
    settings: Settings | None = None,
    dlq_service: DLQAdminService | None = None,
    stream_service: StreamAdminService | None = None,
    plugin_registry: PluginRegistry | None = None,
    faq_engine: Any | None = None,
    plugin_manager: PluginManager | None = None,
    flow_step_registry: FlowStepRegistry | None = None,
    orchestrator: Any | None = None,
    effect_handler_registry: Any | None = None,
    effect_log_store: PostgresEffectLog | None = None,
    media_event_providers: list[Any] | None = None,
    runtime_llm_config_store: RuntimeLlmConfigStore | None = None,
    channel_connection_store: Any | None = None,
) -> APIRouter:
    s = settings or get_settings()
    runtime_llm_store = runtime_llm_config_store or RuntimeLlmConfigStore()
    router = APIRouter(
        prefix="/v1/admin",
        tags=["admin"],
        dependencies=[Depends(build_admin_authorization_dependency(s))],
    )

    @router.get("/tenants/{tenant_id}/capabilities")
    async def get_tenant_capabilities(tenant_id: str, request: Request) -> dict[str, Any]:
        normalized_tenant = tenant_id.strip()
        if not normalized_tenant:
            raise HTTPException(status_code=400, detail="tenant_id_required")
        principal = getattr(request.state, "admin_principal", None)
        if not isinstance(principal, Principal):
            raise HTTPException(status_code=403, detail="admin_principal_required")
        if not tenant_scope_allowed(principal, normalized_tenant):
            raise HTTPException(status_code=403, detail="tenant_scope_forbidden")
        return await build_tenant_capabilities(
            tenant_id=normalized_tenant,
            principal=principal,
            settings=s,
            plugin_registry=plugin_registry,
            plugin_manager=plugin_manager,
            faq_store=faq_store,
            kb_service=kb_service,
            dlq_service=dlq_service,
            stream_service=stream_service,
            orchestrator=orchestrator,
            connection_store=channel_connection_store,
        )

    @router.get("/runtime/llm-config")
    async def get_runtime_llm_config(response: Response) -> dict[str, Any]:
        try:
            resolved = await load_runtime_llm_config(s, store=runtime_llm_store)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime_llm_config_unavailable",
                headers={"Cache-Control": "no-store"},
            ) from exc
        _set_runtime_llm_headers(response, resolved.snapshot.version)
        # The API process cannot attest that every inbound/scheduler replica
        # has restarted on this version.  Once a durable override exists, stay
        # conservative rather than claiming a cluster-wide hot application.
        return _runtime_llm_payload(
            resolved,
            restart_required=resolved.snapshot.version > 0,
        )

    @router.post("/runtime/restart-instructions")
    async def get_restart_instructions() -> dict[str, Any]:
        if plugin_manager is None:
            return {
                "actionable": False,
                "restart_required": False,
                "message": "Restart the FastAPI process or container through the deployment system.",
            }
        return await plugin_manager.restart_instructions()

    @router.post("/runtime/llm-config")
    async def set_runtime_llm_config(
        request: Request,
        response: Response,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ) -> dict[str, Any]:
        expected_version = _required_runtime_llm_if_match(if_match)
        operation_key = _required_runtime_llm_idempotency_key(idempotency_key)
        try:
            raw_body = await request.json()
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid_runtime_llm_config",
            ) from exc
        if not isinstance(raw_body, dict):
            raise HTTPException(status_code=422, detail="invalid_runtime_llm_config")
        if {"openai_api_key", "clear_openai_api_key"} & set(raw_body):
            raise HTTPException(status_code=400, detail="secret_fields_not_mutable")
        try:
            body = RuntimeLlmConfigUpdate.model_validate(raw_body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail="invalid_runtime_llm_config",
            ) from exc
        update_payload = {
            key: value
            for key, value in body.model_dump(exclude_unset=True).items()
            if value is not None
        }
        if not update_payload:
            raise HTTPException(status_code=400, detail="no_mutable_fields")
        try:
            updates = normalize_runtime_llm_overrides(update_payload)
            request_hash = runtime_llm_request_hash(
                expected_version=expected_version,
                updates=updates,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        changed_fields = sorted(set(updates) & RUNTIME_LLM_MUTABLE_FIELDS)

        def finalize_mutation(mutation: RuntimeLlmConfigMutation) -> dict[str, Any]:
            before_resolved = resolve_runtime_llm_config(s, mutation.before)
            after_resolved = resolve_runtime_llm_config(s, mutation.after)
            _set_runtime_llm_headers(response, mutation.after.version)
            set_admin_audit_context(
                request,
                target_type="runtime_llm_config",
                before_state=_runtime_llm_audit_summary(before_resolved),
                after_state=_runtime_llm_audit_summary(
                    after_resolved,
                    changed_fields=[] if mutation.replayed else changed_fields,
                ),
                policy_version=mutation.after.version,
                trace_id=_runtime_llm_trace_id(request),
                reason=(
                    "runtime_llm_config_idempotent_replay"
                    if mutation.replayed
                    else "conditional_runtime_llm_config_update"
                ),
            )
            return _runtime_llm_payload(after_resolved, restart_required=True)

        async def raise_idempotency_conflict() -> NoReturn:
            try:
                current = await runtime_llm_store.get()
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="runtime_llm_config_unavailable",
                    headers={"Cache-Control": "no-store"},
                ) from exc
            raise _runtime_llm_idempotency_conflict(current.version)

        try:
            replay = await runtime_llm_store.replay_idempotent_result(
                idempotency_key=operation_key,
                request_hash=request_hash,
            )
        except RuntimeLlmConfigIdempotencyConflict:
            await raise_idempotency_conflict()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime_llm_config_unavailable",
                headers={"Cache-Control": "no-store"},
            ) from exc
        if replay is not None:
            return finalize_mutation(
                RuntimeLlmConfigMutation(
                    before=replay,
                    after=replay,
                    replayed=True,
                )
            )

        try:
            before = await load_runtime_llm_config(s, store=runtime_llm_store)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime_llm_config_unavailable",
                headers={"Cache-Control": "no-store"},
            ) from exc

        if before.snapshot.version != expected_version:
            try:
                mutation = await runtime_llm_store.compare_and_swap_idempotent(
                    expected_version=expected_version,
                    overrides={},
                    idempotency_key=operation_key,
                    request_hash=request_hash,
                )
            except RuntimeLlmConfigIdempotencyConflict:
                await raise_idempotency_conflict()
            except RuntimeLlmConfigVersionConflict as exc:
                raise _runtime_llm_conflict(exc.expected, exc.current) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="runtime_llm_config_unavailable",
                    headers={"Cache-Control": "no-store"},
                ) from exc
            return finalize_mutation(mutation)

        externally_managed = sorted(set(updates) & externally_managed_runtime_llm_fields(s))
        if externally_managed:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "externally_managed_fields",
                    "fields": externally_managed,
                },
                headers={
                    "ETag": _runtime_llm_etag(before.snapshot.version),
                    "Cache-Control": "no-store",
                },
            )

        next_overrides = {**before.snapshot.overrides, **updates}
        candidate = resolve_runtime_llm_config(
            s,
            RuntimeLlmConfigSnapshot(
                version=before.snapshot.version,
                overrides=next_overrides,
                updated_at=before.snapshot.updated_at,
            ),
        )
        validation_errors = _safe_runtime_llm_validation_errors(candidate.settings)
        if validation_errors:
            raise HTTPException(status_code=400, detail="; ".join(validation_errors))

        try:
            mutation = await runtime_llm_store.compare_and_swap_idempotent(
                expected_version=expected_version,
                overrides=next_overrides,
                idempotency_key=operation_key,
                request_hash=request_hash,
            )
        except RuntimeLlmConfigIdempotencyConflict:
            await raise_idempotency_conflict()
        except RuntimeLlmConfigVersionConflict as exc:
            raise _runtime_llm_conflict(exc.expected, exc.current) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="runtime_llm_config_unavailable",
                headers={"Cache-Control": "no-store"},
            ) from exc
        return finalize_mutation(mutation)

    # -------------- FAQ ----------------------------------------------------

    if faq_store is not None:

        @router.post("/faqs")
        async def create_faq(body: FAQCreate) -> dict[str, Any]:
            rec = await faq_store.create(
                tenant_id=body.tenant_id,
                session_id=body.session_id,
                question=body.question,
                answer=body.answer,
                variants=body.variants,
                tags=body.tags,
            )
            return _faq_to_dict(rec)

        @router.get("/faqs")
        async def list_faqs(
            tenant_id: str, session_id: str | None = None, limit: int = 100, offset: int = 0
        ) -> dict[str, Any]:
            rows = await faq_store.list(tenant_id, session_id, limit=limit, offset=offset)
            return {
                **scope_payload(tenant_id, session_id),
                "items": [_faq_to_dict(r) for r in rows],
            }

        @router.put("/faqs/{faq_id}")
        async def update_faq(
            faq_id: int, tenant_id: str, body: FAQUpdate, session_id: str | None = None
        ) -> dict[str, Any]:
            rec = await faq_store.update(
                tenant_id=tenant_id,
                session_id=session_id,
                faq_id=faq_id,
                question=body.question,
                answer=body.answer,
                variants=body.variants,
                tags=body.tags,
                status=body.status,
            )
            if rec is None:
                raise HTTPException(status_code=404, detail="faq_not_found")
            return _faq_to_dict(rec)

        @router.delete("/faqs/{faq_id}")
        async def delete_faq(
            faq_id: int,
            tenant_id: str,
            request: Request,
            response: Response,
            session_id: str | None = None,
            idempotency_key: Annotated[
                str | None,
                Header(alias="Idempotency-Key"),
            ] = None,
        ) -> dict[str, Any]:
            operation_key = _required_admin_mutation_key(idempotency_key)

            async def mutate() -> MutationChange:
                existing = next(
                    (
                        item
                        for item in await faq_store.list(
                            tenant_id,
                            session_id=session_id,
                            limit=10_000,
                        )
                        if int(item.id) == faq_id
                    ),
                    None,
                )
                if existing is None:
                    return MutationChange(
                        response={
                            **scope_payload(tenant_id, session_id),
                            "deleted": False,
                        },
                        before_state={
                            "exists": False,
                            "faq_hash": hash_identifier(str(faq_id)),
                            "version": 0,
                            "published": False,
                        },
                        after_state={
                            "exists": False,
                            "faq_hash": hash_identifier(str(faq_id)),
                        },
                    )
                ok = await faq_store.delete(
                    tenant_id,
                    faq_id,
                    session_id=session_id,
                )
                result = {
                    **scope_payload(tenant_id, session_id),
                    "deleted": faq_id if ok else False,
                }
                return MutationChange(
                    response=result,
                    before_state={
                        "exists": True,
                        "faq_hash": hash_identifier(str(faq_id)),
                        "version": int(existing.version or 0),
                        "published": str(existing.status or "published") == "published",
                    },
                    after_state={
                        "exists": False,
                        "faq_hash": hash_identifier(str(faq_id)),
                    },
                    resource_version=str(existing.version or 0),
                )

            try:
                outcome = await _run_admin_mutation(
                    identity=MutationIdentity(
                        tenant_id=tenant_id,
                        plugin_name="faq",
                        operation="faq.delete",
                        resource_key=f"{session_id or ''}:{faq_id}",
                        idempotency_key=operation_key,
                        request_payload={
                            "tenant_id": tenant_id,
                            "session_id": normalize_scope_session_id(session_id),
                            "faq_id": faq_id,
                        },
                    ),
                    audit=_admin_mutation_audit(
                        request,
                        scope={
                            "faq_hash": hash_identifier(str(faq_id)),
                            "session_hash": hash_identifier(normalize_scope_session_id(session_id)),
                        },
                        reason_code="faq_delete",
                    ),
                    mutate=mutate,
                )
            except Exception as exc:
                _raise_admin_mutation_error(exc)
            _set_admin_mutation_headers(response, outcome)
            resource_existed = dict(outcome.response).get("deleted") is not False
            set_admin_audit_context(
                request,
                target_type="faq",
                tenant_id=tenant_id,
                session_id=normalize_scope_session_id(session_id),
                before_state={"exists": resource_existed, "faq_id": faq_id},
                after_state={"exists": False, "faq_id": faq_id},
                trace_id=_runtime_llm_trace_id(request),
                reason="faq_delete_replay" if outcome.replayed else "faq_delete",
            )
            return dict(outcome.response)

        @router.post("/faqs/test")
        async def test_faq_hit(body: FAQTestRequest) -> dict[str, Any]:
            if faq_engine is None:
                raise HTTPException(status_code=503, detail="faq_engine_unavailable")
            query = str(body.query or "").strip()
            if not query:
                raise HTTPException(status_code=400, detail="query cannot be empty")

            scoped_session_id = normalize_scope_session_id(body.session_id)
            session = Session(
                session_id=scoped_session_id or "global",
                tenant_id=body.tenant_id,
                user_id="admin_preview",
                channel=Channel.WEB,
                metadata={"source": "admin_faq_test"},
            )
            pre = PreprocessedMessage(original_text=query, cleaned_text=query)

            try:
                result = await faq_engine.answer(
                    pre,
                    session,
                    {"trace_id": "admin_faq_test"},
                )
            except CapabilityError as exc:
                message = str(exc)
                if message == "no_faq_hit" or (
                    message.startswith("faq_search_failed:")
                    and ("doesn't exist" in message or "Not found: Collection" in message)
                ):
                    return {
                        **scope_payload(body.tenant_id, body.session_id),
                        "matched": False,
                        "query": query,
                        "reply_text": "",
                        "score": None,
                        "threshold": None,
                        "resolved_scope": None,
                        "resolved_session_id": None,
                        "rewritten": False,
                        "citation": None,
                    }
                raise HTTPException(status_code=400, detail=message) from exc

            citation = result.citations[0] if result.citations else None
            metadata = dict(result.metadata or {})
            return {
                **scope_payload(body.tenant_id, body.session_id),
                "matched": True,
                "query": query,
                "reply_text": result.reply_text,
                "score": metadata.get("score"),
                "threshold": metadata.get("threshold"),
                "resolved_scope": metadata.get("scope"),
                "resolved_session_id": metadata.get("scope_session_id"),
                "rewritten": bool(metadata.get("rewritten")),
                "citation": (
                    {
                        "id": citation.id,
                        "source": citation.source,
                        "snippet": citation.snippet,
                        "score": citation.score,
                    }
                    if citation is not None
                    else None
                ),
            }

    # -------------- KB -----------------------------------------------------

    if kb_service is not None:

        @router.post("/kb/documents")
        async def add_document(body: DocCreate) -> dict[str, Any]:
            doc_id = await kb_service.add_document(
                tenant_id=body.tenant_id,
                session_id=body.session_id,
                title=body.title,
                content=body.content,
                source=body.source,
                url=body.url,
                metadata=body.metadata,
            )
            return {**scope_payload(body.tenant_id, body.session_id), "doc_id": doc_id}

        @router.get("/kb/documents")
        async def list_documents(
            tenant_id: str, session_id: str | None = None, limit: int = 100, offset: int = 0
        ) -> dict[str, Any]:
            docs = await kb_service.list_documents(
                tenant_id, session_id=session_id, limit=limit, offset=offset
            )
            return {
                **scope_payload(tenant_id, session_id),
                "items": [_doc_to_dict(d, include_content=False) for d in docs],
            }

        @router.get("/kb/documents/{doc_id}")
        async def get_document(
            doc_id: int, tenant_id: str, session_id: str | None = None
        ) -> dict[str, Any]:
            doc = await kb_service.get_document(tenant_id, doc_id, session_id=session_id)
            if doc is None:
                raise HTTPException(status_code=404, detail="document_not_found")
            return {
                **scope_payload(tenant_id, session_id),
                **_doc_to_dict(doc, include_content=True),
            }

        @router.put("/kb/documents/{doc_id}")
        async def update_document(
            doc_id: int, tenant_id: str, body: DocUpdate, session_id: str | None = None
        ) -> dict[str, Any]:
            updated_id = await kb_service.update_document(
                tenant_id=tenant_id,
                session_id=session_id,
                doc_id=doc_id,
                title=body.title,
                content=body.content,
                source=body.source,
                url=body.url,
                metadata=body.metadata,
            )
            if updated_id is None:
                raise HTTPException(status_code=404, detail="document_not_found")
            return {
                **scope_payload(tenant_id, session_id),
                "doc_id": updated_id,
                "updated": updated_id,
            }

        @router.post("/kb/documents/search")
        async def search_documents(body: DocSearchRequest) -> dict[str, Any]:
            query = str(body.query or "").strip()
            if not query:
                raise HTTPException(status_code=400, detail="query cannot be empty")
            hits = await kb_service.search_documents(
                body.tenant_id,
                query,
                session_id=body.session_id,
                top_k=body.top_k,
            )
            return {
                **scope_payload(body.tenant_id, body.session_id),
                "query": query,
                "items": [
                    {
                        "chunk_id": hit.chunk_id,
                        "doc_id": hit.doc_id,
                        "title": hit.title,
                        "content": hit.content,
                        "score": hit.score,
                        "session_id": hit.session_id,
                        "source": hit.source,
                        "url": hit.url,
                        "metadata": hit.metadata,
                    }
                    for hit in hits
                ],
            }

        @router.post("/kb/reindex")
        async def reindex_documents(body: DocReindexRequest) -> dict[str, Any]:
            return await kb_service.reindex_documents(
                body.tenant_id,
                session_id=body.session_id,
                limit=body.limit,
                offset=body.offset,
                dry_run=body.dry_run,
            )

        @router.delete("/kb/documents/{doc_id}")
        async def delete_document(
            doc_id: int,
            tenant_id: str,
            request: Request,
            response: Response,
            session_id: str | None = None,
            idempotency_key: Annotated[
                str | None,
                Header(alias="Idempotency-Key"),
            ] = None,
        ) -> dict[str, Any]:
            operation_key = _required_admin_mutation_key(idempotency_key)

            async def mutate() -> MutationChange:
                existing = await kb_service.get_document(
                    tenant_id,
                    doc_id,
                    session_id=session_id,
                )
                if existing is None:
                    return MutationChange(
                        response={
                            **scope_payload(tenant_id, session_id),
                            "deleted": False,
                        },
                        before_state={
                            "exists": False,
                            "document_hash": hash_identifier(str(doc_id)),
                            "source_configured": False,
                            "url_configured": False,
                        },
                        after_state={
                            "exists": False,
                            "document_hash": hash_identifier(str(doc_id)),
                        },
                    )
                await kb_service.delete_document(
                    tenant_id,
                    doc_id,
                    session_id=session_id,
                )
                result = {
                    **scope_payload(tenant_id, session_id),
                    "deleted": doc_id,
                }
                return MutationChange(
                    response=result,
                    before_state={
                        "exists": True,
                        "document_hash": hash_identifier(str(doc_id)),
                        "source_configured": bool(str(existing.source or "").strip()),
                        "url_configured": bool(str(existing.url or "").strip()),
                    },
                    after_state={
                        "exists": False,
                        "document_hash": hash_identifier(str(doc_id)),
                    },
                    resource_version=str(doc_id),
                )

            try:
                outcome = await _run_admin_mutation(
                    identity=MutationIdentity(
                        tenant_id=tenant_id,
                        plugin_name="knowledge_base",
                        operation="knowledge.document.delete",
                        resource_key=f"{session_id or ''}:{doc_id}",
                        idempotency_key=operation_key,
                        request_payload={
                            "tenant_id": tenant_id,
                            "session_id": normalize_scope_session_id(session_id),
                            "document_id": doc_id,
                        },
                    ),
                    audit=_admin_mutation_audit(
                        request,
                        scope={
                            "document_hash": hash_identifier(str(doc_id)),
                            "session_hash": hash_identifier(normalize_scope_session_id(session_id)),
                        },
                        reason_code="knowledge_document_delete",
                    ),
                    mutate=mutate,
                )
            except Exception as exc:
                _raise_admin_mutation_error(exc)
            _set_admin_mutation_headers(response, outcome)
            resource_existed = dict(outcome.response).get("deleted") is not False
            set_admin_audit_context(
                request,
                target_type="knowledge_document",
                tenant_id=tenant_id,
                session_id=normalize_scope_session_id(session_id),
                before_state={"exists": resource_existed, "document_id": doc_id},
                after_state={"exists": False, "document_id": doc_id},
                trace_id=_runtime_llm_trace_id(request),
                reason=(
                    "knowledge_document_delete_replay"
                    if outcome.replayed
                    else "knowledge_document_delete"
                ),
            )
            return dict(outcome.response)

    # -------------- DLQ ----------------------------------------------------

    if dlq_service is not None:

        @router.get("/dlq/messages")
        async def list_dlq_messages(
            tenant_id: str | None = None,
            limit: int = 100,
            before_id: str | None = None,
        ) -> dict[str, Any]:
            items, next_before_id = await dlq_service.list_messages(
                tenant_id=tenant_id,
                limit=limit,
                before_id=before_id,
            )
            return {
                "items": [_dlq_to_dict(item) for item in items],
                "next_before_id": next_before_id,
            }

        @router.get("/dlq/messages/{entry_id}")
        async def get_dlq_message(entry_id: str) -> dict[str, Any]:
            item = await dlq_service.get_message(entry_id)
            if item is None:
                raise HTTPException(status_code=404, detail="dlq_message_not_found")
            return _dlq_to_dict(item)

        @router.post("/dlq/messages/{entry_id}/replay")
        async def replay_dlq_message(
            entry_id: str,
            request: Request,
            response: Response,
            body: DLQReplayRequest | None = None,
            idempotency_key: Annotated[
                str | None,
                Header(alias="Idempotency-Key", max_length=128),
            ] = None,
        ) -> dict[str, Any]:
            req = body or DLQReplayRequest()
            before_item = await dlq_service.get_message(entry_id)
            set_admin_audit_context(
                request,
                target_type="dlq_message",
                tenant_id=str(before_item.tenant_id or "") if before_item is not None else "",
                before_state=_dlq_audit_state(
                    request,
                    before_item,
                    operation="replay",
                    succeeded=False,
                ),
                after_state=_dlq_audit_state(
                    request,
                    before_item,
                    operation="replay",
                    succeeded=False,
                ),
                trace_id=_runtime_llm_trace_id(request),
                reason="dlq_replay",
            )
            try:
                replay = await dlq_service.replay_message(
                    entry_id,
                    idempotency_key=_required_dlq_idempotency_key(idempotency_key),
                    delete_after_replay=req.delete_after_replay,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="dlq_message_not_found") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            if replay.idempotent_replayed:
                response.headers["Idempotent-Replayed"] = "true"

            set_admin_audit_context(
                request,
                target_type="dlq_message",
                tenant_id=(
                    str(before_item.tenant_id or "")
                    if before_item is not None
                    else replay.tenant_id
                ),
                before_state=_dlq_audit_state(
                    request,
                    before_item,
                    operation="replay",
                    succeeded=False,
                ),
                after_state=_dlq_audit_state(
                    request,
                    before_item,
                    operation="replay",
                    succeeded=True,
                    idempotent_replayed=replay.idempotent_replayed,
                    deleted=replay.deleted,
                ),
                policy_version=1,
                trace_id=_runtime_llm_trace_id(request),
                reason="dlq_replay",
            )

            return {
                "replayed": replay.entry_id,
                "replayed_to_stream": replay.origin_stream,
                "replayed_message_id": replay.replayed_message_id,
                "deleted": replay.deleted,
            }

        @router.delete("/dlq/messages/{entry_id}")
        async def delete_dlq_message(
            entry_id: str,
            request: Request,
            response: Response,
            idempotency_key: Annotated[
                str | None,
                Header(alias="Idempotency-Key", max_length=128),
            ] = None,
        ) -> dict[str, Any]:
            before_item = await dlq_service.get_message(entry_id)
            before_audit = _dlq_audit_state(
                request,
                before_item,
                operation="delete",
                succeeded=False,
            )
            set_admin_audit_context(
                request,
                target_type="dlq_message",
                tenant_id=str(before_item.tenant_id or "") if before_item is not None else "",
                before_state=before_audit,
                after_state=before_audit,
                trace_id=_runtime_llm_trace_id(request),
                reason="dlq_delete",
            )
            try:
                deleted = await dlq_service.delete_message(
                    entry_id,
                    idempotency_key=_required_dlq_idempotency_key(idempotency_key),
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="dlq_message_not_found") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if deleted.idempotent_replayed:
                response.headers["Idempotent-Replayed"] = "true"
            set_admin_audit_context(
                request,
                target_type="dlq_message",
                tenant_id=(
                    str(before_item.tenant_id or "")
                    if before_item is not None
                    else deleted.tenant_id
                ),
                before_state=before_audit,
                after_state=_dlq_audit_state(
                    request,
                    before_item,
                    operation="delete",
                    succeeded=True,
                    idempotent_replayed=deleted.idempotent_replayed,
                    deleted=deleted.deleted,
                ),
                policy_version=1,
                trace_id=_runtime_llm_trace_id(request),
                reason="dlq_delete",
            )
            return {"deleted": deleted.entry_id}

    # -------------- Plugins / channels ------------------------------------

    @router.get("/plugins/summary")
    async def get_plugins_summary() -> dict[str, Any]:
        plugins = plugin_registry.summary if plugin_registry is not None else []
        hooks = plugin_registry.hook_runner.summary if plugin_registry is not None else {}
        hook_owners = (
            plugin_registry.hook_runner.owner_summary() if plugin_registry is not None else {}
        )
        plugin_routes = []
        channel_adapters: list[dict[str, Any]] = []
        if plugin_registry is not None:
            for name, _router in plugin_registry.all_api_routers():
                plugin_routes.append(f"/plugins/{name}")
            registrations_provider = getattr(
                plugin_registry,
                "all_channel_adapters",
                None,
            )
            if callable(registrations_provider):
                for registration in registrations_provider():
                    descriptor = getattr(registration, "descriptor", None)
                    if descriptor is None:
                        continue
                    dump = getattr(descriptor, "model_dump", None)
                    if callable(dump):
                        item = dump(mode="json")
                    else:
                        item = {
                            "adapter_id": str(getattr(descriptor, "adapter_id", "") or ""),
                            "display_name": str(getattr(descriptor, "display_name", "") or ""),
                            "channel": str(getattr(descriptor, "channel", "") or ""),
                        }
                    if str(item.get("adapter_id") or "").strip():
                        channel_adapters.append(item)
        channel_adapters.sort(key=lambda item: str(item.get("adapter_id") or ""))
        channel_labels = {
            str(item.get("channel") or ""): str(
                item.get("display_name") or item.get("channel") or ""
            )
            for item in channel_adapters
            if str(item.get("channel") or "").strip()
        }
        return {
            "plugins": plugins,
            "plugin_routes": plugin_routes,
            "hooks": hooks,
            "hook_owners": hook_owners,
            "channels": sorted(channel_labels),
            "channel_labels": channel_labels,
            "channel_adapters": channel_adapters,
        }

    @router.get("/message-flows")
    async def get_message_flows() -> dict[str, Any]:
        flow_registry, plugin_flow_steps, owner_permissions = _admin_flow_context(
            flow_step_registry=flow_step_registry,
            plugin_registry=plugin_registry,
        )
        return {
            "items": [
                _compiled_flow_to_dict(
                    flow,
                    description=profile.description,
                    bindings=profile.bindings,
                )
                for profile, flow in compile_builtin_flows(
                    flow_registry,
                    owner_permissions=owner_permissions,
                )
            ],
            "step_registry": [
                _flow_step_definition_to_dict(definition)
                for definition in flow_registry.list_definitions()
            ],
            "plugin_step_count": len(plugin_flow_steps),
        }

    @router.get("/message-flows/resolve")
    async def resolve_message_flow(
        tenant_id: str = "",
        channel: str = "*",
        session_kind: str = "",
        session_id: str = "",
        message_type: str = "*",
    ) -> dict[str, Any]:
        normalized_session_kind = session_kind.strip().lower() or normalize_flow_session_kind(
            channel=channel,
            session_id=session_id,
        )
        result = resolve_builtin_flow(
            FlowResolveRequest(
                tenant_id=tenant_id,
                channel=channel,
                session_kind=normalized_session_kind,
                session_id=session_id,
                message_type=message_type,
            )
        )
        return _flow_resolve_result_to_dict(result)

    @router.get("/message-flows/runtime")
    async def get_message_flow_runtime() -> dict[str, Any]:
        return {
            "runtime": _flow_runtime_config_to_dict(s),
            "shadow": _flow_shadow_config_to_dict(s),
            "effect_commit": _flow_effect_commit_config_to_dict(s),
            "trace_snapshot": _flow_trace_snapshot_config_to_dict(s),
            "effect_handlers": effect_handler_registry_payload(effect_handler_registry),
            "last_runtime_result": _flow_run_result_to_dict(orchestrator.last_flow_runtime_result)
            if orchestrator is not None
            and getattr(orchestrator, "last_flow_runtime_result", None) is not None
            else None,
            "last_shadow_result": _flow_run_result_to_dict(orchestrator.last_flow_shadow_result)
            if orchestrator is not None
            and getattr(orchestrator, "last_flow_shadow_result", None) is not None
            else None,
        }

    @router.get("/message-flows/traces/{trace_id}")
    async def get_message_flow_trace(trace_id: str) -> dict[str, Any]:
        normalized_trace_id = trace_id.strip()
        if not normalized_trace_id:
            raise HTTPException(status_code=400, detail="trace_id_required")
        config = _flow_trace_snapshot_config_to_dict(s)
        if not config["enabled"]:
            return {
                "trace_id": normalized_trace_id,
                "enabled": False,
                "backend": "redis",
                "runtime": None,
                "shadow": None,
            }
        try:
            from app.infra.redis_client import get_redis

            snapshots = await read_flow_trace_snapshots(
                get_redis(),
                normalized_trace_id,
                key_prefix=str(config["key_prefix"] or "cs:flow:trace"),
            )
        except Exception as exc:
            return {
                "trace_id": normalized_trace_id,
                "enabled": True,
                "backend": "redis",
                "error": str(exc),
                "runtime": None,
                "shadow": None,
            }
        return {
            "trace_id": normalized_trace_id,
            "enabled": True,
            "backend": "redis",
            "ttl_seconds": config["ttl_seconds"],
            "runtime": snapshots.get("runtime"),
            "shadow": snapshots.get("shadow"),
        }

    @router.get("/message-flows/effects")
    async def list_message_flow_effects(
        limit: int = 50,
        tenant_id: str = "",
        session_id: str = "",
        trace_id: str = "",
        owner: str = "",
        type: str = "",
        status: str = "",
        dry_run: bool | None = None,
        include_payload: bool = False,
    ) -> dict[str, Any]:
        log_backend = str(s.orchestrator_flow_effect_log_backend or "none").strip().lower()
        if log_backend not in {"postgres", "postgresql", "sql"}:
            return {
                "enabled": False,
                "backend": log_backend,
                "items": [],
            }

        store = effect_log_store or PostgresEffectLog()
        try:
            items = await store.list_recent(
                limit=limit,
                tenant_id=tenant_id,
                session_id=session_id,
                trace_id=trace_id,
                owner=owner,
                type=type,
                status=status,
                dry_run=dry_run,
                include_payload=include_payload,
            )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise HTTPException(
                status_code=503,
                detail=f"flow_effect_log_unavailable:{detail}",
            ) from exc
        return {
            "enabled": True,
            "backend": log_backend,
            "items": items,
        }

    @router.get("/message-flows/effects/summary")
    async def summarize_message_flow_effects(
        tenant_id: str = "",
        session_id: str = "",
        trace_id: str = "",
        owner: str = "",
        type: str = "",
        status: str = "",
        dry_run: bool | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        log_backend = str(s.orchestrator_flow_effect_log_backend or "none").strip().lower()
        if log_backend not in {"postgres", "postgresql", "sql"}:
            return {
                "enabled": False,
                "backend": log_backend,
                "summary": {
                    "total": 0,
                    "by_status": [],
                    "by_owner": [],
                    "by_type": [],
                    "by_dry_run": [],
                    "matrix": [],
                },
            }

        store = effect_log_store or PostgresEffectLog()
        try:
            summary = await store.summarize(
                tenant_id=tenant_id,
                session_id=session_id,
                trace_id=trace_id,
                owner=owner,
                type=type,
                status=status,
                dry_run=dry_run,
                limit=limit,
            )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise HTTPException(
                status_code=503,
                detail=f"flow_effect_log_unavailable:{detail}",
            ) from exc
        return {
            "enabled": True,
            "backend": log_backend,
            "summary": summary,
        }

    @router.post("/message-flows/effects/probe")
    async def probe_message_flow_effect(
        body: FlowEffectProbeRequest,
        admin_request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ) -> dict[str, Any]:
        owner = body.owner.strip()
        effect_type = body.type.strip()
        if (owner, effect_type) not in {
            ("memory", "save_memory"),
            ("wxbot", "enqueue_channel_reply"),
        }:
            raise HTTPException(
                status_code=400,
                detail="unsupported_probe_effect:memory_save_or_wxbot_channel_reply_only",
            )
        if effect_handler_registry is None:
            raise HTTPException(status_code=503, detail="effect_handler_registry_unavailable")

        committer = _build_admin_effect_committer(
            settings=s,
            dry_run=body.dry_run,
            effect_log_store=effect_log_store,
        )
        if committer is None:
            raise HTTPException(status_code=409, detail="effect_commit_backend_disabled")
        operation_key = ""
        if not body.dry_run:
            operation_key = _required_admin_mutation_key(idempotency_key)
            if body.idempotency_key and body.idempotency_key.strip() != operation_key:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "idempotency_key_conflict"},
                )
            backend = str(s.orchestrator_flow_effect_commit_backend or "none").strip().lower()
            if backend != "redis":
                raise HTTPException(
                    status_code=409,
                    detail="real_probe_requires_redis_commit_backend",
                )
            if not bool(s.orchestrator_flow_effect_handlers_enabled):
                raise HTTPException(status_code=409, detail="effect_handlers_disabled")

        elif idempotency_key:
            operation_key = _required_admin_mutation_key(idempotency_key)
        elif body.idempotency_key:
            operation_key = _required_admin_mutation_key(body.idempotency_key)

        event_channel = Channel.WECHAT if owner == "wxbot" else body.channel
        event_session_id = body.session_id.strip() or (
            "admin-probe-wxbot-session" if owner == "wxbot" else "admin-probe-session"
        )
        event_user_id = body.user_id.strip() or "admin-probe-user"
        tenant_id = body.tenant_id.strip() or "admin-probe"
        stable_probe_digest = (
            sha256(f"{tenant_id}\0{owner}\0{effect_type}\0{operation_key}".encode()).hexdigest()[
                :32
            ]
            if operation_key
            else secrets.token_hex(16)
        )
        event = InboundEvent(
            message_id=f"effect-probe-{stable_probe_digest}",
            tenant_id=tenant_id,
            channel=event_channel,
            user_id=event_user_id,
            session_id=event_session_id,
            message=Message(content=body.user_text.strip()),
            trace_id=f"probe-{stable_probe_digest}",
            metadata={"source": body.source_key.strip() or "admin_probe", "probe": "admin"},
        )
        event.metadata.update(
            {
                "session_kind": body.session_kind.strip() or "private",
                "session_name": body.session_name.strip() or "admin-probe",
                "sender_name": body.sender_name.strip() or event_user_id,
                "sender_wxid": body.sender_wxid.strip() or event_user_id,
                "msg_svr_id": body.reply_to_msg_svr_id.strip() or "admin-probe-message",
            }
        )
        ctx = PipelineContext(event=event, trace_id=event.trace_id)
        ctx.pre = PreprocessedMessage(
            original_text=body.user_text,
            cleaned_text=body.user_text.strip(),
        )
        ctx.session = Session(
            session_id=event.session_id,
            tenant_id=event.tenant_id,
            user_id=event.user_id,
            channel=event.channel,
        )
        allowlist = _flow_effect_handler_allowlist(s)
        if allowlist:
            ctx.extras["enabled_handlers"] = list(allowlist)
            ctx.signals.setdefault("effects", {})["enabled_handlers"] = list(allowlist)

        effect_payload, idempotency_key = _admin_probe_effect_payload(
            request=body,
            event=event,
            owner=owner,
            effect_type=effect_type,
            idempotency_key_override=(
                f"admin_probe:{owner}:{effect_type}:{sha256(operation_key.encode('utf-8')).hexdigest()}"
                if operation_key
                else None
            ),
        )
        effect = MessageEffect(
            type=effect_type,
            owner=owner,
            payload=effect_payload,
            idempotency_key=idempotency_key,
        )
        dispatcher = EffectDispatcher(
            effect_handler_registry,
            committer,
            enabled_handlers=allowlist or bool(s.orchestrator_flow_effect_handlers_enabled),
            owner_gate=(
                getattr(plugin_registry, "execution_allowed", None)
                if plugin_registry is not None
                else None
            ),
        )
        dispatches = []
        set_admin_audit_context(
            admin_request,
            target_type="message_flow_effect_probe",
            tenant_id=event.tenant_id,
            session_id=event.session_id,
            user_id=event.user_id,
            before_state={
                "owner": owner,
                "effect_type": effect_type,
                "dry_run": bool(body.dry_run),
                "repeat": body.repeat,
                "dispatched": False,
            },
            after_state={"dispatched": False},
            trace_id=event.trace_id,
            reason="effect_probe_preview" if body.dry_run else "effect_probe_dispatch",
        )
        try:
            for _index in range(body.repeat):
                dispatches.append(await dispatcher.dispatch(effect, ctx, dry_run=body.dry_run))
        except EffectIdempotencyConflictError as exc:
            if exc.reason == "effect_idempotency_conflict":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "idempotency_key_conflict"},
                ) from exc
            raise HTTPException(
                status_code=503,
                detail={"code": exc.reason},
            ) from exc
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise HTTPException(status_code=503, detail=f"effect_probe_failed:{detail}") from exc
        dispatch = dispatches[-1]
        replayed = str(getattr(dispatches[0], "status", "")) == "duplicate"
        if replayed:
            response.headers["Idempotent-Replayed"] = "true"
        response.headers["Cache-Control"] = "no-store"
        set_admin_audit_context(
            admin_request,
            target_type="message_flow_effect_probe",
            tenant_id=event.tenant_id,
            session_id=event.session_id,
            user_id=event.user_id,
            before_state={
                "owner": owner,
                "effect_type": effect_type,
                "dry_run": bool(body.dry_run),
                "repeat": body.repeat,
                "dispatched": False,
            },
            after_state={
                "dispatched": True,
                "succeeded": not bool(dispatch.error),
                "idempotent_replayed": replayed,
                "dispatch_status": str(dispatch.status),
            },
            trace_id=event.trace_id,
            reason=(
                "effect_probe_idempotent_replay"
                if replayed
                else "effect_probe_preview"
                if body.dry_run
                else "effect_probe_dispatch"
            ),
        )

        return {
            "ok": not dispatch.error,
            "dry_run": body.dry_run,
            "repeat": body.repeat,
            "trace_id": event.trace_id,
            "effect": {
                "owner": owner,
                "type": effect_type,
                "idempotency_key": effect.idempotency_key,
            },
            "config": {
                "commit_backend": str(s.orchestrator_flow_effect_commit_backend or "none")
                .strip()
                .lower(),
                "log_backend": str(s.orchestrator_flow_effect_log_backend or "none")
                .strip()
                .lower(),
                "handlers_enabled": bool(s.orchestrator_flow_effect_handlers_enabled),
                "handler_allowlist": allowlist,
                "handler_mode": "off"
                if not bool(s.orchestrator_flow_effect_handlers_enabled)
                else "selective"
                if allowlist
                else "all",
            },
            "dispatch": _effect_dispatch_record_to_dict(dispatch),
            "dispatches": [_effect_dispatch_record_to_dict(item) for item in dispatches],
            "memory_session_keys": sorted((ctx.session.variables.get("user_memory") or {}).keys())
            if ctx.session is not None
            else [],
        }

    @router.get("/message-flows/{name}/shadow-run")
    async def shadow_run_message_flow(name: str) -> dict[str, Any]:
        flow_registry, _plugin_flow_steps, owner_permissions = _admin_flow_context(
            flow_step_registry=flow_step_registry,
            plugin_registry=plugin_registry,
        )
        compiled = {
            flow.name: (profile, flow)
            for profile, flow in compile_builtin_flows(
                flow_registry,
                owner_permissions=owner_permissions,
            )
        }
        item = compiled.get(name)
        if item is None:
            raise HTTPException(status_code=404, detail="message_flow_not_found")
        profile, flow = item
        event = InboundEvent(
            message_id="shadow-message",
            tenant_id="shadow",
            channel=Channel.WEB,
            user_id="shadow-user",
            session_id="shadow-session",
            message=Message(content="shadow"),
        )
        result = await FlowRunner(
            shadow=True,
            owner_gate=(
                getattr(plugin_registry, "execution_allowed", None)
                if plugin_registry is not None
                else None
            ),
        ).run(
            flow,
            PipelineContext(event=event, trace_id=event.trace_id),
        )
        return {
            "profile": {
                "name": profile.name,
                "version": profile.version,
                "description": profile.description,
                "bindings": [_flow_binding_to_dict(binding) for binding in profile.bindings],
            },
            "flow": _compiled_flow_to_dict(
                flow,
                description=profile.description,
                bindings=profile.bindings,
            ),
            "run": _flow_run_result_to_dict(result),
        }

    @router.get("/plugins/installed")
    async def get_installed_plugins() -> dict[str, Any]:
        if plugin_manager is None:
            return {"plugins": []}
        return await plugin_manager.installed()

    @router.get("/plugins/marketplace")
    async def get_plugin_marketplace() -> dict[str, Any]:
        if plugin_manager is None:
            return {"items": [], "restart_required": False}
        return await plugin_manager.marketplace()

    @router.get("/plugins/events")
    async def get_plugin_events(
        plugin_name: str = "",
        event_type: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if plugin_manager is None:
            return {"events": []}
        return await plugin_manager.events(
            plugin_name=plugin_name,
            event_type=event_type,
            limit=limit,
            offset=offset,
        )

    @router.get("/plugins/scopes")
    async def get_plugin_scope_states(
        tenant_id: str,
        response: Response,
        session_id: str | None = None,
        plugin_name: str = "",
    ) -> dict[str, Any]:
        if plugin_manager is None:
            if plugin_name:
                _set_plugin_scope_headers(response, 0)
            return {"items": []}
        result = await plugin_manager.scope_states(
            tenant_id=tenant_id,
            session_id=session_id,
            plugin_name=plugin_name,
        )
        if plugin_name:
            items = result.get("items") if isinstance(result, dict) else []
            first = items[0] if isinstance(items, list) and items else {}
            version = int(first.get("version") or 0) if isinstance(first, dict) else 0
            _set_plugin_scope_headers(response, version)
        else:
            response.headers["Cache-Control"] = "no-store"
        return result

    @router.post("/plugins/install/preview")
    async def preview_plugin_install(body: PluginInstallRequest) -> dict[str, Any]:
        if plugin_manager is None:
            raise HTTPException(status_code=404, detail="plugin_manager_unavailable")
        return await plugin_manager.install_preview(body.model_dump(exclude_unset=True))

    @router.post("/plugins/install")
    async def install_plugin(
        body: PluginInstallRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=128),
        ] = None,
    ) -> dict[str, Any]:
        if plugin_manager is None:
            raise HTTPException(status_code=404, detail="plugin_manager_unavailable")
        payload = body.model_dump()
        return await _execute_plugin_lifecycle_route(
            plugin_manager,
            operation="install",
            plugin_name=body.name,
            body=payload,
            request=request,
            response=response,
            idempotency_key=idempotency_key,
        )

    @router.get("/plugins/{name}/config-schema")
    async def get_plugin_config_schema(name: str) -> dict[str, Any]:
        if plugin_manager is None:
            raise HTTPException(status_code=404, detail="plugin_manager_unavailable")
        return await plugin_manager.config_schema(name)

    @router.get("/plugins/{name}/runtime")
    async def get_plugin_runtime(name: str) -> dict[str, Any]:
        if plugin_manager is None:
            raise HTTPException(status_code=404, detail="plugin_manager_unavailable")
        return await plugin_manager.runtime(name)

    @router.post("/plugins/{name}/enable")
    async def enable_plugin(
        name: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=128),
        ] = None,
    ) -> dict[str, Any]:
        if plugin_manager is None:
            raise HTTPException(status_code=404, detail="plugin_manager_unavailable")
        return await _execute_plugin_lifecycle_route(
            plugin_manager,
            operation="enable",
            plugin_name=name,
            body={},
            request=request,
            response=response,
            idempotency_key=idempotency_key,
        )

    @router.post("/plugins/{name}/disable")
    async def disable_plugin(
        name: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=128),
        ] = None,
    ) -> dict[str, Any]:
        if plugin_manager is None:
            raise HTTPException(status_code=404, detail="plugin_manager_unavailable")
        return await _execute_plugin_lifecycle_route(
            plugin_manager,
            operation="disable",
            plugin_name=name,
            body={},
            request=request,
            response=response,
            idempotency_key=idempotency_key,
        )

    @router.post("/plugins/{name}/scopes")
    async def set_plugin_scope_state(
        name: str,
        body: PluginScopeStateRequest,
        request: Request,
        response: Response,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=128),
        ] = None,
    ) -> dict[str, Any]:
        if plugin_manager is None:
            raise HTTPException(status_code=404, detail="plugin_manager_unavailable")
        try:
            validate_config_payload_bounds(body.config)
        except PluginConfigValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.as_detail()) from exc
        expected_version = _required_plugin_scope_if_match(if_match)
        operation_key = _required_admin_mutation_key(idempotency_key)
        payload = body.model_dump()
        session_id = str(body.session_id or "").strip()
        tenant_id = str(body.tenant_id or "").strip()

        async def mutate() -> MutationChange:
            _validated_plugin_scope_config(
                plugin_manager,
                plugin_registry,
                name,
                body.config,
            )
            before_rows = await plugin_manager.state_store.list_scope_states(
                tenant_id=tenant_id,
                session_id=session_id,
                plugin_name=name,
            )
            before = before_rows[0].as_dict() if before_rows else None
            result = await plugin_manager.set_scope_state(
                name,
                payload,
                request,
                expected_version=expected_version,
            )
            after = result.get("scope_state") if isinstance(result, dict) else None
            if not isinstance(after, dict):
                raise RuntimeError("plugin_scope_state_missing_after_write")
            return MutationChange(
                response=result,
                before_state={
                    "exists": before is not None,
                    "enabled": bool(before.get("enabled")) if before else False,
                    "version": int(before.get("version") or 0) if before else 0,
                },
                after_state={
                    "exists": True,
                    "enabled": bool(after.get("enabled")),
                    "version": int(after.get("version") or 0),
                },
                resource_version=str(int(after.get("version") or 0)),
            )

        try:
            outcome = await plugin_manager.state_store.run_admin_mutation(
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name=name,
                    operation="plugin.scope.set",
                    resource_key=session_id,
                    idempotency_key=operation_key,
                    request_payload=payload,
                ),
                audit=_admin_mutation_audit(
                    request,
                    scope={
                        "session_hash": hash_identifier(session_id),
                        "group_scoped": bool(session_id),
                    },
                    reason_code="plugin_scope_set",
                ),
                mutate=mutate,
            )
        except PluginScopeVersionConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "version_conflict",
                    "expected_version": exc.expected,
                    "current_version": exc.current,
                },
                headers={
                    "ETag": _plugin_scope_etag(exc.current),
                    "Cache-Control": "no-store",
                },
            ) from exc
        except Exception as exc:
            _raise_admin_mutation_error(exc)

        response_payload = dict(outcome.response)
        after = response_payload.get("scope_state")
        version = int(after.get("version") or 0) if isinstance(after, dict) else 0
        response.status_code = outcome.status_code
        _set_admin_mutation_headers(response, outcome)
        _set_plugin_scope_headers(response, version)
        set_admin_audit_context(
            request,
            target_type="plugin_scope",
            tenant_id=tenant_id,
            session_id=session_id,
            before_state={"version": expected_version},
            after_state={"version": version, "enabled": bool(body.enabled)},
            policy_version=version,
            trace_id=_runtime_llm_trace_id(request),
            reason="plugin_scope_set_replay" if outcome.replayed else "plugin_scope_set",
        )
        return response_payload

    @router.post("/plugins/{name}/upgrade/preview")
    async def preview_plugin_upgrade(
        name: str, body: PluginUpgradePreviewRequest
    ) -> dict[str, Any]:
        if plugin_manager is None:
            raise HTTPException(status_code=404, detail="plugin_manager_unavailable")
        return await plugin_manager.upgrade_preview(name, body.model_dump(exclude_unset=True))

    @router.post("/plugins/{name}/upgrade")
    async def upgrade_plugin(
        name: str,
        body: PluginUpgradePreviewRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=128),
        ] = None,
    ) -> dict[str, Any]:
        if plugin_manager is None:
            raise HTTPException(status_code=404, detail="plugin_manager_unavailable")
        return await _execute_plugin_lifecycle_route(
            plugin_manager,
            operation="upgrade",
            plugin_name=name,
            body=body.model_dump(),
            request=request,
            response=response,
            idempotency_key=idempotency_key,
        )

    @router.post("/plugins/{name}/uninstall")
    async def uninstall_plugin(
        name: str,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=128),
        ] = None,
    ) -> dict[str, Any]:
        if plugin_manager is None:
            raise HTTPException(status_code=404, detail="plugin_manager_unavailable")
        return await _execute_plugin_lifecycle_route(
            plugin_manager,
            operation="uninstall",
            plugin_name=name,
            body={},
            request=request,
            response=response,
            idempotency_key=idempotency_key,
        )

    # -------------- Streams / queues ---------------------------------------

    if stream_service is not None:

        @router.get("/streams/summary")
        async def get_streams_summary() -> dict[str, Any]:
            return {"streams": await stream_service.summary()}

        @router.get("/streams/messages")
        async def list_stream_messages(
            stream: str,
            limit: int = 100,
            before_id: str | None = None,
            tenant_id: str | None = None,
            session_id: str | None = None,
            trace_id: str | None = None,
        ) -> dict[str, Any]:
            try:
                items, next_before_id = await stream_service.list_messages(
                    stream_key=stream,
                    limit=limit,
                    before_id=before_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    trace_id=trace_id,
                )
            except KeyError as exc:
                raise HTTPException(status_code=400, detail=f"invalid_stream:{stream}") from exc
            return {
                "stream": stream,
                "items": [_stream_message_to_dict(item) for item in items],
                "next_before_id": next_before_id,
            }

        @router.get("/streams/recent-messages")
        async def list_recent_stream_messages(
            stream: str = "inbound",
            limit: int = 100,
            before_id: str | None = None,
            tenant_id: str | None = None,
            session_id: str | None = None,
            trace_id: str | None = None,
            include_media_events: bool = True,
        ) -> dict[str, Any]:
            try:
                items, next_before_id = await stream_service.list_messages(
                    stream_key=stream,
                    limit=limit,
                    before_id=before_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    trace_id=trace_id,
                )
            except KeyError as exc:
                raise HTTPException(status_code=400, detail=f"invalid_stream:{stream}") from exc
            stream_items = [_stream_message_to_dict(item) for item in items]
            media_result = await _list_admin_media_events(
                media_event_providers or [],
                stream=stream,
                before_id=before_id,
                tenant_id=tenant_id,
                session_id=session_id,
                trace_id=trace_id,
                limit=limit,
                include_media_events=include_media_events,
            )
            merged_items = _merge_recent_admin_messages(
                stream_items,
                media_result["items"],
                limit=limit,
            )
            merged_items = _project_recent_admin_messages(
                media_event_providers or [],
                merged_items,
                tenant_id=str(tenant_id or "").strip(),
            )
            return {
                "stream": stream,
                "items": merged_items,
                "next_before_id": next_before_id,
                "sources": {
                    "stream": {"count": len(stream_items)},
                    "media_events": {
                        "count": len(media_result["items"]),
                        "providers": media_result["providers"],
                        "errors": media_result["errors"],
                    },
                },
            }

        @router.get("/streams/messages/{stream}/{entry_id}")
        async def get_stream_message(stream: str, entry_id: str) -> dict[str, Any]:
            try:
                item = await stream_service.get_message(stream_key=stream, entry_id=entry_id)
            except KeyError as exc:
                raise HTTPException(status_code=400, detail=f"invalid_stream:{stream}") from exc
            if item is None:
                raise HTTPException(status_code=404, detail="stream_message_not_found")
            return _stream_message_to_dict(item)

    return router


def _build_admin_effect_committer(
    *,
    settings: Settings,
    dry_run: bool,
    effect_log_store: PostgresEffectLog | None,
) -> Any | None:
    backend = str(settings.orchestrator_flow_effect_commit_backend or "none").strip().lower()
    if backend == "redis":
        from app.infra.redis_client import get_redis

        committer: Any = RedisEffectCommitter(
            get_redis(),
            key_prefix=str(settings.orchestrator_flow_effect_commit_key_prefix or "cs:flow:effect"),
            ttl_seconds=int(settings.orchestrator_flow_effect_commit_ttl_seconds or 604_800),
            log_stream=str(settings.orchestrator_flow_effect_commit_stream or "").strip() or None,
        )
    elif backend == "memory" or dry_run:
        committer = InMemoryEffectCommitter()
    elif backend in {"", "none"}:
        return None
    else:
        raise HTTPException(status_code=409, detail=f"unsupported_effect_commit_backend:{backend}")

    log_backend = str(settings.orchestrator_flow_effect_log_backend or "none").strip().lower()
    if log_backend not in {"postgres", "postgresql", "sql"}:
        return committer
    failure_policy = (
        str(settings.orchestrator_flow_effect_log_failure_policy or "fail_closed").strip().lower()
    )
    return AuditedEffectCommitter(
        committer,
        effect_log_store or PostgresEffectLog(),
        fail_closed=failure_policy != "fail_open",
    )


def _effect_dispatch_record_to_dict(record: Any) -> dict[str, Any]:
    payload = dict(getattr(record, "payload", {}) or {})
    return {
        "owner": getattr(record, "owner", ""),
        "type": getattr(record, "type", ""),
        "idempotency_key": getattr(record, "idempotency_key", ""),
        "status": getattr(record, "status", ""),
        "commit_status": getattr(record, "commit_status", ""),
        "dry_run": bool(getattr(record, "dry_run", False)),
        "error": getattr(record, "error", ""),
        "commit_error": getattr(record, "commit_error", ""),
        "payload_keys": sorted(str(key) for key in payload),
        "payload_size": len(json.dumps(payload, ensure_ascii=False, default=str)),
    }


def _admin_probe_effect_payload(
    *,
    request: FlowEffectProbeRequest,
    event: InboundEvent,
    owner: str,
    effect_type: str,
    idempotency_key_override: str | None = None,
) -> tuple[dict[str, Any], str]:
    if (owner, effect_type) == ("wxbot", "enqueue_channel_reply"):
        command_id = (
            idempotency_key_override
            or request.idempotency_key
            or f"admin_probe:wxbot:reply:{event.tenant_id}:{event.session_id}:{event.user_id}:{event.trace_id}"
        )
        session_kind = request.session_kind.strip() or "private"
        session_name = request.session_name.strip() or "admin-probe"
        sender_name = request.sender_name.strip() or event.user_id
        sender_wxid = request.sender_wxid.strip() or event.user_id
        reply_to_msg_svr_id = request.reply_to_msg_svr_id.strip() or "admin-probe-message"
        delivery = {
            "channel": "wechat",
            "command_id": command_id,
            "idempotency_key": command_id,
            "tenant_id": event.tenant_id,
            "session_id": event.session_id,
            "session_name": session_name,
            "session_kind": session_kind,
            "sender_name": sender_name,
            "sender_wxid": sender_wxid,
            "mention_sender": False,
            "reply_to_msg_svr_id": reply_to_msg_svr_id,
        }
        return (
            {
                "tenant_id": event.tenant_id,
                "channel": "wechat",
                "session_id": event.session_id,
                "session_name": session_name,
                "session_kind": session_kind,
                "user_id": event.user_id,
                "sender_name": sender_name,
                "sender_wxid": sender_wxid,
                "reply_to_msg_svr_id": reply_to_msg_svr_id,
                "body": {"type": "text", "text": request.assistant_text.strip()},
                "trace_id": event.trace_id,
                "mention_sender": False,
                "source_message": event.model_dump(mode="json"),
                "delivery": delivery,
                "command_id": command_id,
                "probe": True,
            },
            command_id,
        )
    return (
        {
            "tenant_id": event.tenant_id,
            "channel": event.channel.value,
            "source_key": request.source_key.strip() or "admin_probe",
            "user_id": event.user_id,
            "session_id": event.session_id,
            "trace_id": event.trace_id,
            "user_text": request.user_text.strip(),
            "assistant_text": request.assistant_text.strip(),
            "probe": True,
        },
        idempotency_key_override
        or f"admin_probe:memory:save:{event.tenant_id}:{event.session_id}:{event.user_id}:{event.trace_id}",
    )


def _admin_flow_context(
    *,
    flow_step_registry: FlowStepRegistry | None,
    plugin_registry: PluginRegistry | None,
) -> tuple[FlowStepRegistry, list[FlowStepDefinition], dict[str, set[str]] | None]:
    flow_registry = flow_step_registry or build_default_flow_registry()
    plugin_flow_steps: list[FlowStepDefinition] = []
    if flow_step_registry is None and plugin_registry is not None:
        collect_flow_steps = getattr(plugin_registry, "all_flow_steps", None)
        if callable(collect_flow_steps):
            plugin_flow_steps = list(collect_flow_steps())
            flow_registry.register_many(plugin_flow_steps)
    elif flow_step_registry is not None:
        plugin_flow_steps = [
            definition
            for definition in flow_registry.list_definitions()
            if definition.owner != "core"
        ]
    owner_permissions: dict[str, set[str]] | None = None
    if plugin_registry is not None:
        collect_permissions = getattr(plugin_registry, "all_permissions", None)
        if callable(collect_permissions):
            owner_permissions = collect_permissions()
    return flow_registry, plugin_flow_steps, owner_permissions


def _compiled_flow_to_dict(
    flow: CompiledFlow,
    *,
    description: str = "",
    bindings: list[FlowBinding] | None = None,
) -> dict[str, Any]:
    return {
        "name": flow.name,
        "version": flow.version,
        "description": description,
        "status": flow.status,
        "active": flow.active,
        "warnings": list(flow.warnings),
        "errors": list(flow.errors),
        "bindings": [_flow_binding_to_dict(binding) for binding in bindings or []],
        "steps": [_compiled_step_to_dict(step) for step in flow.steps],
    }


def _flow_binding_to_dict(binding: FlowBinding) -> dict[str, Any]:
    return {
        "tenant_id": binding.tenant_id,
        "channel": binding.channel,
        "session_kind": binding.session_kind,
        "session_id_pattern": binding.session_id_pattern,
        "message_type": binding.message_type,
        "priority": binding.priority,
        "source": binding.source,
    }


def _flow_resolve_result_to_dict(result: FlowResolveResult) -> dict[str, Any]:
    return {
        "request": {
            "tenant_id": result.request.tenant_id,
            "channel": result.request.channel,
            "session_kind": result.request.session_kind,
            "session_id": result.request.session_id,
            "message_type": result.request.message_type,
        },
        "matched": result.profile is not None,
        "profile": (
            {
                "name": result.profile.name,
                "version": result.profile.version,
                "description": result.profile.description,
            }
            if result.profile is not None
            else None
        ),
        "binding": _flow_binding_to_dict(result.binding) if result.binding is not None else None,
        "candidates": [
            _flow_resolve_candidate_to_dict(candidate) for candidate in result.candidates
        ],
    }


def _flow_resolve_candidate_to_dict(candidate: FlowResolveCandidate) -> dict[str, Any]:
    return {
        "profile_name": candidate.profile_name,
        "profile_version": candidate.profile_version,
        "binding": _flow_binding_to_dict(candidate.binding),
        "matched": candidate.matched,
        "specificity": candidate.specificity,
        "reason": candidate.reason,
    }


def _flow_run_result_to_dict(result: FlowRunResult) -> dict[str, Any]:
    return {
        "flow_name": result.flow_name,
        "flow_version": result.flow_version,
        "status": result.status,
        "ok": result.ok,
        "trace_id": result.trace_id,
        "tenant_id": result.tenant_id,
        "session_id": result.session_id,
        "stop_reason": result.stop_reason,
        "error": result.error,
        "steps": [_flow_run_step_trace_to_dict(step) for step in result.steps],
        "effect_commits": [dict(item) for item in result.effect_commits],
        "effect_dispatches": [dict(item) for item in result.effect_dispatches],
    }


def _flow_run_step_trace_to_dict(step: FlowRunStepTrace) -> dict[str, Any]:
    return {
        "id": step.id,
        "kind": step.kind,
        "owner": step.owner,
        "status": step.status,
        "action": step.action,
        "reason": step.reason,
        "error": step.error,
        "elapsed_ms": step.elapsed_ms,
        "attempts": step.attempts,
    }


def _flow_runtime_config_to_dict(settings: Settings) -> dict[str, Any]:
    return build_flow_runtime_config_payload(settings)


def _flow_shadow_config_to_dict(settings: Settings) -> dict[str, Any]:
    return {
        "enabled": bool(settings.orchestrator_flow_shadow_enabled),
        "name": settings.orchestrator_flow_shadow_name,
        "mode": settings.orchestrator_flow_shadow_mode,
        "core_preview_enabled": bool(settings.orchestrator_flow_shadow_core_preview_enabled),
        "plugin_dry_run_enabled": bool(settings.orchestrator_flow_shadow_plugin_dry_run_enabled),
        "effect_dry_run_enabled": bool(settings.orchestrator_flow_shadow_effect_dry_run_enabled),
    }


def _flow_effect_handler_allowlist(settings: Settings) -> list[str]:
    raw = str(settings.orchestrator_flow_effect_handler_allowlist or "").strip()
    return [item.strip() for chunk in raw.split(",") for item in chunk.split() if item.strip()]


def _flow_effect_commit_config_to_dict(settings: Settings) -> dict[str, Any]:
    backend = str(settings.orchestrator_flow_effect_commit_backend or "none").strip().lower()
    log_backend = str(settings.orchestrator_flow_effect_log_backend or "none").strip().lower()
    log_failure_policy = (
        str(settings.orchestrator_flow_effect_log_failure_policy or "fail_closed").strip().lower()
    )
    handlers_enabled = bool(settings.orchestrator_flow_effect_handlers_enabled)
    handler_allowlist = _flow_effect_handler_allowlist(settings)
    allowed = backend in {"none", "memory", "redis"}
    log_allowed = log_backend in {"none", "postgres", "postgresql", "sql"}
    policy_allowed = log_failure_policy in {"fail_open", "fail_closed"}
    handlers_allowed = not handlers_enabled or backend == "redis"
    config_allowed = allowed and log_allowed and policy_allowed and handlers_allowed
    return {
        "backend": backend,
        "allowed": config_allowed,
        "reason": (
            "allowed"
            if config_allowed
            else (
                "unsupported_log_backend"
                if not log_allowed
                else "unsupported_log_failure_policy"
                if not policy_allowed
                else "unsupported_backend"
                if not allowed
                else "handlers_require_redis_commit_backend"
            )
        ),
        "ttl_seconds": int(settings.orchestrator_flow_effect_commit_ttl_seconds),
        "key_prefix": settings.orchestrator_flow_effect_commit_key_prefix,
        "stream": settings.orchestrator_flow_effect_commit_stream,
        "handlers_enabled": handlers_enabled,
        "handler_allowlist": handler_allowlist,
        "handler_mode": (
            "off" if not handlers_enabled else "selective" if handler_allowlist else "all"
        ),
        "handlers_commit_backend_safe": backend == "redis",
        "log_backend": log_backend,
        "log_failure_policy": log_failure_policy,
    }


def _flow_trace_snapshot_config_to_dict(settings: Settings) -> dict[str, Any]:
    return {
        "enabled": bool(getattr(settings, "orchestrator_flow_trace_snapshot_enabled", True)),
        "backend": "redis",
        "ttl_seconds": int(
            getattr(settings, "orchestrator_flow_trace_snapshot_ttl_seconds", 604_800) or 604_800
        ),
        "key_prefix": str(
            getattr(
                settings,
                "orchestrator_flow_trace_snapshot_key_prefix",
                "cs:flow:trace",
            )
            or "cs:flow:trace"
        ),
    }


def _compiled_step_to_dict(step: CompiledStep) -> dict[str, Any]:
    effects = sorted(output for output in step.outputs if output.startswith("effects."))
    return {
        "id": step.id,
        "kind": step.kind,
        "owner": step.owner,
        "name": step.name,
        "permissions": list(step.permissions),
        "inputs": sorted(step.inputs),
        "outputs": sorted(step.outputs),
        "effects": effects,
        "effectful": bool(effects),
        "when": dict(step.when),
        "timeout_seconds": step.timeout_seconds,
        "error_policy": step.error_policy,
        "optional": step.optional,
    }


def _flow_step_definition_to_dict(definition: FlowStepDefinition) -> dict[str, Any]:
    effects = sorted(output for output in definition.outputs if output.startswith("effects."))
    return {
        "kind": definition.kind,
        "owner": definition.owner,
        "name": definition.name or definition.kind,
        "permissions": list(definition.permissions),
        "inputs": sorted(definition.inputs),
        "outputs": sorted(definition.outputs),
        "effects": effects,
        "effectful": bool(effects),
        "replace_outputs": sorted(definition.replace_outputs),
        "timeout_seconds": definition.timeout_seconds,
        "error_policy": definition.error_policy,
        "optional": definition.optional,
        "enabled": definition.enabled,
        "schema_version": definition.schema_version,
    }


def _doc_to_dict(doc: Any, *, include_content: bool) -> dict[str, Any]:
    normalized_session_id = normalize_scope_session_id(doc.session_id)
    payload = {
        "id": doc.id,
        "tenant_id": doc.tenant_id,
        "session_id": normalized_session_id or None,
        "scope": "session" if normalized_session_id else "global",
        "title": doc.title,
        "source": doc.source,
        "url": doc.url,
        "content_hash": doc.content_hash,
        "metadata": doc.meta,
    }
    if include_content:
        payload["content"] = doc.content
    return payload


def _faq_to_dict(rec: Any) -> dict[str, Any]:
    return {
        "id": rec.id,
        "tenant_id": rec.tenant_id,
        "session_id": normalize_scope_session_id(getattr(rec, "session_id", "")) or None,
        "scope": "session"
        if normalize_scope_session_id(getattr(rec, "session_id", ""))
        else "global",
        "question": rec.question,
        "answer": rec.answer,
        "variants": list(rec.variants or []),
        "tags": list(rec.tags or []),
        "version": rec.version,
        "status": rec.status,
    }


def _dlq_to_dict(rec: DLQMessage) -> dict[str, Any]:
    return {
        "id": rec.id,
        "tenant_id": rec.tenant_id,
        "stream": rec.stream,
        "origin_stream": rec.origin_stream,
        "origin_id": rec.origin_id,
        "reason": rec.reason,
        "attempts": rec.attempts,
        "headers": rec.headers,
        "payload": rec.payload,
    }


def _stream_message_to_dict(rec: Any) -> dict[str, Any]:
    return {
        "id": rec.id,
        "source": "stream",
        "stream_key": rec.stream_key,
        "stream": rec.stream,
        "tenant_id": rec.tenant_id,
        "session_id": rec.session_id,
        "user_id": rec.user_id,
        "trace_id": rec.trace_id,
        "channel": rec.channel,
        "attempts": rec.attempts,
        "reason": rec.reason,
        "origin_stream": rec.origin_stream,
        "origin_id": rec.origin_id,
        "created_ts_ms": rec.created_ts_ms,
        "headers": rec.headers,
        "payload": rec.payload,
    }


async def _list_admin_media_events(
    providers: list[Any],
    *,
    stream: str,
    before_id: str | None,
    tenant_id: str | None,
    session_id: str | None,
    trace_id: str | None,
    limit: int,
    include_media_events: bool,
) -> dict[str, Any]:
    if not include_media_events or stream != "inbound" or trace_id:
        return {"items": [], "providers": [], "errors": []}
    cleaned_tenant = str(tenant_id or "").strip()
    if not cleaned_tenant:
        return {"items": [], "providers": [], "errors": []}
    before_ts_ms = _admin_stream_cursor_time(before_id) if before_id else None
    page_size = max(1, min(int(limit or 100), 200))
    items: list[dict[str, Any]] = []
    provider_names: list[str] = []
    errors: list[dict[str, str]] = []
    for provider in providers:
        name = str(
            getattr(provider, "name", None)
            or getattr(provider, "owner", None)
            or provider.__class__.__name__
        )
        provider_names.append(name)
        list_recent = getattr(provider, "list_recent_media_events", None)
        if not callable(list_recent):
            errors.append({"provider": name, "error": "missing_list_recent_media_events"})
            continue
        try:
            provider_items = await list_recent(
                tenant_id=cleaned_tenant,
                limit=page_size,
                session_id=session_id,
            )
        except Exception as exc:
            errors.append({"provider": name, "error": str(exc)})
            continue
        for item in provider_items or []:
            if not isinstance(item, dict):
                continue
            if before_id:
                item_ts_ms = _admin_message_time(item)
                # Stream cursors are exclusive. Media events only expose a millisecond
                # timestamp, so same-millisecond and unknown events cannot safely be
                # classified as older than the requested cursor.
                if before_ts_ms is None or item_ts_ms <= 0 or item_ts_ms >= before_ts_ms:
                    continue
            items.append(item)
    return {"items": items, "providers": provider_names, "errors": errors}


def _admin_stream_cursor_time(before_id: str | None) -> int | None:
    head = str(before_id or "").split("-", 1)[0].strip()
    if not head:
        return None
    try:
        return int(head)
    except ValueError:
        return None


def _admin_message_time(item: dict[str, Any]) -> int:
    raw = item.get("created_ts_ms")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _admin_message_payload(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload")
    return payload if isinstance(payload, dict) else {}


def _admin_message_message_id(item: dict[str, Any]) -> str:
    payload = _admin_message_payload(item)
    value = payload.get("message_id")
    return value.strip() if isinstance(value, str) else str(value or "").strip()


def _admin_message_identity_keys(item: dict[str, Any]) -> set[tuple[str, str, str]]:
    payload = _admin_message_payload(item)
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    tenant_id = str(item.get("tenant_id") or payload.get("tenant_id") or "").strip()
    connection_id = str(payload.get("connection_id") or metadata.get("connection_id") or "").strip()
    values = {
        str(payload.get("message_id") or "").strip(),
        str(payload.get("external_message_id") or "").strip(),
        str(metadata.get("external_message_id") or "").strip(),
        str(metadata.get("msg_svr_id") or "").strip(),
    }
    return {(tenant_id, connection_id, value) for value in values if value}


def _enrich_stream_message_with_media(
    stream_item: dict[str, Any],
    media_item: dict[str, Any],
) -> dict[str, Any]:
    """Upgrade a pending stream item while preserving its stable identity and quote."""

    stream_payload = dict(_admin_message_payload(stream_item))
    media_payload = _admin_message_payload(media_item)
    stream_message = stream_payload.get("message")
    stream_message = dict(stream_message) if isinstance(stream_message, dict) else {}
    media_message = media_payload.get("message")
    media_message = media_message if isinstance(media_message, dict) else {}
    attachments = media_message.get("attachments")
    if isinstance(attachments, list) and attachments:
        stream_message["attachments"] = attachments
    for key in ("type", "content"):
        if not stream_message.get(key) and media_message.get(key):
            stream_message[key] = media_message[key]
    if stream_message:
        stream_payload["message"] = stream_message

    stream_metadata = stream_payload.get("metadata")
    stream_metadata = dict(stream_metadata) if isinstance(stream_metadata, dict) else {}
    media_metadata = media_payload.get("metadata")
    media_metadata = media_metadata if isinstance(media_metadata, dict) else {}
    for key, value in media_metadata.items():
        if key in {"media", "raw", "image_observation", "media_status"}:
            stream_metadata[key] = value
        elif key not in stream_metadata:
            stream_metadata[key] = value
    if stream_metadata:
        stream_payload["metadata"] = stream_metadata

    for key in ("media", "raw", "media_ready_event"):
        if key in media_payload:
            stream_payload[key] = media_payload[key]
    for key in (
        "connection_id",
        "external_message_id",
        "external_conversation_id",
        "external_participant_id",
    ):
        if not stream_payload.get(key) and media_payload.get(key):
            stream_payload[key] = media_payload[key]

    enriched = dict(stream_item)
    enriched["payload"] = stream_payload
    media_ready_ts_ms = _admin_message_time(media_item)
    if media_ready_ts_ms > _admin_message_time(enriched):
        # A deferred media update is new activity.  Keep the stream item's
        # stable identity and original received_at payload, but sort it by the
        # readiness transition so it is not discarded behind newer media rows.
        enriched["created_ts_ms"] = media_ready_ts_ms
    return enriched


def _merge_recent_admin_messages(
    stream_items: list[dict[str, Any]],
    media_items: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    page_size = max(1, min(int(limit or 100), 200))
    enriched_stream_items = list(stream_items)
    stream_by_identity: dict[tuple[str, str, str], int] = {}
    for index, item in enumerate(enriched_stream_items):
        for key in _admin_message_identity_keys(item):
            stream_by_identity.setdefault(key, index)

    unmatched_media_items: list[dict[str, Any]] = []
    for media_item in media_items:
        stream_index = next(
            (
                stream_by_identity[key]
                for key in _admin_message_identity_keys(media_item)
                if key in stream_by_identity
            ),
            None,
        )
        if stream_index is None:
            unmatched_media_items.append(media_item)
            continue
        enriched_stream_items[stream_index] = _enrich_stream_message_with_media(
            enriched_stream_items[stream_index],
            media_item,
        )

    merged = [*enriched_stream_items, *unmatched_media_items]
    return sorted(merged, key=_admin_message_time, reverse=True)[:page_size]


def _project_recent_admin_messages(
    providers: list[Any],
    items: list[dict[str, Any]],
    *,
    tenant_id: str,
) -> list[dict[str, Any]]:
    projected = items
    if not tenant_id:
        return projected
    for provider in providers:
        projector = getattr(provider, "project_recent_message", None)
        if not callable(projector):
            continue
        next_items: list[dict[str, Any]] = []
        for item in projected:
            candidate = projector(item, tenant_id)
            if not isinstance(candidate, dict):
                raise TypeError("admin media projector must return a dictionary")
            next_items.append(candidate)
        projected = next_items
    return projected
