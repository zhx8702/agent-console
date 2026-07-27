#!/usr/bin/env python
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.common.config import Settings  # noqa: E402
from app.common.context import clear_context, set_trace_id  # noqa: E402
from app.common.types import (  # noqa: E402
    Channel,
    ChatResponse,
    ChatUsage,
    PreprocessedMessage,
    Role,
    Session,
    Turn,
)
from app.orchestrator.simple_capabilities import LLMCapabilityEngine  # noqa: E402


class _ObservationOnlyLLM:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {}

    async def chat(self, request: Any) -> ChatResponse:
        self.metadata = dict(request.metadata or {})
        return ChatResponse(
            content="",
            model="observation-only",
            finish_reason="stop",
            usage=ChatUsage(),
            latency_ms=0,
        )


def _load_metadata(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("metadata"), dict):
        return dict(payload["metadata"])
    if isinstance(payload, dict):
        return payload
    raise ValueError("metadata file must contain a JSON object")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    metadata = _load_metadata(args.metadata_json)
    llm = _ObservationOnlyLLM()
    engine = LLMCapabilityEngine(
        llm,
        settings=Settings(
            wxbot_sdk_url=args.wxbot_sdk_url,
            wxbot_preview_wait_seconds=args.wait_seconds,
        ),
    )
    session = Session(
        session_id=args.session_id,
        tenant_id="verify",
        user_id="verify-user",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content=args.content,
            trace_id="verify-trace",
            metadata=metadata,
        )
    ]
    set_trace_id("verify-trace")
    try:
        await engine.answer(
            PreprocessedMessage(original_text=args.content, cleaned_text=args.content),
            session,
        )
    finally:
        clear_context()
    observation = dict(llm.metadata.get("image_attachment_observation") or {})
    return {
        "source": observation.get("source"),
        "current_image_found": bool(observation.get("current_image_found")),
        "quote_image_found": bool(observation.get("quote_image_found")),
        "attachment_count": int(observation.get("attachment_count") or 0),
        "data_url_count": int(observation.get("data_url_count") or 0),
        "result": observation.get("result"),
        "reason": observation.get("reason"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify quote image attachment metadata without printing image data."
    )
    parser.add_argument("metadata_json", help="Path to JSON metadata or object with a metadata field.")
    parser.add_argument("--wxbot-sdk-url", default="http://127.0.0.1:5080")
    parser.add_argument("--session-id", default="verify@chatroom")
    parser.add_argument("--content", default="图片有什么内容")
    parser.add_argument("--wait-seconds", type=float, default=0.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
