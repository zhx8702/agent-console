from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from app.commands import CommandDefinition
from app.common.types import Channel, MessageType
from app.orchestrator.pipeline import PipelineContext
from plugins.wxbot.agent_tools import WxbotAgentToolService
from plugins.wxbot.store import WxbotStore

_WXID_RE = re.compile(r"\bwxid_[A-Za-z0-9_]+\b")
_MENTION_RE = re.compile(r"@([^\s\u2005\u00a0]+)")
_DURATION_RE = re.compile(r"^(\d+)([smhd天分钟小时日]?)$", re.IGNORECASE)


def _should_handle_research_command(ctx: PipelineContext) -> bool:
    event = ctx.event
    return event.channel == Channel.WECHAT and event.message.type == MessageType.TEXT


def _should_handle_group_text_command(ctx: PipelineContext) -> bool:
    event = ctx.event
    return (
        event.channel == Channel.WECHAT
        and event.message.type == MessageType.TEXT
        and str(event.session_id or "").endswith("@chatroom")
    )


def _format_research_result(result: dict[str, object]) -> str:
    question = str(result.get("question") or "").strip()
    hours = int(result.get("time_window_hours") or 24)
    found = bool(result.get("found"))
    summary = str(result.get("summary") or "").strip()
    keywords = [str(item or "").strip() for item in (result.get("keywords") or []) if str(item or "").strip()]
    matched_keywords = [str(item or "").strip() for item in (result.get("matched_keywords") or []) if str(item or "").strip()]
    solution_hints = [item for item in (result.get("solution_hints") or []) if isinstance(item, dict)]
    messages = list(result.get("messages") or [])

    lines: list[str] = []
    lines.append(f"聊天记录 research（最近 {hours} 小时）")
    lines.append(f"问题：{question}")
    if summary:
        lines.append(f"结论：{summary}")
    if keywords:
        lines.append(f"检索关键词：{'、'.join(keywords[:6])}")
    if matched_keywords:
        lines.append(f"命中关键词：{'、'.join(matched_keywords[:6])}")
    if solution_hints:
        lines.append("可能的解决办法：")
        for index, item in enumerate(solution_hints[:3], start=1):
            sender_name = str(item.get("sender_name") or "未知成员").strip() or "未知成员"
            text = str(item.get("text") or "").strip()
            if len(text) > 90:
                text = text[:90] + "..."
            lines.append(f"{index}. {sender_name}: {text}")
    if not found or not messages:
        return "\n".join(lines)

    lines.append(f"命中消息：{int(result.get('total') or 0)} 条")
    lines.append("相关片段：")
    for index, item in enumerate(messages[:4], start=1):
        if not isinstance(item, dict):
            continue
        timestamp = str(item.get("timestamp") or "").strip() or "-"
        sender_name = str(item.get("sender_name") or item.get("sender_wxid") or "未知成员").strip() or "未知成员"
        text = str(item.get("text") or "").strip()
        if len(text) > 80:
            text = text[:80] + "..."
        lines.append(f"{index}. [{timestamp}] {sender_name}: {text}")
    return "\n".join(lines)


def _sender_wxid(ctx: PipelineContext) -> str:
    return str(
        ctx.event.metadata.get("sender_wxid")
        or ctx.event.metadata.get("sender_id")
        or ctx.event.user_id
        or ""
    ).strip()


def _bot_wxids(ctx: PipelineContext) -> set[str]:
    metadata = ctx.event.metadata if isinstance(ctx.event.metadata, dict) else {}
    values = {
        str(metadata.get(key) or "").strip()
        for key in (
            "bot_wxid",
            "self_wxid",
            "robot_wxid",
            "account_wxid",
            "login_wxid",
            "current_wxid",
        )
    }
    session = getattr(ctx, "session", None)
    if session is not None:
        values.add(str(getattr(session, "metadata", {}).get("bot_wxid") or "").strip())
        values.add(str(getattr(session, "metadata", {}).get("self_wxid") or "").strip())
        values.add(str(getattr(session, "variables", {}).get("bot_wxid") or "").strip())
        values.add(str(getattr(session, "variables", {}).get("self_wxid") or "").strip())
    return {item for item in values if item}


def _metadata_at_wxids(ctx: PipelineContext) -> list[str]:
    value = ctx.event.metadata.get("at_wxids")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                import json

                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                value = parsed
        if isinstance(value, str):
            for separator in (",", "，", ";", "；"):
                if separator in text:
                    return [item.strip() for item in text.split(separator) if item.strip()]
            return [text]
    if isinstance(value, (list, tuple, set)):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    return []


def _parse_duration(token: str) -> tuple[datetime | None, bool]:
    text = str(token or "").strip()
    if not text:
        return None, False
    match = _DURATION_RE.match(text)
    if not match:
        return None, False
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        return None, False
    if unit in {"", "m", "分钟"}:
        delta = timedelta(minutes=amount)
    elif unit in {"s"}:
        delta = timedelta(seconds=amount)
    elif unit in {"h", "小时"}:
        delta = timedelta(hours=amount)
    elif unit in {"d", "天", "日"}:
        delta = timedelta(days=amount)
    else:
        return None, False
    return datetime.now() + delta, True


def _format_ban_time(value: Any) -> str:
    if value is None:
        return "永久"
    text = str(value).replace("T", " ").split(".", 1)[0]
    return text or "永久"


def _member_names(member: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in ("display_name", "nickname", "nick_name", "remark", "alias", "name"):
        value = str(member.get(key) or "").strip()
        if value:
            names.add(value)
    return names


def _strip_target_tokens(args: list[str], target: dict[str, str], duration_used: bool) -> list[str]:
    wxid = target.get("wxid", "")
    names = {target.get("name", ""), *target.get("names", "").split("\n")}
    remaining: list[str] = []
    skipped_duration = False
    for raw in args:
        token = str(raw or "").strip()
        if not token:
            continue
        if wxid and wxid in token:
            continue
        if token.startswith("@") and token[1:] in names:
            continue
        if token in names:
            continue
        if duration_used and not skipped_duration and _parse_duration(token)[1]:
            skipped_duration = True
            continue
        remaining.append(token)
    return remaining


async def _resolve_ban_target(
    ctx: PipelineContext,
    service: WxbotAgentToolService,
    args: list[str],
) -> dict[str, str]:
    arg_text = " ".join(str(item or "").strip() for item in args if str(item or "").strip())
    explicit = _WXID_RE.search(arg_text)
    if explicit:
        wxid = explicit.group(0)
        return {"wxid": wxid, "name": wxid, "names": wxid}

    sender = _sender_wxid(ctx)
    bot_wxids = _bot_wxids(ctx)
    raw_at_wxids = _metadata_at_wxids(ctx)
    at_wxids = [wxid for wxid in raw_at_wxids if wxid and wxid != sender and wxid not in bot_wxids]
    if not bot_wxids and bool(ctx.event.metadata.get("mentioned_me")) and len(raw_at_wxids) > 1:
        at_wxids = [wxid for wxid in raw_at_wxids[1:] if wxid and wxid != sender]
    mention_names = [item.strip() for item in _MENTION_RE.findall(arg_text) if item.strip()]
    if at_wxids:
        if len(at_wxids) > 1:
            raise ValueError("一次只能禁言一个群成员")
        wxid = at_wxids[0]
        names = "\n".join({wxid, *mention_names})
        return {"wxid": wxid, "name": mention_names[0] if mention_names else wxid, "names": names}

    query = mention_names[0] if mention_names else (args[0].strip() if args else "")
    query = query.lstrip("@").strip()
    if not query:
        raise ValueError("用法：/ban @用户 [时长] [原因]")

    members = await service.list_group_roster_members(getattr(ctx, "session", None) or ctx.event)
    matches: list[dict[str, str]] = []
    for member in members:
        wxid = str(member.get("wxid") or "").strip()
        if not wxid:
            continue
        names = _member_names(member)
        if query in names:
            name = str(member.get("display_name") or next(iter(names), wxid)).strip() or wxid
            matches.append({"wxid": wxid, "name": name, "names": "\n".join(sorted(names))})
    if not matches:
        raise ValueError(f"未找到群成员：{query}")
    unique = {item["wxid"] for item in matches}
    if len(unique) != 1:
        raise ValueError(f"群成员昵称不唯一：{query}")
    return matches[0]


def _ensure_target_allowed(ctx: PipelineContext, target: dict[str, str]) -> None:
    wxid = target.get("wxid", "")
    if not wxid:
        raise ValueError("未找到要禁言的群成员")
    if wxid == _sender_wxid(ctx):
        raise ValueError("不能禁言自己")
    if wxid in _bot_wxids(ctx):
        raise ValueError("不能禁言机器人")


def build_wxbot_command_definitions(
    service: WxbotAgentToolService,
    store: WxbotStore | None = None,
) -> list[CommandDefinition]:
    async def _research_command(ctx: PipelineContext, args: list[str]) -> str:
        if not str(ctx.event.session_id or "").endswith("@chatroom"):
            raise ValueError("当前 research 命令先支持群聊会话")

        hours = 24
        question_args = list(args)
        if question_args and str(question_args[0] or "").isdigit():
            hours = max(1, min(int(question_args[0]), 24 * 14))
            question_args = question_args[1:]

        question = " ".join(str(item or "").strip() for item in question_args).strip()
        if not question:
            raise ValueError("用法：/research [小时数] 问题，例如 /research CRS 为什么建议迁移")

        result = await service.research_group_messages(
            getattr(ctx, "session", None) or ctx.event,
            {
                "question": question,
                "hours": hours,
                "limit": 6,
            },
        )
        return _format_research_result(result)

    async def _ban_command(ctx: PipelineContext, args: list[str]) -> str:
        if store is None:
            raise ValueError("wxbot store unavailable")
        target = await _resolve_ban_target(ctx, service, args)
        _ensure_target_allowed(ctx, target)
        expires_at = None
        duration_used = False
        for token in args:
            expires_at, duration_used = _parse_duration(token)
            if duration_used:
                break
        reason = " ".join(_strip_target_tokens(args, target, duration_used)).strip()
        row = await store.create_user_ban(
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            user_wxid=target["wxid"],
            user_name=target.get("name", ""),
            reason=reason,
            created_by=_sender_wxid(ctx),
            expires_at=expires_at,
        )
        name = str(row.get("user_name") or row.get("user_wxid") or target["wxid"])
        return f"已禁言 {name}，到期：{_format_ban_time(row.get('expires_at'))}"

    async def _unban_command(ctx: PipelineContext, args: list[str]) -> str:
        if store is None:
            raise ValueError("wxbot store unavailable")
        target = await _resolve_ban_target(ctx, service, args)
        revoked = await store.revoke_user_ban(
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            user_wxid=target["wxid"],
        )
        if not revoked:
            return "未找到该成员的有效禁言"
        return f"已解禁 {target.get('name') or target['wxid']}"

    async def _banlist_command(ctx: PipelineContext, args: list[str]) -> str:
        if store is None:
            raise ValueError("wxbot store unavailable")
        _ = args
        rows = await store.list_active_user_bans(ctx.event.tenant_id, ctx.event.session_id)
        if not rows:
            return "当前没有有效禁言"
        lines = ["当前禁言列表："]
        for index, row in enumerate(rows[:50], start=1):
            name = str(row.get("user_name") or row.get("user_wxid") or "").strip()
            wxid = str(row.get("user_wxid") or "").strip()
            expires = _format_ban_time(row.get("expires_at"))
            reason = str(row.get("reason") or "").strip()
            suffix = f"，原因：{reason}" if reason else ""
            lines.append(f"{index}. {name} ({wxid})，到期：{expires}{suffix}")
        return "\n".join(lines)

    return [
        CommandDefinition(
            plugin_name="wxbot",
            command="/research",
            aliases=("/查记录", "/搜记录", "/查聊天", "/搜聊天"),
            description="研究当前群最近聊天记录，判断某个问题有没有相关讨论",
            usage="/research [小时数] 问题",
            handler=_research_command,
            should_handle=_should_handle_research_command,
        ),
        CommandDefinition(
            plugin_name="wxbot",
            command="/ban",
            aliases=("/禁言",),
            description="禁言当前微信群成员",
            usage="/ban @用户 [时长] [原因]",
            handler=_ban_command,
            should_handle=_should_handle_group_text_command,
            admin_only=True,
        ),
        CommandDefinition(
            plugin_name="wxbot",
            command="/unban",
            aliases=("/解禁",),
            description="解除当前微信群成员禁言",
            usage="/unban @用户|wxid",
            handler=_unban_command,
            should_handle=_should_handle_group_text_command,
            admin_only=True,
        ),
        CommandDefinition(
            plugin_name="wxbot",
            command="/banlist",
            aliases=("/禁言列表",),
            description="查看当前微信群禁言列表",
            usage="/banlist",
            handler=_banlist_command,
            should_handle=_should_handle_group_text_command,
            admin_only=True,
        ),
    ]
