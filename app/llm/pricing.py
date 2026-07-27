"""
Per-model pricing table and cost computation helpers.

Prices are expressed in USD per 1,000,000 tokens as (input_rate, output_rate).

NOTE: These figures are *placeholders* derived from publicly listed pricing at
a point in time and should be updated by the operator to match their current
commercial agreement. The LLM service never decides business policy based on
these values; it only records an estimated cost for observability.
"""
from __future__ import annotations

# USD per 1M tokens: (input_rate, output_rate)
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # Claude family (approximate public list prices, placeholders)
    "claude-opus-4-6": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    # OpenAI family (approximate placeholders)
    "gpt-4o": (5.0, 15.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    # Embedding models
    "voyage-3": (0.12, 0.0),  # embeddings: input only
    # Fake provider is free
    "fake-chat": (0.0, 0.0),
    "fake-embed": (0.0, 0.0),
}

_DEFAULT_RATE: tuple[float, float] = (0.0, 0.0)


def get_rates(model: str) -> tuple[float, float]:
    """Return (input_rate_per_mtoken, output_rate_per_mtoken) for ``model``.

    Unknown models return (0.0, 0.0) so cost accounting degrades gracefully.
    """
    return MODEL_PRICES.get(model, _DEFAULT_RATE)


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute estimated USD cost for a completion.

    Returns 0.0 for unknown models (avoids incorrect billing signals).
    """
    in_rate, out_rate = get_rates(model)
    if in_rate == 0.0 and out_rate == 0.0:
        return 0.0
    cost = (input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate
    # Round to 8 decimals to avoid float noise in logs/metrics.
    return round(cost, 8)
