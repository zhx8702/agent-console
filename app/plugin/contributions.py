"""One generation's collected plugin contributions before publication.

Container membership is frozen; callback objects remain trusted in-process
objects. Runtime collectors still validate declarations for descriptor drift.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class PluginContributionSet:
    meta: Any
    permissions: tuple[Any, ...]
    hooks: tuple[Any, ...]
    tools: tuple[Any, ...]
    router: Any
    steps: tuple[Any, ...]
    executors: Any
    effects: tuple[Any, ...]
    adapters: tuple[Any, ...]
    engines: Any
    media_provider: Any

    @classmethod
    def collect(cls, plugin: Any) -> PluginContributionSet:
        result = cls(
            meta=copy.deepcopy(plugin.meta),
            permissions=tuple(plugin.get_permissions()),
            hooks=tuple(plugin.get_pipeline_hooks()),
            tools=tuple(plugin.get_agent_tools()),
            router=plugin.get_api_router(),
            steps=tuple(plugin.get_flow_steps()),
            executors=MappingProxyType(dict(plugin.get_flow_executors())),
            effects=tuple(plugin.get_effect_handlers()),
            adapters=tuple(plugin.get_channel_adapters()),
            engines=MappingProxyType(dict(plugin.get_capability_engines())),
            media_provider=plugin.get_admin_media_event_provider(),
        )
        for step in result.steps:
            if str(step.owner).strip() != result.meta.name:
                raise RuntimeError(f"plugin flow step owner mismatch: {step.kind}")
        for kind, executor in result.executors.items():
            if str(getattr(executor, "owner", result.meta.name)).strip() != result.meta.name:
                raise RuntimeError(f"plugin flow executor owner mismatch: {kind}")
        return result

    def get_permissions(self):
        return self.permissions

    def get_pipeline_hooks(self):
        return self.hooks

    def get_agent_tools(self):
        return self.tools

    def get_api_router(self):
        return self.router

    def get_flow_steps(self):
        return self.steps

    def get_flow_executors(self):
        return self.executors

    def get_effect_handlers(self):
        return self.effects

    def get_channel_adapters(self):
        return self.adapters

    def get_capability_engines(self):
        return self.engines

    def get_admin_media_event_provider(self):
        return self.media_provider
