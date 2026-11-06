from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_path = Path(__file__).parents[2] / "deploy/embedding/worker.py"
_spec = importlib.util.spec_from_file_location("embedding_worker", _path)
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)


def test_worker_rejects_unready_and_prioritizes_queries():
    scheduler = worker.Scheduler("http://not-used", "model")
    with pytest.raises(RuntimeError):
        scheduler.submit(["text"], background=False, timeout=1)
    scheduler.ready.set()
    low = scheduler.submit(["index"], background=True, timeout=1)
    high = scheduler.submit(["query"], background=False, timeout=1)
    assert scheduler.queue.get_nowait()[-1] is high
    assert scheduler.queue.get_nowait()[-1] is low


def test_worker_queue_has_finite_capacity():
    scheduler = worker.Scheduler("http://not-used", "model")
    scheduler.ready.set()
    for i in range(64):
        scheduler.submit([str(i)], background=True, timeout=1)
    with pytest.raises(worker.queue.Full):
        scheduler.submit(["overflow"], background=True, timeout=1)


def test_warmup_requires_odd_and_even_sequence_lengths():
    scheduler = worker.Scheduler("http://not-used", "model")
    scheduler.infer = lambda texts: ([], 2 * len(texts[0].split()) + 2)
    with pytest.raises(RuntimeError, match="coverage incomplete"):
        scheduler.warmup()
    assert not scheduler.ready.is_set()
    scheduler.infer = lambda texts: ([], len(texts[0].split()) + 2)
    scheduler.warmup()
    assert scheduler.ready.is_set()
