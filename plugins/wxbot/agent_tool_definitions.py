from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from app.agent.registry import AgentToolDefinition
from app.agent.scopes import (
    DEFAULT_AGENT_SCOPE,
    FILE_ANALYSIS_SCOPE,
    GROUP_PLUGIN_STATUS_SCOPE,
    MESSAGE_EXPORT_SCOPE,
)
from plugins.wxbot.file_artifacts import SUPPORTED_FILE_FORMATS

if TYPE_CHECKING:
    from plugins.wxbot.agent_tool_service import WxbotAgentToolService


_WXBOT_GROUP_TOOL_CATALOG: tuple[dict[str, str], ...] = (
    {
        "name": "get_group_info",
        "description": "获取当前微信群的基础信息，例如群名、成员数量和成员示例。",
    },
    {
        "name": "list_group_members",
        "description": "列出当前微信群成员，适合回答‘群里有谁/有哪些人/成员列表’。",
    },
    {
        "name": "get_group_member_avatar",
        "description": "获取当前微信群某个成员的头像 URL，可按 wxid、群昵称、@对象或当前发言人查询。",
    },
    {
        "name": "search_group_messages",
        "description": "查询当前微信群最近的文本消息，适合回答‘刚才谁提到 xxx’‘今天谁说过 xxx’。",
    },
    {
        "name": "research_group_messages",
        "description": "基于一个问题研究当前微信群最近聊天记录，默认查最近 24 小时，并总结是否查到相关讨论与证据片段。",
    },
    {
        "name": "get_group_public_facts",
        "description": "汇总当前微信群最近的公开信息和插件状态，例如活跃成员、最近消息数量、已开启功能。",
    },
    {
        "name": "get_group_reply_policy",
        "description": "读取当前群的回复策略，包括回复模式、是否默认@发送者和关键词触发配置。",
    },
    {
        "name": "get_group_credits_status",
        "description": "读取当前群积分插件配置和榜单摘要，例如签到模式、每次对话扣分和前几名成员。",
    },
    {
        "name": "get_group_credits_member",
        "description": "读取某个群成员的积分详情，可按 wxid 或昵称查询余额、排名、签到状态和最近积分流水。",
    },
    {
        "name": "get_group_moderation_status",
        "description": "读取当前群审核插件状态，包括关键词数量、提醒模式和最近审核事件。",
    },
    {
        "name": "get_group_repeater_status",
        "description": "读取当前群复读机插件状态，包括冷却时间和最近触发记录。",
    },
    {
        "name": "get_group_welcome_status",
        "description": "读取当前群欢迎语配置，包括是否开启、是否@新成员和欢迎模板。",
    },
    {
        "name": "get_group_report_status",
        "description": "读取当前群日报月报订阅状态，包括日报开关、月报开关和调度时间。",
    },
    {
        "name": "get_group_credits_leaderboard",
        "description": "读取当前群积分榜或今日已签到成员列表，适合回答‘谁积分最高’‘今天谁签到了’。",
    },
    {
        "name": "get_group_recent_moderation_events",
        "description": "读取当前群最近审核命中记录，适合回答‘最近审核拦了什么’‘谁刚触发了审核词’。",
    },
    {
        "name": "get_group_activity_ranking",
        "description": "统计当前群最近一段时间的活跃排行，适合回答‘谁最近最活跃’‘谁话最多’。",
    },
    {
        "name": "build_group_member_profile_report",
        "description": "为当前群成员生成只读人物画像报告草案，包含群内证据、置信度、公开候选和人工审核提示，不写库也不自动绑定公开身份。",
    },
)


_WXBOT_AGENT_TOOL_METADATA = {
    "channels": ["wechat"],
    "session_kinds": ["group"],
}


_GROUP_ADMIN_TOOL_NAMES = {
    "list_group_members",
    "get_group_member_avatar",
    "search_group_messages",
    "research_group_messages",
    "get_group_credits_member",
    "get_group_recent_moderation_events",
    "get_group_activity_ranking",
    "build_group_member_profile_report",
}


def wxbot_group_tool_catalog() -> list[dict[str, str]]:
    return [dict(item) for item in _WXBOT_GROUP_TOOL_CATALOG]


def wxbot_group_plugin_status_tool_catalog() -> list[dict[str, str]]:
    allowed = {
        "get_group_reply_policy",
        "get_group_credits_status",
        "get_group_credits_member",
        "get_group_moderation_status",
        "get_group_repeater_status",
        "get_group_welcome_status",
        "get_group_report_status",
        "get_group_credits_leaderboard",
        "get_group_recent_moderation_events",
    }
    return [dict(item) for item in _WXBOT_GROUP_TOOL_CATALOG if item["name"] in allowed]


def _clone_tool_with_scope(tool: AgentToolDefinition, scope: str) -> AgentToolDefinition:
    return AgentToolDefinition(
        scope=scope,
        name=tool.name,
        description=tool.description,
        parameters=deepcopy(tool.parameters or {}),
        handler=tool.handler,
        metadata=deepcopy(tool.metadata or {}),
    )


def _with_wxbot_metadata(tools: list[AgentToolDefinition]) -> list[AgentToolDefinition]:
    enriched: list[AgentToolDefinition] = []
    for tool in tools:
        metadata = deepcopy(tool.metadata or {})
        metadata.setdefault("channels", list(_WXBOT_AGENT_TOOL_METADATA["channels"]))
        metadata.setdefault("session_kinds", list(_WXBOT_AGENT_TOOL_METADATA["session_kinds"]))
        if tool.name in _GROUP_ADMIN_TOOL_NAMES:
            metadata.setdefault("required_group_role", "admin")
        enriched.append(
            AgentToolDefinition(
                scope=tool.scope,
                name=tool.name,
                description=tool.description,
                parameters=deepcopy(tool.parameters or {}),
                handler=tool.handler,
                metadata=metadata,
                embed_text=tool.embed_text,
                tree_text=tool.tree_text,
                required_params=deepcopy(tool.required_params),
                verb_type=tool.verb_type,
                scopes=deepcopy(tool.scopes),
            )
        )
    return enriched


def build_wxbot_group_agent_tools(service: WxbotAgentToolService) -> list[AgentToolDefinition]:
    descriptions = {item["name"]: item["description"] for item in wxbot_group_tool_catalog()}
    return _with_wxbot_metadata(
        [
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_info",
                description=descriptions["get_group_info"],
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=service.get_group_info,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="list_group_members",
                description=descriptions["list_group_members"],
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "按成员昵称或 wxid 模糊搜索，可留空。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回成员数，默认 20，最大 50。",
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.list_group_members,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_member_avatar",
                description=descriptions["get_group_member_avatar"],
                parameters={
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "成员 wxid，已知时优先传这个。",
                        },
                        "display_name": {
                            "type": "string",
                            "description": "成员群昵称，未知 wxid 时可传昵称模糊匹配。",
                        },
                        "query": {
                            "type": "string",
                            "description": "备用查询词，可传昵称、备注、别名或 wxid。",
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.get_group_member_avatar,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="search_group_messages",
                description=descriptions["search_group_messages"],
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "要搜索的关键词，可留空表示只看最近消息。",
                        },
                        "sender_name": {
                            "type": "string",
                            "description": "按群昵称筛选发送者，可留空。",
                        },
                        "sender_wxid": {
                            "type": "string",
                            "description": "按成员 wxid 筛选发送者，可留空。",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "向前查询的小时数，默认 24，最大 336。",
                            "minimum": 1,
                            "maximum": 336,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回样本消息数，默认 10，最大 20。",
                            "minimum": 1,
                            "maximum": 20,
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.search_group_messages,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="research_group_messages",
                description=descriptions["research_group_messages"],
                parameters={
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "要研究的问题，例如‘最近谁提到 CRS 为什么建议迁移’。",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "向前查询的小时数，默认 24，最大 336。",
                            "minimum": 1,
                            "maximum": 336,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回样本消息数，默认 6，最大 12。",
                            "minimum": 1,
                            "maximum": 12,
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.research_group_messages,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="build_group_member_profile_report",
                description=descriptions["build_group_member_profile_report"],
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "成员显示名、别名或 wxid，例如“示例开发者-LinZhou”。",
                        },
                        "display_name": {
                            "type": "string",
                            "description": "成员群昵称，和 query 二选一即可。",
                        },
                        "user_id": {
                            "type": "string",
                            "description": "成员 wxid，已知时优先传。",
                        },
                        "wxid": {
                            "type": "string",
                            "description": "成员 wxid，user_id 的别名。",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "向前聚合多少小时的群内文本消息，默认 168，最大 720。",
                            "minimum": 1,
                            "maximum": 720,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回证据片段和公开候选的数量上限，默认 8，最大 20。",
                            "minimum": 1,
                            "maximum": 20,
                        },
                        "external_candidates": {
                            "type": "array",
                            "description": "可选的公开搜索候选列表；MVP 只评分和脱敏，不主动联网搜索。",
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.build_group_member_profile_report,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_public_facts",
                description=descriptions["get_group_public_facts"],
                parameters={
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "integer",
                            "description": "统计最近多少小时的群聊数据，默认 72，最大 720。",
                            "minimum": 1,
                            "maximum": 720,
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.get_group_public_facts,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_reply_policy",
                description=descriptions["get_group_reply_policy"],
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=service.get_group_reply_policy,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_credits_status",
                description=descriptions["get_group_credits_status"],
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "返回积分榜前几名成员，默认 5，最大 20。",
                            "minimum": 1,
                            "maximum": 20,
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.get_group_credits_status,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_credits_member",
                description=descriptions["get_group_credits_member"],
                parameters={
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "成员 wxid，已知时优先传这个。",
                        },
                        "display_name": {
                            "type": "string",
                            "description": "成员群昵称，未知 wxid 时可传昵称模糊匹配。",
                        },
                        "query": {
                            "type": "string",
                            "description": "备用查询词，和 display_name 类似。",
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.get_group_credits_member,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_moderation_status",
                description=descriptions["get_group_moderation_status"],
                parameters={
                    "type": "object",
                    "properties": {
                        "keyword_limit": {
                            "type": "integer",
                            "description": "最多返回多少条审核关键词，默认 10，最大 50。",
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "event_limit": {
                            "type": "integer",
                            "description": "最多返回多少条最近审核事件，默认 5，最大 20。",
                            "minimum": 1,
                            "maximum": 20,
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.get_group_moderation_status,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_repeater_status",
                description=descriptions["get_group_repeater_status"],
                parameters={
                    "type": "object",
                    "properties": {
                        "event_limit": {
                            "type": "integer",
                            "description": "最多返回多少条最近复读触发记录，默认 5，最大 20。",
                            "minimum": 1,
                            "maximum": 20,
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.get_group_repeater_status,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_welcome_status",
                description=descriptions["get_group_welcome_status"],
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=service.get_group_welcome_status,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_report_status",
                description=descriptions["get_group_report_status"],
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=service.get_group_report_status,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_credits_leaderboard",
                description=descriptions["get_group_credits_leaderboard"],
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "返回多少名成员，默认 10，最大 50。",
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "query": {
                            "type": "string",
                            "description": "按昵称或 wxid 搜索成员，可留空。",
                        },
                        "checked_in_today_only": {
                            "type": "boolean",
                            "description": "是否只返回今天已签到成员，默认 false。",
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.get_group_credits_leaderboard,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_recent_moderation_events",
                description=descriptions["get_group_recent_moderation_events"],
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "返回多少条审核记录，默认 10，最大 50。",
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "keyword": {
                            "type": "string",
                            "description": "按命中关键词过滤，可留空。",
                        },
                        "action": {
                            "type": "string",
                            "description": "按动作过滤，例如 flagged。",
                        },
                        "webhook_status": {
                            "type": "string",
                            "description": "按 webhook 状态过滤，可留空。",
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.get_group_recent_moderation_events,
            ),
            AgentToolDefinition(
                scope=DEFAULT_AGENT_SCOPE,
                name="get_group_activity_ranking",
                description=descriptions["get_group_activity_ranking"],
                parameters={
                    "type": "object",
                    "properties": {
                        "hours": {
                            "type": "integer",
                            "description": "向前统计多少小时，默认 24，最大 336。",
                            "minimum": 1,
                            "maximum": 336,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回多少名活跃成员，默认 10，最大 30。",
                            "minimum": 1,
                            "maximum": 30,
                        },
                    },
                    "additionalProperties": False,
                },
                handler=service.get_group_activity_ranking,
            ),
        ]
    )


def build_wxbot_group_plugin_status_agent_tools(
    service: WxbotAgentToolService,
) -> list[AgentToolDefinition]:
    allowed = {item["name"] for item in wxbot_group_plugin_status_tool_catalog()}
    return [
        _clone_tool_with_scope(item, GROUP_PLUGIN_STATUS_SCOPE)
        for item in build_wxbot_group_agent_tools(service)
        if item.name in allowed
    ]


def build_wxbot_message_export_agent_tools(
    service: WxbotAgentToolService,
) -> list[AgentToolDefinition]:
    return [
        AgentToolDefinition(
            scope=MESSAGE_EXPORT_SCOPE,
            name="export_current_messages_file",
            description=(
                "把当前群聊或当前私聊指定日期/月度的消息记录整理成文件并发送回当前会话。"
                "仅当用户同时明确要求消息汇总和发送/导出文件时调用；不能指定、改写或跨越目的会话。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "description": "导出时间范围类型，daily=按日，monthly=按月；默认 daily。",
                        "enum": ["daily", "monthly"],
                    },
                    "date": {
                        "type": "string",
                        "description": "日报日期，格式 YYYY-MM-DD；默认今天。",
                    },
                    "year_month": {
                        "type": "string",
                        "description": "月报月份，格式 YYYY-MM；仅 report_type=monthly 时使用。",
                    },
                    "format": {
                        "type": "string",
                        "description": "文件格式；默认 txt。",
                        "enum": list(SUPPORTED_FILE_FORMATS),
                    },
                },
                "additionalProperties": False,
            },
            handler=service.export_current_messages_file,
            metadata={
                "channels": ["wechat"],
                "session_kinds": ["group", "private"],
                "requires_group_file_send": True,
            },
        )
    ]


def build_wxbot_file_analysis_agent_tools(
    service: WxbotAgentToolService,
) -> list[AgentToolDefinition]:
    """Expose file operations only after the deterministic file-intent gate."""

    metadata = {
        "channels": ["wechat"],
        "session_kinds": ["group", "private"],
    }
    return [
        AgentToolDefinition(
            scope=FILE_ANALYSIS_SCOPE,
            name="inspect_current_file",
            description=(
                "读取当前会话最近收到的文件并返回有限的文本预览，用于回答‘总结/解析/看看这个文件’。"
                "只接受当前会话的 SDK 文件，不接受用户路径、URL 或跨会话文件。"
            ),
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=service.inspect_current_file,
            metadata={
                **metadata,
                "embed_text": "读取解析总结当前收到的文件附件内容",
                "tree_text": "inspect current inbound file attachment",
            },
        ),
        AgentToolDefinition(
            scope=FILE_ANALYSIS_SCOPE,
            name="convert_current_file",
            description=(
                "把当前会话最近收到的 txt、md、csv 或 json 文件转换为另一种安全文本格式。"
                "只有用户明确要求生成/转换并发送或下载时才会排队发送；普通总结不会发送文件。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "description": "目标格式。",
                        "enum": list(SUPPORTED_FILE_FORMATS),
                    }
                },
                "required": ["format"],
                "additionalProperties": False,
            },
            handler=service.convert_current_file,
            metadata={
                **metadata,
                "requires_group_file_send": True,
                "embed_text": "转换当前收到的文件格式生成文件并发送",
                "tree_text": "convert current inbound file and send",
            },
        ),
        AgentToolDefinition(
            scope=FILE_ANALYSIS_SCOPE,
            name="generate_text_file",
            description=(
                "把当前回答或已整理的正文生成 txt、md、csv 或 json 文件并发送到当前会话。"
                "仅在用户明确要求生成/整理文件并发送时调用，不接受用户路径或 URL。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要写入文件的正文内容，由当前回答整理得到。",
                    },
                    "format": {
                        "type": "string",
                        "description": "目标格式，默认 txt。",
                        "enum": list(SUPPORTED_FILE_FORMATS),
                    },
                    "file_name": {
                        "type": "string",
                        "description": "可选的文件名（只允许文件名，不含路径）。",
                    },
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            handler=service.generate_text_file,
            metadata={
                **metadata,
                "requires_group_file_send": True,
                "embed_text": "把当前回答整理成文件并发送",
                "tree_text": "generate text file and send",
            },
        ),
    ]


def build_wxbot_core_agent_tools(service: WxbotAgentToolService) -> list[AgentToolDefinition]:
    allowed = {
        "get_group_info",
        "list_group_members",
        "get_group_member_avatar",
        "search_group_messages",
        "research_group_messages",
        "build_group_member_profile_report",
        "get_group_public_facts",
        "get_group_reply_policy",
        "get_group_welcome_status",
        "get_group_report_status",
        "get_group_activity_ranking",
    }
    return [item for item in build_wxbot_group_agent_tools(service) if item.name in allowed]


def build_wxbot_core_plugin_status_agent_tools(
    service: WxbotAgentToolService,
) -> list[AgentToolDefinition]:
    allowed = {
        "get_group_reply_policy",
        "get_group_welcome_status",
        "get_group_report_status",
    }
    return [
        _clone_tool_with_scope(item, GROUP_PLUGIN_STATUS_SCOPE)
        for item in build_wxbot_group_agent_tools(service)
        if item.name in allowed
    ]


def build_wxbot_credits_agent_tools(service: WxbotAgentToolService) -> list[AgentToolDefinition]:
    allowed = {
        "get_group_credits_status",
        "get_group_credits_member",
        "get_group_credits_leaderboard",
    }
    return [item for item in build_wxbot_group_agent_tools(service) if item.name in allowed]


def build_wxbot_credits_plugin_status_agent_tools(
    service: WxbotAgentToolService,
) -> list[AgentToolDefinition]:
    return [
        _clone_tool_with_scope(item, GROUP_PLUGIN_STATUS_SCOPE)
        for item in build_wxbot_credits_agent_tools(service)
    ]


def build_wxbot_moderation_agent_tools(service: WxbotAgentToolService) -> list[AgentToolDefinition]:
    allowed = {
        "get_group_moderation_status",
        "get_group_recent_moderation_events",
    }
    return [item for item in build_wxbot_group_agent_tools(service) if item.name in allowed]


def build_wxbot_moderation_plugin_status_agent_tools(
    service: WxbotAgentToolService,
) -> list[AgentToolDefinition]:
    return [
        _clone_tool_with_scope(item, GROUP_PLUGIN_STATUS_SCOPE)
        for item in build_wxbot_moderation_agent_tools(service)
    ]


def build_wxbot_repeater_agent_tools(service: WxbotAgentToolService) -> list[AgentToolDefinition]:
    allowed = {"get_group_repeater_status"}
    return [item for item in build_wxbot_group_agent_tools(service) if item.name in allowed]


def build_wxbot_repeater_plugin_status_agent_tools(
    service: WxbotAgentToolService,
) -> list[AgentToolDefinition]:
    return [
        _clone_tool_with_scope(item, GROUP_PLUGIN_STATUS_SCOPE)
        for item in build_wxbot_repeater_agent_tools(service)
    ]
