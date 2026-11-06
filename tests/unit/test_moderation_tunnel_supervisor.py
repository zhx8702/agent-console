import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

_spec = importlib.util.spec_from_file_location(
    "moderation_supervisor", Path(__file__).parents[2] / "deploy/moderation/tunnel_supervisor.py")
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


def test_readiness_requires_gpu_and_all_model_digests_but_allows_alias_deduplication():
    tags = {"models": [{"name": "one", "digest": "a"}, {"name": "alias", "digest": "a"}]}
    running = {"models": [{"digest": "a", "size_vram": 10}]}
    assert module.gpu_models_ready(tags, running, ["one", "alias"])
    assert not module.gpu_models_ready(tags, running, ["missing"])
    assert not module.gpu_models_ready(tags, {"models": [{"digest": "a", "size_vram": 0}]}, ["one"])
    assert not module.gpu_models_ready(tags, {"models": [{"digest": "other", "size_vram": 10}]}, ["one"])


def test_model_disappearance_withdraws_tunnel_before_attempting_warmup():
    supervisor = module.Supervisor.__new__(module.Supervisor)
    supervisor.primed = True
    supervisor.ready = Mock(return_value=False)
    calls = []
    supervisor.withdraw = lambda: calls.append("withdraw")

    def failed_warmup():
        calls.append("warmup")
        raise RuntimeError("model unavailable")

    supervisor.warmup = failed_warmup
    with pytest.raises(RuntimeError, match="model unavailable"):
        supervisor.tick()
    assert calls == ["withdraw", "warmup"]
    assert not supervisor.primed


def test_unready_model_cannot_publish_tunnel_even_if_warmup_returned(monkeypatch):
    supervisor = module.Supervisor.__new__(module.Supervisor)
    supervisor.primed = False
    supervisor.withdraw = Mock()
    supervisor.warmup = Mock()
    supervisor.ready = Mock(return_value=False)
    launch = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="not resident"):
        supervisor.tick()
    launch.assert_not_called()
