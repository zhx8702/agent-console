from __future__ import annotations

import re
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

RUNTIME_SCHEMA_REVISION = "0042_wxbot_report_delivery_ack"
RUNTIME_SCHEMA_CONTRACT_NAME = "agent-console-runtime"
# 0042 persists the SDK outbound row for report delivery reconciliation and is
# not compatible with workers that treat an interrupted send as retryable.
RUNTIME_SCHEMA_COMPATIBILITY_LEVEL = 6

# Tables historically created by application/plugin startup code.  They are
# now owned by Alembic and verified as one contract so a partially migrated
# deployment cannot start with only the tables needed by its current role.
RUNTIME_SCHEMA_TABLES = frozenset(
    {
        "app_schema_contract",
        "channel_connection",
        "plugin_agent_session_policy",
        "plugin_agent_tool_audit",
        "plugin_admin_mutation_audit",
        "plugin_admin_mutation_idempotency",
        "plugin_command_center_config",
        "plugin_credits_balance",
        "plugin_credits_checkin",
        "plugin_credits_config",
        "plugin_credits_ledger",
        "plugin_credits_reservation",
        "plugin_draw_task",
        "plugin_events",
        "plugin_group_activity_config",
        "plugin_group_activity_event",
        "plugin_lifecycle_operation",
        "plugin_memory_acceptance_audit",
        "plugin_memory_entity",
        "plugin_memory_episode",
        "plugin_memory_event",
        "plugin_memory_extraction_job",
        "plugin_memory_fact",
        "plugin_memory_identity_profile",
        "plugin_memory_item",
        "plugin_memory_session_profile",
        "plugin_moderation_config",
        "plugin_moderation_events",
        "plugin_moderation_keywords",
        "plugin_persona_jobs",
        "plugin_persona_job_chunks",
        "plugin_persona_profiles",
        "plugin_repeater_config",
        "plugin_repeater_event",
        "plugin_scope_state",
        "plugin_state",
        "plugin_tibo_reset_delivery",
        "plugin_tibo_reset_feed",
        "plugin_tibo_reset_sync_state",
        "plugin_wxbot_group_observations",
        "plugin_wxbot_group_membership",
        "plugin_wxbot_admin_mutation_state",
        "plugin_wxbot_admin_resource_version",
        "plugin_wxbot_group_summary_jobs",
        "plugin_wxbot_group_summary_state",
        "plugin_wxbot_interaction_cursor",
        "plugin_wxbot_media_ready_events",
        "plugin_wxbot_member_events",
        "plugin_wxbot_reply_queue",
        "plugin_wxbot_reply_policy_aggregate_state",
        "plugin_wxbot_reply_policy_idempotency",
        "plugin_wxbot_report_jobs",
        "plugin_wxbot_report_subscriptions",
        "plugin_wxbot_self_review_jobs",
        "plugin_wxbot_self_review_subscriptions",
        "plugin_wxbot_session_policy",
        "plugin_wxbot_tenant_policy",
        "plugin_wxbot_user_bans",
        "processed_messages",
        "runtime_llm_config",
        "runtime_llm_config_history",
        "runtime_llm_config_idempotency",
        "message_outbox",
        "message_effect_intent",
        "social_group_policy",
        "social_group_policy_history",
        "social_group_speech_ledger",
        "social_member_policy",
        "social_member_policy_history",
        "social_participation_event",
        "social_policy_idempotency",
        "social_scope_control",
        "social_scope_control_history",
        "social_tenant_member_control",
        "voice_profile",
        "audit_events",
    }
)

RUNTIME_SCHEMA_INDEXES = frozenset(
    {
        "idx_draw_task_status_heartbeat",
        "ix_channel_connection_tenant_adapter",
        "ix_channel_connection_tenant_state",
        "idx_draw_task_tenant_created",
        "idx_draw_task_queue_due",
        "idx_group_activity_event_scope_created",
        "idx_group_activity_event_status_updated",
        "ix_plugin_lifecycle_in_progress_created",
        "ix_plugin_lifecycle_operation_plugin_status",
        "ix_plugin_lifecycle_operation_updated",
        "idx_memory_acceptance_audit_item",
        "idx_memory_acceptance_audit_scope",
        "idx_memory_entity_scope",
        "idx_memory_episode_scope",
        "idx_memory_event_lookup",
        "idx_memory_extraction_job_ready",
        "idx_memory_extraction_job_scope",
        "idx_memory_fact_scope",
        "idx_memory_fact_subject",
        "idx_memory_identity_lookup",
        "idx_memory_item_scope",
        "idx_memory_session_lookup",
        "idx_plugin_agent_session_policy_scope",
        "idx_plugin_agent_tool_audit_scope",
        "idx_plugin_agent_tool_audit_trace",
        "ix_plugin_admin_mutation_audit_scope_created",
        "ix_plugin_admin_mutation_audit_trace",
        "ix_plugin_admin_mutation_idempotency_created",
        "idx_plugin_credits_balance_session_rank",
        "idx_plugin_credits_checkin_session_date",
        "idx_plugin_credits_checkin_session_user",
        "idx_plugin_credits_ledger_session_created",
        "idx_plugin_credits_ledger_session_user",
        "idx_plugin_credits_reservation_subject_status",
        "ux_plugin_credits_ledger_idempotency",
        "ux_plugin_credits_reservation_idempotency",
        "idx_repeater_event_lookup",
        "idx_tibo_reset_delivery_status",
        "idx_tibo_reset_feed_delivery",
        "idx_tibo_reset_feed_stats",
        "idx_wxbot_group_observations_recent",
        "idx_wxbot_group_membership_active",
        "ix_wxbot_admin_mutation_resource",
        "ix_wxbot_admin_mutation_status_updated",
        "ix_wxbot_admin_resource_pending",
        "idx_wxbot_group_summary_jobs_claim",
        "idx_wxbot_media_ready_events_tenant_created",
        "idx_wxbot_media_ready_events_tenant_sdk_event_id_unique",
        "idx_wxbot_member_events_tenant_created",
        "idx_wxbot_member_events_tenant_sdk_event_id_unique",
        "idx_wxbot_reply_queue_command_id",
        "idx_wxbot_reply_queue_claim",
        "idx_wxbot_reply_queue_connection_claim",
        "idx_wxbot_reply_queue_tenant_command_id_unique",
        "idx_wxbot_reply_queue_due",
        "idx_wxbot_reply_queue_status",
        "ix_wxbot_reply_aggregate_effect",
        "ix_wxbot_reply_policy_idempotency_created",
        "idx_wxbot_report_jobs_scope",
        "idx_wxbot_report_jobs_running_lease",
        "idx_wxbot_report_jobs_sending_lease",
        "idx_wxbot_report_jobs_queued_delivery",
        "idx_wxbot_report_subscriptions_enabled",
        "idx_wxbot_self_review_jobs_scope",
        "idx_wxbot_self_review_jobs_running_lease",
        "idx_wxbot_self_review_subscriptions_enabled",
        "idx_wxbot_user_bans_active_unique",
        "idx_wxbot_user_bans_lookup",
        "ix_mod_events_tenant_created",
        "ix_mod_events_tenant_session",
        "ix_mod_kw_tenant_session",
        "ix_persona_jobs_target",
        "ix_persona_jobs_tenant",
        "idx_persona_jobs_ready",
        "idx_persona_jobs_running_lease",
        "idx_persona_job_chunks_status",
        "ix_persona_profiles_scope",
        "ix_persona_profiles_target",
        "ix_plugin_events_plugin_created",
        "ix_plugin_events_type_created",
        "ix_plugin_scope_state_plugin",
        "ix_processed_messages_tenant_session_created",
        "ix_message_outbox_due",
        "ix_message_outbox_tenant_session_created",
        "ix_message_effect_intent_due",
        "ix_message_effect_intent_source",
        "ix_message_effect_intent_scope_created",
        "uq_mod_kw_tenant_session_keyword",
        "ux_memory_entity_scope_name",
        "ux_memory_episode_memory_item",
        "ux_memory_event_key",
        "ux_memory_extraction_job_idempotency",
        "ux_memory_fact_memory_item",
        "ux_memory_item_dedupe",
        "ix_social_group_policy_history_scope_created",
        "ix_social_group_speech_active_budget",
        "ix_social_group_speech_scope_occurred",
        "ix_social_member_policy_history_scope_created",
        "ix_social_participation_event_scope_created",
        "ix_social_policy_idempotency_created",
        "ix_voice_profile_scope_updated",
        "ix_audit_events_scope_created",
        "ix_audit_events_trace",
        "ix_memory_item_audience_expiry",
        "uq_social_group_speech_observed_message",
    }
)

# Breaking migrations 0034 and 0036 add columns whose presence and NOT NULL
# contract are required for owner provenance and stale-worker fencing.  The
# 0037 reconciliation also supplies columns consumed by the merged wxbot
# runtime; table existence alone cannot prove these guarantees.
RUNTIME_SCHEMA_COLUMN_CONTRACTS = (
    ("message_effect_intent", "producer_owner", False),
    ("plugin_wxbot_report_jobs", "run_attempt", False),
    ("plugin_wxbot_report_jobs", "delivery_attempt", False),
    ("plugin_wxbot_report_jobs", "sdk_outbound_id", True),
    ("plugin_wxbot_report_jobs", "delivery_queued_at", True),
    ("plugin_wxbot_report_jobs", "delivery_checked_at", True),
    ("plugin_wxbot_self_review_jobs", "run_attempt", False),
    ("plugin_wxbot_interaction_cursor", "burst_count", False),
    ("plugin_wxbot_interaction_cursor", "burst_started_at", False),
    ("plugin_wxbot_session_policy", "reply_cooldown_seconds", True),
    ("plugin_wxbot_session_policy", "coalesce_window_ms", True),
    ("plugin_wxbot_session_policy", "adaptive_cooldown_enabled", True),
    ("plugin_persona_jobs", "run_attempt", False),
    ("plugin_persona_jobs", "status", False),
    ("plugin_persona_jobs", "claim_owner", False),
    ("plugin_persona_jobs", "lease_expires_at", True),
    ("plugin_persona_jobs", "request_hash", False),
    ("plugin_persona_jobs", "input_messages_json", False),
    ("plugin_persona_jobs", "total_chunks", False),
    ("plugin_persona_jobs", "completed_chunks", False),
)

# Index names alone are insufficient: an operator could recreate a same-name
# index with the wrong keys or without its partial predicate and still pass a
# name-only readiness probe.
RUNTIME_SCHEMA_INDEX_CONTRACTS = (
    (
        "ix_plugin_lifecycle_in_progress_created",
        "plugin_lifecycle_operation",
        ("created_at",),
        "status='in_progress'",
    ),
    (
        "idx_wxbot_report_jobs_running_lease",
        "plugin_wxbot_report_jobs",
        ("updated_at",),
        "status='running'",
    ),
    (
        "idx_wxbot_report_jobs_sending_lease",
        "plugin_wxbot_report_jobs",
        ("updated_at",),
        "delivery_status='sending'",
    ),
    (
        "idx_wxbot_report_jobs_queued_delivery",
        "plugin_wxbot_report_jobs",
        ("tenant_id", "delivery_checked_at", "delivery_queued_at", "id"),
        "delivery_status='queued'",
    ),
    (
        "idx_wxbot_self_review_jobs_running_lease",
        "plugin_wxbot_self_review_jobs",
        ("updated_at",),
        "status='running'",
    ),
    (
        "idx_persona_jobs_ready",
        "plugin_persona_jobs",
        ("available_at", "created_at", "id"),
        "(status = 'pending' OR status = 'retry_wait') "
        "AND cancel_requested_at IS NULL",
    ),
    (
        "idx_persona_jobs_running_lease",
        "plugin_persona_jobs",
        ("lease_expires_at", "id"),
        "status='running'",
    ),
)

_POSTGRES_TYPE_CAST = re.compile(
    r"::(?:text|character\s+varying(?:\(\d+\))?)\b",
    re.IGNORECASE,
)


def _normalize_index_predicate(value: object) -> str:
    normalized = str(value or "").strip().lower().replace('"', "")
    normalized = _POSTGRES_TYPE_CAST.sub("", normalized)
    return re.sub(r"[\s()]", "", normalized)


def _normalize_index_columns(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = value.strip().strip("{}")
        return tuple(item.strip().strip('"') for item in raw.split(",") if item.strip())
    if isinstance(value, Iterable):
        return tuple(str(item).strip().strip('"') for item in value)
    return ()


def _sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class RuntimeSchemaError(RuntimeError):
    """The database is not at the application schema contract."""


async def verify_runtime_compatibility(
    engine: AsyncEngine,
    *,
    component: str,
) -> None:
    """Verify the cheap compatibility boundary used by periodic probes.

    This deliberately does not repeat the full table/index/column inspection
    performed at process startup.  A single Alembic version row plus the
    explicit compatibility level is sufficient to catch partially applied or
    contract-incompatible migrations while keeping recurring readiness checks
    bounded to two small read-only queries.
    """

    try:
        async with engine.connect() as conn:
            revision_result = await conn.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            )
            revisions = tuple(
                str(row[0]) for row in revision_result.fetchall() if row[0] is not None
            )
            if len(revisions) != 1:
                raise RuntimeSchemaError(
                    f"{component} requires one Alembic revision; "
                    f"found {revisions or ('missing',)}. "
                    "Run `alembic upgrade head` before starting the service."
                )

            contract_result = await conn.execute(
                text(
                    "SELECT compatibility_level FROM app_schema_contract "
                    "WHERE contract_name = :contract_name"
                ),
                {"contract_name": RUNTIME_SCHEMA_CONTRACT_NAME},
            )
            contract_rows = tuple(
                int(row[0]) for row in contract_result.fetchall() if row[0] is not None
            )
            if contract_rows != (RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,):
                found = contract_rows or ("missing",)
                raise RuntimeSchemaError(
                    f"{component} requires runtime schema compatibility level "
                    f"{RUNTIME_SCHEMA_COMPATIBILITY_LEVEL}; found {found} at "
                    f"Alembic revision {revisions[0]}. Run `alembic upgrade head` "
                    "or deploy an application version compatible with this schema."
                )
    except RuntimeSchemaError:
        raise
    except Exception as exc:
        raise RuntimeSchemaError(
            f"{component} could not verify the Alembic schema; "
            "run `alembic upgrade head` before starting the service"
        ) from exc


async def verify_runtime_schema(
    engine: AsyncEngine,
    *,
    component: str,
    required_tables: Iterable[str] = RUNTIME_SCHEMA_TABLES,
    required_indexes: Iterable[str] = RUNTIME_SCHEMA_INDEXES,
) -> None:
    """Read-only verification of the compatible runtime-owned schema.

    Alembic's exact head is deliberately not used as the compatibility
    boundary.  A newer deployment may add a backwards-compatible migration
    while older application replicas are still draining.  Such migrations
    retain the same explicit contract level; a breaking migration must bump
    it, causing older replicas to fail closed.
    """

    expected_tables = sorted({str(item) for item in required_tables if str(item)})
    expected_indexes = sorted({str(item) for item in required_indexes if str(item)})
    expected_column_contracts = tuple(
        contract
        for contract in RUNTIME_SCHEMA_COLUMN_CONTRACTS
        if contract[0] in expected_tables
    )
    expected_index_contracts = tuple(
        contract
        for contract in RUNTIME_SCHEMA_INDEX_CONTRACTS
        if contract[0] in expected_indexes
    )
    column_contract_rows: list[tuple[object, ...]] = []
    index_contract_rows: list[tuple[object, ...]] = []
    try:
        async with engine.connect() as conn:
            revision_result = await conn.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            )
            revisions = tuple(
                str(row[0]) for row in revision_result.fetchall() if row[0] is not None
            )
            if len(revisions) != 1:
                raise RuntimeSchemaError(
                    f"{component} requires one Alembic revision; "
                    f"found {revisions or ('missing',)}. "
                    "Run `alembic upgrade head` before starting the service."
                )
            contract_result = await conn.execute(
                text(
                    "SELECT compatibility_level FROM app_schema_contract "
                    "WHERE contract_name = :contract_name"
                ),
                {"contract_name": RUNTIME_SCHEMA_CONTRACT_NAME},
            )
            contract_rows = tuple(
                int(row[0]) for row in contract_result.fetchall() if row[0] is not None
            )
            if contract_rows != (RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,):
                found = contract_rows or ("missing",)
                raise RuntimeSchemaError(
                    f"{component} requires runtime schema compatibility level "
                    f"{RUNTIME_SCHEMA_COMPATIBILITY_LEVEL}; found {found} at "
                    f"Alembic revision {revisions[0]}. Run `alembic upgrade head` "
                    "or deploy an application version compatible with this schema."
                )

            dialect = str(getattr(conn.dialect, "name", "") or "").lower()
            if dialect == "sqlite":
                table_result = await conn.execute(
                    text("SELECT name AS table_name FROM sqlite_master WHERE type = 'table'")
                )
                index_result = await conn.execute(
                    text("SELECT name AS index_name FROM sqlite_master WHERE type = 'index'")
                )
                sqlite_columns: dict[tuple[str, str], bool] = {}
                for table_name in sorted(
                    {table for table, _column, _nullable in expected_column_contracts}
                ):
                    column_result = await conn.execute(
                        text(f"PRAGMA table_info({_sqlite_identifier(table_name)})")
                    )
                    for row in column_result.fetchall():
                        sqlite_columns[(table_name, str(row[1]))] = not bool(row[3])
                column_contract_rows = [
                    (
                        table_name,
                        column_name,
                        (table_name, column_name) in sqlite_columns,
                        sqlite_columns.get((table_name, column_name)),
                    )
                    for table_name, column_name, _nullable in expected_column_contracts
                ]
                for index_name, _table_name, _columns, _predicate in expected_index_contracts:
                    definition_result = await conn.execute(
                        text(
                            "SELECT tbl_name, sql FROM sqlite_master "
                            "WHERE type = 'index' AND name = :index_name"
                        ),
                        {"index_name": index_name},
                    )
                    definition_rows = definition_result.fetchall()
                    if not definition_rows:
                        index_contract_rows.append((index_name, False, None, (), None))
                        continue
                    info_result = await conn.execute(
                        text(f"PRAGMA index_info({_sqlite_identifier(index_name)})")
                    )
                    columns = tuple(
                        str(row[2])
                        for row in sorted(info_result.fetchall(), key=lambda row: int(row[0]))
                    )
                    definition = str(definition_rows[0][1] or "")
                    predicate_match = re.search(
                        r"\bWHERE\b(.*)$",
                        definition,
                        re.IGNORECASE | re.DOTALL,
                    )
                    index_contract_rows.append(
                        (
                            index_name,
                            True,
                            str(definition_rows[0][0]),
                            columns,
                            predicate_match.group(1) if predicate_match else "",
                        )
                    )
            else:
                table_result = await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = ANY (current_schemas(FALSE))"
                    )
                )
                index_result = await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = ANY (current_schemas(FALSE))"
                    )
                )
                if expected_column_contracts:
                    column_values = ", ".join(
                        f"('{table_name}', '{column_name}')"
                        for table_name, column_name, _nullable in expected_column_contracts
                    )
                    column_result = await conn.execute(
                        text(
                            f"""
                            WITH runtime_required_columns(table_name, column_name) AS (
                                VALUES {column_values}
                            )
                            SELECT required.table_name,
                                   required.column_name,
                                   attribute.attname IS NOT NULL AS present,
                                   CASE
                                       WHEN attribute.attname IS NULL THEN NULL
                                       ELSE NOT attribute.attnotnull
                                   END AS nullable
                            FROM runtime_required_columns required
                            LEFT JOIN pg_catalog.pg_class relation
                              ON relation.oid = to_regclass(required.table_name)
                            LEFT JOIN pg_catalog.pg_attribute attribute
                              ON attribute.attrelid = relation.oid
                             AND attribute.attname = required.column_name
                             AND attribute.attnum > 0
                             AND NOT attribute.attisdropped
                            """
                        )
                    )
                    column_contract_rows = [
                        tuple(result_row) for result_row in column_result.fetchall()
                    ]
                if expected_index_contracts:
                    index_values = ", ".join(
                        f"('{index_name}')"
                        for index_name, _table, _columns, _predicate in expected_index_contracts
                    )
                    definition_result = await conn.execute(
                        text(
                            f"""
                            WITH runtime_required_indexes(index_name) AS (
                                VALUES {index_values}
                            )
                            SELECT required.index_name,
                                   index_meta.indexrelid IS NOT NULL
                                   AND index_meta.indisvalid
                                   AND index_meta.indisready AS present,
                                   table_relation.relname AS table_name,
                                   COALESCE(
                                       ARRAY(
                                           SELECT attribute.attname::TEXT
                                           FROM unnest(
                                               index_meta.indkey::smallint[]
                                           ) WITH ORDINALITY AS indexed_key(attnum, position)
                                           JOIN pg_catalog.pg_attribute attribute
                                             ON attribute.attrelid = index_meta.indrelid
                                            AND attribute.attnum = indexed_key.attnum
                                           WHERE indexed_key.position <= index_meta.indnkeyatts
                                           ORDER BY indexed_key.position
                                       ),
                                       ARRAY[]::TEXT[]
                                   ) AS column_names,
                                   pg_get_expr(
                                       index_meta.indpred,
                                       index_meta.indrelid,
                                       TRUE
                                   ) AS predicate
                            FROM runtime_required_indexes required
                            LEFT JOIN pg_catalog.pg_class index_relation
                              ON index_relation.oid = to_regclass(required.index_name)
                            LEFT JOIN pg_catalog.pg_index index_meta
                              ON index_meta.indexrelid = index_relation.oid
                            LEFT JOIN pg_catalog.pg_class table_relation
                              ON table_relation.oid = index_meta.indrelid
                            """
                        )
                    )
                    index_contract_rows = [
                        tuple(result_row) for result_row in definition_result.fetchall()
                    ]
            present = {str(row[0]) for row in table_result.fetchall() if row[0] is not None}
            present_indexes = {str(row[0]) for row in index_result.fetchall() if row[0] is not None}
    except RuntimeSchemaError:
        raise
    except Exception as exc:
        raise RuntimeSchemaError(
            f"{component} could not verify the Alembic schema; "
            "run `alembic upgrade head` before starting the service"
        ) from exc

    missing = sorted(set(expected_tables) - present)
    if missing:
        raise RuntimeSchemaError(
            f"{component} database schema is incomplete at "
            f"{RUNTIME_SCHEMA_REVISION}; missing tables: {', '.join(missing)}"
        )
    missing_indexes = sorted(set(expected_indexes) - present_indexes)
    if missing_indexes:
        raise RuntimeSchemaError(
            f"{component} database schema is incomplete at "
            f"{RUNTIME_SCHEMA_REVISION}; missing indexes: {', '.join(missing_indexes)}"
        )

    actual_columns = {
        (str(row[0]), str(row[1])): (bool(row[2]), row[3])
        for row in column_contract_rows
    }
    invalid_columns: list[str] = []
    for table_name, column_name, expected_nullable in expected_column_contracts:
        present_column, nullable = actual_columns.get(
            (table_name, column_name),
            (False, None),
        )
        label = f"{table_name}.{column_name}"
        if not present_column:
            invalid_columns.append(f"{label} missing")
        elif bool(nullable) is not expected_nullable:
            invalid_columns.append(
                f"{label} nullable={str(bool(nullable)).lower()} "
                f"expected={str(expected_nullable).lower()}"
            )
    if invalid_columns:
        raise RuntimeSchemaError(
            f"{component} database schema column contract is invalid at "
            f"{RUNTIME_SCHEMA_REVISION}: {', '.join(invalid_columns)}"
        )

    actual_indexes = {str(row[0]): row for row in index_contract_rows}
    invalid_index_contracts: list[str] = []
    for index_name, table_name, columns, predicate in expected_index_contracts:
        actual_index_row = actual_indexes.get(index_name)
        if actual_index_row is None or not bool(actual_index_row[1]):
            invalid_index_contracts.append(f"{index_name} missing")
            continue
        actual_table = str(actual_index_row[2] or "")
        actual_index_columns = _normalize_index_columns(actual_index_row[3])
        actual_predicate = _normalize_index_predicate(actual_index_row[4])
        if (
            actual_table != table_name
            or actual_index_columns != columns
            or actual_predicate != _normalize_index_predicate(predicate)
        ):
            invalid_index_contracts.append(
                f"{index_name} table={actual_table!r} "
                f"columns={actual_index_columns!r} predicate={actual_predicate!r}"
            )
    if invalid_index_contracts:
        raise RuntimeSchemaError(
            f"{component} database schema index contract is invalid at "
            f"{RUNTIME_SCHEMA_REVISION}: {', '.join(invalid_index_contracts)}"
        )
