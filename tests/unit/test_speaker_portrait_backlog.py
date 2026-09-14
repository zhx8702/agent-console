from __future__ import annotations

from datetime import UTC, datetime, timedelta

from plugins.speaker_portrait.jobs import clip_oldest, incremental_fetch_window


def _job(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 102,
        "mode": "incremental",
        "days_limit": 14,
        "max_messages": 800,
        "claimed_pending_messages": 1195,
        "since_timestamp": (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    base.update(overrides)
    return base


def test_fetch_window_covers_whole_claimed_backlog() -> None:
    # Production job 102: 1,195 pending but the SDK was asked for the newest
    # 800, so a day of history was skipped and the cursor jumped past it.
    days, fetch = incremental_fetch_window(_job())

    assert fetch >= 1195 + 200
    assert days >= 8


def test_fetch_window_widens_days_when_cursor_is_old() -> None:
    old_cursor = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    days, _fetch = incremental_fetch_window(_job(since_timestamp=old_cursor))

    assert days >= 41


def test_fetch_window_is_bounded() -> None:
    days, fetch = incremental_fetch_window(
        _job(
            claimed_pending_messages=999_999,
            since_timestamp=(datetime.now(UTC) - timedelta(days=3000)).isoformat(),
        )
    )

    assert fetch == 5000
    assert days == 365


def test_fetch_window_leaves_full_jobs_alone() -> None:
    assert incremental_fetch_window(_job(mode="full", days_limit=0, max_messages=0)) == (0, 0)
    assert incremental_fetch_window(_job(days_limit=90, max_messages=4000, claimed_pending_messages=0)) == (
        90,
        4000,
    )


def test_clip_oldest_keeps_contiguous_prefix_and_reports_split() -> None:
    messages = [
        {"text": "c", "timestamp": "2026-09-10 10:00:00"},
        {"text": "a", "timestamp": "2026-09-08 10:00:00"},
        {"text": "b", "timestamp": "2026-09-09 10:00:00"},
    ]

    kept, truncated = clip_oldest(messages, 2)

    assert truncated is True
    assert [item["text"] for item in kept] == ["a", "b"]

    kept_all, truncated_all = clip_oldest(messages, 0)
    assert truncated_all is False
    assert [item["text"] for item in kept_all] == ["a", "b", "c"]

    kept_fit, truncated_fit = clip_oldest(messages, 3)
    assert truncated_fit is False
    assert len(kept_fit) == 3
