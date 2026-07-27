from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from app.common.logging import get_logger

logger = get_logger(__name__)


@dataclass
class AgentToolDefinition:
    scope: str
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    embed_text: str | None = None
    tree_text: str | None = None
    required_params: list[str] | None = None
    verb_type: str | None = None
    scopes: list[str] | None = None


class AgentToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, dict[str, AgentToolDefinition]] = {}
        self._owners: dict[str, set[tuple[str, str]]] = {}

    def register(self, tool: AgentToolDefinition, *, owner: str = "") -> None:
        self.register_many([tool], owner=owner)

    def register_many(self, tools: list[AgentToolDefinition], *, owner: str = "") -> int:
        """Register a contribution batch atomically.

        A tool name is an executable routing key, so silently replacing an
        existing definition would let discovery order change behavior and
        ownership.  The complete batch is normalized and checked before any
        registry state is mutated.
        """

        prepared = [self._prepare(tool, owner=owner) for tool in tools]
        batch_keys: set[tuple[str, str]] = set()
        for normalized, _tool_owner in prepared:
            key = (normalized.scope, normalized.name)
            if key in batch_keys:
                raise ValueError(
                    f"duplicate agent tool in registration batch: {normalized.scope}:{normalized.name}"
                )
            batch_keys.add(key)
            if normalized.name in self._tools.get(normalized.scope, {}):
                existing = self._tools[normalized.scope][normalized.name]
                existing_owner = str(
                    existing.metadata.get("owner")
                    or existing.metadata.get("source_plugin")
                    or ""
                ).strip()
                raise ValueError(
                    "duplicate agent tool registration: "
                    f"{normalized.scope}:{normalized.name} "
                    f"(existing_owner={existing_owner or 'compat'}, "
                    f"new_owner={_tool_owner or 'compat'})"
                )

        for normalized, tool_owner in prepared:
            self._tools.setdefault(normalized.scope, {})[normalized.name] = normalized
            if tool_owner:
                self._owners.setdefault(tool_owner, set()).add(
                    (normalized.scope, normalized.name)
                )
            logger.info(
                "agent.tool_registered",
                scope=normalized.scope,
                name=normalized.name,
                owner=tool_owner,
            )
        return len(prepared)

    def _prepare(
        self,
        tool: AgentToolDefinition,
        *,
        owner: str,
    ) -> tuple[AgentToolDefinition, str]:
        scope = str(tool.scope or "").strip()
        name = str(tool.name or "").strip()
        if not scope or not name:
            raise ValueError("agent tool requires non-empty scope and name")
        requested_owner = str(owner or "").strip()
        metadata = self._normalize_metadata(tool)
        declared_owners = {
            str(metadata.get(key) or "").strip()
            for key in ("owner", "source_plugin")
            if str(metadata.get(key) or "").strip()
        }
        if len(declared_owners) > 1:
            raise ValueError(f"agent tool ownership metadata disagrees: {scope}:{name}")
        declared_owner = next(iter(declared_owners), "")
        if requested_owner and declared_owner and requested_owner != declared_owner:
            raise ValueError(
                "agent tool owner mismatch: "
                f"{scope}:{name} expected={requested_owner!r} actual={declared_owner!r}"
            )
        tool_owner = requested_owner or declared_owner
        if tool_owner:
            metadata["owner"] = tool_owner
            metadata["source_plugin"] = tool_owner
        normalized = AgentToolDefinition(
            scope=scope,
            name=name,
            description=tool.description,
            parameters=dict(tool.parameters or {}),
            handler=tool.handler,
            metadata=metadata,
            embed_text=self._metadata_text(metadata, "embed_text"),
            tree_text=self._metadata_text(metadata, "tree_text"),
            required_params=self._metadata_list(metadata, "required_params"),
            verb_type=self._metadata_text(metadata, "verb_type"),
            scopes=self._metadata_list(metadata, "scopes"),
        )
        return normalized, tool_owner

    def list_tools(self, scope: str) -> list[AgentToolDefinition]:
        return list(self._tools.get(str(scope or "").strip(), {}).values())

    def unregister_owner(self, owner: str) -> int:
        owner = str(owner or "").strip()
        if not owner:
            return 0
        targets = self._owners.pop(owner, set())
        removed = 0
        for scope, name in targets:
            scope_tools = self._tools.get(scope)
            if scope_tools is None or name not in scope_tools:
                continue
            del scope_tools[name]
            removed += 1
            if not scope_tools:
                self._tools.pop(scope, None)
        return removed

    def catalog_by_owner(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for scope in self.scopes():
            for item in self.catalog(scope):
                owner = str(item.get("owner") or "").strip()
                if owner:
                    result.setdefault(owner, []).append(item)
        return result

    def catalog(self, scope: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in self.list_tools(scope):
            row: dict[str, Any] = {
                "scope": item.scope,
                "name": item.name,
                "description": item.description,
            }
            owner = str(item.metadata.get("owner") or item.metadata.get("source_plugin") or "").strip()
            if owner:
                row["owner"] = owner
            for key in (
                "embed_text",
                "tree_text",
                "required_params",
                "verb_type",
                "scopes",
                "channels",
                "session_kinds",
                "required_group_role",
            ):
                if key in item.metadata:
                    row[key] = deepcopy(item.metadata[key])
            items.append(row)
        return items

    def scopes(self) -> list[str]:
        return sorted(self._tools.keys())

    @classmethod
    def _normalize_metadata(cls, tool: AgentToolDefinition) -> dict[str, Any]:
        metadata = deepcopy(tool.metadata or {})
        for key in ("embed_text", "tree_text", "verb_type"):
            if key in metadata:
                text = str(metadata.get(key) or "").strip()
                if text:
                    metadata[key] = text
                else:
                    metadata.pop(key, None)
            value = getattr(tool, key, None)
            if value is not None:
                text = str(value).strip()
                if text:
                    metadata[key] = text
        scopes = cls._normalize_string_list(getattr(tool, "scopes", None))
        if scopes is None:
            scopes = cls._normalize_string_list(metadata.get("scopes"))
        if scopes is not None:
            metadata["scopes"] = scopes
        channels = cls._normalize_string_list(metadata.get("channels"))
        if channels is not None:
            metadata["channels"] = [item.lower() for item in channels]
        session_kinds = cls._normalize_string_list(metadata.get("session_kinds"))
        if session_kinds is not None:
            metadata["session_kinds"] = [item.lower() for item in session_kinds]
        required_params = cls._normalize_string_list(getattr(tool, "required_params", None))
        if required_params is None:
            required_params = cls._normalize_string_list(metadata.get("required_params"))
        if required_params is None:
            required_params = cls._normalize_string_list((tool.parameters or {}).get("required"))
        if required_params is not None:
            metadata["required_params"] = required_params
        return metadata

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            return None
        normalized = [str(item).strip() for item in items if str(item).strip()]
        return normalized if normalized else None

    @classmethod
    def _metadata_list(cls, metadata: dict[str, Any], key: str) -> list[str] | None:
        return cls._normalize_string_list(metadata.get(key))

    @staticmethod
    def _metadata_text(metadata: dict[str, Any], key: str) -> str | None:
        if key not in metadata:
            return None
        text = str(metadata.get(key) or "").strip()
        return text or None
