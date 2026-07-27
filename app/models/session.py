from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, _utcnow


class SessionRow(Base, TimestampMixin):
    __tablename__ = "sessions"

    tenant_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    pii_map: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    # Monotonic distributed-lock fencing token. A stale worker may only update
    # a row while its token is at least the last committed token.
    fence_token: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    turns: Mapped[list[TurnRow]] = relationship(back_populates="session", lazy="noload")

    __table_args__ = (Index("ix_sessions_tenant_user", "tenant_id", "user_id"),)


class TurnRow(Base):
    __tablename__ = "turns"

    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    session: Mapped[SessionRow] = relationship(back_populates="turns")

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["sessions.tenant_id", "sessions.session_id"],
            name="fk_turns_tenant_session",
            ondelete="CASCADE",
        ),
        Index("ix_turns_session_created", "session_id", "created_at"),
        Index(
            "ix_turns_tenant_session_created",
            "tenant_id",
            "session_id",
            "created_at",
        ),
    )
