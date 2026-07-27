"""Tenant-scoped configured channel connections."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ChannelConnectionRow(Base, TimestampMixin):
    __tablename__ = "channel_connection"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_channel_connection_version"),
        CheckConstraint(
            "priority >= 0 AND priority <= 100000",
            name="ck_channel_connection_priority",
        ),
        CheckConstraint(
            "desired_state IN ('draft', 'disabled', 'enabled')",
            name="ck_channel_connection_desired_state",
        ),
        CheckConstraint(
            "effective_state IN "
            "('unverified', 'ready', 'enabled', 'disabled', 'error')",
            name="ck_channel_connection_effective_state",
        ),
        CheckConstraint(
            "secret_status IN ('missing', 'reference_configured', 'not_required')",
            name="ck_channel_connection_secret_status",
        ),
        CheckConstraint(
            "secret_ref = '' OR secret_ref LIKE '%:%'",
            name="ck_channel_connection_secret_ref",
        ),
        Index(
            "ix_channel_connection_tenant_adapter",
            "tenant_id",
            "adapter_id",
            "priority",
        ),
        Index(
            "ix_channel_connection_tenant_state",
            "tenant_id",
            "desired_state",
            "effective_state",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    adapter_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    desired_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft"
    )
    effective_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="unverified"
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # This column accepts provider references (for example env:NAME), never a value.
    secret_ref: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    secret_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="missing"
    )
    secret_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    required_for_launch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    last_probed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_probe_status: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    last_error_code: Mapped[str] = mapped_column(
        String(96), nullable=False, default=""
    )
    last_inbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_outbound_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["ChannelConnectionRow"]
