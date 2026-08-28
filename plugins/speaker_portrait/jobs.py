"""Run one local-CLI portrait job instead of many remote LLM chunks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from app.common.logging import get_logger
from app.common.wxbot_auth import wxbot_sdk_headers
from app.egress.safe_http import safe_trusted_service_request
from plugins.local_agent.complete import complete_chat, resolve_local_backend
from plugins.persona_extract.store import PersonaExtractStore
from plugins.speaker_portrait.pipeline import (
    apply_coverage,
    build_portrait_prompt,
    compile_reply_style,
    merge_portrait,
    parse_portrait_payload,
    portrait_style_slug,
)
from plugins.speaker_portrait.store import SpeakerPortraitStore
from plugins.speaker_portrait.workspace import (
    build_tool_prompt,
    cleanup_workspaces,
    workspace_paths,
    write_messages_jsonl,
)

logger = get_logger(__name__)


async def collect_speaker_messages(store: SpeakerPortraitStore, job: dict[str, Any]) -> list[dict[str, Any]]:
    settings = store.settings
    sdk_url = str(getattr(settings, "wxbot_sdk_url", "") or "").rstrip("/")
    if not sdk_url:
        raise RuntimeError("wxbot_sdk_url is not configured")
    external_session_id = str(job.get("external_session_id") or job.get("session_id") or "").strip()
    if not external_session_id:
        raise RuntimeError("portrait_history_session_missing")
    days_limit = int(job.get("days_limit") or 0)
    max_messages = int(job.get("max_messages") or 0)
    payload = {
        "session_id": external_session_id,
        "target_wxid": str(job.get("speaker_id") or ""),
        "target_name": str(job.get("speaker_name") or ""),
        "days_limit": days_limit,
        "max_messages": max_messages,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        **wxbot_sdk_headers(settings),
    }
    full = days_limit <= 0 or max_messages <= 0
    timeout_seconds = 180.0 if full else 60.0
    max_bytes = 32 * 1024 * 1024 if full else 12 * 1024 * 1024
    async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
        resp = await safe_trusted_service_request(
            client,
            "POST",
            sdk_url,
            "/ext/persona/messages",
            json=payload,
            headers=headers,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_bytes,
            allowed_response_content_types=(
                "application/json",
                "application/problem+json",
                "text/plain",
            ),
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"wxbot sdk error: HTTP {resp.status_code}")
    try:
        body = resp.json() if resp.content else {}
    except ValueError as exc:
        raise RuntimeError("wxbot sdk returned an invalid portrait payload") from exc
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        messages = body.get("items") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        return []
    speaker_name = str(job.get("speaker_name") or job.get("speaker_id") or "User")
    cleaned: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        cleaned.append(
            {
                "sender_name": str(item.get("sender_name") or item.get("name") or speaker_name)[:256],
                "text": text[:8000],
                "timestamp": str(item.get("timestamp") or item.get("created_at") or "")[:64],
            }
        )
    since = str(job.get("since_timestamp") or "").strip()
    if since:
        cleaned = [item for item in cleaned if str(item.get("timestamp") or "") > since]
    return cleaned


async def sync_applied_styles(
    settings: Any,
    *,
    tenant_id: str,
    speaker_id: str,
    speaker_name: str,
    portrait: dict[str, Any],
    persona_store: PersonaExtractStore | None = None,
) -> int:
    """Recompile reply styles derived from this portrait after it changes.

    Applied styles live in persona profiles (the runtime injection surface);
    without this sync a hot update refreshes the portrait JSON but group
    replies keep using the stale compiled prompt.
    """

    persona_store = persona_store or PersonaExtractStore(settings)
    slug = portrait_style_slug(speaker_id)
    profiles = await persona_store.list_profiles_by_slug(tenant_id, slug)
    synced = 0
    for profile in profiles:
        name = str(
            profile.get("target_name")
            or profile.get("profile_name")
            or speaker_name
            or speaker_id
        )
        prompt = compile_reply_style(portrait, name=name)
        await persona_store.upsert_profile(
            tenant_id=tenant_id,
            session_id=str(profile.get("session_id") or ""),
            channel=str(profile.get("channel") or "wechat"),
            source_key=str(profile.get("source_key") or "wxbot"),
            source_label=str(profile.get("source_label") or name),
            profile_name=str(profile.get("profile_name") or name),
            prompt_text=prompt,
            enabled=bool(profile.get("enabled")),
            profile_id=int(profile["id"]),
            target_user_id=str(profile.get("target_user_id") or speaker_id),
            target_name=name,
            skill_slug=slug,
        )
        synced += 1
    return synced


async def run_portrait_job(store: SpeakerPortraitStore, job: dict[str, Any]) -> dict[str, Any]:
    settings = store.settings
    backend = resolve_local_backend(
        str(getattr(settings, "speaker_portrait_llm_backend", "grok") or "grok")
    )
    if not backend:
        raise RuntimeError("speaker_portrait_requires_local_cli")
    mode = str(job.get("mode") or "full").strip().lower()
    messages = await collect_speaker_messages(store, job)
    record = await store.get_portrait(
        tenant_id=str(job.get("tenant_id") or ""),
        speaker_id=str(job.get("speaker_id") or ""),
    )
    previous = record.get("portrait") if isinstance(record, dict) else None
    if isinstance(record, dict) and record.get("id"):
        history = await store.list_revision_portraits(int(record["id"]))
        folded: dict[str, Any] = {}
        for item in history:
            folded = merge_portrait(folded, item)
        if folded:
            previous = folded
    if mode == "incremental" and not messages:
        logger.info("speaker_portrait.incremental_empty", job_id=job.get("id"))
        await store.complete_empty_job(int(job["id"]))
        return {"skipped": True, "reason": "no_new_messages"}
    if not messages:
        raise RuntimeError("no_speaker_messages")
    max_chars = int(getattr(settings, "speaker_portrait_max_chars", 80_000) or 80_000)
    timeout = float(getattr(settings, "speaker_portrait_timeout_seconds", 900.0) or 900.0)
    inline_limit = int(getattr(settings, "speaker_portrait_inline_max_messages", 400) or 400)
    use_tools = mode != "incremental" or len(messages) > inline_limit
    cwd = ""
    max_turns = None
    paths = None
    speaker_name = str(job.get("speaker_name") or job.get("speaker_id") or "")
    if use_tools:
        data_dir = str(getattr(settings, "speaker_portrait_data_dir", "/data/portraits") or "/data/portraits")
        host_dir = str(getattr(settings, "speaker_portrait_host_dir", "") or data_dir)
        paths = workspace_paths(data_dir, int(job["id"]))
        written = write_messages_jsonl(paths["messages"], messages)
        paths["manifest"].write_text(
            json.dumps({"lines_total": written, "speaker_name": speaker_name}, ensure_ascii=False),
            encoding="utf-8",
        )
        if isinstance(previous, dict) and previous:
            paths["previous"].write_text(
                json.dumps(previous, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        system, user, stats = build_tool_prompt(
            speaker_name=speaker_name,
            messages=messages,
            previous_portrait=previous if isinstance(previous, dict) else None,
        )
        stats["used_messages"] = written
        cwd = str(Path(host_dir) / f"job-{int(job['id'])}")
        max_turns = int(getattr(settings, "speaker_portrait_max_turns", 40) or 40)
    else:
        system, user, stats = build_portrait_prompt(
            speaker_name=speaker_name,
            messages=messages,
            max_chars=max_chars,
            previous_portrait=previous if isinstance(previous, dict) else None,
        )
    result = await complete_chat(
        settings,
        backend=backend,
        system=system,
        user=user,
        timeout_seconds=timeout,
        cwd=cwd,
        max_turns=max_turns,
    )
    portrait = parse_portrait_payload(result.content)
    if previous:
        portrait = merge_portrait(previous, portrait)
    coverage_file = paths["coverage"] if paths is not None else None
    portrait = apply_coverage(
        portrait,
        lines_total=len(messages),
        coverage_file=coverage_file,
    )
    if not use_tools:
        portrait["coverage"] = {
            "lines_total": int(stats.get("used_messages") or len(messages)),
            "lines_read": int(stats.get("used_messages") or len(messages)),
            "complete": True,
        }
    if not portrait.get("summary") and not any(portrait.get(key) for key in ("likes", "topics")):
        raise RuntimeError("portrait_parse_empty")
    saved = await store.complete_job(
        int(job["id"]),
        portrait=portrait,
        evidence=stats,
        speaker_id=str(job.get("speaker_id") or ""),
        speaker_name=str(job.get("speaker_name") or ""),
        tenant_id=str(job.get("tenant_id") or ""),
        session_id=str(job.get("session_id") or ""),
        message_count=int(stats.get("used_messages") or 0),
        last_message_at=str((stats.get("time_span") or "").split(" ~ ")[-1] if stats.get("time_span") else ""),
        mode=mode,
    )
    if bool(getattr(settings, "speaker_portrait_style_sync_enabled", True)):
        try:
            synced = await sync_applied_styles(
                settings,
                tenant_id=str(job.get("tenant_id") or ""),
                speaker_id=str(job.get("speaker_id") or ""),
                speaker_name=str(job.get("speaker_name") or ""),
                portrait=portrait,
            )
            if synced:
                logger.info(
                    "speaker_portrait.style_synced",
                    job_id=job.get("id"),
                    profiles=synced,
                )
        except Exception:
            logger.warning(
                "speaker_portrait.style_sync_failed",
                job_id=job.get("id"),
                exc_info=True,
            )
    try:
        cleanup_workspaces(
            str(getattr(settings, "speaker_portrait_data_dir", "/data/portraits") or "/data/portraits"),
            int(job["id"]),
        )
    except Exception:
        logger.warning("speaker_portrait.workspace_cleanup_failed", exc_info=True)
    logger.info(
        "speaker_portrait.job_completed",
        job_id=job.get("id"),
        speaker_id=str(job.get("speaker_id") or "")[:32],
        used_messages=stats.get("used_messages"),
        backend=result.backend,
    )
    return {**saved, "portrait": portrait, "evidence": stats}
