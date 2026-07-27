from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class KBDocument(Base, TimestampMixin):
    __tablename__ = "kb_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False, default="", server_default="")
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)

    chunks: Mapped[list[KBChunk]] = relationship(back_populates="document", lazy="noload")


class KBChunk(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(256), nullable=False, default="", server_default="")
    doc_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)

    document: Mapped[KBDocument] = relationship(back_populates="chunks")

    __table_args__ = (Index("ix_kb_chunks_tenant_session_doc", "tenant_id", "session_id", "doc_id"),)
