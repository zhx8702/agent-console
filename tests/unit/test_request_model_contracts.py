from __future__ import annotations

from types import ModuleType

import pytest
from pydantic import BaseModel, ValidationError

from app.admin import kb_router
from plugins.amap import router as amap_router
from plugins.commands import router as commands_router
from plugins.credits import router as credits_router
from plugins.group_activity import router as group_activity_router
from plugins.memory import router as memory_router
from plugins.moderation import router as moderation_router
from plugins.persona_extract import router as persona_extract_router
from plugins.repeater import router as repeater_router
from plugins.wxbot import router as wxbot_router

_PUBLIC_PLUGIN_ROUTERS = (
    kb_router,
    amap_router,
    commands_router,
    credits_router,
    group_activity_router,
    memory_router,
    moderation_router,
    persona_extract_router,
    repeater_router,
    wxbot_router,
)


def _declared_models(module: ModuleType) -> list[type[BaseModel]]:
    return [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == module.__name__
    ]


@pytest.mark.parametrize("module", _PUBLIC_PLUGIN_ROUTERS)
def test_public_plugin_request_models_forbid_unknown_fields(module: ModuleType) -> None:
    models = _declared_models(module)

    assert models, module.__name__
    assert all(model.model_config.get("extra") == "forbid" for model in models)


def test_unknown_plugin_request_field_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="unexpected_field"):
        commands_router.CommandConfigUpdate(unexpected_field=True)
