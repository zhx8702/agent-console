"""
Central contracts shared across all modules.

These Pydantic models are the stable contract between modules. Downstream
services (ingress, orchestrator, capability engines, egress) depend on them
and should not redefine equivalent types.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.common.ids import new_trace_id, new_turn_id

# --- Enums -------------------------------------------------------------------

class Channel(str, Enum):
    WEB = "web"
    WECHAT = "wechat"
    FEISHU = "feishu"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    EMAIL = "email"
    SMS = "sms"
    VOICE = "voice"
    CUSTOM = "custom"


def _normalize_channel_id(value: Any) -> Channel | str:
    if isinstance(value, Channel):
        return value
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("channel cannot be empty")
    try:
        return Channel(normalized)
    except ValueError:
        return normalized


# Known channels retain their enum representation. Plugin-contributed channel
# IDs remain strings, which keeps the transport contract open for extension.
ChannelId = Annotated[Channel | str, BeforeValidator(_normalize_channel_id)]


def channel_id_value(channel: ChannelId | Channel | str) -> str:
    """Return the normalized string value for enum and dynamic channel IDs."""

    return str(getattr(channel, "value", channel) or "").strip().lower()


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    EVENT = "event"


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    AGENT_HUMAN = "agent_human"


class SessionState(str, Enum):
    IDLE = "idle"
    CHATTING = "chatting"
    AWAITING_INFO = "awaiting_info"
    ESCALATED = "escalated"
    CLOSED = "closed"


class RouteType(str, Enum):
    FAQ = "faq"
    RAG = "rag"
    AGENT = "agent"
    LLM = "llm"
    HANDOFF = "handoff"
    CANNED = "canned"  # safety block, use canned reply


class ReplyType(str, Enum):
    TEXT = "text"
    MARKDOWN = "markdown"
    CARD = "card"
    MULTI = "multi"


class EmotionLabel(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class IntentCoarse(str, Enum):
    FAQ = "faq"
    BUSINESS = "business"
    CHITCHAT = "chitchat"
    COMPLAINT = "complaint"
    HANDOFF_REQUEST = "handoff_request"
    UNKNOWN = "unknown"


# --- Base model --------------------------------------------------------------

class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=False, populate_by_name=True)


# --- Inbound -----------------------------------------------------------------

class _ExternalInput(_Base):
    """Strict boundary model for untrusted, caller-controlled payloads."""

    model_config = ConfigDict(extra="forbid", frozen=False, populate_by_name=True)


class Attachment(_ExternalInput):
    type: MessageType
    url: str | None = None
    content: str | None = None
    mime: str | None = None
    size: int | None = None
    # File metadata is kept separate from ``content``.  The latter may contain
    # an image data URL, while a received wxbot file is an SDK URL that must be
    # fetched through the trusted connector before it is inspected.
    name: str | None = None
    ext: str | None = None
    path: str | None = None
    md5: str | None = None
    sha256: str | None = None
    status: str | None = None
    download_status: str | None = None
    failure_reason: str | None = None


class Message(_ExternalInput):
    type: MessageType = MessageType.TEXT
    content: str = ""
    attachments: list[Attachment] = Field(default_factory=list)


class InboundEvent(_ExternalInput):
    """Normalized payload produced by the Inbound Gateway and consumed by the Orchestrator."""

    message_id: str = Field(
        min_length=1,
        max_length=128,
        description="Upstream-generated unique id for idempotency",
    )
    tenant_id: str = Field(min_length=1, max_length=64)
    channel: ChannelId = Field(max_length=64)
    adapter_id: str = Field(default="", max_length=64)
    connection_id: str = Field(default="", max_length=64)
    user_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    external_message_id: str = Field(default="", max_length=256)
    canonical_message_id: str = Field(default="", max_length=256)
    conversation_id: str = Field(default="", max_length=256)
    external_conversation_id: str = Field(default="", max_length=256)
    canonical_conversation_id: str = Field(default="", max_length=256)
    external_user_id: str = Field(default="", max_length=256)
    external_participant_id: str = Field(default="", max_length=256)
    canonical_participant_id: str = Field(default="", max_length=256)
    message: Message
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trace_id: str = Field(default_factory=new_trace_id, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_connection_identity(self) -> InboundEvent:
        self.external_message_id = self.external_message_id or self.message_id
        self.canonical_message_id = self.canonical_message_id or self.message_id
        canonical_conversation = (
            self.canonical_conversation_id or self.conversation_id or self.session_id
        )
        self.conversation_id = canonical_conversation
        self.canonical_conversation_id = canonical_conversation
        self.external_conversation_id = self.external_conversation_id or self.session_id
        external_participant = (
            self.external_participant_id or self.external_user_id or self.user_id
        )
        self.external_user_id = external_participant
        self.external_participant_id = external_participant
        self.canonical_participant_id = self.canonical_participant_id or self.user_id
        return self


# --- Session / Turn (runtime representation) ---------------------------------

class Citation(_Base):
    id: str
    source: str | None = None
    title: str | None = None
    url: str | None = None
    snippet: str | None = None
    score: float | None = None


class ToolCall(_Base):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: str | None = None
    latency_ms: int | None = None


class Turn(_Base):
    turn_id: str = Field(default_factory=new_turn_id, min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=256)
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    trace_id: str | None = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class Session(_Base):
    session_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=256)
    channel: ChannelId = Field(max_length=64)
    adapter_id: str = Field(default="", max_length=64)
    connection_id: str = Field(default="", max_length=64)
    conversation_id: str = Field(default="", max_length=256)
    external_conversation_id: str = Field(default="", max_length=256)
    canonical_conversation_id: str = Field(default="", max_length=256)
    external_user_id: str = Field(default="", max_length=256)
    external_participant_id: str = Field(default="", max_length=256)
    canonical_participant_id: str = Field(default="", max_length=256)
    state: SessionState = SessionState.IDLE
    summary: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    pii_map: dict[str, str] = Field(
        default_factory=dict,
        description="Placeholder -> original PII value. Used for restoration on egress.",
    )
    turns: list[Turn] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_active_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- Preprocessing -----------------------------------------------------------

class Entity(_Base):
    type: str
    value: str
    start: int | None = None
    end: int | None = None


class PreprocessedMessage(_Base):
    original_text: str
    cleaned_text: str
    language: str = "zh"
    pii_map: dict[str, str] = Field(default_factory=dict)
    sensitive: bool = False
    block_reason: str | None = None
    intent_coarse: IntentCoarse = IntentCoarse.UNKNOWN
    emotion: EmotionLabel = EmotionLabel.NEUTRAL
    entities: list[Entity] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)


# --- Router ------------------------------------------------------------------

class RouteDecision(_Base):
    type: RouteType
    confidence: float = 0.0
    reason: str = ""
    hints: dict[str, Any] = Field(default_factory=dict)


# --- LLM contract ------------------------------------------------------------

class ChatMessage(_Base):
    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)


class ToolSchema(_Base):
    name: str
    description: str
    parameters: dict[str, Any]


class ChatRequest(_Base):
    tenant_id: str
    trace_id: str
    model_tier: Literal["tier-1", "tier-2", "tier-3"] = "tier-2"
    model: str | None = None
    messages: list[ChatMessage]
    system: str | None = None
    tools: list[ToolSchema] = Field(default_factory=list)
    stream: bool = False
    temperature: float = 0.3
    max_tokens: int = 1024
    cache_system: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatUsage(_Base):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


class ChatResponse(_Base):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    model: str = ""
    finish_reason: str = "stop"
    usage: ChatUsage = Field(default_factory=ChatUsage)
    latency_ms: int = 0


# --- Egress ------------------------------------------------------------------

class ReplySegment(_Base):
    type: ReplyType = ReplyType.TEXT
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutboundReply(_Base):
    reply_id: str = Field(default_factory=new_turn_id, min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    channel: ChannelId = Field(max_length=64)
    adapter_id: str = Field(default="", max_length=64)
    connection_id: str = Field(default="", max_length=64)
    user_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    conversation_id: str = Field(default="", max_length=256)
    external_conversation_id: str = Field(default="", max_length=256)
    canonical_conversation_id: str = Field(default="", max_length=256)
    external_user_id: str = Field(default="", max_length=256)
    external_participant_id: str = Field(default="", max_length=256)
    canonical_participant_id: str = Field(default="", max_length=256)
    type: ReplyType = ReplyType.TEXT
    segments: list[ReplySegment] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    quick_replies: list[str] = Field(default_factory=list)
    trace_id: str | None = Field(default=None, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def primary_text(self) -> str:
        if self.segments:
            return self.segments[0].content
        return ""


# --- Capability result envelope ---------------------------------------------

class CapabilityResult(_Base):
    """Internal: result from a capability engine (FAQ/RAG/Agent/LLM)."""

    route: RouteType
    reply_text: str
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: ChatUsage = Field(default_factory=ChatUsage)
    metadata: dict[str, Any] = Field(default_factory=dict)
