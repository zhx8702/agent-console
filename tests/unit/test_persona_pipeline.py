from __future__ import annotations

from plugins.persona_extract.pipeline import (
    aggregate_chunk_summaries,
    bounded_knowledge_sample,
    build_message_chunks,
)


def test_message_chunks_preserve_order_and_respect_budgets() -> None:
    lines = [f"消息-{index}-" + ("内容" * 8) for index in range(9)]

    chunks = build_message_chunks(lines, max_tokens=40, max_messages=3)

    assert [line for chunk in chunks for line in chunk.text.splitlines()] == lines
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.message_count <= 3 for chunk in chunks)
    assert all(chunk.estimated_tokens <= 40 for chunk in chunks)
    assert len({chunk.input_hash for chunk in chunks}) == len(chunks)


def test_single_oversized_message_remains_intact() -> None:
    oversized = "很长的单条消息" * 100

    chunks = build_message_chunks([oversized], max_tokens=20, max_messages=5)

    assert len(chunks) == 1
    assert chunks[0].text == oversized
    assert chunks[0].estimated_tokens > 20


def test_aggregate_is_deterministic_and_bounded_per_category() -> None:
    summaries = [
        {
            "tone_signals": ["直接", "简洁", "务实"],
            "work_traits": ["推进快"],
            "confidence": 0.8,
        },
        {
            "tone_signals": ["简洁", "直接", "幽默"],
            "work_traits": ["推进快", "重结果"],
            "confidence": 0.6,
        },
    ]

    aggregate = aggregate_chunk_summaries(summaries, max_items=2)

    assert aggregate["tone_signals"] == [
        {"value": "直接", "mentions": 2},
        {"value": "简洁", "mentions": 2},
    ]
    assert aggregate["work_traits"] == [
        {"value": "推进快", "mentions": 2},
        {"value": "重结果", "mentions": 1},
    ]
    assert aggregate["chunk_count"] == 2
    assert aggregate["mean_confidence"] == 0.7


def test_knowledge_sample_is_bounded_and_time_stratified() -> None:
    lines = [f"line-{index:02d}-" + ("x" * 20) for index in range(20)]

    sample = bounded_knowledge_sample(lines, max_chars=100)

    assert sum(len(line) + 1 for line in sample) <= 100
    assert sample == sorted(sample)
    assert any("line-00" in line for line in sample)
    assert any("line-10" in line for line in sample)
    assert any("line-19" in line for line in sample)
