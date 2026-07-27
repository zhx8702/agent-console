from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.orchestrator.pipeline import PipelineContext

CommandHandler = Callable[[PipelineContext, list[str]], Awaitable[str]]
CommandPredicate = Callable[[PipelineContext], bool]
CommandBillingMetadata = Callable[[PipelineContext, list[str]], dict[str, Any]]


class CommandSkip(Exception):
    """Raised by command definitions to indicate the command should be ignored."""


def normalize_command_token(value: str) -> str:
    token = str(value or "").strip().lower()
    if not token:
        return ""
    if not token.startswith("/"):
        token = f"/{token}"
    return token


@dataclass(frozen=True)
class CommandDefinition:
    plugin_name: str
    command: str
    handler: CommandHandler
    should_handle: CommandPredicate | None = None
    billing_metadata: CommandBillingMetadata | None = None
    description: str = ""
    admin_only: bool = False
    aliases: tuple[str, ...] = ()
    usage: str = ""

    def normalized_command(self) -> str:
        return normalize_command_token(self.command)

    def normalized_aliases(self) -> tuple[str, ...]:
        items: list[str] = []
        seen: set[str] = set()
        for alias in self.aliases:
            token = normalize_command_token(alias)
            if not token or token in seen or token == self.normalized_command():
                continue
            seen.add(token)
            items.append(token)
        return tuple(items)

    def all_tokens(self) -> tuple[str, ...]:
        return (self.normalized_command(), *self.normalized_aliases())

    def as_dict(self) -> dict[str, object]:
        return {
            "plugin_name": self.plugin_name,
            "owner": self.plugin_name,
            "command": self.normalized_command(),
            "aliases": list(self.normalized_aliases()),
            "description": self.description,
            "admin_only": self.admin_only,
            "usage": self.usage,
        }
