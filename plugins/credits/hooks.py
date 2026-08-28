"""
Pipeline hooks for credits checks, auto check-in, and deduction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agent.scopes import GROUP_PERSONAL_MAP_SCOPE, normalize_agent_scope
from app.channel.models import configuration_session_id
from app.commands import CommandDefinition
from app.common.intent_runtime import decision_from_pre
from app.common.logging import get_logger
from app.common.types import CapabilityResult, Channel, RouteType
from app.orchestrator.effect_handlers import effect_handler_opt_in_enabled
from app.orchestrator.effects import (
    EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
    EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT,
    EffectCommitRecord,
)
from app.orchestrator.flow import MessageEffect, StepResult
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort, HookPoint
from plugins.credits.intent import (
    CreditIntent,
    CreditIntentType,
    classify_credit_intent,
)
from plugins.credits.store import (
    CHECKIN_MODE_COMMAND,
    CHECKIN_MODE_MENTION_ONLY,
    CHECKIN_MODE_SILENT_ANY,
    CreditStore,
    normalize_checkin_mode,
)

logger = get_logger(__name__)


_AMAP_COMPLEX_RE = re.compile(r"(一日游|多日游|路线|行程|旅游|旅行|打卡|多点)")
_AMAP_EXPLICIT_MAP_RE = re.compile(
    r"((生成|创建|做成|整理成|标记到).{0,8}(高德)?地图|"
    r"(高德)?地图.{0,8}(二维码|分享)|"
    r"打卡地图|路线地图|地图二维码|生成二维码)"
)


def _strip_leading_mention_prefix(text: str) -> str:
    return re.sub(r"^\s*@[^@\s，,。？！?：:]{1,40}(?:\s+|$)", "", text).strip()


def _normalize_query_text(ctx: PipelineContext) -> str:
    normalized = str(
        ctx.event.metadata.get("wxbot_normalized_content")
        or ctx.event.metadata.get("cleaned_content")
        or ""
    ).strip()
    if normalized:
        return normalized
    if ctx.pre is not None:
        return str(ctx.pre.cleaned_text or ctx.pre.original_text or "").strip()
    return str(ctx.event.message.content or "").strip()


def _settings_int(store: CreditStore, name: str, default: int) -> int:
    settings = getattr(store, "settings", None)
    value = getattr(settings, name, default) if settings is not None else default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _config_int(cfg: dict | None, name: str, default: int) -> int:
    if not cfg or name not in cfg:
        return default
    try:
        return max(0, int(cfg.get(name) or 0))
    except (TypeError, ValueError):
        return default


def _amap_costs(store: CreditStore, cfg: dict | None = None) -> dict[str, int]:
    search_default = _settings_int(store, "amap_search_credit_cost", 2)
    map_default = _settings_int(store, "amap_map_credit_cost", 8)
    route_map_default = _settings_int(store, "amap_route_map_credit_cost", 12)
    return {
        "search": _config_int(cfg, "amap_search_credit_cost", search_default),
        "map": _config_int(cfg, "amap_map_credit_cost", map_default),
        "route_map": _config_int(cfg, "amap_route_map_credit_cost", route_map_default),
    }


def _is_amap_agent_context(ctx: PipelineContext) -> bool:
    scope = str(ctx.extras.get("agent_tool_scope") or "").strip()
    if not scope and ctx.route is not None and isinstance(ctx.route.hints, dict):
        scope = str(ctx.route.hints.get("agent_tool_scope") or "").strip()
    if not scope and ctx.result is not None and isinstance(ctx.result.metadata, dict):
        scope = str(ctx.result.metadata.get("agent_tool_scope") or "").strip()
    return normalize_agent_scope(scope) == GROUP_PERSONAL_MAP_SCOPE


def _is_group_context(ctx: PipelineContext) -> bool:
    session_kind = str(ctx.event.metadata.get("session_kind") or "").strip().lower()
    if session_kind in {"group", "chatroom", "channel", "guild"}:
        return True
    if session_kind in {"private", "dm", "direct"}:
        return False
    return str(ctx.event.session_id or "").endswith("@chatroom")


def _credit_user_id(ctx: PipelineContext) -> str:
    if ctx.event.channel == Channel.WECHAT:
        sender_wxid = str(ctx.event.metadata.get("sender_wxid") or "").strip()
        if sender_wxid:
            return sender_wxid
    return str(ctx.event.user_id or "").strip()


def _credit_session_id(ctx: PipelineContext) -> str:
    return configuration_session_id(ctx.event, ctx.session)


def _estimate_amap_agent_cost(ctx: PipelineContext, costs: dict[str, int]) -> int:
    if not _is_amap_agent_context(ctx):
        return 0
    text = _normalize_query_text(ctx)
    if _AMAP_EXPLICIT_MAP_RE.search(text):
        if _AMAP_COMPLEX_RE.search(text):
            return int(costs.get("route_map") or 0)
        return int(costs.get("map") or 0)
    return int(costs.get("search") or 0)

def _mentioned_credit_target_user_id(ctx: PipelineContext) -> str:
    at_wxids = ctx.event.metadata.get("at_wxids") or []
    if not isinstance(at_wxids, list):
        return ""
    values = [str(item or "").strip() for item in at_wxids if str(item or "").strip()]
    if not values:
        return ""
    if bool(ctx.event.metadata.get("mentioned_me")):
        return values[-1] if len(values) >= 2 else ""
    return values[0]


def _member_matches_target(item: dict, target: str) -> bool:
    lowered = target.lower()
    user_id = str(item.get("user_id") or "").strip().lower()
    display_name = str(item.get("display_name") or "").strip().lower()
    return lowered in {user_id, display_name}


async def _resolve_other_credit_member(
    store: CreditStore,
    ctx: PipelineContext,
    *,
    text: str,
    credit_name: str,
    target_user_id: str = "",
    target_display_name: str = "",
) -> dict[str, str] | None:
    if target_user_id:
        return {"user_id": target_user_id, "display_name": target_display_name}
    mentioned_user_id = _mentioned_credit_target_user_id(ctx)
    if mentioned_user_id:
        return {"user_id": mentioned_user_id, "display_name": ""}

    target = target_display_name
    if not target:
        return None

    members = await store.list_members(
        ctx.event.tenant_id,
        _credit_session_id(ctx),
        limit=20,
        query=target,
    )
    items = [item for item in members.get("items", []) if isinstance(item, dict)]
    exact = next((item for item in items if _member_matches_target(item, target)), None)
    matched = exact or (items[0] if items else None)
    if not matched:
        return {"user_id": "", "display_name": target}
    return {
        "user_id": str(matched.get("user_id") or "").strip(),
        "display_name": str(matched.get("display_name") or target).strip(),
    }


def _format_member_credit_snapshot(detail: dict) -> str:
    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}
    status = detail.get("checkin_status") if isinstance(detail.get("checkin_status"), dict) else {}
    credit_name = str(config.get("credit_name") or "积分")
    credits = int(detail.get("credits") or 0)
    rank = detail.get("rank")
    checked_in_today = bool(status.get("checked_in_today"))
    lines = [f"你当前有 {credits} {credit_name}。"]
    if rank:
        lines.append(f"当前排名：第 {int(rank)} 名。")
    if checked_in_today:
        reward = int(status.get("today_reward") or 0)
        streak = int(status.get("today_streak") or 0)
        lines.append(f"今日已签到，获得 {reward} {credit_name}。")
        if streak > 0:
            lines.append(f"当前连签：{streak} 天。")
    else:
        next_reward = int(status.get("next_reward") or 0)
        lines.append(f"今日未签到，现在签到可获得 {next_reward} {credit_name}。")
    return "\n".join(lines)


def _format_other_member_credit_snapshot(detail: dict, *, display_name: str = "") -> str:
    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}
    status = detail.get("checkin_status") if isinstance(detail.get("checkin_status"), dict) else {}
    credit_name = str(config.get("credit_name") or "积分")
    user_label = str(display_name or detail.get("display_name") or detail.get("user_id") or "该成员").strip()
    if not bool(detail.get("has_balance_record", True)):
        return f"没找到 {user_label} 的积分记录。"
    credits = int(detail.get("credits") or 0)
    rank = detail.get("rank")
    checked_in_today = bool(status.get("checked_in_today"))
    lines = [f"{user_label} 当前有 {credits} {credit_name}。"]
    if rank:
        lines.append(f"当前排名：第 {int(rank)} 名。")
    if checked_in_today:
        reward = int(status.get("today_reward") or 0)
        streak = int(status.get("today_streak") or 0)
        lines.append(f"今日已签到，获得 {reward} {credit_name}。")
        if streak > 0:
            lines.append(f"当前连签：{streak} 天。")
    else:
        lines.append("今日未签到。")
    return "\n".join(lines)


def _format_member_checkin_snapshot(detail: dict) -> str:
    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}
    status = detail.get("checkin_status") if isinstance(detail.get("checkin_status"), dict) else {}
    credit_name = str(config.get("credit_name") or "积分")
    if bool(status.get("checked_in_today")):
        reward = int(status.get("today_reward") or 0)
        streak = int(status.get("today_streak") or 0)
        return (
            f"今天已经签到。"
            f"本次获得 {reward} {credit_name}，当前连签 {streak} 天。"
        )
    next_reward = int(status.get("next_reward") or 0)
    mode_label = str(status.get("checkin_mode_label") or config.get("checkin_mode_label") or "").strip()
    suffix = f"当前签到模式：{mode_label}。" if mode_label else ""
    return f"今天还没签到，现在签到可获得 {next_reward} {credit_name}。{suffix}".strip()


def _format_member_rank_snapshot(detail: dict) -> str:
    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}
    credit_name = str(config.get("credit_name") or "积分")
    credits = int(detail.get("credits") or 0)
    rank = detail.get("rank")
    if rank:
        return f"你当前排第 {int(rank)} 名，余额是 {credits} {credit_name}。"
    return f"你当前有 {credits} {credit_name}，暂时还没有进入排行榜。"


def _format_checkin_result(cfg: dict, result: dict, *, rank: int | None = None) -> str:
    if result.get("already_checked_in"):
        return "今日已签到，请勿重复签到。"
    reward = int(result.get("reward") or 0)
    streak = int(result.get("streak") or 0)
    balance = int(result.get("balance") or 0)
    credit_name = str(cfg.get("credit_name") or "积分")
    lines = [f"签到成功！+{reward} {credit_name}"]
    bonus = int(result.get("bonus") or 0)
    if bonus > 0:
        lines.append(f"连签 {streak} 天，额外 +{bonus}")
    lines.append(f"当前积分：{balance} {credit_name}")
    if rank:
        lines.append(f"当前排名：第 {int(rank)} 名")
    return "\n".join(lines)


def _checkin_mode_label(cfg: dict, mode: int) -> str:
    return str(cfg.get("checkin_mode_label") or f"模式 {mode}").strip()


def _manual_checkin_tip(cfg: dict, *, source: str = "command") -> str:
    mode = normalize_checkin_mode(cfg.get("checkin_mode", CHECKIN_MODE_COMMAND))
    label = _checkin_mode_label(cfg, mode)
    if mode == CHECKIN_MODE_COMMAND:
        return f"当前群签到模式为：{label}\n请发送 /签到 完成签到。"
    if mode == CHECKIN_MODE_SILENT_ANY:
        target = "/签到" if source == "command" else "签到"
        return f"当前群签到模式为：{label}\n无需手动发送「{target}」，普通发言会自动签到。"
    target = "/签到" if source == "command" else "@签到"
    return f"当前群签到模式为：{label}\n无需手动发送「{target}」，@ 机器人发言会自动签到。"


async def _format_checkin_result_with_detail(
    store: CreditStore,
    ctx: PipelineContext,
    cfg: dict,
    result: dict,
) -> str:
    rank: int | None = None
    try:
        detail = await store.get_member_detail(
            ctx.event.tenant_id,
            _credit_session_id(ctx),
            _credit_user_id(ctx),
            ledger_limit=5,
        )
        raw_rank = detail.get("rank") if isinstance(detail, dict) else None
        rank = int(raw_rank) if raw_rank else None
    except Exception:
        rank = None
    return _format_checkin_result(cfg, result, rank=rank)


async def _maybe_auto_checkin(store: CreditStore, ctx: PipelineContext, cfg: dict) -> dict:
    if ctx.extras.get("_credits_auto_checkin_done"):
        existing = ctx.extras.get("_credits_auto_checkin_result")
        return existing if isinstance(existing, dict) else {}
    if not _should_auto_checkin(ctx, cfg):
        return {}

    result = await store.checkin(
        ctx.event.tenant_id,
        _credit_session_id(ctx),
        _credit_user_id(ctx),
        display_name=str(ctx.event.metadata.get("sender_name") or ""),
    )
    ctx.extras["_credits_auto_checkin_done"] = True
    ctx.extras["_credits_auto_checkin_result"] = result
    return result



def _display_name(value: str, fallback: str) -> str:
    cleaned = str(value or "").strip()
    return cleaned or fallback


async def _cmd_checkin(store: CreditStore, ctx: PipelineContext, cfg: dict) -> str:
    if not cfg.get("enabled"):
        return "当前会话未开启积分系统"
    mode = normalize_checkin_mode(cfg.get("checkin_mode", CHECKIN_MODE_COMMAND))
    if _is_group_context(ctx) and mode != CHECKIN_MODE_COMMAND:
        return _manual_checkin_tip(cfg, source="command")
    result = await store.checkin(
        ctx.event.tenant_id,
        _credit_session_id(ctx),
        _credit_user_id(ctx),
        display_name=str(ctx.event.metadata.get("sender_name") or ""),
    )
    return await _format_checkin_result_with_detail(store, ctx, cfg, result)


async def _cmd_balance(store: CreditStore, ctx: PipelineContext, cfg: dict) -> str:
    if not cfg.get("enabled"):
        return "当前会话未开启积分系统"
    balance = await store.get_balance(
        ctx.event.tenant_id,
        _credit_session_id(ctx),
        _credit_user_id(ctx),
        display_name=str(ctx.event.metadata.get("sender_name") or ""),
    )
    credit_name = str(cfg.get("credit_name") or "积分")
    return f"当前余额：{balance} {credit_name}"


async def _cmd_top(store: CreditStore, ctx: PipelineContext, cfg: dict) -> str:
    if not cfg.get("enabled"):
        return "当前会话未开启积分系统"
    rows = await store.get_top(
        ctx.event.tenant_id,
        _credit_session_id(ctx),
        limit=10,
    )
    credit_name = str(cfg.get("credit_name") or "积分")
    if not rows:
        return f"暂无 {credit_name} 记录"
    lines = [f"{credit_name} 榜 Top 10："]
    for index, row in enumerate(rows, 1):
        user_id = str(row.get("user_id") or "")
        display_name = _display_name(row.get("display_name") or "", user_id)
        credits = int(row.get("credits") or 0)
        lines.append(f"  {index}. {display_name} ({user_id}) — {credits}")
    return "\n".join(lines)


def _parse_amount(text: str) -> int:
    try:
        amount = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("金额须为整数") from exc
    if amount <= 0:
        raise ValueError("金额须为正整数")
    return amount


def _parse_target_user(arg: str) -> str:
    target = str(arg or "").strip().lstrip("@")
    if not target:
        raise ValueError("请提供目标 user_id")
    return target


async def _cmd_transfer(store: CreditStore, ctx: PipelineContext, cfg: dict, args: list[str]) -> str:
    if not cfg.get("enabled"):
        return "当前会话未开启积分系统"
    if len(args) < 2:
        return "用法：/转账 <user_id> <数量>"
    target_user_id = _parse_target_user(args[0])
    amount = _parse_amount(args[1])
    balances = await store.transfer(
        ctx.event.tenant_id,
        _credit_session_id(ctx),
        _credit_user_id(ctx),
        target_user_id,
        amount,
        actor=_credit_user_id(ctx),
        reference=ctx.event.trace_id,
    )
    credit_name = str(cfg.get("credit_name") or "积分")
    return (
        f"转账成功：→ {target_user_id} +{amount} {credit_name}\n"
        f"你的余额：{balances['from_balance']} {credit_name}\n"
        f"对方余额：{balances['to_balance']} {credit_name}"
    )


async def _cmd_grant(store: CreditStore, ctx: PipelineContext, cfg: dict, args: list[str]) -> str:
    if not cfg.get("enabled"):
        return "当前会话未开启积分系统"
    if len(args) < 2:
        return "用法：/赠送 <user_id> <数量>"
    target_user_id = _parse_target_user(args[0])
    amount = _parse_amount(args[1])
    balance = await store.adjust(
        ctx.event.tenant_id,
        _credit_session_id(ctx),
        target_user_id,
        amount,
        "admin_grant",
        actor=_credit_user_id(ctx),
        reference=ctx.event.trace_id,
        display_name="",
    )
    credit_name = str(cfg.get("credit_name") or "积分")
    return f"[管理员] 赠送 {target_user_id} {amount} {credit_name}\n对方余额：{balance}"


def _checkin_mode_usage() -> str:
    return (
        "用法：/sign-in mode 1|2|3\n"
        "1 = 发送 /签到 主动签到\n"
        "2 = 群里发送任意消息静默签到\n"
        "3 = 发送带 @ 的消息静默签到"
    )


async def _cmd_signin_mode(store: CreditStore, ctx: PipelineContext, cfg: dict, args: list[str]) -> str:
    if not _is_group_context(ctx):
        return "签到模式切换仅支持群聊"
    current_mode = normalize_checkin_mode(cfg.get("checkin_mode", CHECKIN_MODE_COMMAND))
    current_label = cfg.get("checkin_mode_label") or f"模式 {current_mode}"
    if not args:
        return f"当前签到模式：{current_label}\n{_checkin_mode_usage()}"

    mode_arg = args[0]
    if mode_arg.lower() in ("mode", "模式"):
        if len(args) < 2:
            return _checkin_mode_usage()
        mode_arg = args[1]
    try:
        next_mode = normalize_checkin_mode(mode_arg)
    except ValueError:
        return _checkin_mode_usage()
    updated = await store.set_config(
        ctx.event.tenant_id,
        _credit_session_id(ctx),
        checkin_mode=next_mode,
    )
    return f"已切换签到模式：{updated.get('checkin_mode_label') or next_mode}"


def build_credit_command_definitions(store: CreditStore) -> list[CommandDefinition]:
    async def _handle_checkin(ctx: PipelineContext, args: list[str]) -> str:
        cfg = await store.get_config(ctx.event.tenant_id, _credit_session_id(ctx))
        return await _cmd_checkin(store, ctx, cfg)

    async def _handle_balance(ctx: PipelineContext, args: list[str]) -> str:
        cfg = await store.get_config(ctx.event.tenant_id, _credit_session_id(ctx))
        return await _cmd_balance(store, ctx, cfg)

    async def _handle_top(ctx: PipelineContext, args: list[str]) -> str:
        cfg = await store.get_config(ctx.event.tenant_id, _credit_session_id(ctx))
        return await _cmd_top(store, ctx, cfg)

    async def _handle_transfer(ctx: PipelineContext, args: list[str]) -> str:
        cfg = await store.get_config(ctx.event.tenant_id, _credit_session_id(ctx))
        return await _cmd_transfer(store, ctx, cfg, args)

    async def _handle_grant(ctx: PipelineContext, args: list[str]) -> str:
        cfg = await store.get_config(ctx.event.tenant_id, _credit_session_id(ctx))
        return await _cmd_grant(store, ctx, cfg, args)

    async def _handle_signin_mode(ctx: PipelineContext, args: list[str]) -> str:
        cfg = await store.get_config(ctx.event.tenant_id, _credit_session_id(ctx))
        return await _cmd_signin_mode(store, ctx, cfg, args)

    return [
        CommandDefinition(
            plugin_name="credits",
            command="/签到",
            aliases=("/checkin",),
            description="主动签到并领取当日积分奖励",
            usage="/签到",
            handler=_handle_checkin,
        ),
        CommandDefinition(
            plugin_name="credits",
            command="/余额",
            aliases=("/balance",),
            description="查看当前会话里的积分余额",
            usage="/余额",
            handler=_handle_balance,
        ),
        CommandDefinition(
            plugin_name="credits",
            command="/榜单",
            aliases=("/top", "/rank", "/积分排名"),
            description="查看当前会话的积分排行榜",
            usage="/榜单",
            handler=_handle_top,
        ),
        CommandDefinition(
            plugin_name="credits",
            command="/转账",
            aliases=("/transfer",),
            description="给当前会话里的其他成员转账积分",
            usage="/转账 <user_id> <数量>",
            handler=_handle_transfer,
        ),
        CommandDefinition(
            plugin_name="credits",
            command="/赠送",
            aliases=("/grant",),
            description="管理员给成员补发积分",
            admin_only=True,
            usage="/赠送 <user_id> <数量>",
            handler=_handle_grant,
        ),
        CommandDefinition(
            plugin_name="credits",
            command="/sign-in",
            aliases=("/signin", "/签到模式"),
            description="管理员切换群签到模式",
            admin_only=True,
            usage="/sign-in mode 1|2|3",
            handler=_handle_signin_mode,
        ),
    ]


def _should_auto_checkin(ctx: PipelineContext, cfg: dict) -> bool:
    if not _is_group_context(ctx):
        return False
    if bool(ctx.event.metadata.get("is_self_sent")):
        return False

    mode = normalize_checkin_mode(cfg.get("checkin_mode", CHECKIN_MODE_COMMAND))
    content = str(ctx.event.message.content or "").strip()
    if mode == CHECKIN_MODE_COMMAND or not content:
        return False
    if mode == CHECKIN_MODE_SILENT_ANY:
        return True
    if mode == CHECKIN_MODE_MENTION_ONLY:
        return bool(
            ctx.event.metadata.get("mentioned_me")
            or (ctx.event.metadata.get("at_wxids") or [])
        )
    return False


def _insufficient_tip(cfg: dict, credit_name: str, *, auto_checked_in: bool = False) -> str:
    if auto_checked_in:
        return f"{credit_name}不足，请补充后再试。"
    mode = normalize_checkin_mode(cfg.get("checkin_mode", CHECKIN_MODE_COMMAND))
    if mode == CHECKIN_MODE_COMMAND:
        return f"{credit_name}不足，请先签到。"
    return f"{credit_name}不足，请先签到或补充。"


@dataclass
class CreditAutoCheckinHook:
    store: CreditStore
    name: str = "credits.auto_checkin"
    point: HookPoint = HookPoint.BEFORE_ROUTE
    # Run after command dispatch, but before wxbot group reply-policy suppression.
    priority: int = 12

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.session is None:
            return
        cfg = await self.store.get_config(
            ctx.event.tenant_id,
            _credit_session_id(ctx),
        )
        if not cfg.get("enabled"):
            return
        await _maybe_auto_checkin(self.store, ctx, cfg)


@dataclass
class CreditDeductionHook:
    store: CreditStore
    name: str = "credits.deduction"
    point: HookPoint = HookPoint.BEFORE_CAPABILITY
    priority: int = 10

    async def run(self, ctx: PipelineContext) -> None:
        event = ctx.event
        session = ctx.session
        if session is None:
            return

        credit_session_id = _credit_session_id(ctx)
        cfg = await self.store.get_config(event.tenant_id, credit_session_id)
        if not cfg.get("enabled"):
            return

        if _should_auto_checkin(ctx, cfg) and not ctx.extras.get("_credits_auto_checkin_done"):
            auto_result = await _maybe_auto_checkin(self.store, ctx, cfg)

        amap_estimated_cost = _estimate_amap_agent_cost(ctx, _amap_costs(self.store, cfg))
        cost = amap_estimated_cost or int(cfg.get("cost_per_chat", 0) or 0)
        if cost <= 0:
            return

        auto_result = ctx.extras.get("_credits_auto_checkin_result") or {}
        balance = await self.store.peek_balance(
            event.tenant_id,
            credit_session_id,
            _credit_user_id(ctx),
            display_name=str(event.metadata.get("sender_name") or ""),
        )
        if balance < cost:
            credit_name = str(cfg.get("credit_name", "积分") or "积分")
            raise HookAbort(
                _insufficient_tip(
                    cfg,
                    credit_name,
                    auto_checked_in=bool(auto_result.get("checked_in")),
                ),
                reason="insufficient_credits",
            )

        ctx.extras["_credits_cost"] = cost
        ctx.extras["_credits_cfg"] = cfg
        if amap_estimated_cost > 0:
            ctx.extras["_credits_agent_billing"] = "amap"
            return
        reserve_charge = getattr(self.store, "reserve_charge", None)
        if not callable(reserve_charge):
            return
        try:
            reservation = await reserve_charge(
                event.tenant_id,
                credit_session_id,
                _credit_user_id(ctx),
                cost,
                reason="chat_cost",
                reference=event.trace_id,
                display_name=str(event.metadata.get("sender_name") or ""),
                metadata={"resource_kind": "chat", "resource_operation": "llm"},
                idempotency_key=f"chat:llm:{event.trace_id}" if event.trace_id else "",
            )
        except ValueError as exc:
            credit_name = str(cfg.get("credit_name", "积分") or "积分")
            raise HookAbort(
                _insufficient_tip(
                    cfg,
                    credit_name,
                    auto_checked_in=bool(auto_result.get("checked_in")),
                ),
                reason="insufficient_credits",
            ) from exc
        ctx.extras["_credits_reservation_id"] = reservation.get("reservation_id", "")


def _sync_credit_intent_signal(ctx: PipelineContext, intent: CreditIntent) -> dict[str, object]:
    signal: dict[str, object] = {
        "type": intent.type.value,
        "confidence": intent.confidence,
        "reason": intent.reason,
        "should_handle": intent.should_handle,
    }
    if intent.target_user_id:
        signal["target_user_id"] = intent.target_user_id
    if intent.display_name:
        signal["display_name"] = intent.display_name
    if intent.amount is not None:
        signal["amount"] = intent.amount
    ctx.signals.setdefault("credits", {})["intent"] = signal
    return signal


@dataclass
class CreditNaturalLanguageHook:
    store: CreditStore
    name: str = "credits.natural_language"
    point: HookPoint = HookPoint.BEFORE_CAPABILITY
    priority: int = 5

    async def run(self, ctx: PipelineContext) -> None:
        session = ctx.session
        if session is None:
            return
        if ctx.route is None or ctx.route.type not in {RouteType.FAQ, RouteType.LLM}:
            return

        cfg = await self.store.get_config(
            ctx.event.tenant_id,
            _credit_session_id(ctx),
        )
        if not cfg.get("enabled"):
            return

        text = _normalize_query_text(ctx)
        credit_name = str(cfg.get("credit_name") or "积分").strip() or "积分"
        balance_text = (
            _strip_leading_mention_prefix(text)
            if bool(ctx.event.metadata.get("mentioned_me"))
            else text
        )
        intent = classify_credit_intent(
            text=text,
            balance_text=balance_text,
            credit_name=credit_name,
            mentioned_target_user_id=_mentioned_credit_target_user_id(ctx),
            decision=decision_from_pre(ctx.pre),
        )
        _sync_credit_intent_signal(ctx, intent)

        if intent.type == CreditIntentType.TRANSFER_REVERSE_UNAUTHORIZED:
            raise HookAbort(
                f"不支持从别人账户划走或扣除{credit_name}，我不能执行这类操作。",
                reason="credit_transfer_unsupported",
            )
        if intent.type == CreditIntentType.TRANSFER_SELF_TO_OTHER_UNSUPPORTED:
            raise HookAbort(
                f"暂不支持用自然语言转账{credit_name}，当前只能查询余额、排名和签到。",
                reason="credit_transfer_unsupported",
            )
        if not intent.should_handle:
            return

        other_member = None
        if intent.type == CreditIntentType.BALANCE_OTHER:
            other_member = await _resolve_other_credit_member(
                self.store,
                ctx,
                text=text,
                credit_name=credit_name,
                target_user_id=intent.target_user_id,
                target_display_name=intent.display_name,
            )

        auto_result = await _maybe_auto_checkin(self.store, ctx, cfg)
        if intent.type == CreditIntentType.CHECKIN_ACTION and auto_result:
            raise HookAbort(
                _manual_checkin_tip(cfg, source="natural"),
                reason="credit_checkin_action",
            )

        if other_member is not None:
            user_id = str(other_member.get("user_id") or "").strip()
            display_name = str(other_member.get("display_name") or "").strip()
            if not user_id:
                raise HookAbort(
                    f"没找到 {display_name or '这个成员'} 的积分记录。",
                    reason="credit_member_not_found",
                )
            detail = await self.store.get_member_detail(
                ctx.event.tenant_id,
                _credit_session_id(ctx),
                user_id,
                ledger_limit=5,
            )
            raise HookAbort(
                _format_other_member_credit_snapshot(detail, display_name=display_name),
                reason="credit_member_query",
            )

        if intent.type == CreditIntentType.RANK:
            raise HookAbort(await _cmd_top(self.store, ctx, cfg), reason="credits_command")

        detail = await self.store.get_member_detail(
            ctx.event.tenant_id,
            _credit_session_id(ctx),
            _credit_user_id(ctx),
            ledger_limit=5,
        )

        if intent.type == CreditIntentType.CHECKIN_STATUS:
            raise HookAbort(_format_member_checkin_snapshot(detail), reason="credit_checkin_query")
        if intent.type == CreditIntentType.RANK:
            raise HookAbort(_format_member_rank_snapshot(detail), reason="credit_rank_query")
        if intent.type == CreditIntentType.CHECKIN_ACTION:
            raise HookAbort(_manual_checkin_tip(cfg, source="natural"), reason="credit_checkin_action")
        raise HookAbort(_format_member_credit_snapshot(detail), reason="credit_balance_query")


@dataclass
class CreditSettlementHook:
    store: CreditStore
    name: str = "credits.settlement"
    point: HookPoint = HookPoint.AFTER_CAPABILITY
    priority: int = 90

    async def run(self, ctx: PipelineContext) -> None:
        event = ctx.event
        result = ctx.result
        cost = int(ctx.extras.get("_credits_cost") or 0)
        if cost <= 0 or result is None:
            return
        if ctx.extras.get("_credits_deducted"):
            return
        reservation_id = str(ctx.extras.get("_credits_reservation_id") or "")
        if result.route == RouteType.CANNED:
            if reservation_id:
                await self.store.release_reservation(reservation_id)
            return
        if ctx.extras.get("_credits_agent_billing") == "amap":
            return
        if reservation_id:
            await self.store.capture_reservation(
                reservation_id,
                amount=cost,
                reference=event.trace_id,
                display_name=str(event.metadata.get("sender_name") or ""),
            )
        else:
            await self.store.adjust(
                event.tenant_id,
                _credit_session_id(ctx),
                _credit_user_id(ctx),
                -cost,
                "chat_cost",
                actor="system",
                reference=event.trace_id,
                display_name=str(event.metadata.get("sender_name") or ""),
            )
        ctx.extras["_credits_deducted"] = True


def _sync_credit_query_signal(
    ctx: PipelineContext,
    *,
    handled: bool,
    reason: str = "",
) -> dict[str, object]:
    signal: dict[str, object] = {
        "handled": handled,
        "reason": reason or ("handled" if handled else "not_matched"),
        "query_text": _normalize_query_text(ctx),
    }
    if ctx.extras.get("_credits_auto_checkin_done"):
        auto_result = ctx.extras.get("_credits_auto_checkin_result")
        signal["auto_checkin"] = dict(auto_result) if isinstance(auto_result, dict) else {}
    ctx.signals.setdefault("credits", {})["query"] = signal
    return signal


def _sync_credit_reservation_signal(
    ctx: PipelineContext,
    *,
    reason: str = "",
) -> dict[str, object]:
    cfg = ctx.extras.get("_credits_cfg")
    config = dict(cfg) if isinstance(cfg, dict) else {}
    cost = int(ctx.extras.get("_credits_cost") or 0)
    reservation_id = str(ctx.extras.get("_credits_reservation_id") or "")
    signal: dict[str, object] = {
        "reserved": bool(reservation_id),
        "reservation_id": reservation_id,
        "amount": cost,
        "reason": reason or ("reserved" if reservation_id else "no_cost"),
    }
    if config:
        signal.update(
            {
                "enabled": bool(config.get("enabled")),
                "credit_name": str(config.get("credit_name") or "积分"),
                "cost_per_chat": int(config.get("cost_per_chat") or 0),
            }
        )
    if ctx.extras.get("_credits_agent_billing"):
        signal["agent_billing"] = str(ctx.extras.get("_credits_agent_billing") or "")
    if ctx.extras.get("_credits_auto_checkin_done"):
        auto_result = ctx.extras.get("_credits_auto_checkin_result")
        signal["auto_checkin"] = dict(auto_result) if isinstance(auto_result, dict) else {}
    ctx.signals.setdefault("billing", {})["reservation"] = signal
    return signal


def _settlement_reason(ctx: PipelineContext, *, before_deducted: bool) -> str:
    cost = int(ctx.extras.get("_credits_cost") or 0)
    if cost <= 0:
        return "no_cost"
    result = ctx.result
    if result is None:
        return "no_result"
    reservation_id = str(ctx.extras.get("_credits_reservation_id") or "")
    if before_deducted:
        return "already_settled"
    if result.route == RouteType.CANNED:
        return "released" if reservation_id else "skipped_canned"
    if ctx.extras.get("_credits_agent_billing") == "amap":
        return "agent_billing"
    if ctx.extras.get("_credits_deducted"):
        return "captured" if reservation_id else "adjusted"
    return "not_settled"


def _planned_settlement_reason(ctx: PipelineContext, *, before_deducted: bool) -> str:
    cost = int(ctx.extras.get("_credits_cost") or 0)
    if cost <= 0:
        return "no_cost"
    result = ctx.result
    if result is None:
        return "no_result"
    reservation_id = str(ctx.extras.get("_credits_reservation_id") or "")
    if before_deducted:
        return "already_settled"
    if result.route == RouteType.CANNED:
        return "released" if reservation_id else "skipped_canned"
    if ctx.extras.get("_credits_agent_billing") == "amap":
        return "agent_billing"
    return "captured" if reservation_id else "adjusted"


def _sync_credit_settlement_signal(
    ctx: PipelineContext,
    *,
    reason: str = "",
    settle_as_effect: bool = False,
) -> dict[str, object]:
    cost = int(ctx.extras.get("_credits_cost") or 0)
    reservation_id = str(ctx.extras.get("_credits_reservation_id") or "")
    result_route = ctx.result.route.value if ctx.result is not None else ""
    signal: dict[str, object] = {
        "settled": reason in {"captured", "adjusted"},
        "released": reason == "released",
        "reservation_id": reservation_id,
        "amount": cost,
        "result_route": result_route,
        "reason": reason or "not_settled",
    }
    if settle_as_effect:
        signal["settle_as_effect"] = True
    if ctx.extras.get("_credits_agent_billing"):
        signal["agent_billing"] = str(ctx.extras.get("_credits_agent_billing") or "")
    ctx.signals.setdefault("billing", {})["settlement"] = signal
    return signal


def _credit_effect_user_id(ctx: PipelineContext) -> str:
    return _credit_user_id(ctx)


def _auto_checkin_effect(ctx: PipelineContext) -> MessageEffect | None:
    if not ctx.extras.get("_credits_auto_checkin_done"):
        return None
    result = ctx.extras.get("_credits_auto_checkin_result")
    payload = dict(result) if isinstance(result, dict) else {}
    return MessageEffect(
        type="auto_checkin",
        owner="credits",
        payload={
            "commit_semantics": EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
            "tenant_id": ctx.event.tenant_id,
            "session_id": _credit_session_id(ctx),
            "user_id": _credit_effect_user_id(ctx),
            "result": payload,
            "trace_id": ctx.event.trace_id,
        },
        idempotency_key=(
            "credits:auto_checkin:"
            f"{ctx.event.tenant_id}:{_credit_session_id(ctx)}:"
            f"{_credit_effect_user_id(ctx)}:{ctx.event.trace_id}"
        ),
    )


def _reserve_credits_effect(
    ctx: PipelineContext,
    signal: dict[str, object],
) -> MessageEffect | None:
    if not signal.get("reserved"):
        return None
    reservation_id = str(signal.get("reservation_id") or "")
    return MessageEffect(
        type="reserve_credits",
        owner="credits",
        payload={
            "commit_semantics": EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
            "tenant_id": ctx.event.tenant_id,
            "session_id": _credit_session_id(ctx),
            "user_id": _credit_effect_user_id(ctx),
            "reservation_id": reservation_id,
            "amount": int(signal.get("amount") or 0),
            "reason": str(signal.get("reason") or ""),
            "trace_id": ctx.event.trace_id,
        },
        idempotency_key=f"credits:reserve:{reservation_id or ctx.event.trace_id}",
    )


def _settle_credits_effect(
    ctx: PipelineContext,
    signal: dict[str, object],
) -> MessageEffect | None:
    reason = str(signal.get("reason") or "")
    if reason not in {"captured", "adjusted", "released"}:
        return None
    effect_type = "release_credits" if reason == "released" else "capture_credits"
    reservation_id = str(signal.get("reservation_id") or "")
    return MessageEffect(
        type=effect_type,
        owner="credits",
        payload={
            "commit_semantics": (
                EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT
                if bool(signal.get("settle_as_effect"))
                else EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT
            ),
            "tenant_id": ctx.event.tenant_id,
            "session_id": _credit_session_id(ctx),
            "user_id": _credit_effect_user_id(ctx),
            "reservation_id": reservation_id,
            "amount": int(signal.get("amount") or 0),
            "reason": reason,
            "result_route": str(signal.get("result_route") or ""),
            "display_name": str(ctx.event.metadata.get("sender_name") or ""),
            "trace_id": ctx.event.trace_id,
        },
        idempotency_key=(
            f"credits:{effect_type}:{reservation_id}"
            if reservation_id
            else (
                f"credits:{effect_type}:{ctx.event.tenant_id}:"
                f"{_credit_session_id(ctx)}:{ctx.event.trace_id}"
            )
        ),
    )


@dataclass
class CreditQueryCommandStep:
    store: CreditStore
    kind: str = "plugin.credits.query_command"
    owner: str = "credits"
    name: str = "Credit query command"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre", "route"})
    outputs: set[str] = field(
        default_factory=lambda: {"signals.credits.query", "result", "effects.auto_checkin"}
    )
    timeout_seconds: float = 1.5
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        try:
            await CreditNaturalLanguageHook(self.store).run(ctx)
        except HookAbort as exc:
            _sync_credit_query_signal(ctx, handled=True, reason=exc.reason)
            effects = [effect] if (effect := _auto_checkin_effect(ctx)) else []
            return StepResult(
                action="stop",
                reason=exc.reason,
                result=CapabilityResult(route=RouteType.CANNED, reply_text=exc.reply_text),
                finalize=True,
                skip_output_safety=True,
                route_label=RouteType.CANNED.value,
                effects=effects,
            )
        signal = _sync_credit_query_signal(ctx, handled=False, reason="not_matched")
        effects = [effect] if (effect := _auto_checkin_effect(ctx)) else []
        return StepResult(reason=str(signal.get("reason") or "not_matched"), effects=effects)


@dataclass
class CreditReserveStep:
    store: CreditStore
    kind: str = "plugin.credits.reserve"
    owner: str = "credits"
    name: str = "Reserve credits"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre", "route"})
    outputs: set[str] = field(
        default_factory=lambda: {"signals.billing.reservation", "effects.reserve_credits"}
    )
    timeout_seconds: float = 2.0
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        try:
            await CreditDeductionHook(self.store).run(ctx)
        except HookAbort as exc:
            _sync_credit_reservation_signal(ctx, reason=exc.reason)
            return StepResult(
                action="stop",
                reason=exc.reason,
                result=CapabilityResult(route=RouteType.CANNED, reply_text=exc.reply_text),
                finalize=True,
                skip_output_safety=True,
                route_label=RouteType.CANNED.value,
            )

        cost = int(ctx.extras.get("_credits_cost") or 0)
        reservation_id = str(ctx.extras.get("_credits_reservation_id") or "")
        if cost <= 0:
            reason = "no_cost"
        elif ctx.extras.get("_credits_agent_billing") == "amap":
            reason = "agent_billing"
        elif reservation_id:
            reason = "reserved"
        else:
            reason = "pending_adjustment"
        signal = _sync_credit_reservation_signal(ctx, reason=reason)
        effects = []
        if auto_checkin_effect := _auto_checkin_effect(ctx):
            effects.append(auto_checkin_effect)
        if reserve_effect := _reserve_credits_effect(ctx, signal):
            effects.append(reserve_effect)
        return StepResult(reason=reason, effects=effects)


@dataclass
class CreditSettleStep:
    store: CreditStore
    effect_handler_enabled: bool = False
    kind: str = "plugin.credits.settle"
    owner: str = "credits"
    name: str = "Settle credits"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(
        default_factory=lambda: {
            "event",
            "session",
            "route",
            "result",
            "signals.billing.reservation",
        }
    )
    outputs: set[str] = field(
        default_factory=lambda: {"effects.capture_credits", "effects.release_credits"}
    )
    timeout_seconds: float = 2.0
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        before_deducted = bool(ctx.extras.get("_credits_deducted"))
        settle_as_effect = self.effect_handler_enabled or effect_handler_opt_in_enabled(
            ctx,
            effect_type="capture_credits",
            owner="credits",
        ) or effect_handler_opt_in_enabled(
            ctx,
            effect_type="release_credits",
            owner="credits",
        )
        if settle_as_effect:
            reason = _planned_settlement_reason(ctx, before_deducted=before_deducted)
            signal = _sync_credit_settlement_signal(
                ctx,
                reason=reason,
                settle_as_effect=reason in {"captured", "adjusted", "released"},
            )
            effects = [effect] if (effect := _settle_credits_effect(ctx, signal)) else []
            return StepResult(reason=reason, effects=effects)

        await CreditSettlementHook(self.store).run(ctx)
        reason = _settlement_reason(ctx, before_deducted=before_deducted)
        signal = _sync_credit_settlement_signal(ctx, reason=reason)
        effects = [effect] if (effect := _settle_credits_effect(ctx, signal)) else []
        return StepResult(reason=reason, effects=effects)


@dataclass
class CreditAuditEffectHandler:
    """Record already-executed credit effects without repeating store writes."""

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        ctx.signals.setdefault("effects", {}).setdefault("credits", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "idempotency_key": effect.idempotency_key,
                "status": "audited",
            }
        )


@dataclass
class CreditSettlementEffectHandler:
    """Capture or release a credit reservation after the effect gate succeeds."""

    store: CreditStore

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        payload = dict(effect.payload)
        amount = int(payload.get("amount") or 0)
        reservation_id = str(payload.get("reservation_id") or "").strip()
        display_name = str(payload.get("display_name") or "").strip()
        trace_id = str(payload.get("trace_id") or ctx.event.trace_id or ctx.trace_id)
        if effect.type == "release_credits":
            if not reservation_id:
                raise ValueError("release_credits effect missing reservation_id")
            await self.store.release_reservation(reservation_id)
            status = "released"
        elif effect.type == "capture_credits":
            if reservation_id:
                await self.store.capture_reservation(
                    reservation_id,
                    amount=amount,
                    reference=trace_id,
                    display_name=display_name,
                )
                status = "captured"
            else:
                if amount <= 0:
                    raise ValueError("capture_credits effect missing amount")
                await self.store.adjust(
                    str(payload.get("tenant_id") or ctx.event.tenant_id),
                    str(payload.get("session_id") or ctx.event.session_id),
                    str(payload.get("user_id") or _credit_effect_user_id(ctx)),
                    -amount,
                    "chat_cost",
                    actor="system",
                    reference=trace_id,
                    display_name=display_name,
                )
                status = "adjusted"
        else:
            raise ValueError(f"unsupported credit settlement effect: {effect.type}")
        ctx.extras["_credits_deducted"] = status in {"captured", "adjusted"}
        ctx.signals.setdefault("effects", {}).setdefault("credits", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "idempotency_key": effect.idempotency_key,
                "reservation_id": reservation_id,
                "amount": amount,
                "status": status,
            }
        )
