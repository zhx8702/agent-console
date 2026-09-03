from __future__ import annotations

import os
import socket
import warnings
from collections.abc import Mapping
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

SETTINGS_ENV_FILE = os.getenv("AGENT_CONSOLE_ENV_FILE", ".env")

_NON_SETTINGS_ENV_KEYS = frozenset(
    {
        "agent_console_env_file",
        "agent_console_strict_config",
        "all_proxy",
        "ci",
        "home",
        "hostname",
        "http_proxy",
        "https_proxy",
        "lang",
        "no_proxy",
        "path",
        "shell",
        "temp",
        "term",
        "tmp",
        "tmpdir",
        "tz",
        "user",
        "username",
        "virtual_env",
        "wxbot_ascii_build_ready",
        "wxbot_build_home",
        "wxbot_build_python",
        "wxbot_config",
        "wxbot_nuitka_lto",
    }
)
_NON_SETTINGS_ENV_PREFIXES = (
    "compose_",
    "docker_",
    "github_",
    "lc_",
    "npm_",
    "pip_",
    "pnpm_",
    "poetry_",
    "pytest_",
    "python",
    "uv_",
    "vite_",
    "yarn_",
)


def _normalize_env_name(name: object) -> str:
    return str(name or "").strip().lower()


def _is_non_settings_env(name: str) -> bool:
    normalized = _normalize_env_name(name)
    return normalized in _NON_SETTINGS_ENV_KEYS or normalized.startswith(_NON_SETTINGS_ENV_PREFIXES)


def _looks_like_settings_env(name: str, known_names: frozenset[str]) -> bool:
    """Limit process-environment checks to likely Agent Console settings.

    Operating-system and CI environments contain many unrelated variables.  A
    shared environment must therefore not be treated like an app-owned dotenv
    file.  We flag a name only when it uses one of our setting namespaces or is
    a close spelling of an actual setting.
    """

    normalized = _normalize_env_name(name)
    if not normalized or normalized in known_names or _is_non_settings_env(normalized):
        return False
    parts = normalized.split("_", 2)
    known_namespaces = {"_".join(item.split("_", 2)[:2]) for item in known_names if "_" in item}
    if len(parts) >= 2 and "_".join(parts[:2]) in known_namespaces:
        return True
    return bool(get_close_matches(normalized, known_names, n=1, cutoff=0.82))


def _effective_app_env(*sources: Mapping[str, Any]) -> str:
    """Resolve APP_ENV using the same first-source-wins precedence as Settings."""

    for source in sources:
        if "app_env" in source:
            return str(source["app_env"] or "").strip().lower()
    return "dev"


def _strict_config_enabled(app_env: str, dotenv_values: Mapping[str, Any]) -> bool:
    raw = os.getenv("AGENT_CONSOLE_STRICT_CONFIG")
    if raw is None:
        raw = dotenv_values.get("agent_console_strict_config")
    if raw is not None:
        normalized = str(raw).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
    # Staging and any other non-development deployment should be fail-closed
    # too; no compatibility switch may weaken validation there.  The explicit
    # switch only lets developers opt into production-level strictness early.
    return app_env not in {"dev", "test"}


def _unknown_settings_message(
    *,
    process_names: list[str],
    dotenv_names: list[str],
    known_names: frozenset[str],
) -> str:
    rendered: list[str] = []
    for source, names in (("process environment", process_names), ("dotenv", dotenv_names)):
        for name in names:
            normalized = _normalize_env_name(name)
            match = get_close_matches(normalized, known_names, n=1, cutoff=0.6)
            suggestion = f" (did you mean {match[0].upper()}?)" if match else ""
            rendered.append(f"{source}: {normalized.upper()}{suggestion}")
    return "Unknown Agent Console setting(s): " + ", ".join(rendered)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=SETTINGS_ENV_FILE,
        env_file_encoding="utf-8",
        # Init kwargs are an explicit programming contract: never accept a
        # misspelled key silently.  Dotenv/process-env compatibility is handled
        # by the guarded source below so unrelated system variables are not
        # mistaken for application settings.
        extra="forbid",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        _ = settings_cls

        # Explicit process environment and mounted secrets must override the
        # developer-friendly dotenv file.  The previous ordering allowed a
        # checked-out .env to silently replace production-injected secrets.
        def guarded_dotenv_settings() -> dict[str, Any]:
            dotenv_values = dotenv_settings()
            filtered_dotenv_values = {
                key: value
                for key, value in dotenv_values.items()
                if _normalize_env_name(key) in cls.model_fields
            }
            init_values = init_settings()
            environment_values = env_settings()
            secret_values = file_secret_settings()
            known_names = frozenset(cls.model_fields)

            process_unknown = sorted(
                {
                    _normalize_env_name(name)
                    for name in os.environ
                    if _looks_like_settings_env(name, known_names)
                }
            )
            dotenv_unknown = sorted(
                {
                    _normalize_env_name(name)
                    for name in getattr(dotenv_settings, "env_vars", {})
                    if _normalize_env_name(name) not in known_names
                    and not _is_non_settings_env(name)
                }
            )
            if not process_unknown and not dotenv_unknown:
                return filtered_dotenv_values

            app_env = _effective_app_env(
                init_values,
                environment_values,
                secret_values,
                dotenv_values,
            )
            message = _unknown_settings_message(
                process_names=process_unknown,
                dotenv_names=dotenv_unknown,
                known_names=known_names,
            )
            if _strict_config_enabled(app_env, dotenv_values):
                raise ValueError(message)
            warnings.warn(message, UserWarning, stacklevel=3)

            # DotEnvSettingsSource includes unknown keys when extra='forbid'.
            # Development diagnoses them above, then discards them so the app
            # can still start.  Explicit init kwargs remain strictly forbidden.
            return filtered_dotenv_values

        return (
            init_settings,
            env_settings,
            file_secret_settings,
            cast(PydanticBaseSettingsSource, guarded_dotenv_settings),
        )

    app_env: str = "dev"
    app_log_level: str = "INFO"
    app_service_name: str = "agent-console"
    app_process_role: Literal[
        "api",
        "inbound",
        "outbound",
        "scheduler",
        "wxbot_bridge",
    ] = "api"
    scheduler_lease_key: str = "agent-console:scheduler:leader"
    scheduler_lease_ttl_seconds: int = Field(default=30, ge=3)
    scheduler_lease_acquire_timeout_seconds: float = Field(default=30.0, gt=0)
    scheduler_lease_poll_interval_seconds: float = Field(default=1.0, gt=0)

    db_dsn: str = "postgresql+asyncpg://cs:cs@localhost:5432/cs"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    redis_url: str = "redis://localhost:6379/0"

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None

    llm_provider: str = "fake"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    # Grok-compatible gateways commonly expose these names. They are mapped
    # onto the existing OpenAI-compatible provider below.
    xai_api_key: str | None = None
    grok_models_base_url: str | None = None
    openai_api_mode: str = "responses"
    openai_disable_fallback: bool = False
    openai_web_search_enabled: bool = False
    openai_web_search_tool: str = "web_search"
    openai_web_search_live_enabled: bool = True
    llm_embed_provider: str = "fake"
    knowledge_features_enabled: bool = True
    customer_service_prompt_enabled: bool = True
    response_guard_bot_aliases: str = "zzz"
    llm_model_tier1: str = "gpt-5.5"
    llm_model_tier2: str = "gpt-5.5"
    llm_model_tier3: str = "gpt-5.5"
    # Keep ordinary turns bounded; explicit live-search and tool contracts can
    # still opt in through request metadata.
    llm_context_budget_chars: int = Field(default=12_000, ge=2_000, le=100_000)
    agent_tool_result_max_chars: int = Field(default=6_000, ge=1_000, le=30_000)
    llm_embed_model: str = "voyage-3"
    tenant_default_daily_tokens: int = 1_000_000
    rag_vector_relevance_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    rag_keyword_overlap_threshold: float = Field(default=0.12, ge=0.0, le=1.0)
    rag_rerank_enabled: bool = True
    rag_citation_validation_enabled: bool = True
    rag_citation_repair_enabled: bool = True
    rag_citation_support_threshold: float = Field(default=0.08, ge=0.0, le=1.0)
    persona_extract_llm_backend: str = "http"
    persona_extract_stage_timeout_seconds: float = Field(default=120.0, gt=0)
    # Provider implementations own immediate network retries. Persona jobs
    # retry durably with a new lease, so the stage wrapper must not multiply
    # provider attempts inside one worker claim.
    persona_extract_stage_max_retries: int = Field(default=0, ge=0, le=1)
    persona_extract_stage_retry_backoff_seconds: float = Field(default=2.0, gt=0)
    persona_extract_job_max_attempts: int = Field(default=3, ge=1, le=10)
    persona_extract_job_retry_backoff_seconds: float = Field(default=30.0, gt=0)
    persona_extract_job_lease_seconds: float = Field(default=180.0, ge=30.0)
    persona_extract_job_heartbeat_seconds: float = Field(default=30.0, ge=1.0)
    persona_extract_job_poll_interval_seconds: float = Field(default=2.0, ge=0.1)
    persona_extract_worker_roles: str = "scheduler"
    persona_extract_chunk_max_tokens: int = Field(default=8_000, ge=1_000, le=32_000)
    persona_extract_chunk_max_messages: int = Field(default=400, ge=10, le=2_000)
    persona_extract_chunk_concurrency: int = Field(default=3, ge=1, le=8)
    persona_extract_aggregate_max_items: int = Field(default=80, ge=10, le=200)
    persona_extract_knowledge_sample_max_chars: int = Field(
        default=50_000,
        ge=5_000,
        le=200_000,
    )
    persona_extract_online_max_messages: int = Field(
        default=10_000,
        ge=100,
        le=50_000,
    )
    persona_extract_offline_export_dir: str = "/data/config/persona-exports"
    persona_extract_offline_export_timeout_seconds: float = Field(
        default=600.0,
        ge=30.0,
        le=3_600.0,
    )
    persona_extract_offline_export_max_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=1024 * 1024,
        le=1024 * 1024 * 1024,
    )
    persona_extract_offline_retention_seconds: int = Field(
        default=7 * 24 * 60 * 60,
        ge=3_600,
        le=30 * 24 * 60 * 60,
    )
    wxbot_report_llm_backend: str = "http"
    wxbot_self_review_llm_backend: str = ""
    wxbot_report_stage_timeout_seconds: float = 240.0
    wxbot_report_max_chars_per_chunk: int = Field(default=12_000, ge=1000)
    wxbot_report_transient_backoff_seconds: float = Field(default=900.0, gt=0)
    wxbot_daily_report_footer: str = ""
    orchestrator_handle_timeout_seconds: float = Field(default=150.0, gt=0)
    orchestrator_flow_runtime_enabled: bool = False
    orchestrator_flow_runtime_name: str = "default_compatible_flow"
    orchestrator_flow_runtime_allowed_names: str = "default_compatible_flow"
    orchestrator_flow_runtime_allow_target_flows: bool = False
    # `auto` may resolve to the legacy-compatible profile for an unmatched
    # message shape.  Production keeps this off so an unmodelled shape fails
    # closed instead of silently re-entering the dual-track hook pipeline.
    orchestrator_flow_runtime_allow_compatible_fallback: bool = False
    orchestrator_flow_effect_commit_backend: str = "none"
    orchestrator_flow_effect_commit_ttl_seconds: int = Field(default=604_800, ge=1)
    orchestrator_flow_effect_commit_key_prefix: str = "cs:flow:effect"
    orchestrator_flow_effect_commit_stream: str = "cs:flow:effects"
    orchestrator_flow_effect_handlers_enabled: bool = False
    orchestrator_flow_effect_handler_allowlist: str = ""
    orchestrator_flow_effect_log_backend: str = "none"
    orchestrator_flow_effect_log_failure_policy: str = "fail_closed"
    orchestrator_flow_trace_snapshot_enabled: bool = True
    orchestrator_flow_trace_snapshot_ttl_seconds: int = Field(default=604_800, ge=1)
    orchestrator_flow_trace_snapshot_key_prefix: str = "cs:flow:trace"
    orchestrator_flow_trace_snapshot_timeout_seconds: float = Field(
        default=0.25,
        ge=0.05,
        le=5.0,
    )
    orchestrator_flow_shadow_enabled: bool = False
    orchestrator_flow_shadow_name: str = "default_compatible_flow"
    orchestrator_flow_shadow_mode: str = "noop"
    orchestrator_flow_shadow_core_preview_enabled: bool = False
    orchestrator_flow_shadow_plugin_dry_run_enabled: bool = False
    orchestrator_flow_shadow_effect_dry_run_enabled: bool = False
    memory_llm_extraction_enabled: bool = False
    memory_llm_extraction_timeout_seconds: float = Field(default=1.0, gt=0)
    memory_llm_extraction_max_actions: int = Field(default=4, ge=1)
    memory_llm_extraction_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    memory_acceptance_auto_accept_min: float = Field(default=0.78, ge=0.0, le=1.0)
    memory_acceptance_reject_below: float = Field(default=0.35, ge=0.0, le=1.0)
    memory_llm_extraction_job_enabled: bool = True
    memory_llm_extraction_job_drain_enabled: bool = False
    memory_llm_extraction_job_scope_allowlist: str = ""
    memory_llm_extraction_job_drain_batch_size: int = Field(default=5, ge=1)
    memory_llm_extraction_job_drain_max_claims: int = Field(default=0, ge=0)
    memory_llm_extraction_job_max_attempts: int = Field(default=3, ge=1)
    memory_llm_extraction_job_backoff_seconds: float = Field(default=30.0, gt=0)
    memory_llm_extraction_job_timeout_seconds: float = Field(default=5.0, gt=0)
    memory_llm_extraction_job_lock_ttl_seconds: float = Field(default=60.0, gt=0)
    memory_llm_extraction_job_drain_interval_seconds: float = Field(default=5.0, gt=0)
    memory_retrieval_enabled: bool = True
    # Identity-wide memory can contain facts learned in private conversations.
    # Group sessions therefore use only the current group's session-scoped
    # memory unless an operator explicitly accepts the audience expansion.
    memory_group_identity_memory_enabled: bool = False
    memory_hybrid_retrieval_enabled: bool = False
    memory_retrieval_top_k: int = Field(default=6, ge=1, le=20)
    memory_retrieval_budget_chars: int = Field(default=1600, ge=300, le=8000)
    memory_vector_index_enabled: bool = False
    memory_vector_collection: str = "agent_console_memory_items"
    memory_vector_size: int = Field(default=64, ge=1)
    memory_vector_embed_model: str = ""
    memory_vector_timeout_seconds: float = Field(default=2.0, gt=0)
    memory_vector_top_k: int = Field(default=12, ge=1, le=100)
    memory_graph_vector_top_k: int = Field(default=12, ge=1, le=100)
    memory_vector_index_strict_startup_check: bool = False
    memory_graph_retrieval_enabled: bool = False
    memory_graph_retrieval_fact_top_k: int = Field(default=3, ge=1, le=10)
    memory_graph_retrieval_episode_top_k: int = Field(default=2, ge=1, le=10)
    memory_graph_retrieval_budget_chars: int = Field(default=600, ge=100, le=3000)
    memory_graph_llm_extraction_enabled: bool = False
    memory_graph_llm_extraction_timeout_seconds: float = Field(default=1.0, gt=0)
    memory_graph_llm_extraction_max_actions: int = Field(default=16, ge=1)
    memory_graph_llm_extraction_max_entities: int = Field(default=8, ge=1)
    memory_graph_llm_extraction_max_facts: int = Field(default=4, ge=1)
    memory_graph_llm_extraction_max_episodes: int = Field(default=2, ge=0)
    memory_graph_llm_extraction_min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    memory_governance_auto_cleanup_enabled: bool = True
    memory_governance_interval_seconds: float = Field(default=86_400.0, gt=0)
    memory_group_graph_auto_extract_enabled: bool = False
    memory_group_graph_auto_extract_llm_enabled: bool = False
    memory_group_graph_auto_extract_interval_seconds: float = Field(default=3_600.0, gt=0)
    memory_group_graph_auto_extract_lookback_days: int = Field(default=7, ge=1, le=14)
    memory_group_graph_auto_extract_max_sessions_per_tick: int = Field(default=10, ge=1, le=20)
    memory_group_graph_auto_extract_max_windows_per_session: int = Field(default=20, ge=1, le=100)
    memory_group_graph_auto_extract_window_size: int = Field(default=50, ge=10, le=100)
    memory_group_graph_auto_extract_time_budget_seconds: int = Field(default=180, ge=5, le=180)
    memory_group_graph_auto_extract_roles: str = "scheduler"
    memory_group_graph_auto_extract_sync_enabled: bool = False
    memory_group_graph_auto_extract_sync_max_messages: int = Field(default=200, ge=20, le=500)
    memory_needs_review_retention_days: int = Field(default=30, ge=1)
    memory_rejected_retention_days: int = Field(default=7, ge=1)
    memory_auto_expire_days: int = Field(default=180, ge=1)
    memory_governance_batch_size: int = Field(default=500, ge=1, le=5000)
    agent_max_tool_rounds: int = Field(default=5, ge=1)
    agent_max_tool_calls_per_round: int = Field(default=4, ge=1)
    agent_required_web_search_timeout_seconds: float = Field(
        default=90.0,
        ge=5.0,
        le=120.0,
    )
    agent_required_web_search_max_output_tokens: int = Field(
        default=2_400,
        ge=512,
        le=8_192,
    )
    agent_required_web_search_max_attempts: int = Field(default=2, ge=1, le=2)
    agent_tools_require_explicit_policy: bool = True

    inbound_signature_window_seconds: int = 300
    inbound_max_body_bytes: int = 1_048_576
    inbound_idempotency_ttl_seconds: int = 86_400
    inbound_default_rate_limit: int = 50

    outbound_webhook_url: str = "http://localhost:9999/deliver"
    outbound_hmac_secret: str = "change_me"
    # JSON objects injected by the runtime secret provider, keyed by tenant.
    # Once either outbound map is configured, delivery fails closed for tenants
    # missing an explicit destination or signing key.
    tenant_outbound_webhook_urls: dict[str, str] = Field(default_factory=dict)
    tenant_outbound_hmac_secrets: dict[str, str] = Field(default_factory=dict)
    # Reliability controls are centralized here so every worker role observes
    # the same retry, timeout, backoff, and pending-claim contract.
    outbound_max_retries: int = Field(default=5, ge=1, le=1000)
    outbound_timeout_seconds: float = Field(default=10.0, gt=0)
    outbound_transport_max_attempts: int = Field(default=3, ge=1, le=20)
    outbound_transport_retry_base_seconds: float = Field(default=0.2, ge=0)
    outbound_transport_retry_max_seconds: float = Field(default=2.0, ge=0)
    outbox_relay_poll_interval_seconds: float = Field(default=1.0, gt=0)
    outbox_relay_batch_size: int = Field(default=50, ge=1, le=1000)
    outbox_relay_lease_seconds: int = Field(default=30, ge=3)
    outbox_relay_publish_timeout_seconds: float = Field(default=10.0, gt=0)
    outbox_relay_max_attempts: int = Field(default=12, ge=1, le=1000)
    effect_intent_relay_poll_interval_seconds: float = Field(default=0.5, gt=0)
    effect_intent_relay_batch_size: int = Field(default=32, ge=1, le=1000)
    effect_intent_relay_lease_seconds: int = Field(default=30, ge=3)
    effect_intent_relay_handler_timeout_seconds: float = Field(default=20.0, gt=0)
    effect_intent_relay_max_attempts: int = Field(default=12, ge=1, le=1000)
    inbox_processing_lease_seconds: float = Field(default=180.0, ge=10.0)
    # Empty disables legacy platform-admin bearer login. Development launchers
    # generate a per-checkout secret; deployments must inject their own.
    admin_bearer_token: str = ""
    # Optional delegated control-plane identities. The JSON value is a list of
    # claims containing only SHA-256 token digests (never plaintext tokens),
    # subject, roles, tenant_ids and group_ids.
    admin_principal_tokens_json: str = ""
    # Required when delegated identities are used without the legacy platform
    # admin bearer; supplied by the runtime Secret Provider.
    admin_session_signing_secret: str = ""
    admin_session_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    admin_session_cookie_name: str = "agent_console_admin_session"
    admin_session_cookie_secure: bool = False
    admin_allow_bearer_fallback: bool = True
    wxbot_api_token: str = ""
    media_id_signing_secret: str = ""
    wxbot_auth_server_url: str = ""
    wxbot_auth_admin_key: str = ""
    # Optional control-plane connection selected by a connector worker.  When
    # empty, the worker keeps the one-release legacy WXBOT_* environment path.
    channel_connection_id: str = ""
    # Comma-separated infrastructure origins approved to receive connector
    # credentials.  The legacy WXBOT_SDK_URL origin is always included.
    channel_allowed_sdk_origins: str = ""
    wxbot_sdk_url: str = "http://127.0.0.1:5080"
    wxbot_default_tenant_id: str = "default"
    wxbot_bridge_poll_interval: float = 3.0
    wxbot_bridge_send_interval: float = 2.0
    wxbot_bridge_max_message_age_seconds: int = 1_800
    wxbot_group_reply_cooldown_seconds: float = Field(default=1.0, ge=0.0, le=30.0)
    # Emergency compatibility adapter only. Public social policy remains the
    # fail-closed runtime authority unless this is explicitly enabled.
    social_policy_legacy_wxbot_fallback_enabled: bool = False
    wxbot_group_context_enabled: bool = True
    wxbot_group_context_recent_limit: int = Field(default=80, ge=10, le=500)
    wxbot_group_context_budget_chars: int = Field(default=6000, ge=1000, le=20000)
    wxbot_group_summary_enabled: bool = True
    # Recording observations is cheap and also feeds participation policy.  A
    # rolling-summary LLM call is only useful when the bot was addressed by
    # default; set this false to retain the old all-messages scheduling mode.
    wxbot_group_summary_only_when_addressed: bool = True
    wxbot_group_summary_debounce_seconds: float = Field(default=20.0, ge=0.0, le=300.0)
    wxbot_group_summary_drain_interval_seconds: float = Field(default=2.0, gt=0.0)
    wxbot_group_summary_batch_size: int = Field(default=80, ge=1, le=500)
    wxbot_group_summary_input_budget_chars: int = Field(
        default=12_000,
        ge=2000,
        le=100_000,
    )
    wxbot_group_summary_observation_max_chars: int = Field(
        default=800,
        ge=200,
        le=4000,
    )
    wxbot_group_summary_old_summary_max_chars: int = Field(
        default=2000,
        ge=500,
        le=12000,
    )
    wxbot_group_summary_max_output_tokens: int = Field(
        default=600,
        ge=128,
        le=4000,
    )
    wxbot_group_summary_max_chars: int = Field(default=2500, ge=500, le=12000)
    wxbot_group_summary_timeout_seconds: float = Field(default=90.0, gt=0.0)
    wxbot_group_summary_lock_ttl_seconds: float = Field(default=180.0, gt=0.0)
    wxbot_group_summary_retry_backoff_seconds: float = Field(default=15.0, gt=0.0)
    wxbot_group_observation_retention_days: int = Field(default=30, ge=1, le=3650)
    wxbot_group_observation_prune_interval_seconds: float = Field(
        default=3600.0,
        gt=0.0,
    )
    wxbot_group_reply_coalesce_window_ms: int = Field(default=250, ge=0, le=5000)
    wxbot_group_reply_adaptive_cooldown_enabled: bool = True
    wxbot_group_reply_burst_window_seconds: float = Field(default=10.0, gt=0, le=120.0)
    wxbot_group_reply_adaptive_cooldown_max_seconds: float = Field(default=8.0, ge=0.0, le=60.0)
    wxbot_media_base_url: str = ""
    wxbot_file_download_max_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=4 * 1024 * 1024 * 1024,
    )
    # Agent-side text inspection/conversion is intentionally much smaller
    # than the raw SDK download allowance to keep prompt/tool work bounded.
    wxbot_file_analysis_max_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1024 * 1024,
        le=100 * 1024 * 1024,
    )
    wxbot_pending_media_ttl_seconds: int = Field(default=15 * 60, ge=60, le=24 * 60 * 60)
    wxbot_pending_media_max_items: int = Field(default=1000, ge=1, le=100_000)
    # Outbound artifacts must live at the same absolute path in Agent Console
    # and the companion SDK container.  The SDK receives this path verbatim.
    wxbot_outbound_file_dir: str = "/data/wxbot-outbound"
    wxbot_outbound_file_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=64 * 1024,
        le=100 * 1024 * 1024,
    )
    wxbot_outbound_file_retention_seconds: int = Field(
        default=7 * 24 * 60 * 60,
        ge=5 * 60,
        le=30 * 24 * 60 * 60,
    )
    wxbot_outbound_file_cleanup_grace_seconds: int = Field(
        default=5 * 60,
        ge=0,
        le=60 * 60,
    )
    wxbot_preview_wait_seconds: float = 30.0
    wxbot_preview_poll_interval_seconds: float = 0.7
    # Compatibility flag for existing env files. Runtime enablement is
    # plugin_state; a missing API URL is reported as unconfigured.
    tibo_reset_enabled: bool = False
    tibo_reset_api_url: str = ""
    tibo_reset_poll_interval_seconds: float = Field(default=300.0, ge=30.0)
    tibo_reset_request_timeout_seconds: float = Field(default=15.0, gt=0)
    tibo_reset_timezone: str = "UTC"
    draw_api_url: str = ""
    draw_api_edit_url: str = ""
    draw_api_key: str = ""
    draw_api_model: str = ""
    draw_api_provider: str = ""
    draw_api_timeout_seconds: float = 600.0
    draw_api_key_header: str = "Authorization"
    draw_api_key_prefix: str = "Bearer "
    draw_api_prompt_field: str = "prompt"
    draw_api_model_field: str = "model"
    draw_api_response_format: str = ""
    draw_api_extra_body: str = ""
    draw_fallback_api_url: str = ""
    draw_fallback_api_edit_url: str = ""
    draw_fallback_api_key: str = ""
    draw_fallback_api_model: str = ""
    draw_fallback_api_provider: str = ""
    draw_fallback_api_timeout_seconds: float = 600.0
    draw_fallback_api_key_header: str = "Authorization"
    draw_fallback_api_key_prefix: str = "Bearer "
    draw_fallback_api_prompt_field: str = "prompt"
    draw_fallback_api_model_field: str = "model"
    draw_fallback_api_response_format: str = ""
    draw_fallback_api_extra_body: str = ""
    draw_storage_dir: str = "/mnt/c/Users/Public/agent-console-draw"
    video_api_url: str = ""
    video_api_key: str = ""
    video_api_model: str = "grok-imagine-video-1.5-preview"
    video_api_timeout_seconds: float = 600.0
    video_api_poll_interval_seconds: float = Field(default=5.0, gt=0)
    video_api_poll_timeout_seconds: float = Field(default=1800.0, gt=0)
    video_api_key_header: str = "Authorization"
    video_api_key_prefix: str = "Bearer "
    video_api_extra_body: str = ""
    video_storage_dir: str = "/mnt/c/Users/Public/agent-console-video"
    local_agent_base_url: str = ""
    local_agent_token: str = ""
    local_agent_probe_timeout_seconds: float = Field(default=5.0, gt=0)
    local_agent_probe_cache_seconds: float = Field(default=15.0, gt=0)
    local_agent_task_timeout_seconds: float = Field(default=600.0, gt=0)
    local_agent_worker_roles: str = "scheduler"
    local_agent_job_poll_interval_seconds: float = Field(default=2.0, gt=0)
    local_agent_job_lock_ttl_seconds: float = Field(default=120.0, gt=0)
    local_agent_job_batch_size: int = Field(default=3, ge=1)
    local_agent_overflow_enabled: bool = True
    local_agent_overflow_min_chars: int = Field(default=24000, ge=1000, le=200000)
    local_agent_overflow_backend: str = "auto"
    speaker_portrait_llm_backend: str = "grok"
    speaker_portrait_timeout_seconds: float = Field(default=900.0, gt=0)
    speaker_portrait_max_chars: int = Field(default=80000, ge=4000, le=200000)
    speaker_portrait_full_max_chars: int = Field(default=80000, ge=4000, le=200000)
    speaker_portrait_worker_roles: str = "scheduler"
    speaker_portrait_hot_update_enabled: bool = True
    speaker_portrait_hot_update_min_messages: int = Field(default=40, ge=1)
    speaker_portrait_hot_update_min_seconds: float = Field(default=3600.0, ge=60)
    speaker_portrait_style_sync_enabled: bool = True
    speaker_portrait_data_dir: str = "/data/portraits"
    speaker_portrait_host_dir: str = "/opt/agent-console-portraits"
    speaker_portrait_inline_max_messages: int = Field(default=400, ge=50)
    speaker_portrait_max_turns: int = Field(default=40, ge=8, le=80)
    draw_task_stale_seconds: float = Field(default=3600.0, gt=0)
    draw_task_recovery_enabled: bool = True
    draw_task_recovery_interval_seconds: float = Field(default=60.0, gt=0)
    draw_task_queue_worker_enabled: bool = True
    draw_task_queue_interval_seconds: float = Field(default=5.0, gt=0)
    draw_task_queue_batch_size: int = Field(default=5, ge=1)
    draw_task_lock_ttl_seconds: float = Field(default=900.0, gt=0)
    draw_task_auto_retry_enabled: bool = False
    draw_task_max_retries: int = Field(default=3, ge=0)
    draw_task_retry_backoff_seconds: float = Field(default=0.0, ge=0)
    moderation_webhook_allowed_hosts: str = "qyapi.weixin.qq.com"
    amap_api_key: str = ""
    amap_api_timeout_seconds: float = 30.0
    amap_storage_dir: str = "/mnt/c/Users/Public/agent-console-amap"
    amap_search_credit_cost: int = Field(default=2, ge=0)
    amap_map_credit_cost: int = Field(default=8, ge=0)
    amap_route_map_credit_cost: int = Field(default=12, ge=0)
    frontend_cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    tenant_demo_secret: str = "demo_secret"
    tenant_inbound_secrets: dict[str, str] = Field(default_factory=dict)

    session_window_turns: int = 20
    session_ttl_seconds: int = 1800
    # Must comfortably exceed normal request latency.  SessionManager also
    # renews this lease, so long-running model/tool calls cannot overlap.
    session_lock_ttl_seconds: int = 180
    session_lock_lost_max_retries: int = Field(default=1, ge=0, le=3)
    session_lock_retry_backoff_seconds: float = Field(default=0.05, ge=0.0, le=5.0)

    router_config_path: str = "config/router.yaml"
    plugin_marketplace_path: str = "config/plugin-marketplace.yaml"
    # Runtime-installed packages must never share the image-owned built-in
    # directory; otherwise startup discovery could mistake mutable code for a
    # trusted built-in before durable approval.
    plugin_install_dir: str = ".runtime/plugins"
    # Production images are immutable. Global plugin lifecycle mutation
    # (install/upgrade/uninstall/enable/disable) is development-only; scoped
    # tenant/session policy remains independently mutable.
    plugin_dynamic_mutations_enabled: bool = True
    safety_keywords_path: str = "config/safety_keywords.txt"
    safety_block_pii_output: bool = True

    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_insecure: bool = True
    otel_traces_sampler_arg: float = 1.0

    # Bus / topics
    bus_inbound_stream: str = "cs:inbound"
    bus_outbound_stream: str = "cs:outbound"
    bus_dlq_stream: str = "cs:dlq"
    bus_consumer_group: str = "cs-workers"
    bus_consume_batch_size: int = Field(default=16, ge=1)
    bus_consume_block_ms: int = Field(default=5_000, ge=1)
    bus_max_attempts: int = Field(default=5, ge=1, le=1000)
    bus_retry_base_seconds: float = Field(default=1.0, ge=0)
    bus_retry_max_seconds: float = Field(default=30.0, ge=0)
    # Must exceed the longest normal orchestration window to avoid reclaiming
    # a message that another live worker is still processing.
    bus_pending_claim_idle_ms: int = Field(default=300_000, ge=1_000)
    worker_shutdown_timeout_seconds: float = Field(default=15.0, gt=0)
    worker_instance_id: str | None = None
    # A zero port keeps the standalone worker metrics endpoint disabled.  The
    # production Compose contract binds the endpoint only to its private
    # service network, where the colocated OTel collector scrapes it.
    worker_metrics_host: str = Field(default="127.0.0.1", min_length=1)
    worker_metrics_port: int = Field(default=0, ge=0, le=65_535)
    inbound_worker_consumer_name: str | None = None
    outbound_worker_consumer_name: str | None = None
    worker_heartbeat_key_prefix: str = "agent-console:worker:heartbeat"
    worker_heartbeat_interval_seconds: float = Field(default=5.0, gt=0)
    worker_heartbeat_ttl_seconds: int = Field(default=20, ge=3)
    readiness_required_worker_roles: str = ""

    @model_validator(mode="after")
    def _validate_environment_safety(self) -> Settings:
        if self.xai_api_key:
            self.openai_api_key = self.xai_api_key
        if self.grok_models_base_url:
            self.openai_base_url = self.grok_models_base_url.strip()
        if self.bus_retry_max_seconds < self.bus_retry_base_seconds:
            raise ValueError(
                "bus_retry_max_seconds must be greater than or equal to bus_retry_base_seconds"
            )
        if self.outbound_transport_retry_max_seconds < self.outbound_transport_retry_base_seconds:
            raise ValueError(
                "outbound_transport_retry_max_seconds must be greater than or "
                "equal to outbound_transport_retry_base_seconds"
            )
        if (
            self.memory_llm_extraction_job_lock_ttl_seconds
            <= self.memory_llm_extraction_job_timeout_seconds
        ):
            raise ValueError(
                "memory_llm_extraction_job_lock_ttl_seconds must be greater than "
                "memory_llm_extraction_job_timeout_seconds"
            )
        if self.tibo_reset_enabled and not self.tibo_reset_api_url.strip():
            raise ValueError("TIBO_RESET_API_URL is required when TIBO_RESET_ENABLED is true")
        if self.is_prod and self.orchestrator_flow_effect_handlers_enabled:
            commit_backend = self.orchestrator_flow_effect_commit_backend.strip().lower()
            if commit_backend not in {"redis", "memory"}:
                raise ValueError("production effect handlers require an effect commit backend")
            log_backend = self.orchestrator_flow_effect_log_backend.strip().lower()
            if log_backend not in {"postgres", "postgresql", "sql"}:
                raise ValueError(
                    "production effect handlers require a durable PostgreSQL effect log"
                )
            if self.orchestrator_flow_effect_log_failure_policy.strip().lower() != ("fail_closed"):
                raise ValueError("production effect handlers require fail_closed effect logging")
        return self

    @field_validator("memory_llm_extraction_job_drain_max_claims", mode="before")
    @classmethod
    def _empty_memory_job_drain_max_claims_is_unlimited(cls, value: object) -> object:
        if value == "":
            return 0
        return value

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def is_prod(self) -> bool:
        # Treat every deployment environment outside the two explicit local
        # modes as production-like.  This matches strict-config resolution and
        # fails closed for common names such as "production" and "staging".
        return _normalize_env_name(self.app_env) not in {"dev", "test"}

    @property
    def allow_dynamic_plugin_mutations(self) -> bool:
        return self.plugin_dynamic_mutations_enabled and not self.is_prod

    @property
    def resolved_worker_instance_id(self) -> str:
        if self.worker_instance_id:
            return self.worker_instance_id
        return f"{socket.gethostname()}-{os.getpid()}"

    @property
    def resolved_inbound_worker_consumer_name(self) -> str:
        if self.inbound_worker_consumer_name:
            return self.inbound_worker_consumer_name
        return f"inbound-{self.resolved_worker_instance_id}"

    @property
    def resolved_outbound_worker_consumer_name(self) -> str:
        if self.outbound_worker_consumer_name:
            return self.outbound_worker_consumer_name
        return f"egress-{self.resolved_worker_instance_id}"

    @property
    def resolved_readiness_required_worker_roles(self) -> list[str]:
        allowed = {"inbound", "outbound", "scheduler", "wxbot_bridge"}
        roles = (item.strip().lower() for item in self.readiness_required_worker_roles.split(","))
        return list(dict.fromkeys(role for role in roles if role in allowed))

    def get_tenant_secret(self, tenant_id: str) -> str | None:
        normalized = str(tenant_id or "").strip()
        if self.tenant_inbound_secrets:
            secret = self.tenant_inbound_secrets.get(normalized)
            return str(secret).strip() if secret else None
        # One-release compatibility for the original single demo tenant.
        if normalized == "demo":
            return self.tenant_demo_secret
        return None

    def get_tenant_outbound_webhook_url(self, tenant_id: str) -> str | None:
        normalized = str(tenant_id or "").strip()
        if self.tenant_outbound_webhook_urls or self.tenant_outbound_hmac_secrets:
            value = self.tenant_outbound_webhook_urls.get(normalized)
            return str(value).strip() if value else None
        return str(self.outbound_webhook_url or "").strip() or None

    def get_tenant_outbound_hmac_secret(self, tenant_id: str) -> str | None:
        normalized = str(tenant_id or "").strip()
        if self.tenant_outbound_webhook_urls or self.tenant_outbound_hmac_secrets:
            value = self.tenant_outbound_hmac_secrets.get(normalized)
            return str(value).strip() if value else None
        return str(self.outbound_hmac_secret or "").strip() or None

    @property
    def resolved_frontend_cors_origins(self) -> list[str]:
        raw = self.frontend_cors_origins.strip()
        if not raw:
            return []
        if raw == "*":
            return ["*"]
        return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
