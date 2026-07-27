from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, _utcnow


class ProcessedMessageRow(Base, TimestampMixin):
    __tablename__ = "processed_messages"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="processing",
    )
    route_label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    claim_owner: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    claim_token: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    claim_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'processing', 'completed', 'intentionally_suppressed', "
            "'permanent_failure'"
            ")",
            name="ck_processed_messages_status",
        ),
        Index(
            "ix_processed_messages_tenant_session_created",
            "tenant_id",
            "session_id",
            "created_at",
        ),
    )


class MessageOutboxRow(Base, TimestampMixin):
    __tablename__ = "message_outbox"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reply_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    stream: Mapped[str] = mapped_column(String(128), nullable=False)
    partition_key: Mapped[str] = mapped_column(String(384), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    headers: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
    lease_owner: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    published_message_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'dead_letter')",
            name="ck_message_outbox_status",
        ),
        Index(
            "ix_message_outbox_due",
            "status",
            "available_at",
            "lease_until",
        ),
        Index(
            "ix_message_outbox_tenant_session_created",
            "tenant_id",
            "session_id",
            "created_at",
        ),
    )


class MessageEffectIntentRow(Base, TimestampMixin):
    """Durable, tenant-scoped intent for a post-commit flow side effect.

    The intent is inserted in the same transaction as the inbox terminal
    state, session turns, and outbound outbox.  A separate relay owns the
    ``prepared -> running -> completed/failed`` execution lifecycle.
    """

    __tablename__ = "message_effect_intent"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    source_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    producer_owner: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    effect_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=_utcnow,
    )
    claim_owner: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    claim_token: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    claim_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared', 'running', 'completed', 'failed')",
            name="ck_message_effect_intent_status",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="ck_message_effect_intent_attempts",
        ),
        Index(
            "ix_message_effect_intent_due",
            "status",
            "available_at",
            "claim_until",
        ),
        Index(
            "ix_message_effect_intent_source",
            "tenant_id",
            "source_message_id",
        ),
        Index(
            "ix_message_effect_intent_scope_created",
            "tenant_id",
            "session_id",
            "created_at",
        ),
    )
