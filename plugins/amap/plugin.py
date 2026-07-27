from __future__ import annotations

from app.common.logging import get_logger
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.amap.agent import AMapAgentToolService, build_amap_agent_tools
from plugins.amap.router import _config_payload, build_amap_router

logger = get_logger(__name__)


class AMapPlugin(Plugin):
    meta = PluginMeta(
        name="amap",
        version="0.1.0",
        description="AMap personal map, POI search, and route planning agent tools",
    )

    def __init__(self) -> None:
        self._agent_tool_service: AMapAgentToolService | None = None
        self._ctx: PluginContext | None = None

    async def _scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        ctx = self._ctx
        registry = getattr(getattr(ctx, "container", None), "plugin_registry", None)
        gate = getattr(registry, "scope_execution_allowed", None)
        if not callable(gate):
            return False
        try:
            result = await gate(
                self.meta.name,
                tenant_id=str(tenant_id or "").strip(),
                session_id=str(session_id or "").strip(),
            )
        except Exception:
            return False
        return result is True

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._agent_tool_service = AMapAgentToolService(
            ctx.settings,
            channel_registry=getattr(ctx.container, "channel_registry", None),
            scope_execution_allowed=self._scope_execution_allowed,
            effect_reply_enabled=(
                bool(getattr(ctx.settings, "orchestrator_flow_runtime_enabled", False))
                and bool(
                    getattr(
                        ctx.settings,
                        "orchestrator_flow_effect_handlers_enabled",
                        False,
                    )
                )
            ),
        )
        logger.info("amap.initialized")

    async def shutdown(self) -> None:
        if self._agent_tool_service is not None:
            await self._agent_tool_service.close()
        self._agent_tool_service = None
        self._ctx = None

    def get_api_router(self):
        if self._ctx is None:
            return None
        return build_amap_router(self._ctx.settings)

    def get_agent_tools(self):
        if self._agent_tool_service is None:
            return []
        return build_amap_agent_tools(self._agent_tool_service)

    def get_permissions(self) -> list[str]:
        return ["network:amap", "storage:plugin", "agent_tools", "admin_api"]

    async def get_runtime_status(self) -> dict[str, object]:
        if self._ctx is None:
            return {"configured": False, "tools": []}
        return _config_payload(self._ctx.settings)


plugin = AMapPlugin()
