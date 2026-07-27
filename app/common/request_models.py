"""Pydantic base class for caller-controlled HTTP request bodies."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictRequestModel(BaseModel):
    """Reject misspelled or unsupported request fields instead of ignoring them."""

    model_config = ConfigDict(extra="forbid")
