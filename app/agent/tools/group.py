from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from app.agent.registry import AgentToolDefinition
from app.agent.scopes import DEFAULT_AGENT_SCOPE, GROUP_PLUGIN_STATUS_SCOPE


class GroupAgentToolService(Protocol):
    async def get_group_info(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def list_group_members(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_member_avatar(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def search_group_messages(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def research_group_messages(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_public_facts(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_reply_policy(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_credits_status(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_credits_member(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_moderation_status(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_repeater_status(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_welcome_status(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_report_status(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_credits_leaderboard(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_recent_moderation_events(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def get_group_activity_ranking(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        ...


_GROUP_TOOL_CATALOG: tuple[dict[str, str], ...] = (
    {
        "name": "get_group_info",
        "description": "获取当前群或频道的基础信息，例如名称、成员数量和成员示例。",
    },
    {
        "name": "list_group_members",
        "description": "列出当前群或频道成员，适合回答‘群里有谁/有哪些人/成员列表’。",
    },
    {
        "name": "get_group_member_avatar",
        "description": "获取当前群或频道某个成员的头像 URL，可按 user_id、昵称、@对象或当前发言人查询。",
    },
    {
        "name": "search_group_messages",
        "description": "查询当前群或频道最近的文本消息，适合回答‘刚才谁提到 xxx’‘今天谁说过 xxx’。",
    },
    {
        "name": "research_group_messages",
        "description": "基于一个问题研究当前群或频道最近聊天记录，并总结是否查到相关讨论与证据片段。",
    },
    {
        "name": "get_group_public_facts",
        "description": "汇总当前群或频道最近的公开信息和插件状态，例如活跃成员、最近消息数量、已开启功能。",
    },
    {
        "name": "get_group_reply_policy",
        "description": "读取当前会话的回复策略，包括回复模式、是否默认提及发送者和关键词触发配置。",
    },
    {
        "name": "get_group_credits_status",
        "description": "读取当前会话积分插件配置和榜单摘要，例如签到模式、每次对话扣分和前几名成员。",
    },
    {
        "name": "get_group_credits_member",
        "description": "读取某个成员的积分详情，可按 user_id 或昵称查询余额、排名、签到状态和最近积分流水。",
    },
    {
        "name": "get_group_moderation_status",
        "description": "读取当前会话审核插件状态，包括关键词数量、提醒模式和最近审核事件。",
    },
    {
        "name": "get_group_repeater_status",
        "description": "读取当前会话复读机插件状态，包括冷却时间和最近触发记录。",
    },
    {
        "name": "get_group_welcome_status",
        "description": "读取当前会话欢迎语配置，包括是否开启、是否提及新成员和欢迎模板。",
    },
    {
        "name": "get_group_report_status",
        "description": "读取当前会话日报月报订阅状态，包括日报开关、月报开关和调度时间。",
    },
    {
        "name": "get_group_credits_leaderboard",
        "description": "读取当前会话积分榜或今日已签到成员列表，适合回答‘谁积分最高’‘今天谁签到了’。",
    },
    {
        "name": "get_group_recent_moderation_events",
        "description": "读取当前会话最近审核命中记录，适合回答‘最近审核拦了什么’‘谁刚触发了审核词’。",
    },
    {
        "name": "get_group_activity_ranking",
        "description": "统计当前群或频道最近一段时间的活跃排行，适合回答‘谁最近最活跃’‘谁话最多’。",
    },
)
_GROUP_ADMIN_TOOL_NAMES = {
    "list_group_members",
    "get_group_member_avatar",
    "search_group_messages",
    "research_group_messages",
    "get_group_credits_member",
    "get_group_recent_moderation_events",
    "get_group_activity_ranking",
}


def group_tool_catalog() -> list[dict[str, str]]:
    return [dict(item) for item in _GROUP_TOOL_CATALOG]


def group_plugin_status_tool_catalog() -> list[dict[str, str]]:
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
    return [dict(item) for item in _GROUP_TOOL_CATALOG if item["name"] in allowed]


def clone_tool_with_scope(tool: AgentToolDefinition, scope: str) -> AgentToolDefinition:
    return AgentToolDefinition(
        scope=scope,
        name=tool.name,
        description=tool.description,
        parameters=deepcopy(tool.parameters or {}),
        handler=tool.handler,
        metadata=deepcopy(tool.metadata or {}),
    )


def _descriptions() -> dict[str, str]:
    return {item["name"]: item["description"] for item in group_tool_catalog()}


def _definition(
    service: GroupAgentToolService,
    *,
    name: str,
    parameters: dict[str, Any],
) -> AgentToolDefinition:
    return AgentToolDefinition(
        scope=DEFAULT_AGENT_SCOPE,
        name=name,
        description=_descriptions()[name],
        parameters=parameters,
        handler=getattr(service, name),
        metadata=(
            {"required_group_role": "admin"}
            if name in _GROUP_ADMIN_TOOL_NAMES
            else {}
        ),
    )


def build_group_agent_tools(service: GroupAgentToolService) -> list[AgentToolDefinition]:
    return [
        _definition(
            service,
            name="get_group_info",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _definition(
            service,
            name="list_group_members",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "按成员昵称或 user_id 模糊搜索，可留空。"},
                    "limit": {"type": "integer", "description": "返回成员数，默认 20，最大 50。", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="get_group_member_avatar",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "成员 user_id，已知时优先传这个。"},
                    "display_name": {"type": "string", "description": "成员群昵称，未知 user_id 时可传昵称模糊匹配。"},
                    "query": {"type": "string", "description": "备用查询词，可传昵称、备注、别名或 user_id。"},
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="search_group_messages",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要搜索的关键词，可留空表示只看最近消息。"},
                    "sender_name": {"type": "string", "description": "按群昵称筛选发送者，可留空。"},
                    "sender_user_id": {"type": "string", "description": "按成员 user_id 筛选发送者，可留空。"},
                    "sender_wxid": {"type": "string", "description": "兼容旧微信工具参数；新渠道应优先使用 sender_user_id。"},
                    "hours": {"type": "integer", "description": "向前查询的小时数，默认 24，最大 336。", "minimum": 1, "maximum": 336},
                    "limit": {"type": "integer", "description": "返回样本消息数，默认 10，最大 20。", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="research_group_messages",
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要研究的问题，例如‘最近谁提到 CRS 为什么建议迁移’。"},
                    "hours": {"type": "integer", "description": "向前查询的小时数，默认 24，最大 336。", "minimum": 1, "maximum": 336},
                    "limit": {"type": "integer", "description": "返回样本消息数，默认 6，最大 12。", "minimum": 1, "maximum": 12},
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="get_group_public_facts",
            parameters={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "统计最近多少小时的群聊数据，默认 72，最大 720。", "minimum": 1, "maximum": 720}
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="get_group_reply_policy",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _definition(
            service,
            name="get_group_credits_status",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回积分榜前几名成员，默认 5，最大 20。", "minimum": 1, "maximum": 20}
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="get_group_credits_member",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "成员 user_id，已知时优先传这个。"},
                    "display_name": {"type": "string", "description": "成员群昵称，未知 user_id 时可传昵称模糊匹配。"},
                    "query": {"type": "string", "description": "备用查询词，和 display_name 类似。"},
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="get_group_moderation_status",
            parameters={
                "type": "object",
                "properties": {
                    "keyword_limit": {"type": "integer", "description": "最多返回多少条审核关键词，默认 10，最大 50。", "minimum": 1, "maximum": 50},
                    "event_limit": {"type": "integer", "description": "最多返回多少条最近审核事件，默认 5，最大 20。", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="get_group_repeater_status",
            parameters={
                "type": "object",
                "properties": {
                    "event_limit": {"type": "integer", "description": "最多返回多少条最近复读触发记录，默认 5，最大 20。", "minimum": 1, "maximum": 20}
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="get_group_welcome_status",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _definition(
            service,
            name="get_group_report_status",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _definition(
            service,
            name="get_group_credits_leaderboard",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回多少名成员，默认 10，最大 50。", "minimum": 1, "maximum": 50},
                    "query": {"type": "string", "description": "按昵称或 user_id 搜索成员，可留空。"},
                    "checked_in_today_only": {"type": "boolean", "description": "是否只返回今天已签到成员，默认 false。"},
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="get_group_recent_moderation_events",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回多少条审核记录，默认 10，最大 50。", "minimum": 1, "maximum": 50},
                    "keyword": {"type": "string", "description": "按命中关键词过滤，可留空。"},
                    "action": {"type": "string", "description": "按动作过滤，例如 flagged。"},
                    "webhook_status": {"type": "string", "description": "按 webhook 状态过滤，可留空。"},
                },
                "additionalProperties": False,
            },
        ),
        _definition(
            service,
            name="get_group_activity_ranking",
            parameters={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "向前统计多少小时，默认 24，最大 336。", "minimum": 1, "maximum": 336},
                    "limit": {"type": "integer", "description": "返回多少名活跃成员，默认 10，最大 30。", "minimum": 1, "maximum": 30},
                },
                "additionalProperties": False,
            },
        ),
    ]


def build_group_plugin_status_agent_tools(service: GroupAgentToolService) -> list[AgentToolDefinition]:
    allowed = {item["name"] for item in group_plugin_status_tool_catalog()}
    return [
        clone_tool_with_scope(item, GROUP_PLUGIN_STATUS_SCOPE)
        for item in build_group_agent_tools(service)
        if item.name in allowed
    ]
