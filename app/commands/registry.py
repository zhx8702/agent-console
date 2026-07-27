from __future__ import annotations

from app.commands.models import CommandDefinition, normalize_command_token


class CommandRegistryService:
    def __init__(self) -> None:
        self._catalog: dict[tuple[str, str], CommandDefinition] = {}
        self._resolved: dict[str, CommandDefinition] = {}

    def register(self, definitions: list[CommandDefinition], *, owner: str = "") -> None:
        owner = str(owner or "").strip()
        for definition in definitions:
            normalized = definition.normalized_command()
            if not normalized:
                raise ValueError("command token cannot be empty")
            plugin_name = owner or definition.plugin_name
            stored = (
                definition
                if plugin_name == definition.plugin_name
                else CommandDefinition(
                    plugin_name=plugin_name,
                    command=definition.command,
                    handler=definition.handler,
                    should_handle=definition.should_handle,
                    billing_metadata=definition.billing_metadata,
                    description=definition.description,
                    admin_only=definition.admin_only,
                    aliases=definition.aliases,
                    usage=definition.usage,
                )
            )
            key = (plugin_name, normalized)
            self._catalog[key] = stored

        self._rebuild_index()

    def unregister_owner(self, owner: str) -> int:
        owner = str(owner or "").strip()
        if not owner:
            return 0
        keys = [key for key in self._catalog if key[0] == owner]
        for key in keys:
            self._catalog.pop(key, None)
        self._rebuild_index()
        return len(keys)

    def _rebuild_index(self) -> None:
        resolved: dict[str, CommandDefinition] = {}
        for definition in self._catalog.values():
            for token in definition.all_tokens():
                owner = resolved.get(token)
                if owner is not None and owner != definition:
                    raise ValueError(
                        f"duplicate command token registered: {token} "
                        f"({owner.plugin_name} vs {definition.plugin_name})"
                    )
                resolved[token] = definition
        self._resolved = resolved

    def resolve(self, token: str) -> CommandDefinition | None:
        return self._resolved.get(normalize_command_token(token))

    def catalog(self) -> list[dict[str, object]]:
        items = [definition.as_dict() for definition in self._catalog.values()]
        items.sort(key=lambda item: (str(item["plugin_name"]), str(item["command"])))
        return items
