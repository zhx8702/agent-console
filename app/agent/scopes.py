from __future__ import annotations

from typing import Any

GROUP_INFO_SCOPE = "group_info"
GROUP_PLUGIN_STATUS_SCOPE = "group_plugin_status"
GROUP_DRAW_GENERATION_SCOPE = "group_draw_generation"
GROUP_PERSONAL_MAP_SCOPE = "group_personal_map"

DEFAULT_AGENT_SCOPE = GROUP_INFO_SCOPE

WXBOT_GROUP_INFO_SCOPE = "wxbot_group_info"
WXBOT_GROUP_PLUGIN_STATUS_SCOPE = "wxbot_group_plugin_status"
WXBOT_GROUP_DRAW_GENERATION_SCOPE = "wxbot_group_draw_generation"
WXBOT_GROUP_PERSONAL_MAP_SCOPE = "wxbot_group_personal_map"

_SCOPE_ALIASES: dict[str, str] = {
    WXBOT_GROUP_INFO_SCOPE: GROUP_INFO_SCOPE,
    WXBOT_GROUP_PLUGIN_STATUS_SCOPE: GROUP_PLUGIN_STATUS_SCOPE,
    WXBOT_GROUP_DRAW_GENERATION_SCOPE: GROUP_DRAW_GENERATION_SCOPE,
    WXBOT_GROUP_PERSONAL_MAP_SCOPE: GROUP_PERSONAL_MAP_SCOPE,
}

_CANONICAL_TO_ALIASES: dict[str, tuple[str, ...]] = {
    GROUP_INFO_SCOPE: (WXBOT_GROUP_INFO_SCOPE,),
    GROUP_PLUGIN_STATUS_SCOPE: (WXBOT_GROUP_PLUGIN_STATUS_SCOPE,),
    GROUP_DRAW_GENERATION_SCOPE: (WXBOT_GROUP_DRAW_GENERATION_SCOPE,),
    GROUP_PERSONAL_MAP_SCOPE: (WXBOT_GROUP_PERSONAL_MAP_SCOPE,),
}

_SCOPE_CONFIG: dict[str, dict[str, str]] = {
    GROUP_INFO_SCOPE: {
        "label": "群资料查询",
        "disabled_reply": "当前群未启用群资料 Agent 查询。",
        "empty_reply": "我查了一下当前群资料，但这次没整理出可读结果。你可以换个问法再试。",
        "system_hint": (
            "你当前处于群聊/频道信息查询 Agent 模式。"
            "当问题涉及当前群成员、成员头像、当前群资料、最近消息、活跃情况时，优先调用工具读取真实数据，不要凭空猜测。"
            "你只能查询当前群，不能跨群查数据。"
            "工具结果只用于支撑你的自然语言短回复，不要原样输出 JSON。"
            "如果工具结果明确提示 roster_available=false、member_count_known=false、数据为空或工具报错，"
            "你要直接说明当前群资料暂时取不到，不能把空结果解释成 0 人或没人。"
            "回复保持群聊短句风格，结论先说，不要写后台面板口吻。"
        ),
    },
    GROUP_PLUGIN_STATUS_SCOPE: {
        "label": "群插件状态查询",
        "disabled_reply": "当前群未启用群插件状态 Agent 查询。",
        "empty_reply": "我查了一下当前群插件状态，但这次没整理出可读结果。你可以换个问法再试。",
        "system_hint": (
            "你当前处于群聊/频道插件状态查询 Agent 模式。"
            "当问题涉及当前群的积分、签到、审核、复读机、欢迎语、日报月报、回复策略等配置和状态时，优先调用工具读取真实数据，不要自己编规则。"
            "你只能读取当前群的插件状态与公开配置，不能跨群查数据。"
            "工具结果只用于支撑你的自然语言短回复，不要原样输出 JSON。"
            "回复保持群聊短句风格，结论先说，必要时补一句规则摘要。"
        ),
    },
    GROUP_DRAW_GENERATION_SCOPE: {
        "label": "群绘图生成",
        "disabled_reply": "当前群未启用群绘图 Agent。",
        "empty_reply": "绘图请求已处理，但这次没整理出可读结果。你可以换个问法再试。",
        "system_hint": (
            "你当前处于群聊/频道绘图 Agent 模式。"
            "当用户明确要求你画图、生图、生成图片、来一张图时，应优先调用绘图工具，不要把提示词润色成长篇教程。"
            "如果用户要求基于当前群某个成员头像生成图片，保留成员名和头像意图交给绘图工具处理。"
            "如果绘图工具已成功接单，你只需要简短确认正在生成、完成后会自动发送，不要重复输出大段提示词。"
            "你只能处理当前群的绘图请求，不能跨群操作。"
        ),
    },
    GROUP_PERSONAL_MAP_SCOPE: {
        "label": "高德个人地图",
        "disabled_reply": "当前群未启用高德个人地图 Agent。",
        "empty_reply": "我查了一下地图结果，但这次没整理出可读回复。你可以换个问法再试。",
        "system_hint": (
            "你当前处于高德个人地图 Agent 模式。"
            "当用户询问地点、附近、周边、餐厅、景点、咖啡店、商场、导航、路线、出行、旅游、打卡地图或行程规划时，优先调用高德地图工具。"
            "如果用户给的是地址而不是经纬度，先用地理编码工具拿到坐标，再做周边搜索或路线规划。"
            "如果用户只是找附近/周边/地点/餐厅/咖啡店/景点/商场，默认只给文字摘要，不要自动生成地图二维码；"
            "回复末尾可以简短提示：需要的话可以继续生成高德地图二维码。"
            "只有当用户明确要求地图、二维码、分享、导航地图、标记多个地点、打卡地图、路线地图或行程规划时，才调用个人地图生成工具。"
            "用户明确要求生成地图时，你必须在同一轮最终调用 amap_create_personal_map；不能只做搜索后结束。"
            "如果用户要求宽泛的美食/景点打卡路线，可以先做一次城市区域关键词搜索，从结果中挑选符合要求的点位；不要为每个品类反复搜索导致耗尽工具轮次。"
            "工具结果只用于支撑你的自然语言短回复，不要原样输出 JSON。"
            "回复要直接给结论，并说明二维码可用高德地图 App 扫码打开；不要在成功回复里展示 amapuri:// 备用链接。"
            "如果工具提示 API Key 缺失或生成失败，要明确说明配置或上游问题。"
            "保持克制、礼貌、专业的群聊短句风格，不要使用脏话、粗口、网络黑话或“寄了”“第一把”这类失败调侃。"
        ),
    },
}


def normalize_agent_scope(value: str | None) -> str:
    scope = str(value or "").strip()
    if not scope:
        return DEFAULT_AGENT_SCOPE
    return _SCOPE_ALIASES.get(scope, scope)


def agent_scope_aliases(scope: str | None) -> tuple[str, ...]:
    normalized = normalize_agent_scope(scope)
    return _CANONICAL_TO_ALIASES.get(normalized, ())


def agent_scope_lookup_order(scope: str | None) -> tuple[str, ...]:
    normalized = normalize_agent_scope(scope)
    return (normalized, *agent_scope_aliases(normalized))


def agent_scope_config(scope: str | None) -> dict[str, Any]:
    normalized = normalize_agent_scope(scope)
    config = dict(_SCOPE_CONFIG.get(normalized) or _SCOPE_CONFIG[DEFAULT_AGENT_SCOPE])
    config["scope"] = normalized
    return config


def agent_scope_disabled_reply(scope: str | None) -> str:
    return str(
        agent_scope_config(scope).get("disabled_reply")
        or _SCOPE_CONFIG[DEFAULT_AGENT_SCOPE]["disabled_reply"]
    )


def agent_scope_empty_reply(scope: str | None) -> str:
    return str(
        agent_scope_config(scope).get("empty_reply")
        or _SCOPE_CONFIG[DEFAULT_AGENT_SCOPE]["empty_reply"]
    )


def agent_scope_system_hint(scope: str | None) -> str:
    return str(
        agent_scope_config(scope).get("system_hint")
        or _SCOPE_CONFIG[DEFAULT_AGENT_SCOPE]["system_hint"]
    )
