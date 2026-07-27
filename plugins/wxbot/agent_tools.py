"""Public facade for WeChat agent tools.

Implementations live in focused definition, analysis, and service modules.  This
module intentionally keeps the historical import surface stable for plugins and
external agent-tool adapters.
"""

from __future__ import annotations

from plugins.wxbot.agent_tool_definitions import (
    build_wxbot_core_agent_tools,
    build_wxbot_core_plugin_status_agent_tools,
    build_wxbot_credits_agent_tools,
    build_wxbot_credits_plugin_status_agent_tools,
    build_wxbot_group_agent_tools,
    build_wxbot_group_plugin_status_agent_tools,
    build_wxbot_moderation_agent_tools,
    build_wxbot_moderation_plugin_status_agent_tools,
    build_wxbot_repeater_agent_tools,
    build_wxbot_repeater_plugin_status_agent_tools,
    wxbot_group_plugin_status_tool_catalog,
    wxbot_group_tool_catalog,
)
from plugins.wxbot.agent_tool_service import WxbotAgentToolService

__all__ = [
    "WxbotAgentToolService",
    "build_wxbot_core_agent_tools",
    "build_wxbot_core_plugin_status_agent_tools",
    "build_wxbot_credits_agent_tools",
    "build_wxbot_credits_plugin_status_agent_tools",
    "build_wxbot_group_agent_tools",
    "build_wxbot_group_plugin_status_agent_tools",
    "build_wxbot_moderation_agent_tools",
    "build_wxbot_moderation_plugin_status_agent_tools",
    "build_wxbot_repeater_agent_tools",
    "build_wxbot_repeater_plugin_status_agent_tools",
    "wxbot_group_plugin_status_tool_catalog",
    "wxbot_group_tool_catalog",
]
