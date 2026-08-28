from __future__ import annotations

from app.common.context_budget import select_recent_turns


def test_context_budget_keeps_newest_turn_and_drops_oldest_first() -> None:
    items = ["old-" + "x" * 30, "middle-" + "y" * 30, "new-question"]
    window = select_recent_turns(
        items,
        max_turns=10,
        max_chars=45,
        render=lambda item: item,
    )

    assert window.turns == [items[-1]]
    assert window.dropped_turns == 2
    assert window.selected_chars == len(items[-1])


def test_context_budget_respects_turn_count_and_returns_chronological_order() -> None:
    window = select_recent_turns(
        ["a", "b", "c", "d"],
        max_turns=2,
        max_chars=100,
        render=lambda item: item,
    )

    assert window.turns == ["c", "d"]
    assert window.source_turns == 4
    assert window.dropped_turns == 2
