from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def test_send_loop_does_not_claim_outbound_when_identity_is_unresolved(
    monkeypatch,
) -> None:
    calls: list[str] = []
    queue_store = ModuleType("queue_store")

    def unexpected(*args, **kwargs):
        _ = args, kwargs
        calls.append("queue_accessed")
        raise AssertionError("outbound queue must not be claimed")

    queue_store.recover_stale_outbound = unexpected
    queue_store.claim_outbound_pending = unexpected
    events = ModuleType("events")
    config = ModuleType("config")
    config.SEND_INTERVAL = 5
    runtime = ModuleType("sealed_core.runtime")
    runtime.require_capability = lambda _name: calls.append("capability_checked")
    sender_loader = ModuleType("sealed_core.sender_loader")
    sender_loader.send_batch = unexpected
    ingest_loader = ModuleType("sealed_core.ingest_loader")
    ingest_loader.identity_status = lambda refresh=False: {
        "ready": False,
        "self_wxid": "",
        "self_rowid": None,
        "reason": "self_rowid_missing",
    }
    sealed_core = ModuleType("sealed_core")
    sealed_core.sender_loader = sender_loader
    sealed_core.ingest_loader = ingest_loader
    monkeypatch.setitem(sys.modules, "queue_store", queue_store)
    monkeypatch.setitem(sys.modules, "events", events)
    monkeypatch.setitem(sys.modules, "config", config)
    monkeypatch.setitem(sys.modules, "sealed_core", sealed_core)
    monkeypatch.setitem(sys.modules, "sealed_core.runtime", runtime)
    monkeypatch.setitem(sys.modules, "sealed_core.sender_loader", sender_loader)
    monkeypatch.setitem(sys.modules, "sealed_core.ingest_loader", ingest_loader)

    path = Path(__file__).parents[2] / "wxbot_client" / "sealed_core" / "send_loop.py"
    spec = importlib.util.spec_from_file_location("_send_loop_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.tick() is None
    assert calls == ["capability_checked"]
