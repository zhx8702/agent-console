"""
Application entrypoint.

Responsibilities:
- Build the service container from configuration.
- Mount HTTP routers (ingress webhook, admin, health, /metrics).
- Wire OpenTelemetry instrumentation.

The container is process-global so tests can override individual collaborators
by importing ``app.main.build_container`` and patching before ``create_app``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import orjson
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.admin.audit import install_admin_audit_middleware
from app.admin.auth_router import build_admin_auth_router
from app.admin.authorization import build_admin_authorization_dependency
from app.admin.dlq_service import DLQAdminService
from app.admin.kb_router import build_admin_router
from app.admin.route_permissions import bind_default_route_permissions
from app.admin.stream_service import StreamAdminService
from app.agent.engine import AgentCapabilityEngine
from app.agent.registry import AgentToolRegistry
from app.agent.store import AgentStore
from app.billing import BillingCoordinator
from app.bus.redis_streams import RedisStreamBus
from app.channel import ChannelRegistry, ChannelTarget
from app.channel.adapters import ChannelAdapterCatalog
from app.channel.connections import ChannelConnectionStore
from app.channel.router import build_channel_admin_router
from app.common.config import Settings, get_settings
from app.common.logging import configure_logging, get_logger
from app.common.runtime_llm_config import (
    load_runtime_llm_config,
    runtime_llm_overlay_enabled_for_role,
)
from app.common.types import RouteType
from app.container import (
    ApiContainer,
    Container,
    CoreRuntimeContainer,
    InboundContainer,
    OutboundContainer,
    RuntimeContainer,
    SchedulerContainer,
    set_container,
)
from app.egress.dispatcher import OutboundDispatcher
from app.faq.engine import FAQEngine
from app.faq.store import FAQStore, InMemoryFAQRepository, SQLAlchemyFAQRepository
from app.infra.db import dispose_engine, get_engine, get_session_factory
from app.infra.otel import setup_tracing
from app.infra.redis_client import close_redis, get_redis
from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_REVISION,
    verify_runtime_schema,
)
from app.ingress.router import build_router as build_ingress_router
from app.kb.ingest import IngestionService
from app.kb.service import InMemoryKBStore, KnowledgeBaseService, SQLAlchemyKBStore
from app.kb.vector.memory_store import InMemoryVectorStore
from app.kb.vector.qdrant_store import QdrantVectorStore
from app.llm.service import build_llm_service, validate_llm_settings
from app.orchestrator.adapters import AsyncRouterAdapter, AsyncSafetyAdapter
from app.orchestrator.effect_handlers import (
    ChannelReplyEffectHandler,
    EffectHandlerRegistry,
    effect_handler_registry_payload,
    register_core_publish_outbound_handler,
    register_core_session_effect_handlers,
)
from app.orchestrator.effect_log import PostgresEffectLog
from app.orchestrator.engine import DialogOrchestrator
from app.orchestrator.flow import build_default_flow_registry
from app.orchestrator.flow_runtime_config import build_flow_runtime_config_payload
from app.orchestrator.simple_capabilities import (
    HandoffCapabilityEngine,
    LLMCapabilityEngine,
)
from app.plugin.base import PluginContext
from app.plugin.manager import PluginManager
from app.plugin.registry import PluginRegistry
from app.plugin.state import PluginStateStore
from app.postprocessing.processor import build_postprocessor
from app.common.intent_classify import LlmIntentClassifier
from app.preprocessing.processor import build_preprocessor
from app.rag.engine import RAGEngine
from app.rag.retriever import HybridRetriever
from app.reliability import (
    MessageOutboxRelay,
    MessageReliabilityStore,
    TransactionalOutboxBus,
)
from app.router.engine import build_router as build_rule_router
from app.safety.service import build_safety
from app.session.manager import SessionManager
from app.social.effects import MemberMemoryForgetEffectHandler
from app.social.router import build_social_admin_router
from app.social.store import SocialPolicyStore
from app.workers.readiness import (
    probe_db_semantics,
    probe_qdrant_semantics,
    probe_redis_semantics,
    required_dependencies_for_role,
)

log = get_logger(__name__)


def _build_plugin_runtime_dependency(
    registry: PluginRegistry,
    plugin_name: str,
):
    """Create a fail-closed, request-scope-aware plugin execution dependency."""

    default_tenant_lookup = getattr(registry, "api_default_tenant_id", None)
    implicit_tenant_id = (
        str(default_tenant_lookup(plugin_name) or "").strip()
        if callable(default_tenant_lookup)
        else ""
    )

    async def require_plugin_runtime(request: Request) -> None:
        scopes = await _plugin_request_scopes(
            request,
            implicit_tenant_id=implicit_tenant_id,
        )
        if scopes:
            allowed = all(
                [
                    await registry.scope_execution_allowed(
                        plugin_name,
                        tenant_id=tenant_id,
                        session_id=session_id,
                    )
                    for tenant_id, session_id in scopes
                ]
            )
        else:
            allowed = await registry.global_execution_allowed(plugin_name)
        if not allowed:
            raise HTTPException(
                status_code=503,
                detail="plugin_runtime_disabled",
            )

    return require_plugin_runtime


async def _plugin_request_scopes(
    request: Request,
    *,
    implicit_tenant_id: str = "",
) -> tuple[tuple[str, str], ...]:
    """Extract every declared tenant/session pair from a plugin API request.

    Plugin routers use a mix of path, query and JSON-body scope fields. A
    bulk request is allowed only if the owner is executable for every target.
    The bounded walk avoids turning this authorization dependency into an
    unbounded parser for arbitrary plugin payloads.
    """

    scopes: list[tuple[str, str]] = []

    def add(mapping: object, *, fallback_tenant: str = "") -> str:
        if not isinstance(mapping, dict):
            return fallback_tenant
        if not ({"tenant_id", "session_id", "session_ids"} & mapping.keys()):
            return fallback_tenant
        tenant_id = str(mapping.get("tenant_id") or fallback_tenant).strip()
        session_id = str(mapping.get("session_id") or "").strip()
        raw_session_ids = mapping.get("session_ids") or []
        if not isinstance(raw_session_ids, list):
            raise HTTPException(
                status_code=400,
                detail="plugin_scope_sessions_invalid",
            )
        if len(raw_session_ids) > 256:
            raise HTTPException(
                status_code=413,
                detail="plugin_scope_payload_too_complex",
            )
        session_ids = [str(value or "").strip() for value in raw_session_ids]
        if (session_id or any(session_ids)) and not tenant_id:
            raise HTTPException(
                status_code=400,
                detail="plugin_scope_tenant_required",
            )
        if tenant_id:
            if session_id or not any(session_ids):
                scopes.append((tenant_id, session_id))
            scopes.extend((tenant_id, value) for value in session_ids if value)
        return tenant_id or fallback_tenant

    implicit_tenant = str(implicit_tenant_id or "").strip()
    path_values = dict(request.path_params)
    query_scope_values: dict[str, str] = {}
    for key in ("tenant_id", "session_id"):
        raw_values = request.query_params.getlist(key)
        if len(raw_values) > 1:
            raise HTTPException(
                status_code=400,
                detail="plugin_scope_query_ambiguous",
            )
        query_scope_values[key] = str(raw_values[0] or "").strip() if raw_values else ""
    path_tenant = str(path_values.get("tenant_id") or "").strip()
    path_session = str(path_values.get("session_id") or "").strip()
    query_tenant = query_scope_values["tenant_id"]
    query_session = query_scope_values["session_id"]
    inherited_tenant = path_tenant or query_tenant or implicit_tenant
    if path_tenant or path_session:
        add(
            {
                "tenant_id": path_tenant or query_tenant or implicit_tenant,
                "session_id": path_session,
            }
        )
    if query_tenant or query_session:
        add(
            {
                "tenant_id": query_tenant or path_tenant or implicit_tenant,
                "session_id": query_session,
            }
        )

    content_type = str(request.headers.get("content-type") or "").lower()
    # Starlette/FastAPI accepts a JSON request body when Content-Type is
    # omitted.  The execution gate must parse the same inputs as the mounted
    # plugin route or a caller could hide a disabled session from this scope
    # walk simply by dropping the header.
    body_may_be_json = not content_type or "json" in content_type
    if request.method not in {"GET", "HEAD", "OPTIONS"} and body_may_be_json:
        try:
            payload = await request.json()
        except Exception:
            payload = None
        pending = [(payload, inherited_tenant)]
        visited = 0
        while pending and visited < 256:
            current, parent_tenant = pending.pop()
            visited += 1
            if isinstance(current, dict):
                current_tenant = add(current, fallback_tenant=parent_tenant)
                pending.extend((value, current_tenant) for value in current.values())
            elif isinstance(current, list):
                pending.extend((value, parent_tenant) for value in current)
        if pending:
            # Silently truncating an authorization walk turns the node limit
            # into a scope-bypass primitive: an attacker can place a denied
            # target beyond the visited prefix. Reject the whole request.
            raise HTTPException(
                status_code=413,
                detail="plugin_scope_payload_too_complex",
            )

    if not scopes and implicit_tenant:
        scopes.append((implicit_tenant, ""))

    return tuple(dict.fromkeys(scopes))


def _plugin_discovery_paths(settings: Settings) -> tuple[Path, ...]:
    builtin_dir = (settings.project_root / "plugins").resolve()
    configured_install_dir = Path(str(settings.plugin_install_dir or "plugins"))
    if not configured_install_dir.is_absolute():
        configured_install_dir = settings.project_root / configured_install_dir
    install_dir = configured_install_dir.resolve()
    return tuple(dict.fromkeys((builtin_dir, install_dir)))


def _discover_plugin_directories(
    registry: PluginRegistry,
    settings: Settings,
) -> int:
    builtin_dir = (settings.project_root / "plugins").resolve()
    return sum(
        registry.discover_directory(
            path,
            trusted_builtin=path == builtin_dir,
        )
        for path in _plugin_discovery_paths(settings)
    )


def _build_channel_registry(registry: PluginRegistry) -> ChannelRegistry:
    """Adapt plugin scope policy to the channel target execution boundary."""

    async def owner_gate(owner: str, target: ChannelTarget) -> bool:
        return await registry.scope_execution_allowed(
            owner,
            tenant_id=str(target.tenant_id or "").strip(),
            session_id=str(target.session_id or "").strip(),
        )

    return ChannelRegistry(owner_gate=owner_gate)


def _validate_startup_settings(settings: Settings) -> list[str]:
    errors: list[str] = []
    role = str(settings.app_process_role or "api").strip().lower()
    configured_install_dir = Path(str(settings.plugin_install_dir or ""))
    if not configured_install_dir.is_absolute():
        configured_install_dir = settings.project_root / configured_install_dir
    if (
        settings.allow_dynamic_plugin_mutations
        and configured_install_dir.resolve() == (settings.project_root / "plugins").resolve()
    ):
        errors.append(
            "PLUGIN_INSTALL_DIR must be separate from the trusted built-in plugins directory"
        )
    if settings.is_prod:
        if role == "api":
            configured_worker_roles = [
                item.strip().lower()
                for item in settings.readiness_required_worker_roles.split(",")
                if item.strip()
            ]
            allowed_worker_roles = {
                "inbound",
                "outbound",
                "scheduler",
                "wxbot_bridge",
            }
            unknown_worker_roles = sorted(set(configured_worker_roles) - allowed_worker_roles)
            if unknown_worker_roles:
                errors.append(
                    "READINESS_REQUIRED_WORKER_ROLES contains unknown roles: "
                    + ", ".join(unknown_worker_roles)
                )
            duplicate_worker_roles = sorted(
                {
                    worker_role
                    for worker_role in configured_worker_roles
                    if configured_worker_roles.count(worker_role) > 1
                }
            )
            if duplicate_worker_roles:
                errors.append(
                    "READINESS_REQUIRED_WORKER_ROLES contains duplicate roles: "
                    + ", ".join(duplicate_worker_roles)
                )
            missing_core_worker_roles = sorted(
                {"inbound", "outbound", "scheduler"} - set(configured_worker_roles)
            )
            if missing_core_worker_roles:
                errors.append(
                    "READINESS_REQUIRED_WORKER_ROLES must include core roles: "
                    + ", ".join(missing_core_worker_roles)
                )
        # Message-platform connectors are optional process roles. The WeChat
        # SDK used by the connector does not require a platform credential.
        if role in {"api", "outbound"} and settings.outbound_hmac_secret in {
            "change_me",
            "compose_dev_outbound_secret",
        }:
            errors.append("OUTBOUND_HMAC_SECRET must be changed in prod")
        if role == "api":
            if settings.tenant_demo_secret in {
                "demo_secret",
                "compose_dev_tenant_secret",
            }:
                errors.append("TENANT_DEMO_SECRET must be changed in prod")
            if settings.admin_bearer_token in {
                "admin_dev_token",
                "compose_dev_admin_token",
            }:
                errors.append("ADMIN_BEARER_TOKEN must be changed in prod")
            session_secret = str(settings.admin_session_signing_secret or "").strip()
            if len(session_secret) < 32 or session_secret == settings.admin_bearer_token:
                errors.append(
                    "ADMIN_SESSION_SIGNING_SECRET must be an independent 32+ character secret in prod"
                )
            media_secret = str(settings.media_id_signing_secret or "").strip()
            if len(media_secret) < 32 or media_secret in {
                settings.admin_bearer_token,
                settings.wxbot_api_token,
                settings.outbound_hmac_secret,
            }:
                errors.append(
                    "MEDIA_ID_SIGNING_SECRET must be an independent 32+ character secret in prod"
                )
            if not settings.admin_session_cookie_secure:
                errors.append("ADMIN_SESSION_COOKIE_SECURE must be enabled in prod")
            if not settings.orchestrator_flow_runtime_enabled:
                errors.append("ORCHESTRATOR_FLOW_RUNTIME_ENABLED must be enabled in prod")
            if str(settings.orchestrator_flow_runtime_name).strip() != "auto":
                errors.append("ORCHESTRATOR_FLOW_RUNTIME_NAME must be auto in prod")
            allowed_flow_names = _csv_items(settings.orchestrator_flow_runtime_allowed_names)
            if "auto" not in allowed_flow_names and "*" not in allowed_flow_names:
                errors.append("ORCHESTRATOR_FLOW_RUNTIME_ALLOWED_NAMES must allow auto in prod")
            if not settings.orchestrator_flow_runtime_allow_target_flows:
                errors.append(
                    "ORCHESTRATOR_FLOW_RUNTIME_ALLOW_TARGET_FLOWS must be enabled in prod"
                )
            if settings.orchestrator_flow_runtime_allow_compatible_fallback:
                errors.append(
                    "ORCHESTRATOR_FLOW_RUNTIME_ALLOW_COMPATIBLE_FALLBACK must be disabled in prod"
                )
            if str(settings.orchestrator_flow_effect_commit_backend).strip().lower() != "redis":
                errors.append("ORCHESTRATOR_FLOW_EFFECT_COMMIT_BACKEND must be redis in prod")
            if not settings.orchestrator_flow_effect_handlers_enabled:
                errors.append("ORCHESTRATOR_FLOW_EFFECT_HANDLERS_ENABLED must be enabled in prod")
            if str(settings.orchestrator_flow_effect_log_backend).strip().lower() not in {
                "postgres",
                "postgresql",
                "sql",
            }:
                errors.append("ORCHESTRATOR_FLOW_EFFECT_LOG_BACKEND must be postgres in prod")
            if (
                str(settings.orchestrator_flow_effect_log_failure_policy).strip().lower()
                != "fail_closed"
            ):
                errors.append(
                    "ORCHESTRATOR_FLOW_EFFECT_LOG_FAILURE_POLICY must be fail_closed in prod"
                )
        if role in {"api", "inbound", "scheduler"} and (
            not settings.agent_tools_require_explicit_policy
        ):
            errors.append("AGENT_TOOLS_REQUIRE_EXPLICIT_POLICY must be enabled in prod")

    # Outbound delivery is deliberately independent from inference.  Requiring
    # an LLM credential there coupled an egress-only replica to unrelated
    # providers and made an LLM outage prevent already-committed replies from
    # draining.
    if role in {"api", "inbound", "scheduler"}:
        errors.extend(validate_llm_settings(settings))

    return errors


def _memory_vector_startup_config_error(settings: Settings) -> str | None:
    if not settings.memory_vector_index_enabled:
        return None
    if not settings.memory_vector_index_strict_startup_check:
        return None
    if not settings.knowledge_features_enabled:
        return "memory_vector_knowledge_features_disabled"
    return None


def _enforce_startup_settings(settings: Settings) -> None:
    errors = _validate_startup_settings(settings)
    memory_vector_error = _memory_vector_startup_config_error(settings)
    if memory_vector_error:
        errors.append(memory_vector_error)
    if not errors:
        return
    msg = "; ".join(errors)
    log.error("startup.validation_failed", errors=errors)
    raise RuntimeError(f"startup configuration invalid: {msg}")


async def _load_runtime_llm_settings_for_role(settings: Settings) -> Settings:
    """Load the durable overlay only for roles that construct LLM services."""

    if not runtime_llm_overlay_enabled_for_role(settings.app_process_role):
        return settings
    return (await load_runtime_llm_config(settings)).settings


async def _probe_qdrant(url: str) -> bool:
    """Compatibility wrapper around the shared semantic Qdrant probe."""

    settings = get_settings()
    if str(url).rstrip("/") != str(settings.qdrant_url).rstrip("/"):
        settings = settings.model_copy(update={"qdrant_url": url})
    return await probe_qdrant_semantics(settings)


async def _probe_redis() -> bool:
    """Compatibility wrapper around the shared semantic Redis probe."""

    return await probe_redis_semantics()


async def _probe_db() -> bool:
    """Compatibility wrapper around the shared semantic database probe."""

    return await probe_db_semantics()


async def _probe_worker_heartbeats(settings: Settings) -> dict[str, bool]:
    roles = settings.resolved_readiness_required_worker_roles
    if not roles:
        return {}
    redis = get_redis()
    checks: dict[str, bool] = {}
    for role in roles:
        pattern = f"{settings.worker_heartbeat_key_prefix.rstrip(':')}:{quote(role, safe='')}:*"
        alive = False
        try:
            async for key in redis.scan_iter(match=pattern, count=20):
                if int(await redis.ttl(key)) <= 0:
                    continue
                payload = await redis.get(key)
                if _worker_heartbeat_payload_compatible(payload, role=role):
                    alive = True
                    break
        except Exception:
            alive = False
        checks[role] = alive
    return checks


def _worker_heartbeat_payload_compatible(payload: object, *, role: str) -> bool:
    """Accept only a ready worker that runs this API's schema contract."""

    if not isinstance(payload, (str, bytes, bytearray)):
        return False
    if len(payload) > 16 * 1024:
        return False
    try:
        decoded = orjson.loads(payload)
    except (orjson.JSONDecodeError, TypeError):
        return False
    if not isinstance(decoded, dict):
        return False
    return (
        str(decoded.get("role") or "").strip().lower() == role
        and decoded.get("state") == "ready"
        and decoded.get("schema_revision") == RUNTIME_SCHEMA_REVISION
        and decoded.get("schema_compatibility") == RUNTIME_SCHEMA_COMPATIBILITY_LEVEL
    )


def _dependency_startup_errors(
    settings: Settings,
    *,
    redis_ok: bool,
    db_ok: bool,
    qdrant_ok: bool,
) -> list[str]:
    errors: list[str] = []
    required = set(required_dependencies_for_role(settings.app_process_role, settings))
    if "redis" in required and not redis_ok:
        errors.append("redis_unreachable")
    if "db" in required and not db_ok:
        errors.append("db_unreachable")
    if "qdrant" in required and not qdrant_ok:
        errors.append("qdrant_unreachable")
    return errors


async def _build_outbound_container(settings: Settings) -> OutboundContainer:
    """Build only the dependencies required to drain the durable outbox.

    This role must stay available when inference, vector search, plugin
    discovery, or admin-only dependencies are unavailable.
    """

    redis = get_redis()
    redis_ok, db_ok = await asyncio.gather(_probe_redis(), _probe_db())
    dependency_errors = _dependency_startup_errors(
        settings,
        redis_ok=redis_ok,
        db_ok=db_ok,
        qdrant_ok=True,
    )
    if dependency_errors:
        log.error(
            "startup.dependencies_unready",
            process_role=settings.app_process_role,
            errors=dependency_errors,
        )
        raise RuntimeError("startup dependencies unavailable: " + ", ".join(dependency_errors))

    await verify_runtime_schema(
        get_engine(),
        component="outbound worker",
    )
    raw_bus = RedisStreamBus(redis, settings)
    message_store = MessageReliabilityStore()
    # Ambient HTTP(S)_PROXY variables would make the proxy, rather than this
    # process, resolve and connect to the validated destination.  That breaks
    # the DNS/redirect SSRF boundary and also makes loopback test endpoints
    # depend on machine proxy configuration.  A future explicit egress proxy
    # must implement the same destination validation contract.
    http_client = httpx.AsyncClient(
        timeout=settings.outbound_timeout_seconds,
        trust_env=False,
    )
    dispatcher = OutboundDispatcher(http_client, raw_bus, settings)
    outbox_relay = MessageOutboxRelay(
        message_store,
        raw_bus,
        worker_id=settings.resolved_outbound_worker_consumer_name,
        poll_interval_seconds=settings.outbox_relay_poll_interval_seconds,
        batch_size=settings.outbox_relay_batch_size,
        lease_seconds=settings.outbox_relay_lease_seconds,
        publish_timeout_seconds=settings.outbox_relay_publish_timeout_seconds,
        max_attempts=settings.outbox_relay_max_attempts,
    )
    return OutboundContainer(
        bus=raw_bus,
        dispatcher=dispatcher,
        http_client=http_client,
        message_store=message_store,
        outbox_relay=outbox_relay,
    )


async def _build_scheduler_container(settings: Settings) -> SchedulerContainer:
    """Build only services used by elected, scheduled plugin jobs."""

    redis_ok, db_ok = await asyncio.gather(_probe_redis(), _probe_db())
    base_dependency_errors = _dependency_startup_errors(
        settings,
        redis_ok=redis_ok,
        db_ok=db_ok,
        qdrant_ok=True,
    )
    if base_dependency_errors:
        log.error(
            "startup.dependencies_unready",
            process_role="scheduler",
            errors=base_dependency_errors,
        )
        raise RuntimeError("startup dependencies unavailable: " + ", ".join(base_dependency_errors))

    await verify_runtime_schema(
        get_engine(),
        component="scheduler worker",
    )
    settings = await _load_runtime_llm_settings_for_role(settings)
    _enforce_startup_settings(settings)
    qdrant_ok = (
        await _probe_qdrant(settings.qdrant_url) if settings.knowledge_features_enabled else True
    )
    dependency_errors = _dependency_startup_errors(
        settings,
        redis_ok=redis_ok,
        db_ok=db_ok,
        qdrant_ok=qdrant_ok,
    )
    if dependency_errors:
        log.error(
            "startup.dependencies_unready",
            process_role="scheduler",
            errors=dependency_errors,
        )
        raise RuntimeError("startup dependencies unavailable: " + ", ".join(dependency_errors))

    vector_store = None
    vector_backend = "disabled"
    kb_service = None
    llm_service = build_llm_service(settings)
    if settings.knowledge_features_enabled:
        # Worker roles never use the in-memory fallback: it would create a
        # private, divergent knowledge view in each replica.
        vector_store = QdrantVectorStore(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
        )
        vector_backend = "qdrant"
        kb_store = SQLAlchemyKBStore()
        ingest = IngestionService(kb_store, vector_store, llm_service, settings)
        kb_service = KnowledgeBaseService(kb_store, vector_store, ingest)

    if settings.memory_vector_index_enabled and settings.memory_vector_index_strict_startup_check:
        from plugins.memory.vector_index import MemoryItemVectorIndex

        startup_vector_check = await MemoryItemVectorIndex(
            settings,
            vector_store=vector_store,
            llm_service=llm_service,
        ).smoke_enable_preflight()
        if not bool(startup_vector_check.get("safe_to_enable")):
            reasons = ", ".join(str(reason) for reason in startup_vector_check.get("reasons") or [])
            raise RuntimeError(f"memory vector startup check failed: {reasons or 'unknown'}")

    agent_store = AgentStore(settings)
    await agent_store.ensure_tables()
    agent_tool_registry = AgentToolRegistry()
    billing = BillingCoordinator()
    agent_capability = AgentCapabilityEngine(
        llm_service,
        settings=settings,
        agent_store=agent_store,
        agent_tool_registry=agent_tool_registry,
        billing=billing,
        max_tool_rounds=settings.agent_max_tool_rounds,
        max_tool_calls_per_round=settings.agent_max_tool_calls_per_round,
    )
    capabilities = {RouteType.AGENT: agent_capability}

    plugin_state_store = PluginStateStore()
    registry = PluginRegistry(plugin_state_store)
    agent_capability.set_tool_owner_gate(registry.session_execution_allowed)
    channel_registry = _build_channel_registry(registry)
    dir_count = _discover_plugin_directories(registry, settings)
    ep_count = registry.discover_entrypoints()
    await registry.reconcile_state()
    log.info(
        "plugins.discovered",
        process_role="scheduler",
        directory=dir_count,
        entrypoints=ep_count,
    )

    social_policy_store = SocialPolicyStore(get_session_factory())
    plugin_ctx = PluginContext(
        container=None,
        settings=settings,
        db_ok=True,
        redis_ok=True,
    )
    plugin_manager = PluginManager(registry, plugin_state_store, plugin_ctx)
    container = SchedulerContainer(
        plugin_registry=registry,
        plugin_manager=plugin_manager,
        llm_service=llm_service,
        vector_store=vector_store,
        capabilities=capabilities,
        agent_tool_registry=agent_tool_registry,
        channel_registry=channel_registry,
        billing=billing,
        kb_service=kb_service,
        agent_store=agent_store,
        social_policy_store=social_policy_store,
        vector_backend=vector_backend,
        persistence_backend="postgres",
        knowledge_features_enabled=settings.knowledge_features_enabled,
    )
    plugin_ctx.container = container
    await registry.initialize_all(plugin_ctx)
    return container


async def build_container(settings: Settings | None = None) -> RuntimeContainer:
    s = settings or get_settings()
    role = str(s.app_process_role or "api").strip().lower()
    if role == "outbound":
        _enforce_startup_settings(s)
        return await _build_outbound_container(s)
    if role == "scheduler":
        return await _build_scheduler_container(s)
    if role not in {"api", "inbound"}:
        raise RuntimeError(f"unsupported build_container process role: {role}")
    redis = get_redis()
    redis_ok, db_ok = await asyncio.gather(_probe_redis(), _probe_db())
    if db_ok:
        await verify_runtime_schema(
            get_engine(),
            component=f"{role} process",
        )
        s = await _load_runtime_llm_settings_for_role(s)
    _enforce_startup_settings(s)
    qdrant_ok = await _probe_qdrant(s.qdrant_url) if s.knowledge_features_enabled else True
    dependency_errors = _dependency_startup_errors(
        s,
        redis_ok=redis_ok,
        db_ok=db_ok,
        qdrant_ok=qdrant_ok,
    )
    if dependency_errors:
        log.error(
            "startup.dependencies_unready",
            process_role=s.app_process_role,
            errors=dependency_errors,
        )
        raise RuntimeError("startup dependencies unavailable: " + ", ".join(dependency_errors))

    # Vector store — Qdrant if reachable, else in-process.
    vector_store: object | None
    vector_backend: str
    if not s.knowledge_features_enabled:
        vector_store = None
        vector_backend = "disabled"
        log.info("vector_store.disabled")
    else:
        if qdrant_ok:
            vector_store = QdrantVectorStore(url=s.qdrant_url, api_key=s.qdrant_api_key)
            vector_backend = "qdrant"
            log.info("vector_store.qdrant", url=s.qdrant_url)
        else:
            vector_store = InMemoryVectorStore()
            vector_backend = "memory"
            log.warning("vector_store.fallback_memory", qdrant_url=s.qdrant_url)

    # Offline API development may use in-memory knowledge stores. Production
    # and state-mutating worker roles fail before reaching this branch.
    persistence_backend: str
    if not s.knowledge_features_enabled:
        kb_store = None
        faq_repo = None
        persistence_backend = "disabled"
        log.info("persistence.disabled")
    else:
        if db_ok:
            kb_store = SQLAlchemyKBStore()
            faq_repo = SQLAlchemyFAQRepository()
            persistence_backend = "postgres"
            log.info("persistence.postgres")
        else:
            kb_store = InMemoryKBStore()
            faq_repo = InMemoryFAQRepository()
            persistence_backend = "memory"
            log.warning("persistence.fallback_memory")

    # LLM
    llm_service = build_llm_service(s)
    if s.memory_vector_index_enabled and s.memory_vector_index_strict_startup_check:
        from plugins.memory.vector_index import MemoryItemVectorIndex

        startup_vector_check = await MemoryItemVectorIndex(
            s,
            vector_store=vector_store,  # type: ignore[arg-type]
            llm_service=llm_service,
        ).smoke_enable_preflight()
        if not bool(startup_vector_check.get("safe_to_enable")):
            reasons = ", ".join(str(reason) for reason in startup_vector_check.get("reasons") or [])
            log.error(
                "memory.vector_strict_startup_check_failed",
                reasons=startup_vector_check.get("reasons"),
            )
            raise RuntimeError(f"memory vector startup check failed: {reasons or 'unknown'}")
    agent_store = AgentStore(s)
    agent_tool_registry = AgentToolRegistry()

    # Knowledge base + FAQ
    faq_store = None
    kb_service = None
    retriever = None
    if s.knowledge_features_enabled:
        ingest = IngestionService(kb_store, vector_store, llm_service, s)  # type: ignore[arg-type]
        kb_service = KnowledgeBaseService(kb_store, vector_store, ingest)  # type: ignore[arg-type]
        faq_store = FAQStore(faq_repo, vector_store, llm_service, embed_model=s.llm_embed_model)  # type: ignore[arg-type]

        async def chunk_source(tenant_id: str, session_id: str | None):
            return await kb_store.list_chunks(tenant_id, session_id)  # type: ignore[union-attr]

        retriever = HybridRetriever(vector_store, chunk_source, llm_service, s)  # type: ignore[arg-type]
        ingest.set_cache_invalidator(retriever.invalidate)

    # Core modules
    preprocessor = build_preprocessor(LlmIntentClassifier(llm_service))
    rule_router = build_rule_router(s)
    safety_raw = build_safety(s)
    postprocessor = build_postprocessor()
    session_manager = SessionManager(redis, s)

    # Capability engines
    faq_engine = None
    rag_engine = None
    if s.knowledge_features_enabled:
        faq_engine = FAQEngine(vector_store, llm_service, s, faq_store=faq_store)  # type: ignore[arg-type]
        rag_engine = RAGEngine(retriever, llm_service, s)  # type: ignore[arg-type]
    llm_capability = LLMCapabilityEngine(llm_service, settings=s)
    if db_ok:
        await agent_store.ensure_tables()
        effective_agent_store = agent_store
    else:
        effective_agent_store = None
    billing = BillingCoordinator()
    agent_capability = AgentCapabilityEngine(
        llm_service,
        settings=s,
        agent_store=effective_agent_store,
        agent_tool_registry=agent_tool_registry,
        billing=billing,
        max_tool_rounds=s.agent_max_tool_rounds,
        max_tool_calls_per_round=s.agent_max_tool_calls_per_round,
    )
    handoff_capability = HandoffCapabilityEngine(session_manager)

    capabilities = {
        RouteType.LLM: llm_capability,
        RouteType.AGENT: agent_capability,
        RouteType.HANDOFF: handoff_capability,
    }
    if faq_engine is not None:
        capabilities[RouteType.FAQ] = faq_engine
    if rag_engine is not None:
        capabilities[RouteType.RAG] = rag_engine

    # Plugins — discover before building orchestrator so plugins can
    # register capability engines and pipeline hooks.
    plugin_state_store = PluginStateStore() if db_ok else None
    registry = PluginRegistry(
        plugin_state_store,
        allow_offline_execution=plugin_state_store is not None,
    )
    agent_capability.set_tool_owner_gate(registry.session_execution_allowed)
    channel_registry = _build_channel_registry(registry)
    dir_count = _discover_plugin_directories(registry, s)
    ep_count = registry.discover_entrypoints()
    if plugin_state_store is not None:
        await registry.reconcile_state()
    log.info("plugins.discovered", directory=dir_count, entrypoints=ep_count)

    flow_step_registry = build_default_flow_registry()
    effect_handler_registry = EffectHandlerRegistry()
    register_core_session_effect_handlers(
        effect_handler_registry,
        session_manager,
        replace=True,
    )
    effect_handler_registry.register(
        "enqueue_channel_reply",
        "channel",
        ChannelReplyEffectHandler(
            channel_registry,
            owner_gate=registry.execution_allowed,
        ),
        replace=True,
    )
    # Member erasure is an always-on privacy compensation.  It is registered
    # by the kernel so disabling (or failing to initialize) the memory plugin
    # cannot strand a committed deletion request.
    from plugins.memory.store import MemoryStore

    effect_handler_registry.register(
        "forget_member",
        "core",
        MemberMemoryForgetEffectHandler(
            MemoryStore(
                s,
                llm_service=llm_service,
                vector_store=vector_store,
            ),
            SocialPolicyStore(get_session_factory()),
        ),
        replace=True,
    )

    flow_effect_log = None
    if str(s.orchestrator_flow_effect_log_backend or "none").strip().lower() in {
        "postgres",
        "postgresql",
        "sql",
    }:
        flow_effect_log = PostgresEffectLog()
        await flow_effect_log.ensure_schema()

    # PluginManager and PluginContext reference each other through the final
    # container.  Build the context first, attach a fully typed core container,
    # and promote it to the concrete process role once plugin contributions are
    # known.  No untyped attribute injection is involved.
    plugin_ctx = PluginContext(
        container=None,
        settings=s,
        db_ok=db_ok,
        redis_ok=redis_ok,
    )
    plugin_manager = (
        PluginManager(registry, plugin_state_store, plugin_ctx)
        if plugin_state_store is not None
        else None
    )
    core_container = CoreRuntimeContainer(
        session_manager=session_manager,
        preprocessor=preprocessor,
        router=rule_router,  # type: ignore[arg-type]
        postprocessor=postprocessor,
        safety=safety_raw,  # type: ignore[arg-type]
        faq_engine=faq_engine,
        rag_engine=rag_engine,
        llm_service=llm_service,
        llm_provider=llm_service,  # type: ignore[arg-type]
        vector_store=vector_store,  # type: ignore[arg-type]
        capabilities=capabilities,
        plugin_registry=registry,
        plugin_manager=plugin_manager,
        agent_tool_registry=agent_tool_registry,
        channel_registry=channel_registry,
        flow_step_registry=flow_step_registry,
        flow_step_executors={},
        flow_effect_handler_registry=effect_handler_registry,
        flow_effect_log=flow_effect_log,
        billing=billing,
        faq_store=faq_store,
        kb_service=kb_service,
        agent_store=effective_agent_store,
        vector_backend=vector_backend,
        persistence_backend=persistence_backend,
        knowledge_features_enabled=s.knowledge_features_enabled,
    )
    plugin_ctx.container = core_container
    await registry.initialize_all(plugin_ctx)

    plugin_flow_steps = registry.all_flow_steps()
    flow_step_registry.register_many(plugin_flow_steps)
    core_container.flow_step_executors.update(registry.all_flow_executors())
    plugin_effect_handlers = registry.all_effect_handlers()
    for effect_type, owner, handler in plugin_effect_handlers:
        effect_handler_registry.register(effect_type, owner, handler)
    log.info(
        "flow_steps.registered",
        plugin_steps=len(plugin_flow_steps),
        plugin_executors=len(core_container.flow_step_executors),
        plugin_effect_handlers=len(plugin_effect_handlers),
    )

    # Merge plugin capability engines after plugin initialization so plugins
    # can expose engines that depend on initialized resources.
    plugin_capabilities = registry.all_capability_engines()
    reserved_capabilities = sorted(
        str(route_type.value) for route_type in set(capabilities).intersection(plugin_capabilities)
    )
    if reserved_capabilities:
        raise RuntimeError(
            "plugin capability engines cannot replace core routes: "
            + ", ".join(reserved_capabilities)
        )
    capabilities.update(plugin_capabilities)

    # API and inbound both publish through the durable transactional outbox;
    # neither role initializes HTTP delivery or the outbox relay.
    raw_bus = RedisStreamBus(redis, s)
    message_store = MessageReliabilityStore()
    bus = TransactionalOutboxBus(
        raw_bus,
        message_store,
        outbound_stream=s.bus_outbound_stream,
    )
    register_core_publish_outbound_handler(
        effect_handler_registry,
        bus,
        default_stream=s.bus_outbound_stream,
        replace=True,
    )
    orchestrator = DialogOrchestrator(
        session_manager=session_manager,
        preprocessor=preprocessor,
        router=AsyncRouterAdapter(rule_router),
        safety=AsyncSafetyAdapter(safety_raw),
        postprocessor=postprocessor,
        capabilities=capabilities,
        bus=bus,
        settings=s,
        hook_runner=registry.hook_runner,
        flow_step_registry=flow_step_registry,
        flow_owner_permissions=registry.all_permissions(),
        flow_step_executors=core_container.flow_step_executors,
        flow_effect_handler_registry=effect_handler_registry,
        message_store=message_store,
    )
    orchestrator.plugin_registry = registry
    if role == "inbound":
        inbound_container = InboundContainer.from_core(
            core_container,
            bus=bus,
            orchestrator=orchestrator,
            message_store=message_store,
        )
        plugin_ctx.container = inbound_container
        return inbound_container

    dlq_admin_service = DLQAdminService(redis, s)
    stream_admin_service = StreamAdminService(redis, s)
    api_container = ApiContainer.from_core(
        core_container,
        bus=bus,
        orchestrator=orchestrator,
        message_store=message_store,
        dlq_admin_service=dlq_admin_service,
        stream_admin_service=stream_admin_service,
        social_policy_store=SocialPolicyStore(get_session_factory()),
    )
    plugin_ctx.container = api_container
    return api_container


def _setup_instrumentation(app: FastAPI) -> None:
    setup_tracing()
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()


def _setup_frontend_cors(app: FastAPI, settings: Settings) -> None:
    allowed_origins = settings.resolved_frontend_cors_origins
    allow_any = "*" in allowed_origins
    # Treat every environment outside the explicit local/test set as
    # production-like.  Aliases such as ``production`` and pre-production
    # environments must never inherit the credentialed reflect-any-origin
    # development policy.
    allow_dev_any = not settings.is_prod

    if not allowed_origins and not allow_dev_any:
        return

    @app.middleware("http")
    async def frontend_cors(request: Request, call_next):
        origin = request.headers.get("origin")
        is_allowed = bool(origin) and (allow_any or allow_dev_any or origin in allowed_origins)
        is_preflight = (
            request.method == "OPTIONS"
            and bool(origin)
            and "access-control-request-method" in request.headers
        )

        if is_preflight and is_allowed:
            response = Response(status_code=204)
        else:
            response = await call_next(request)

        if is_allowed and origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
            requested_headers = request.headers.get("access-control-request-headers")
            response.headers["Access-Control-Allow-Headers"] = requested_headers or "*"
            vary_value = response.headers.get("Vary")
            response.headers["Vary"] = "Origin" if not vary_value else f"{vary_value}, Origin"

        return response


def _setup_legacy_api_deprecation_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def legacy_api_deprecation(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        through_console_api = request.headers.get("x-agent-console-api-prefix") == "/api"
        if not through_console_api and path.startswith(("/v1/", "/plugins/")):
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = "Sun, 31 Jan 2027 00:00:00 GMT"
            response.headers["Link"] = f'</api{path}>; rel="successor-version"'
        return response


async def _readiness_payload(
    container: ApiContainer | Container,
    settings: Settings,
) -> tuple[int, dict[str, object]]:
    startup_errors = _validate_startup_settings(settings)
    flow_runtime = _flow_runtime_config_payload(settings)
    flow_effect_commit = _flow_effect_commit_config_payload(settings)
    redis_ok = await _probe_redis()
    worker_heartbeats = (
        await _probe_worker_heartbeats(settings)
        if redis_ok
        else {role: False for role in settings.resolved_readiness_required_worker_roles}
    )
    if settings.knowledge_features_enabled:
        db_ok, qdrant_ok = await asyncio.gather(
            _probe_db(),
            _probe_qdrant(settings.qdrant_url),
        )
    else:
        db_ok = await _probe_db()
        qdrant_ok = True

    vector_backend = str(container.vector_backend)
    persistence_backend = str(container.persistence_backend)

    errors = list(startup_errors)
    if settings.orchestrator_flow_runtime_enabled and not bool(flow_runtime.get("allowed")):
        errors.append(f"flow_runtime_{flow_runtime.get('reason')}")
    if settings.orchestrator_flow_runtime_enabled and not bool(flow_effect_commit.get("allowed")):
        errors.append(f"flow_effect_commit_{flow_effect_commit.get('reason')}")
    if not redis_ok:
        errors.append("redis_unreachable")
    for role, alive in worker_heartbeats.items():
        if not alive:
            errors.append(f"worker_{role}_heartbeat_missing")
    memory_vector_error = _memory_vector_startup_config_error(settings)
    if memory_vector_error:
        errors.append(memory_vector_error)
    if settings.memory_vector_index_enabled and settings.memory_vector_index_strict_startup_check:
        if not qdrant_ok:
            errors.append("memory_vector_qdrant_unreachable")
        if vector_backend != "qdrant":
            errors.append("memory_vector_qdrant_not_active_backend")

    if settings.is_prod:
        if not db_ok:
            errors.append("db_unreachable")
        if not qdrant_ok:
            errors.append("qdrant_unreachable")
        if persistence_backend == "memory":
            errors.append("persistence_fallback_memory")
        if vector_backend == "memory":
            errors.append("vector_store_fallback_memory")

    ready = not errors
    status_code = 200 if ready else 503
    payload: dict[str, object] = {
        "status": "ready" if ready else "not_ready",
        "env": settings.app_env,
        "checks": {
            "redis": {"ok": redis_ok},
            "workers": {role: {"ok": alive} for role, alive in worker_heartbeats.items()},
            "db": {"ok": db_ok, "backend": persistence_backend},
            "qdrant": {"ok": qdrant_ok, "backend": vector_backend},
            "knowledge_features": {"ok": True, "enabled": settings.knowledge_features_enabled},
            "flow_runtime": flow_runtime,
            "flow_shadow": _flow_shadow_config_payload(settings),
            "flow_effect_commit": flow_effect_commit,
            "flow_effect_handlers": effect_handler_registry_payload(
                container.flow_effect_handler_registry
            ),
        },
        "errors": errors,
    }
    return status_code, payload


def _csv_items(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _flow_runtime_config_payload(settings: Settings) -> dict[str, object]:
    return build_flow_runtime_config_payload(settings)


def _flow_shadow_config_payload(settings: Settings) -> dict[str, object]:
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


def _flow_effect_commit_config_payload(settings: Settings) -> dict[str, object]:
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


def _mount_routes(app: FastAPI, container: ApiContainer) -> None:
    settings = get_settings()
    app.include_router(build_ingress_router(container))
    app.include_router(build_admin_auth_router(settings))
    # The API advertises only adapters contributed by currently loaded
    # plugins. A built-in descriptor without its plugin/provider would make an
    # unavailable platform look configurable and enabled.
    channel_adapter_catalog = ChannelAdapterCatalog(
        live_registrations_provider=container.plugin_registry.all_channel_adapters
    )
    channel_connection_store = ChannelConnectionStore(
        get_session_factory(),
        channel_adapter_catalog,
    )
    app.include_router(
        build_channel_admin_router(
            channel_connection_store,
            settings,
            catalog=channel_adapter_catalog,
            legacy_settings=settings,
        )
    )
    app.include_router(
        build_admin_router(
            container.faq_store,
            container.kb_service,
            settings=settings,
            stream_service=container.stream_admin_service,
            plugin_registry=container.plugin_registry,
            faq_engine=container.faq_engine,
            plugin_manager=container.plugin_manager,
            dlq_service=container.dlq_admin_service,
            flow_step_registry=container.flow_step_registry,
            orchestrator=container.orchestrator,
            effect_handler_registry=container.flow_effect_handler_registry,
            effect_log_store=container.flow_effect_log,
            media_event_providers=(container.plugin_registry.all_admin_media_event_providers()),
            channel_connection_store=channel_connection_store,
        )
    )
    app.include_router(
        build_social_admin_router(
            container.social_policy_store,
            settings,
        )
    )

    # The shared dependency enforces both RBAC and declared tenant scope;
    # mounting it here keeps every discovered plugin fail-closed by default.
    require_plugin_admin = build_admin_authorization_dependency(settings)
    for name, router in container.plugin_registry.all_api_routers():
        app.include_router(
            router,
            prefix=f"/plugins/{name}",
            tags=[f"plugin:{name}"],
            dependencies=[
                Depends(require_plugin_admin),
                Depends(
                    _build_plugin_runtime_dependency(
                        container.plugin_registry,
                        name,
                    )
                ),
            ],
        )
    log.info("plugins.routes_mounted", count=len(container.plugin_registry.all_api_routers()))

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["meta"])
    async def readyz() -> JSONResponse:
        status_code, payload = await _readiness_payload(container, get_settings())
        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # All control-plane routes are mounted at this point.  Bind their reviewed
    # declarations and abort startup if a new admin/plugin route has no policy.
    bind_default_route_permissions(app)


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        container = await build_container(settings)
        if not isinstance(container, ApiContainer):
            if settings.is_prod or not isinstance(container, Container):
                raise RuntimeError(
                    "FastAPI lifespan requires APP_PROCESS_ROLE=api and an ApiContainer"
                )
            log.warning("container.legacy_test_compatibility")
        set_container(container)
        _mount_routes(_app, container)

        log.info("app.started", service=settings.app_service_name, env=settings.app_env)
        try:
            yield
        finally:
            log.info("app.shutting_down")

            if container.plugin_registry is not None:
                await container.plugin_registry.shutdown_all()

            if container.bus is not None:
                try:
                    await container.bus.close()
                except Exception as exc:
                    log.warning(
                        "app.bus_close_failed",
                        error_class=exc.__class__.__name__,
                    )
            if isinstance(container, Container) and container.http_client is not None:
                try:
                    await container.http_client.aclose()
                except Exception as exc:
                    log.warning(
                        "app.http_client_close_failed",
                        error_class=exc.__class__.__name__,
                    )
            await close_redis()
            await dispose_engine()
            log.info("app.stopped")

    app = FastAPI(
        title="cs-system",
        version="0.1.0",
        lifespan=lifespan,
    )
    install_admin_audit_middleware(app, settings)
    _setup_legacy_api_deprecation_headers(app)
    _setup_frontend_cors(app, settings)
    _setup_instrumentation(app)
    return app


app = create_app()
