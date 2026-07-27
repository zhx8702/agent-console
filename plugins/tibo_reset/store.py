from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdentity,
    MutationOutcome,
    run_idempotent_mutation,
)
from app.common.logging import get_logger
from app.infra.db import get_engine
from app.infra.runtime_schema import verify_runtime_schema
from plugins.tibo_reset.client import TiboResetEntry, notification_validation

logger = get_logger(__name__)
_TIBO_RESET_SCHEMA_LOCK_KEY = 2026071601
_PLUGIN_NAME = "tibo_reset"
_ACTIVE_ADMIN_MUTATION_CONNECTION: ContextVar[AsyncConnection | None] = ContextVar(
    "tibo_reset_admin_mutation_connection",
    default=None,
)


def _rows(result: Any) -> list[dict[str, Any]]:
    if not result.returns_rows:
        return []
    return [dict(row._mapping) for row in result.fetchall()]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


@asynccontextmanager
async def _write_connection() -> AsyncIterator[AsyncConnection]:
    active = _ACTIVE_ADMIN_MUTATION_CONNECTION.get()
    if active is not None:
        yield active
        return
    async with get_engine().begin() as conn:
        yield conn


class TiboResetStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="tibo reset store")
        logger.info("tibo_reset.schema_verified")

    async def run_admin_mutation(
        self,
        *,
        identity: MutationIdentity,
        audit: MutationAudit,
        mutate: Callable[[], Awaitable[MutationChange]],
    ) -> MutationOutcome:
        """Persist manual polling replay state and semantic audit atomically."""

        async with get_engine().begin() as conn:
            token = _ACTIVE_ADMIN_MUTATION_CONNECTION.set(conn)
            try:
                return await run_idempotent_mutation(
                    conn,
                    identity=identity,
                    audit=audit,
                    mutate=mutate,
                )
            finally:
                _ACTIVE_ADMIN_MUTATION_CONNECTION.reset(token)

    async def ingest_feed(self, entries: list[TiboResetEntry]) -> dict[str, Any]:
        inserted = 0
        eligible_inserted = 0
        latest = max(entries, key=lambda item: item.created_at, default=None)
        async with _write_connection() as conn:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _TIBO_RESET_SCHEMA_LOCK_KEY},
            )
            state_result = await conn.execute(
                text(
                    """
                    SELECT initialized
                    FROM plugin_tibo_reset_sync_state
                    WHERE id = 1
                    FOR UPDATE
                    """
                )
            )
            state = state_result.mappings().first()
            initialized = bool(state and state.get("initialized"))
            for entry in entries:
                content_valid, validation_reason = notification_validation(entry)
                params = {
                    "tweet_id": entry.tweet_id,
                    "tweet_text": entry.text,
                    "tweet_created_at": entry.created_at,
                    "source_url": entry.source_url,
                    "confidence": entry.confidence,
                    "evidence": entry.evidence,
                    "stated_reason": entry.stated_reason,
                    "reset_type": entry.reset_type,
                    "beneficiaries": entry.beneficiaries,
                    "content_valid": content_valid,
                    "validation_reason": validation_reason,
                    "after_baseline": initialized,
                    "notify_eligible": initialized and content_valid,
                }
                insert_result = await conn.execute(
                    text(
                        """
                        INSERT INTO plugin_tibo_reset_feed (
                            tweet_id, tweet_text, tweet_created_at, source_url, confidence,
                            evidence, stated_reason, reset_type, beneficiaries, content_valid,
                            validation_reason, after_baseline, notify_eligible
                        ) VALUES (
                            :tweet_id, :tweet_text,
                            CAST(CAST(:tweet_created_at AS TEXT) AS TIMESTAMPTZ),
                            :source_url, :confidence, :evidence, :stated_reason, :reset_type,
                            :beneficiaries, :content_valid, :validation_reason,
                            :after_baseline, :notify_eligible
                        )
                        ON CONFLICT (tweet_id) DO NOTHING
                        RETURNING tweet_id
                        """
                    ),
                    params,
                )
                if insert_result.first() is not None:
                    inserted += 1
                    if params["notify_eligible"]:
                        eligible_inserted += 1
                await conn.execute(
                    text(
                        """
                        UPDATE plugin_tibo_reset_feed
                        SET tweet_text = :tweet_text,
                            tweet_created_at = CAST(
                                CAST(:tweet_created_at AS TEXT) AS TIMESTAMPTZ
                            ),
                            source_url = :source_url,
                            confidence = :confidence,
                            evidence = :evidence,
                            stated_reason = :stated_reason,
                            reset_type = :reset_type,
                            beneficiaries = :beneficiaries,
                            content_valid = :content_valid,
                            validation_reason = :validation_reason,
                            notify_eligible = (
                                plugin_tibo_reset_feed.notify_eligible
                                OR (
                                    plugin_tibo_reset_feed.after_baseline = TRUE
                                    AND :content_valid = TRUE
                                )
                            ),
                            updated_at = NOW()
                        WHERE tweet_id = :tweet_id
                        """
                    ),
                    params,
                )

            await conn.execute(
                text(
                    """
                    UPDATE plugin_tibo_reset_sync_state
                    SET initialized = TRUE,
                        last_poll_at = NOW(),
                        last_success_at = NOW(),
                        latest_tweet_id = :latest_tweet_id,
                        fetched_count = :fetched_count,
                        last_error = '',
                        updated_at = NOW()
                    WHERE id = 1
                    """
                ),
                {
                    "latest_tweet_id": latest.tweet_id if latest is not None else "",
                    "fetched_count": len(entries),
                },
            )
        return {
            "baseline": not initialized,
            "fetched": len(entries),
            "inserted": inserted,
            "eligible_inserted": eligible_inserted,
            "latest_tweet_id": latest.tweet_id if latest is not None else "",
        }

    async def mark_poll_failed(self, error: str) -> None:
        await self._execute(
            """
            UPDATE plugin_tibo_reset_sync_state
            SET last_poll_at = NOW(), last_error = :error, updated_at = NOW()
            WHERE id = 1
            """,
            {"error": str(error or "")[:4000]},
        )

    async def is_scope_enabled(self, tenant_id: str, session_id: str) -> bool:
        rows = await self._fetch(
            """
            SELECT enabled
            FROM plugin_scope_state
            WHERE tenant_id = :tenant_id
              AND session_id = :session_id
              AND plugin_name = :plugin_name
            LIMIT 1
            """,
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "plugin_name": _PLUGIN_NAME,
            },
        )
        return bool(rows and rows[0].get("enabled"))

    async def reset_stats(
        self,
        *,
        timezone_name: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        configured_timezone = str(
            timezone_name
            or getattr(self.settings, "tibo_reset_timezone", "UTC")
            or "UTC"
        ).strip()
        try:
            local_timezone = ZoneInfo(configured_timezone)
        except ZoneInfoNotFoundError:
            configured_timezone = "UTC"
            local_timezone = ZoneInfo(configured_timezone)

        current_utc = now or datetime.now(UTC)
        if current_utc.tzinfo is None:
            current_utc = current_utc.replace(tzinfo=UTC)
        current_utc = current_utc.astimezone(UTC)
        local_now = current_utc.astimezone(local_timezone)
        today_start_local = datetime.combine(
            local_now.date(),
            time.min,
            tzinfo=local_timezone,
        )
        tomorrow_start_local = today_start_local + timedelta(days=1)
        week_start_local = today_start_local - timedelta(days=local_now.weekday())
        week_end_local = week_start_local + timedelta(days=7)
        params = {
            "now_utc": current_utc,
            "today_start_utc": today_start_local.astimezone(UTC),
            "tomorrow_start_utc": tomorrow_start_local.astimezone(UTC),
            "week_start_utc": week_start_local.astimezone(UTC),
            "week_end_utc": week_end_local.astimezone(UTC),
        }
        rows = await self._fetch(
            """
            WITH valid AS (
                SELECT tweet_id, tweet_text, tweet_created_at, source_url,
                       reset_type, beneficiaries
                FROM plugin_tibo_reset_feed
                WHERE content_valid = TRUE
                  AND tweet_created_at <= :now_utc
            ),
            aggregate AS (
                SELECT
                    COUNT(*) AS history_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :week_start_utc
                          AND tweet_created_at < :week_end_utc
                    ) AS week_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :week_start_utc
                          AND tweet_created_at < :week_end_utc
                          AND LOWER(beneficiaries) = 'everyone'
                    ) AS week_everyone_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :week_start_utc
                          AND tweet_created_at < :week_end_utc
                          AND LOWER(reset_type) = 'weekly_usage'
                    ) AS week_weekly_usage_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :week_start_utc
                          AND tweet_created_at < :week_end_utc
                          AND LOWER(reset_type) = 'banked_reset'
                    ) AS week_banked_reset_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :week_start_utc
                          AND tweet_created_at < :week_end_utc
                          AND LOWER(beneficiaries) = 'everyone'
                          AND LOWER(reset_type) = 'weekly_usage'
                    ) AS week_everyone_weekly_usage_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :week_start_utc
                          AND tweet_created_at < :week_end_utc
                          AND LOWER(beneficiaries) = 'everyone'
                          AND LOWER(reset_type) = 'banked_reset'
                    ) AS week_everyone_banked_reset_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :week_start_utc
                          AND tweet_created_at < :week_end_utc
                          AND LOWER(beneficiaries) NOT IN ('', 'everyone')
                    ) AS week_subset_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :today_start_utc
                          AND tweet_created_at < :tomorrow_start_utc
                    ) AS today_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :today_start_utc
                          AND tweet_created_at < :tomorrow_start_utc
                          AND LOWER(beneficiaries) = 'everyone'
                    ) AS today_everyone_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :today_start_utc
                          AND tweet_created_at < :tomorrow_start_utc
                          AND LOWER(reset_type) = 'weekly_usage'
                    ) AS today_weekly_usage_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :today_start_utc
                          AND tweet_created_at < :tomorrow_start_utc
                          AND LOWER(reset_type) = 'banked_reset'
                    ) AS today_banked_reset_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :today_start_utc
                          AND tweet_created_at < :tomorrow_start_utc
                          AND LOWER(beneficiaries) = 'everyone'
                          AND LOWER(reset_type) = 'weekly_usage'
                    ) AS today_everyone_weekly_usage_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :today_start_utc
                          AND tweet_created_at < :tomorrow_start_utc
                          AND LOWER(beneficiaries) = 'everyone'
                          AND LOWER(reset_type) = 'banked_reset'
                    ) AS today_everyone_banked_reset_count,
                    COUNT(*) FILTER (
                        WHERE tweet_created_at >= :today_start_utc
                          AND tweet_created_at < :tomorrow_start_utc
                          AND LOWER(beneficiaries) NOT IN ('', 'everyone')
                    ) AS today_subset_count
                FROM valid
            )
            SELECT aggregate.*,
                   latest.tweet_id AS latest_tweet_id,
                   latest.tweet_text AS latest_text,
                   latest.tweet_created_at AS latest_reset_at,
                   latest.source_url AS latest_source_url,
                   latest.reset_type AS latest_reset_type,
                   latest.beneficiaries AS latest_beneficiaries
            FROM aggregate
            LEFT JOIN LATERAL (
                SELECT tweet_id, tweet_text, tweet_created_at, source_url,
                       reset_type, beneficiaries
                FROM valid
                ORDER BY tweet_created_at DESC, tweet_id DESC
                LIMIT 1
            ) AS latest ON TRUE
            """,
            params,
        )
        row = dict(rows[0]) if rows else {}
        count_keys = (
            "history_count",
            "week_count",
            "week_everyone_count",
            "week_weekly_usage_count",
            "week_banked_reset_count",
            "week_everyone_weekly_usage_count",
            "week_everyone_banked_reset_count",
            "week_subset_count",
            "today_count",
            "today_everyone_count",
            "today_weekly_usage_count",
            "today_banked_reset_count",
            "today_everyone_weekly_usage_count",
            "today_everyone_banked_reset_count",
            "today_subset_count",
        )
        for key in count_keys:
            row[key] = int(row.get(key) or 0)
        latest_reset_at = row.get("latest_reset_at")
        if isinstance(latest_reset_at, datetime):
            row["latest_reset_at"] = latest_reset_at.isoformat()
        row.update(
            {
                "today_has_reset": row["today_count"] > 0,
                "timezone": configured_timezone,
                "as_of": local_now.isoformat(),
                "today_start": today_start_local.isoformat(),
                "week_start": week_start_local.isoformat(),
                "week_end": week_end_local.isoformat(),
                "retention": "persistent",
            }
        )
        return row

    async def list_enabled_scopes(self, *, limit: int = 500) -> list[dict[str, Any]]:
        rows = await self._fetch(
            """
            SELECT tenant_id, session_id, config_json, updated_at
            FROM plugin_scope_state
            WHERE plugin_name = :plugin_name
              AND enabled = TRUE
              AND session_id <> ''
            ORDER BY updated_at ASC
            LIMIT :limit
            """,
            {"plugin_name": _PLUGIN_NAME, "limit": max(1, min(int(limit), 2000))},
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            config = _json_object(row.get("config_json"))
            result.append(
                {
                    **row,
                    "config": config,
                    "session_name": str(config.get("session_name") or ""),
                    "enabled_at": str(row.get("updated_at") or ""),
                }
            )
        return result

    async def list_deliverable(
        self,
        *,
        tenant_id: str,
        session_id: str,
        enabled_at: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._fetch(
            """
            SELECT f.tweet_id, f.tweet_text AS text, f.tweet_created_at AS created_at,
                   f.source_url, f.confidence, f.evidence, f.stated_reason,
                   f.reset_type, f.beneficiaries, f.discovered_at
            FROM plugin_tibo_reset_feed AS f
            LEFT JOIN plugin_tibo_reset_delivery AS d
              ON d.tenant_id = :tenant_id
             AND d.session_id = :session_id
             AND d.tweet_id = f.tweet_id
            WHERE f.notify_eligible = TRUE
              AND f.discovered_at >= CAST(CAST(:enabled_at AS TEXT) AS TIMESTAMPTZ)
              AND (
                    d.id IS NULL
                    OR (d.status = 'failed' AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= NOW()))
                    OR (d.status = 'running' AND d.started_at <= NOW() - INTERVAL '10 minutes')
                  )
            ORDER BY f.tweet_created_at ASC, f.tweet_id ASC
            LIMIT :limit
            """,
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "enabled_at": enabled_at,
                "limit": max(1, min(int(limit), 500)),
            },
        )

    async def claim_delivery(
        self,
        *,
        tenant_id: str,
        session_id: str,
        session_name: str,
        tweet_id: str,
        command_id: str,
    ) -> dict[str, Any] | None:
        rows = await self._fetch(
            """
            INSERT INTO plugin_tibo_reset_delivery (
                tenant_id, session_id, session_name, tweet_id, status,
                attempt_count, command_id, started_at, updated_at
            ) VALUES (
                :tenant_id, :session_id, :session_name, :tweet_id, 'running',
                1, :command_id, NOW(), NOW()
            )
            ON CONFLICT (tenant_id, session_id, tweet_id) DO UPDATE
            SET session_name = EXCLUDED.session_name,
                status = 'running',
                attempt_count = plugin_tibo_reset_delivery.attempt_count + 1,
                command_id = EXCLUDED.command_id,
                error = '',
                started_at = NOW(),
                next_attempt_at = NULL,
                updated_at = NOW()
            WHERE (
                    plugin_tibo_reset_delivery.status = 'failed'
                    AND (
                        plugin_tibo_reset_delivery.next_attempt_at IS NULL
                        OR plugin_tibo_reset_delivery.next_attempt_at <= NOW()
                    )
                  )
               OR (
                    plugin_tibo_reset_delivery.status = 'running'
                    AND plugin_tibo_reset_delivery.started_at <= NOW() - INTERVAL '10 minutes'
                  )
            RETURNING id, tenant_id, session_id, session_name, tweet_id, status,
                      attempt_count, command_id
            """,
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "session_name": str(session_name or "")[:256],
                "tweet_id": tweet_id,
                "command_id": command_id,
            },
        )
        return rows[0] if rows else None

    async def mark_delivery_queued(self, delivery_id: int, *, reply_queue_id: int | None) -> None:
        await self._execute(
            """
            UPDATE plugin_tibo_reset_delivery
            SET status = 'queued',
                reply_queue_id = :reply_queue_id,
                error = '',
                queued_at = NOW(),
                next_attempt_at = NULL,
                updated_at = NOW()
            WHERE id = :id
            """,
            {"id": int(delivery_id), "reply_queue_id": reply_queue_id},
        )

    async def expire_stale_queued(self, *, max_age_seconds: int = 300) -> dict[str, int]:
        """Settle stale notifications and cancel any that are still unsent."""

        age_seconds = max(60, min(int(max_age_seconds), 86400))
        async with _write_connection() as conn:
            result = await conn.execute(
                text(
                    """
                    WITH stale AS MATERIALIZED (
                        SELECT id, reply_queue_id
                        FROM plugin_tibo_reset_delivery
                        WHERE status IN ('queued', 'failed', 'running')
                          AND COALESCE(queued_at, created_at)
                              <= NOW() - (:max_age_seconds * INTERVAL '1 second')
                    ), cancelled AS (
                        UPDATE plugin_wxbot_reply_queue AS q
                        SET status = 'cancelled',
                            claim_owner = '',
                            claim_token = '',
                            claim_until = NULL,
                            error = 'reply_expired:tibo_reset'
                        FROM stale
                        WHERE q.id = stale.reply_queue_id
                          AND q.status IN ('pending', 'sending')
                        RETURNING q.id
                    ), marked AS (
                        UPDATE plugin_tibo_reset_delivery AS d
                        SET status = CASE
                                WHEN EXISTS (
                                    SELECT 1
                                    FROM plugin_wxbot_reply_queue AS sent_reply
                                    WHERE sent_reply.id = d.reply_queue_id
                                      AND sent_reply.status = 'sent'
                                ) THEN 'sent'
                                ELSE 'dlq'
                            END,
                            error = CASE
                                WHEN EXISTS (
                                    SELECT 1
                                    FROM plugin_wxbot_reply_queue AS sent_reply
                                    WHERE sent_reply.id = d.reply_queue_id
                                      AND sent_reply.status = 'sent'
                                ) THEN ''
                                ELSE 'delivery_expired'
                            END,
                            next_attempt_at = NULL,
                            updated_at = NOW()
                        FROM stale
                        WHERE d.id = stale.id
                        RETURNING d.id, d.status
                    )
                    SELECT
                        (SELECT COUNT(*) FROM marked) AS delivery_count,
                        (SELECT COUNT(*) FROM marked WHERE status = 'dlq') AS dlq_count,
                        (SELECT COUNT(*) FROM marked WHERE status = 'sent') AS sent_count,
                        (SELECT COUNT(*) FROM cancelled) AS reply_count
                    """
                ),
                {"max_age_seconds": age_seconds},
            )
            rows = _rows(result)
        row = rows[0] if rows else {}
        return {
            "delivery_count": int(row.get("delivery_count") or 0),
            "dlq_count": int(row.get("dlq_count") or 0),
            "sent_count": int(row.get("sent_count") or 0),
            "reply_count": int(row.get("reply_count") or 0),
        }

    async def mark_delivery_failed(
        self,
        delivery_id: int,
        *,
        error: str,
        retry_seconds: int = 300,
    ) -> None:
        await self._execute(
            """
            UPDATE plugin_tibo_reset_delivery
            SET status = 'failed',
                error = :error,
                next_attempt_at = NOW() + (:retry_seconds * INTERVAL '1 second'),
                updated_at = NOW()
            WHERE id = :id
            """,
            {
                "id": int(delivery_id),
                "error": str(error or "")[:4000],
                "retry_seconds": max(30, min(int(retry_seconds), 3600)),
            },
        )

    async def list_feed(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return await self._fetch(
            """
            SELECT tweet_id, tweet_text AS text, tweet_created_at AS created_at,
                   source_url, confidence, evidence, stated_reason, reset_type,
                   beneficiaries, content_valid, validation_reason, after_baseline,
                   notify_eligible, discovered_at, updated_at
            FROM plugin_tibo_reset_feed
            ORDER BY tweet_created_at DESC, tweet_id DESC
            LIMIT :limit
            """,
            {"limit": max(1, min(int(limit), 500))},
        )

    async def list_deliveries(
        self,
        *,
        tenant_id: str = "",
        session_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._fetch(
            """
            SELECT id, tenant_id, session_id, session_name, tweet_id, status,
                   attempt_count, reply_queue_id, command_id, error, started_at,
                   next_attempt_at, queued_at, created_at, updated_at
            FROM plugin_tibo_reset_delivery
            WHERE (:tenant_id = '' OR tenant_id = :tenant_id)
              AND (:session_id = '' OR session_id = :session_id)
              AND (:status = '' OR status = :status)
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
            """,
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "status": status,
                "limit": max(1, min(int(limit), 500)),
            },
        )

    async def runtime_status(self) -> dict[str, Any]:
        state_rows = await self._fetch(
            """
            SELECT initialized, last_poll_at, last_success_at, latest_tweet_id,
                   fetched_count, last_error, updated_at
            FROM plugin_tibo_reset_sync_state
            WHERE id = 1
            """
        )
        count_rows = await self._fetch(
            """
            SELECT
                (SELECT COUNT(*) FROM plugin_tibo_reset_feed) AS feed_count,
                (SELECT COUNT(*) FROM plugin_scope_state
                    WHERE plugin_name = :plugin_name AND enabled = TRUE AND session_id <> '')
                    AS enabled_groups,
                (SELECT COUNT(*) FROM plugin_tibo_reset_delivery WHERE status = 'queued')
                    AS queued_count,
                (SELECT COUNT(*) FROM plugin_tibo_reset_delivery WHERE status = 'failed')
                    AS failed_count,
                (SELECT COUNT(*) FROM plugin_tibo_reset_delivery WHERE status = 'dlq')
                    AS dlq_count
            """,
            {"plugin_name": _PLUGIN_NAME},
        )
        return {
            **(state_rows[0] if state_rows else {}),
            **(count_rows[0] if count_rows else {}),
        }

    @staticmethod
    async def _fetch(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        async with _write_connection() as conn:
            return _rows(await conn.execute(text(sql), params or {}))

    @classmethod
    async def _execute(cls, sql: str, params: dict[str, Any] | None = None) -> None:
        await cls._fetch(sql, params)
