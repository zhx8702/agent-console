"""Bounded conversation-window selection shared by LLM routes."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

TurnT = TypeVar("TurnT")


@dataclass(frozen=True)
class ContextWindow:
    """Selected recent turns plus cheap diagnostics for request auditing."""

    turns: list[TurnT]
    source_turns: int
    dropped_turns: int
    source_chars: int
    selected_chars: int


def select_recent_turns(
    turns: Iterable[TurnT],
    *,
    max_turns: int,
    max_chars: int,
    render: Callable[[TurnT], str],
) -> ContextWindow:
    """Keep the newest turns while respecting a character budget.

    The newest turn is always retained, even if it alone exceeds the budget;
    otherwise a long historical message can hide the actual user question.
    Selection walks backwards and returns chronological order, so it is safe
    to use directly as model context.
    """

    candidates = list(turns)
    source_turns = len(candidates)
    if max_turns > 0:
        candidates = candidates[-max_turns:]
    source_chars = sum(len(str(render(turn) or "")) for turn in candidates)
    budget = max(1, int(max_chars or 1))
    selected_reversed: list[TurnT] = []
    selected_chars = 0
    for index, turn in enumerate(reversed(candidates)):
        rendered_chars = len(str(render(turn) or ""))
        is_newest = index == 0
        if selected_reversed and selected_chars + rendered_chars > budget:
            continue
        if not selected_reversed and not is_newest and rendered_chars > budget:
            continue
        selected_reversed.append(turn)
        selected_chars += rendered_chars
    selected = list(reversed(selected_reversed))
    return ContextWindow(
        turns=selected,
        source_turns=source_turns,
        dropped_turns=max(0, source_turns - len(selected)),
        source_chars=source_chars,
        selected_chars=selected_chars,
    )
