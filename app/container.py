"""Strongly typed process-role service containers.

Production assembly returns one of :class:`ApiContainer`,
:class:`InboundContainer`, :class:`OutboundContainer`,
:class:`SchedulerContainer`, or :class:`WxbotBridgeContainer`.  Each role
makes its required dependencies
constructor arguments and validates feature-dependent collaborators at the
boundary.

``Container`` remains as a deliberately permissive compatibility fixture for
older unit tests and the separately assembled wxbot bridge.  It is never
returned by :func:`app.main.build_container`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    import httpx

    from app.admin.dlq_service import DLQAdminService
    from app.admin.stream_service import StreamAdminService
    from app.agent.registry import AgentToolRegistry
    from app.agent.store import AgentStore
    from app.billing import BillingCoordinator
    from app.bus import MessageBus
    from app.channel.registry import ChannelRegistry
    from app.common.capability import CapabilityEngine
    from app.common.types import RouteType
    from app.egress.dispatcher import OutboundDispatcher
    from app.faq.engine import FAQEngine
    from app.faq.store import FAQStore
    from app.kb.service import KnowledgeBaseService
    from app.kb.vector import VectorStore
    from app.llm import LLMProvider
    from app.llm.service import LLMService
    from app.orchestrator.effect_handlers import EffectHandlerRegistry
    from app.orchestrator.effect_log import PostgresEffectLog
    from app.orchestrator.engine import DialogOrchestrator
    from app.orchestrator.flow import FlowStep, FlowStepRegistry
    from app.plugin.manager import PluginManager
    from app.plugin.registry import PluginRegistry
    from app.postprocessing.processor import Postprocessor
    from app.preprocessing.processor import Preprocessor
    from app.rag.engine import RAGEngine
    from app.reliability import MessageOutboxRelay, MessageReliabilityStore
    from app.router.engine import Router
    from app.safety.service import SafetyService
    from app.session.manager import SessionManager
    from app.social.store import SocialPolicyStore
    from plugins.wxbot.store import WxbotStore


class ContainerDependencyError(RuntimeError):
    """Raised when a process role is assembled without a required service."""


def _require_dependencies(role: str, **dependencies: object | None) -> None:
    missing = sorted(name for name, value in dependencies.items() if value is None)
    if missing:
        raise ContainerDependencyError(
            f"{role} container is missing required dependencies: {', '.join(missing)}"
        )


@dataclass(kw_only=True, slots=True)
class CoreRuntimeContainer:
    """Typed plugin/orchestration services shared by API and inbound roles.

    Every field is explicitly supplied, including feature-disabled ``None``
    values.  This object is used briefly while plugins initialize, then becomes
    part of a concrete process-role container.
    """

    session_manager: SessionManager
    preprocessor: Preprocessor
    router: Router
    postprocessor: Postprocessor
    safety: SafetyService
    faq_engine: FAQEngine | None
    rag_engine: RAGEngine | None
    llm_service: LLMService
    llm_provider: LLMProvider
    vector_store: VectorStore | None
    capabilities: dict[RouteType, CapabilityEngine]
    plugin_registry: PluginRegistry
    plugin_manager: PluginManager | None
    agent_tool_registry: AgentToolRegistry
    channel_registry: ChannelRegistry
    flow_step_registry: FlowStepRegistry
    flow_step_executors: dict[str, FlowStep]
    flow_effect_handler_registry: EffectHandlerRegistry
    flow_effect_log: PostgresEffectLog | None
    billing: BillingCoordinator
    faq_store: FAQStore | None
    kb_service: KnowledgeBaseService | None
    agent_store: AgentStore | None
    vector_backend: str
    persistence_backend: str
    knowledge_features_enabled: bool

    def __post_init__(self) -> None:
        _require_dependencies(
            "core runtime",
            session_manager=self.session_manager,
            preprocessor=self.preprocessor,
            router=self.router,
            postprocessor=self.postprocessor,
            safety=self.safety,
            llm_service=self.llm_service,
            llm_provider=self.llm_provider,
            capabilities=self.capabilities,
            plugin_registry=self.plugin_registry,
            agent_tool_registry=self.agent_tool_registry,
            channel_registry=self.channel_registry,
            flow_step_registry=self.flow_step_registry,
            flow_step_executors=self.flow_step_executors,
            flow_effect_handler_registry=self.flow_effect_handler_registry,
            billing=self.billing,
        )
        if self.knowledge_features_enabled:
            _require_dependencies(
                "knowledge-enabled core runtime",
                vector_store=self.vector_store,
                faq_engine=self.faq_engine,
                rag_engine=self.rag_engine,
                faq_store=self.faq_store,
                kb_service=self.kb_service,
            )
        if self.persistence_backend == "postgres":
            _require_dependencies(
                "postgres core runtime",
                plugin_manager=self.plugin_manager,
                agent_store=self.agent_store,
            )

    @property
    def _agent_store(self) -> AgentStore | None:
        """One-release compatibility for plugins migrating to ``agent_store``."""

        return self.agent_store


@dataclass(kw_only=True, slots=True)
class ApiContainer(CoreRuntimeContainer):
    bus: MessageBus
    orchestrator: DialogOrchestrator
    message_store: MessageReliabilityStore
    dlq_admin_service: DLQAdminService
    stream_admin_service: StreamAdminService
    social_policy_store: SocialPolicyStore

    def __post_init__(self) -> None:
        super(ApiContainer, self).__post_init__()
        _require_dependencies(
            "api",
            bus=self.bus,
            orchestrator=self.orchestrator,
            message_store=self.message_store,
            dlq_admin_service=self.dlq_admin_service,
            stream_admin_service=self.stream_admin_service,
            social_policy_store=self.social_policy_store,
        )

    @classmethod
    def from_core(
        cls,
        core: CoreRuntimeContainer,
        *,
        bus: MessageBus,
        orchestrator: DialogOrchestrator,
        message_store: MessageReliabilityStore,
        dlq_admin_service: DLQAdminService,
        stream_admin_service: StreamAdminService,
        social_policy_store: SocialPolicyStore,
    ) -> ApiContainer:
        return cls(
            session_manager=core.session_manager,
            preprocessor=core.preprocessor,
            router=core.router,
            postprocessor=core.postprocessor,
            safety=core.safety,
            faq_engine=core.faq_engine,
            rag_engine=core.rag_engine,
            llm_service=core.llm_service,
            llm_provider=core.llm_provider,
            vector_store=core.vector_store,
            capabilities=core.capabilities,
            plugin_registry=core.plugin_registry,
            plugin_manager=core.plugin_manager,
            agent_tool_registry=core.agent_tool_registry,
            channel_registry=core.channel_registry,
            flow_step_registry=core.flow_step_registry,
            flow_step_executors=core.flow_step_executors,
            flow_effect_handler_registry=core.flow_effect_handler_registry,
            flow_effect_log=core.flow_effect_log,
            billing=core.billing,
            faq_store=core.faq_store,
            kb_service=core.kb_service,
            agent_store=core.agent_store,
            vector_backend=core.vector_backend,
            persistence_backend=core.persistence_backend,
            knowledge_features_enabled=core.knowledge_features_enabled,
            bus=bus,
            orchestrator=orchestrator,
            message_store=message_store,
            dlq_admin_service=dlq_admin_service,
            stream_admin_service=stream_admin_service,
            social_policy_store=social_policy_store,
        )


@dataclass(kw_only=True, slots=True)
class InboundContainer(CoreRuntimeContainer):
    bus: MessageBus
    orchestrator: DialogOrchestrator
    message_store: MessageReliabilityStore

    def __post_init__(self) -> None:
        super(InboundContainer, self).__post_init__()
        _require_dependencies(
            "inbound",
            bus=self.bus,
            orchestrator=self.orchestrator,
            message_store=self.message_store,
        )

    @classmethod
    def from_core(
        cls,
        core: CoreRuntimeContainer,
        *,
        bus: MessageBus,
        orchestrator: DialogOrchestrator,
        message_store: MessageReliabilityStore,
    ) -> InboundContainer:
        return cls(
            session_manager=core.session_manager,
            preprocessor=core.preprocessor,
            router=core.router,
            postprocessor=core.postprocessor,
            safety=core.safety,
            faq_engine=core.faq_engine,
            rag_engine=core.rag_engine,
            llm_service=core.llm_service,
            llm_provider=core.llm_provider,
            vector_store=core.vector_store,
            capabilities=core.capabilities,
            plugin_registry=core.plugin_registry,
            plugin_manager=core.plugin_manager,
            agent_tool_registry=core.agent_tool_registry,
            channel_registry=core.channel_registry,
            flow_step_registry=core.flow_step_registry,
            flow_step_executors=core.flow_step_executors,
            flow_effect_handler_registry=core.flow_effect_handler_registry,
            flow_effect_log=core.flow_effect_log,
            billing=core.billing,
            faq_store=core.faq_store,
            kb_service=core.kb_service,
            agent_store=core.agent_store,
            vector_backend=core.vector_backend,
            persistence_backend=core.persistence_backend,
            knowledge_features_enabled=core.knowledge_features_enabled,
            bus=bus,
            orchestrator=orchestrator,
            message_store=message_store,
        )


@dataclass(kw_only=True, slots=True)
class OutboundContainer:
    bus: MessageBus
    dispatcher: OutboundDispatcher
    http_client: httpx.AsyncClient
    message_store: MessageReliabilityStore
    outbox_relay: MessageOutboxRelay
    vector_backend: str = "disabled"
    persistence_backend: str = "postgres"
    knowledge_features_enabled: bool = False

    def __post_init__(self) -> None:
        _require_dependencies(
            "outbound",
            bus=self.bus,
            dispatcher=self.dispatcher,
            http_client=self.http_client,
            message_store=self.message_store,
            outbox_relay=self.outbox_relay,
        )


@dataclass(kw_only=True, slots=True)
class SchedulerContainer:
    """Dependencies used by the elected scheduler and scheduled plugins.

    A scheduler deliberately has no message bus, dialog orchestrator, session
    manager, request pipeline, admin services, or HTTP egress client.  It keeps
    only the services used by scheduled plugin jobs (memory extraction, group
    summaries/reports, activity generation, and draw recovery).
    """

    plugin_registry: PluginRegistry
    plugin_manager: PluginManager
    llm_service: LLMService
    vector_store: VectorStore | None
    capabilities: dict[RouteType, CapabilityEngine]
    agent_tool_registry: AgentToolRegistry
    channel_registry: ChannelRegistry
    billing: BillingCoordinator
    kb_service: KnowledgeBaseService | None
    agent_store: AgentStore
    social_policy_store: SocialPolicyStore
    vector_backend: str
    persistence_backend: str = "postgres"
    knowledge_features_enabled: bool = False

    def __post_init__(self) -> None:
        _require_dependencies(
            "scheduler",
            plugin_registry=self.plugin_registry,
            plugin_manager=self.plugin_manager,
            llm_service=self.llm_service,
            capabilities=self.capabilities,
            agent_tool_registry=self.agent_tool_registry,
            channel_registry=self.channel_registry,
            billing=self.billing,
            agent_store=self.agent_store,
            social_policy_store=self.social_policy_store,
        )
        if self.knowledge_features_enabled:
            _require_dependencies(
                "knowledge-enabled scheduler",
                vector_store=self.vector_store,
                kb_service=self.kb_service,
            )

    @property
    def _agent_store(self) -> AgentStore:
        """One-release compatibility for scheduled wxbot report services."""

        return self.agent_store

    @property
    def _kb_service(self) -> KnowledgeBaseService | None:
        """One-release compatibility for scheduled self-review services."""

        return self.kb_service


@dataclass(kw_only=True, slots=True)
class WxbotBridgeContainer:
    """Minimal dependencies for the SDK polling/delivery bridge role."""

    bus: MessageBus
    wxbot_store: WxbotStore
    social_policy_store: SocialPolicyStore
    vector_backend: str = "disabled"
    persistence_backend: str = "postgres"
    knowledge_features_enabled: bool = False

    def __post_init__(self) -> None:
        _require_dependencies(
            "wxbot bridge",
            bus=self.bus,
            wxbot_store=self.wxbot_store,
            social_policy_store=self.social_policy_store,
        )


RuntimeContainer: TypeAlias = (
    ApiContainer
    | InboundContainer
    | OutboundContainer
    | SchedulerContainer
    | WxbotBridgeContainer
)


@dataclass(kw_only=True)
class Container:
    """Permissive legacy/test fixture; production builders must not return it."""

    bus: MessageBus | None = None
    session_manager: SessionManager | None = None
    preprocessor: Preprocessor | None = None
    router: Router | None = None
    postprocessor: Postprocessor | None = None
    safety: SafetyService | None = None
    faq_engine: FAQEngine | None = None
    rag_engine: RAGEngine | None = None
    llm_service: LLMService | None = None
    llm_provider: LLMProvider | None = None
    vector_store: VectorStore | None = None
    dispatcher: OutboundDispatcher | None = None
    orchestrator: DialogOrchestrator | None = None
    capabilities: dict[RouteType, CapabilityEngine] = field(default_factory=dict)
    plugin_registry: PluginRegistry | None = None
    plugin_manager: PluginManager | None = None
    agent_tool_registry: AgentToolRegistry | None = None
    channel_registry: ChannelRegistry | None = None
    flow_step_registry: FlowStepRegistry | None = None
    flow_step_executors: dict[str, FlowStep] = field(default_factory=dict)
    flow_effect_handler_registry: EffectHandlerRegistry | None = None
    flow_effect_log: PostgresEffectLog | None = None
    billing: BillingCoordinator | None = None
    social_policy_store: SocialPolicyStore | None = None
    _http_client: httpx.AsyncClient | None = None
    _faq_store: FAQStore | None = None
    _kb_service: KnowledgeBaseService | None = None
    _dlq_admin_service: DLQAdminService | None = None
    _stream_admin_service: StreamAdminService | None = None
    _agent_store: AgentStore | None = None
    _message_store: MessageReliabilityStore | None = None
    _outbox_relay: MessageOutboxRelay | None = None
    _vector_backend: str = "unknown"
    _persistence_backend: str = "unknown"
    _knowledge_features_enabled: bool = False

    @property
    def http_client(self) -> httpx.AsyncClient | None:
        return self._http_client

    @property
    def faq_store(self) -> FAQStore | None:
        return self._faq_store

    @property
    def kb_service(self) -> KnowledgeBaseService | None:
        return self._kb_service

    @property
    def dlq_admin_service(self) -> DLQAdminService | None:
        return self._dlq_admin_service

    @property
    def stream_admin_service(self) -> StreamAdminService | None:
        return self._stream_admin_service

    @property
    def agent_store(self) -> AgentStore | None:
        return self._agent_store

    @property
    def message_store(self) -> MessageReliabilityStore | None:
        return self._message_store

    @property
    def outbox_relay(self) -> MessageOutboxRelay | None:
        return self._outbox_relay

    @property
    def vector_backend(self) -> str:
        return self._vector_backend

    @property
    def persistence_backend(self) -> str:
        return self._persistence_backend

    @property
    def knowledge_features_enabled(self) -> bool:
        return self._knowledge_features_enabled


ContainerLike: TypeAlias = RuntimeContainer | Container

_CONTAINER: ContainerLike | None = None


def get_container() -> ContainerLike:
    if _CONTAINER is None:
        raise RuntimeError("service container has not been initialized")
    return _CONTAINER


def set_container(container: ContainerLike) -> None:
    global _CONTAINER
    _CONTAINER = container


def reset_container() -> None:
    global _CONTAINER
    _CONTAINER = None
