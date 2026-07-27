from __future__ import annotations

import os

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("OUTBOUND_HMAC_SECRET", "test_secret")
os.environ.setdefault("ADMIN_BEARER_TOKEN", "test_admin_token")
os.environ.setdefault("TENANT_DEMO_SECRET", "test_tenant_secret")

from app.common.logging import configure_logging

configure_logging()


@pytest.fixture
def tenant_id() -> str:
    return "demo"


@pytest.fixture
def session_id() -> str:
    return "se_test00000000000001"
