from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.common.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def _documented_env_keys() -> set[str]:
    return {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


def test_every_memory_setting_is_documented_and_mapped_into_compose() -> None:
    memory_keys = {
        field_name.upper()
        for field_name in Settings.model_fields
        if field_name.startswith("memory_")
    }
    documented = _documented_env_keys()
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    app_env = set(compose["x-app-env"])

    assert memory_keys <= documented
    assert memory_keys <= app_env
    assert {f"COMPOSE_{key}" for key in memory_keys} <= documented


def test_memory_job_lease_must_outlive_the_processing_timeout() -> None:
    with pytest.raises(
        ValidationError,
        match="memory_llm_extraction_job_lock_ttl_seconds",
    ):
        Settings(
            _env_file=None,
            app_env="test",
            memory_llm_extraction_job_timeout_seconds=30,
            memory_llm_extraction_job_lock_ttl_seconds=30,
        )
