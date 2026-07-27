from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin.mutation_ledger import MutationOutcome, fingerprint
from app.common.config import Settings
from app.plugin.base import PluginContext
from plugins.tibo_reset.router import build_tibo_reset_router


class _RouterStore:
    async def run_admin_mutation(self, *, identity, audit, mutate):
        _ = (identity, audit)
        change = await mutate()
        return MutationOutcome(
            response=change.response,
            status_code=change.status_code,
            replayed=False,
            mutation_id="mutation-test",
        )

    async def list_feed(self, *, limit: int = 50):
        return [{"tweet_id": "1", "limit": limit}]

    async def list_deliveries(self, **kwargs):
        return [kwargs]


class _ReplayRouterStore(_RouterStore):
    def __init__(self) -> None:
        self.mutations: dict[tuple[str, str], tuple[str, MutationOutcome]] = {}

    async def run_admin_mutation(self, *, identity, audit, mutate):
        _ = audit
        key = (identity.operation, identity.idempotency_key)
        request_hash = fingerprint(identity.request_payload)
        existing = self.mutations.get(key)
        if existing is not None:
            previous = existing[1]
            assert existing[0] == request_hash
            return MutationOutcome(
                response=previous.response,
                status_code=previous.status_code,
                replayed=True,
                mutation_id=previous.mutation_id,
            )
        change = await mutate()
        outcome = MutationOutcome(
            response=change.response,
            status_code=change.status_code,
            replayed=False,
            mutation_id="tibo-replay-test",
        )
        self.mutations[key] = (request_hash, outcome)
        return outcome


class _RouterService:
    def __init__(self) -> None:
        self.poll_calls = 0

    async def status(self):
        return {"running": True}

    async def stats(self, *, timezone_name=None):
        return {"week_count": 5, "timezone": timezone_name or "Asia/Shanghai"}

    async def poll_once(self):
        self.poll_calls += 1
        return {"status": "completed"}


def test_tibo_reset_router_requires_admin_and_exposes_diagnostics() -> None:
    app = FastAPI()
    app.include_router(
        build_tibo_reset_router(
            _RouterStore(),
            _RouterService(),
            Settings(admin_bearer_token="secret"),
        )
    )
    with TestClient(app) as client:
        forbidden = client.get("/status")
        headers = {"Authorization": "Bearer secret"}
        status = client.get("/status", headers=headers)
        stats = client.get("/stats?timezone=Asia/Shanghai", headers=headers)
        run_once = client.post(
            "/poll/run-once",
            headers={**headers, "Idempotency-Key": "tibo-poll-test"},
        )
        feed = client.get("/feed?limit=3", headers=headers)

    assert forbidden.status_code == 401
    assert status.json() == {"running": True}
    assert stats.json() == {"week_count": 5, "timezone": "Asia/Shanghai"}
    assert run_once.json() == {"status": "completed"}
    assert feed.json()["items"][0]["limit"] == 3


def test_tibo_reset_manual_poll_requires_idempotency_key() -> None:
    app = FastAPI()
    app.include_router(
        build_tibo_reset_router(
            _RouterStore(),
            _RouterService(),
            Settings(admin_bearer_token="secret"),
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/poll/run-once",
            headers={"Authorization": "Bearer secret"},
        )

    assert response.status_code == 428


def test_tibo_reset_manual_poll_exactly_replays_lost_response() -> None:
    app = FastAPI()
    service = _RouterService()
    app.include_router(
        build_tibo_reset_router(
            _ReplayRouterStore(),
            service,
            Settings(admin_bearer_token="secret"),
        )
    )
    headers = {
        "Authorization": "Bearer secret",
        "Idempotency-Key": "tibo-lost-response",
    }
    with TestClient(app) as client:
        first = client.post("/poll/run-once", headers=headers)
        replay = client.post("/poll/run-once", headers=headers)

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {"status": "completed"}
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert service.poll_calls == 1


class _PluginStore:
    ensured = 0

    def __init__(self, settings) -> None:
        self.settings = settings

    async def ensure_tables(self) -> None:
        type(self).ensured += 1

    async def runtime_status(self):
        return {"initialized": True}

    async def reset_stats(self):
        return {"week_count": 5, "today_count": 1}


class _PluginClient:
    def __init__(self, *args, **kwargs) -> None:
        self.api_url = str(args[0])
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _PluginService:
    polls = 0

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def poll_once(self):
        type(self).polls += 1
        return {"status": "completed"}


@pytest.mark.asyncio
async def test_tibo_reset_plugin_only_schedules_in_scheduler_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.tibo_reset.plugin as module

    _PluginStore.ensured = 0
    _PluginService.polls = 0
    monkeypatch.setattr(module, "TiboResetStore", _PluginStore)
    monkeypatch.setattr(module, "TiboResetClient", _PluginClient)
    monkeypatch.setattr(module, "TiboResetService", _PluginService)
    monkeypatch.setattr(module, "WxbotStore", lambda settings: SimpleNamespace(settings=settings))
    monkeypatch.setattr(module, "WxbotChannelOutbound", lambda store: SimpleNamespace(store=store))

    # Runtime plugin discovery can replace the module object in ``sys.modules``.
    # Resolve the class from the same current module object that was patched so
    # its method globals cannot retain the real store from an older module.
    worker_plugin = module.TiboResetPlugin()
    worker_settings = SimpleNamespace(
        app_process_role="inbound",
        tibo_reset_enabled=True,
        tibo_reset_api_url="https://tibo-reset.test/api/resets",
        tibo_reset_request_timeout_seconds=1,
        tibo_reset_poll_interval_seconds=30,
        admin_bearer_token="secret",
    )
    await worker_plugin.initialize(
        PluginContext(
            container=SimpleNamespace(),
            settings=worker_settings,
            db_ok=True,
            redis_ok=False,
        )
    )
    await asyncio.sleep(0)

    assert _PluginStore.ensured == 1
    assert (await worker_plugin.get_runtime_status())["scheduler_enabled"] is False
    assert _PluginService.polls == 0
    await worker_plugin.shutdown()

    scheduler_plugin = module.TiboResetPlugin()
    scheduler_settings = SimpleNamespace(
        **{**worker_settings.__dict__, "app_process_role": "scheduler"}
    )
    await scheduler_plugin.initialize(
        PluginContext(
            container=SimpleNamespace(),
            settings=scheduler_settings,
            db_ok=True,
            redis_ok=False,
        )
    )
    await asyncio.sleep(0)

    assert (await scheduler_plugin.get_runtime_status())["scheduler_enabled"] is True
    assert (await scheduler_plugin.get_runtime_status())["stats"]["week_count"] == 5
    assert scheduler_plugin.get_pipeline_hooks()[0].name == "tibo_reset.intent"
    assert scheduler_plugin.get_flow_steps()[0].kind == "plugin.tibo_reset.intent"
    assert "plugin.tibo_reset.intent" in scheduler_plugin.get_flow_executors()
    assert "hooks:pipeline" in scheduler_plugin.get_permissions()
    assert _PluginService.polls >= 1
    await scheduler_plugin.shutdown()

    api_plugin = module.TiboResetPlugin()
    api_settings = SimpleNamespace(
        **{**worker_settings.__dict__, "app_process_role": "api"}
    )
    await api_plugin.initialize(
        PluginContext(
            container=SimpleNamespace(),
            settings=api_settings,
            db_ok=True,
            redis_ok=False,
        )
    )
    await asyncio.sleep(0)
    assert (await api_plugin.get_runtime_status())["scheduler_enabled"] is False
    await api_plugin.shutdown()


@pytest.mark.asyncio
async def test_tibo_reset_plugin_is_inert_without_deployment_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.tibo_reset.plugin as module

    def unexpected_dependency(*_args, **_kwargs):
        raise AssertionError("disabled plugin must not initialize outbound dependencies")

    _PluginStore.ensured = 0
    monkeypatch.setattr(module, "TiboResetStore", _PluginStore)
    monkeypatch.setattr(module, "TiboResetClient", unexpected_dependency)
    monkeypatch.setattr(module, "TiboResetService", unexpected_dependency)
    monkeypatch.setattr(module, "WxbotStore", unexpected_dependency)
    monkeypatch.setattr(module, "WxbotChannelOutbound", unexpected_dependency)
    disabled_plugin = module.TiboResetPlugin()

    await disabled_plugin.initialize(
        PluginContext(
            container=SimpleNamespace(),
            settings=SimpleNamespace(
                app_process_role="scheduler",
                tibo_reset_enabled=False,
            ),
            db_ok=True,
            redis_ok=False,
        )
    )

    status = await disabled_plugin.get_runtime_status()
    assert status["configured_enabled"] is False
    assert status["scheduler_enabled"] is False
    assert _PluginStore.ensured == 1
    assert disabled_plugin.get_api_router() is not None
    assert disabled_plugin.get_pipeline_hooks()[0].name == "tibo_reset.intent"
    assert disabled_plugin.get_flow_steps()[0].kind == "plugin.tibo_reset.intent"
    executor = disabled_plugin.get_flow_executors()["plugin.tibo_reset.intent"]
    result = await executor.run(SimpleNamespace())
    assert result.reason == "tibo_reset_disabled"
    await disabled_plugin.shutdown()
