from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.agent.registry import AgentToolDefinition
from app.agent.scopes import GROUP_PERSONAL_MAP_SCOPE
from app.channel import ChannelMedia, ChannelRegistry, ChannelSendOptions, ChannelTarget
from app.common.config import Settings
from app.common.context import get_trace_id
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.common.types import Role, Session
from plugins.amap.client import AMapClient

logger = get_logger(__name__)

_COORD_RE = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*$")
_GROUP_AGENT_TOOL_METADATA = {"session_kinds": ["group"]}
_DELIVERY_CONTRACT_METADATA_KEY = "_wxbot_delivery_contract"

ScopeExecutionAllowed = Callable[[str, str], Awaitable[bool]]


def _with_group_metadata(tools: list[AgentToolDefinition]) -> list[AgentToolDefinition]:
    enriched: list[AgentToolDefinition] = []
    for tool in tools:
        metadata = deepcopy(tool.metadata or {})
        metadata.setdefault("session_kinds", list(_GROUP_AGENT_TOOL_METADATA["session_kinds"]))
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


def build_amap_agent_tools(service: AMapAgentToolService) -> list[AgentToolDefinition]:
    return _with_group_metadata([
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_geo",
            description="使用高德地图把地址转换为经纬度坐标。",
            parameters={
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "详细地址或地点名称。"},
                    "city": {"type": "string", "description": "城市名称，可留空。"},
                },
                "required": ["address"],
                "additionalProperties": False,
            },
            handler=service.geo,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_text_search",
            description="使用高德地图按关键词搜索地点、餐厅、景点、咖啡店、商场等 POI；普通搜索只需文字摘要，用户明确要求地图/二维码/分享时再调用 amap_create_personal_map。",
            parameters={
                "type": "object",
                "properties": {
                    "keywords": {"type": "string", "description": "搜索关键词。"},
                    "city": {"type": "string", "description": "城市名称，可留空。"},
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，默认 10，最大 20。",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["keywords"],
                "additionalProperties": False,
            },
            handler=service.text_search,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_regeo",
            description="使用高德地图把经纬度反查为详细地址，并返回附近 POI/AOI。location 必须是经度,纬度。",
            parameters={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "坐标，格式为 经度,纬度。"},
                    "radius": {
                        "type": "integer",
                        "description": "周边搜索半径，单位米，默认 1000，最大 3000。",
                        "minimum": 0,
                        "maximum": 3000,
                    },
                    "extensions": {
                        "type": "string",
                        "description": "返回类型：base 或 all，默认 all。",
                        "enum": ["base", "all"],
                    },
                },
                "required": ["location"],
                "additionalProperties": False,
            },
            handler=service.regeo,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_place_detail",
            description="使用高德地图根据 POI ID 查询地点详情，包括地址、电话、类型、城市区县等信息。",
            parameters={
                "type": "object",
                "properties": {
                    "poi_id": {"type": "string", "description": "高德 POI ID。"},
                },
                "required": ["poi_id"],
                "additionalProperties": False,
            },
            handler=service.place_detail,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_input_tips",
            description="使用高德地图输入提示做模糊地名、公司简称、错别字或半截地址的候选补全。",
            parameters={
                "type": "object",
                "properties": {
                    "keywords": {"type": "string", "description": "输入关键词。"},
                    "city": {"type": "string", "description": "城市名称或 adcode，可留空。"},
                    "citylimit": {"type": "boolean", "description": "是否限制在 city 内，默认 false。"},
                    "location": {"type": "string", "description": "偏好位置，经度,纬度，可留空。"},
                    "datatype": {"type": "string", "description": "返回类型，默认 all。"},
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，默认 10，最大 20。",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["keywords"],
                "additionalProperties": False,
            },
            handler=service.input_tips,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_around_search",
            description="使用高德地图围绕某个中心点搜索周边 POI。location 必须是经度,纬度；普通搜索只需文字摘要，用户明确要求地图/二维码/分享时再调用 amap_create_personal_map。",
            parameters={
                "type": "object",
                "properties": {
                    "keywords": {"type": "string", "description": "周边搜索关键词。"},
                    "location": {"type": "string", "description": "中心点坐标，格式为 经度,纬度。"},
                    "radius": {
                        "type": "integer",
                        "description": "搜索半径，单位米，默认 1000，最大 50000。",
                        "minimum": 1,
                        "maximum": 50000,
                    },
                    "types": {"type": "string", "description": "高德 POI 类型编码，可留空。"},
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，默认 10，最大 20。",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                "required": ["keywords", "location"],
                "additionalProperties": False,
            },
            handler=service.around_search,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_route_plan",
            description="使用高德地图规划步行、驾车或公交路线；地址会自动先转坐标。",
            parameters={
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "起点地址或 经度,纬度。"},
                    "destination": {"type": "string", "description": "终点地址或 经度,纬度。"},
                    "mode": {
                        "type": "string",
                        "description": "路线模式：walking、driving、transit。",
                        "enum": ["walking", "driving", "transit"],
                    },
                    "city": {"type": "string", "description": "城市名称，公交路线建议填写。"},
                },
                "required": ["origin", "destination"],
                "additionalProperties": False,
            },
            handler=service.route_plan,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_distance",
            description="使用高德地图测量一个或多个起点到终点的直线、驾车或步行距离；地址会自动先转坐标。",
            parameters={
                "type": "object",
                "properties": {
                    "origins": {
                        "type": "array",
                        "description": "起点地址或 经度,纬度 列表，最多 100 个。",
                        "items": {"type": "string"},
                    },
                    "destination": {"type": "string", "description": "终点地址或 经度,纬度。"},
                    "mode": {
                        "type": "string",
                        "description": "距离模式：straight、driving、walking。",
                        "enum": ["straight", "driving", "walking"],
                    },
                    "city": {"type": "string", "description": "城市名称，可留空；用于地址转坐标。"},
                },
                "required": ["origins", "destination"],
                "additionalProperties": False,
            },
            handler=service.distance,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_weather",
            description="使用高德地图查询城市或 adcode 的天气实况或预报。",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市 adcode 或城市名称。"},
                    "extensions": {
                        "type": "string",
                        "description": "base=实况，all=预报，默认 base。",
                        "enum": ["base", "all"],
                    },
                },
                "required": ["city"],
                "additionalProperties": False,
            },
            handler=service.weather,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_district",
            description="使用高德地图查询行政区域、adcode、下级区划和边界信息。",
            parameters={
                "type": "object",
                "properties": {
                    "keywords": {"type": "string", "description": "行政区名称或 adcode，可留空。"},
                    "subdistrict": {
                        "type": "integer",
                        "description": "下级行政区层级，0 到 3，默认 1。",
                        "minimum": 0,
                        "maximum": 3,
                    },
                    "extensions": {
                        "type": "string",
                        "description": "base 或 all，all 会返回边界坐标。",
                        "enum": ["base", "all"],
                    },
                },
                "additionalProperties": False,
            },
            handler=service.district,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_static_map",
            description="使用高德地图生成静态地图图片，可添加标注、标签、路线和实时路况。普通查询优先文字回答，用户要求地图截图/图片时调用。",
            parameters={
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "地图中心点，经度,纬度。"},
                    "zoom": {"type": "integer", "description": "缩放级别 1 到 17，默认 12。"},
                    "size": {"type": "string", "description": "图片尺寸，如 750*500。"},
                    "markers": {"type": "string", "description": "高德静态图 markers 参数，可留空。"},
                    "labels": {"type": "string", "description": "高德静态图 labels 参数，可留空。"},
                    "paths": {"type": "string", "description": "高德静态图 paths 参数，可留空。"},
                    "traffic": {"type": "boolean", "description": "是否叠加实时路况，默认 false。"},
                    "map_name": {"type": "string", "description": "本地保存图片名称。"},
                },
                "required": ["location"],
                "additionalProperties": False,
            },
            handler=service.static_map,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_coordinate_convert",
            description="使用高德地图把 GPS、百度、mapbar 或高德坐标转换为高德坐标。",
            parameters={
                "type": "object",
                "properties": {
                    "locations": {
                        "type": "array",
                        "description": "坐标列表，格式为 经度,纬度，最多 40 个。",
                        "items": {"type": "string"},
                    },
                    "coordsys": {
                        "type": "string",
                        "description": "原始坐标系：gps、mapbar、baidu、autonavi。",
                        "enum": ["gps", "mapbar", "baidu", "autonavi"],
                    },
                },
                "required": ["locations"],
                "additionalProperties": False,
            },
            handler=service.coordinate_convert,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_traffic_status",
            description="使用高德地图查询指定道路的实时交通态势。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "道路名称。"},
                    "city": {"type": "string", "description": "城市名称，可留空。"},
                    "adcode": {"type": "string", "description": "城市 adcode，可留空。"},
                    "level": {"type": "integer", "description": "道路等级 1 到 6，默认 5。"},
                    "extensions": {"type": "string", "description": "base 或 all。", "enum": ["base", "all"]},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=service.traffic_status,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_bus_info",
            description="使用高德地图查询公交线路或公交站信息。",
            parameters={
                "type": "object",
                "properties": {
                    "keywords": {"type": "string", "description": "公交线路名或站点名。"},
                    "city": {"type": "string", "description": "城市名称或 adcode。"},
                    "search_type": {
                        "type": "string",
                        "description": "line=线路，stop=站点，默认 line。",
                        "enum": ["line", "stop"],
                    },
                    "offset": {"type": "integer", "description": "返回条数，默认 10，最大 20。"},
                    "page": {"type": "integer", "description": "页码，默认 1。"},
                },
                "required": ["keywords", "city"],
                "additionalProperties": False,
            },
            handler=service.bus_info,
        ),
        AgentToolDefinition(
            scope=GROUP_PERSONAL_MAP_SCOPE,
            name="amap_create_personal_map",
            description="把已选地点生成高德个人地图二维码，并在微信群中发送二维码图片；用户明确要求生成地图、二维码、打卡地图或路线地图时必须调用此工具。",
            parameters={
                "type": "object",
                "properties": {
                    "map_name": {"type": "string", "description": "个人地图名称。"},
                    "points": {
                        "type": "array",
                        "description": "要标记在地图上的地点列表。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "longitude": {"type": "number"},
                                "latitude": {"type": "number"},
                                "poi_id": {"type": "string"},
                                "address": {"type": "string"},
                            },
                            "required": ["name", "longitude", "latitude"],
                            "additionalProperties": False,
                        },
                    },
                    "line_title": {"type": "string", "description": "路线或点位列表标题，可留空。"},
                    "city": {"type": "string", "description": "城市名称，可留空；用于补全缺失 poi_id 的点位。"},
                    "scene_type": {
                        "type": "integer",
                        "description": "1=资源点+路线，2=仅资源点，3=仅路线。",
                        "enum": [1, 2, 3],
                    },
                    "send_to_group": {
                        "type": "boolean",
                        "description": "是否把二维码图片发送到当前微信群，默认 true。",
                    },
                },
                "required": ["map_name", "points"],
                "additionalProperties": False,
            },
            handler=service.create_personal_map,
        ),
    ])


class AMapAgentToolService:
    def __init__(
        self,
        settings: Settings,
        *,
        channel_registry: ChannelRegistry | None = None,
        client: AMapClient | None = None,
        effect_reply_enabled: bool = False,
        scope_execution_allowed: ScopeExecutionAllowed | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or AMapClient(
            api_key=str(getattr(settings, "amap_api_key", "") or ""),
            timeout=float(getattr(settings, "amap_api_timeout_seconds", 15.0) or 15.0),
        )
        self._channel_registry = channel_registry
        self._effect_reply_enabled = effect_reply_enabled
        self._scope_execution_allowed = scope_execution_allowed
        self._storage_dir = Path(
            str(getattr(settings, "amap_storage_dir", "/mnt/c/Users/Public/agent-console-amap") or "")
        )

    async def close(self) -> None:
        await self._client.close()

    async def geo(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        address = str(arguments.get("address") or "").strip()
        if not address:
            raise ValueError("address 不能为空")
        return await self._client.geo(
            address=address,
            city=str(arguments.get("city") or "").strip(),
        )

    async def text_search(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        keywords = str(arguments.get("keywords") or "").strip()
        if not keywords:
            raise ValueError("keywords 不能为空")
        return await self._client.text_search(
            keywords=keywords,
            city=str(arguments.get("city") or "").strip(),
            limit=int(arguments.get("limit") or 10),
        )

    async def regeo(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        location = str(arguments.get("location") or "").strip()
        if not _COORD_RE.match(location):
            raise ValueError("location 必须是 经度,纬度")
        return await self._client.regeo(
            location=re.sub(r"\s+", "", location),
            radius=int(arguments.get("radius") or 1000),
            extensions=str(arguments.get("extensions") or "all"),
        )

    async def place_detail(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        poi_id = str(arguments.get("poi_id") or "").strip()
        if not poi_id:
            raise ValueError("poi_id 不能为空")
        return await self._client.place_detail(poi_id=poi_id)

    async def input_tips(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        keywords = str(arguments.get("keywords") or "").strip()
        location = str(arguments.get("location") or "").strip()
        if not keywords:
            raise ValueError("keywords 不能为空")
        if location and not _COORD_RE.match(location):
            raise ValueError("location 必须是 经度,纬度")
        return await self._client.input_tips(
            keywords=keywords,
            city=str(arguments.get("city") or "").strip(),
            citylimit=bool(arguments.get("citylimit", False)),
            location=re.sub(r"\s+", "", location),
            datatype=str(arguments.get("datatype") or "all").strip(),
            limit=int(arguments.get("limit") or 10),
        )

    async def around_search(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        keywords = str(arguments.get("keywords") or "").strip()
        location = str(arguments.get("location") or "").strip()
        if not keywords:
            raise ValueError("keywords 不能为空")
        if not _COORD_RE.match(location):
            raise ValueError("location 必须是 经度,纬度")
        return await self._client.around_search(
            keywords=keywords,
            location=re.sub(r"\s+", "", location),
            radius=int(arguments.get("radius") or 1000),
            types=str(arguments.get("types") or "").strip(),
            limit=int(arguments.get("limit") or 10),
        )

    async def route_plan(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        city = str(arguments.get("city") or "").strip()
        origin = await self._resolve_location(str(arguments.get("origin") or "").strip(), city=city)
        destination = await self._resolve_location(
            str(arguments.get("destination") or "").strip(),
            city=city,
        )
        if not origin or not destination:
            raise ValueError("origin 和 destination 不能为空")
        return await self._client.route_plan(
            origin=origin,
            destination=destination,
            mode=str(arguments.get("mode") or "driving"),
            city=city,
        )

    async def distance(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        city = str(arguments.get("city") or "").strip()
        raw_origins = arguments.get("origins")
        if not isinstance(raw_origins, list):
            raise ValueError("origins 必须是数组")
        origins = [
            await self._resolve_location(str(item or "").strip(), city=city)
            for item in raw_origins[:100]
        ]
        origins = [item for item in origins if item]
        destination = await self._resolve_location(str(arguments.get("destination") or "").strip(), city=city)
        if not origins or not destination:
            raise ValueError("origins 和 destination 不能为空")
        return await self._client.distance(
            origins=origins,
            destination=destination,
            mode=str(arguments.get("mode") or "driving"),
        )

    async def weather(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        city = str(arguments.get("city") or "").strip()
        if not city:
            raise ValueError("city 不能为空")
        return await self._client.weather(
            city=city,
            extensions=str(arguments.get("extensions") or "base"),
        )

    async def district(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        return await self._client.district(
            keywords=str(arguments.get("keywords") or "").strip(),
            subdistrict=int(arguments.get("subdistrict") or 1),
            extensions=str(arguments.get("extensions") or "base"),
        )

    async def static_map(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        location = str(arguments.get("location") or "").strip()
        if not _COORD_RE.match(location):
            raise ValueError("location 必须是 经度,纬度")
        return await self._client.static_map(
            location=re.sub(r"\s+", "", location),
            zoom=int(arguments.get("zoom") or 12),
            size=str(arguments.get("size") or "750*500"),
            markers=str(arguments.get("markers") or "").strip(),
            labels=str(arguments.get("labels") or "").strip(),
            paths=str(arguments.get("paths") or "").strip(),
            traffic=bool(arguments.get("traffic", False)),
            storage_dir=str(self._storage_dir),
            public_base_url=str(getattr(self._settings, "wxbot_media_base_url", "") or ""),
            map_name=str(arguments.get("map_name") or "amap-static-map").strip(),
        )

    async def coordinate_convert(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        raw_locations = arguments.get("locations")
        if not isinstance(raw_locations, list):
            raise ValueError("locations 必须是数组")
        locations = []
        for item in raw_locations[:40]:
            location = str(item or "").strip()
            if not _COORD_RE.match(location):
                raise ValueError("locations 必须都是 经度,纬度")
            locations.append(re.sub(r"\s+", "", location))
        if not locations:
            raise ValueError("locations 不能为空")
        return await self._client.coordinate_convert(
            locations=locations,
            coordsys=str(arguments.get("coordsys") or "gps"),
        )

    async def traffic_status(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        name = str(arguments.get("name") or "").strip()
        if not name:
            raise ValueError("name 不能为空")
        return await self._client.traffic_status(
            name=name,
            city=str(arguments.get("city") or "").strip(),
            adcode=str(arguments.get("adcode") or "").strip(),
            level=int(arguments.get("level") or 5),
            extensions=str(arguments.get("extensions") or "base"),
        )

    async def bus_info(self, session: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_group_session(session)
        keywords = str(arguments.get("keywords") or "").strip()
        city = str(arguments.get("city") or "").strip()
        if not keywords:
            raise ValueError("keywords 不能为空")
        if not city:
            raise ValueError("city 不能为空")
        return await self._client.bus_info(
            keywords=keywords,
            city=city,
            search_type=str(arguments.get("search_type") or "line"),
            offset=int(arguments.get("offset") or 10),
            page=int(arguments.get("page") or 1),
        )

    async def create_personal_map(
        self,
        session: Session,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_group_session(session)
        await self._require_scope_execution(session, phase="start")
        await self._bind_group_delivery_contract(session)
        map_name = str(arguments.get("map_name") or "").strip()
        if not map_name:
            raise ValueError("map_name 不能为空")
        points = self._normalize_points(arguments.get("points"))
        if not points:
            raise ValueError("points 至少需要一个有效地点")
        line_title = str(arguments.get("line_title") or "").strip() or map_name
        city = self._infer_city(arguments, map_name=map_name, line_title=line_title)
        points, dropped_points = await self._ensure_point_poi_ids(points, city=city)
        await self._require_scope_execution(session, phase="before_create")
        if not points:
            raise ValueError("个人地图点位缺少有效 poiId，请先用高德搜索工具获取真实 POI 后再生成地图")
        scene_type = int(arguments.get("scene_type") or 2)
        line_list = [{"title": line_title, "pointInfoList": points}]
        result = await self._client.create_personal_map(
            map_name=map_name,
            line_list=line_list,
            scene_type=scene_type,
        )
        await self._require_scope_execution(session, phase="after_create")
        if result.get("error"):
            return result

        qr_path = ""
        qr_url = ""
        qr_error = ""
        try:
            await self._require_scope_execution(session, phase="before_qr_write")
            qr_path = self._write_qr_image(str(result.get("schema_url") or ""), map_name)
            qr_url = self._public_qr_url(qr_path)
        except Exception as exc:
            qr_error = str(exc)
            logger.warning("amap.qr_write_failed", error_class=exc.__class__.__name__)

        send_to_group = bool(arguments.get("send_to_group", True))
        wxbot_qr_path = self._wxbot_image_path(qr_path)
        channel_reply_effects: list[dict[str, Any]] = []
        if send_to_group and qr_path:
            await self._require_scope_execution(session, phase="before_reply")
            if self._effect_reply_enabled:
                channel_reply_effects = self._qr_message_effects(
                    session,
                    map_name=map_name,
                    qr_path=wxbot_qr_path,
                    qr_url=qr_url,
                )
            else:
                await self._enqueue_qr_messages(
                    session,
                    map_name=map_name,
                    schema_url=str(result.get("schema_url") or ""),
                    qr_path=wxbot_qr_path,
                    qr_url=qr_url,
                )
        enqueued_reply = bool(send_to_group and qr_path)

        return {
            "map_name": map_name,
            "scene_type": scene_type,
            "point_count": len(points),
            "schema_url": result.get("schema_url"),
            "qr_image_path": qr_path,
            "qr_wxbot_image_path": wxbot_qr_path,
            "qr_image_url": qr_url,
            "qr_image_sent": enqueued_reply,
            "qr_image_error": qr_error,
            "dropped_point_count": dropped_points,
            "self_enqueued_reply": enqueued_reply,
            "suppress_final_reply": enqueued_reply,
            "channel_reply_effects": channel_reply_effects,
            "message": "个人地图已生成，可使用高德地图 App 扫码打开。",
        }

    async def _resolve_location(self, value: str, *, city: str = "") -> str:
        if not value:
            return ""
        if _COORD_RE.match(value):
            return re.sub(r"\s+", "", value)
        result = await self._client.geo(address=value, city=city)
        if result.get("error"):
            raise ValueError(str(result.get("message") or result.get("error")))
        lon = result.get("longitude")
        lat = result.get("latitude")
        if lon is None or lat is None:
            raise ValueError(f"无法解析地址坐标: {value}")
        return f"{lon},{lat}"

    def _write_qr_image(self, schema_url: str, map_name: str) -> str:
        if not schema_url:
            raise ValueError("schema_url 为空，无法生成二维码")
        try:
            import qrcode
        except ImportError as exc:
            raise RuntimeError("缺少 qrcode 依赖，无法本地生成二维码图片") from exc

        self._storage_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff._-]+", "-", map_name).strip("-") or "map"
        trace_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(get_trace_id() or new_trace_id()))
        path = self._storage_dir / f"amap-{safe_name}-{trace_id}.png"
        img = qrcode.make(schema_url)
        img.save(path)
        return str(path)

    def _public_qr_url(self, qr_path: str) -> str:
        base_url = str(getattr(self._settings, "wxbot_media_base_url", "") or "").strip().rstrip("/")
        if not base_url:
            return ""
        file_name = Path(qr_path).name
        if not file_name:
            return ""
        return f"{base_url}/plugins/amap/files/{quote(file_name)}"

    @staticmethod
    def _wxbot_image_path(path: str) -> str:
        value = str(path or "").strip()
        match = re.match(r"^/mnt/([a-zA-Z])/(.+)$", value)
        if not match:
            return value
        drive = match.group(1).upper()
        rest = match.group(2).replace("/", "\\")
        return f"{drive}:\\{rest}"

    async def _enqueue_qr_messages(
        self,
        session: Session,
        *,
        map_name: str,
        schema_url: str,
        qr_path: str,
        qr_url: str = "",
    ) -> None:
        await self._require_scope_execution(session, phase="before_text_send")
        target = self._target(session)
        if self._channel_registry is None:
            raise RuntimeError("channel registry is not configured")
        outbound = self._channel_registry.require_outbound_for_target(target)
        session_id = target.session_id
        trace_id = str(get_trace_id() or new_trace_id())
        source_message = {
            "agent_tool": "amap_create_personal_map",
            "trace_id": trace_id,
            "session_id": session_id,
            "map_name": map_name,
            _DELIVERY_CONTRACT_METADATA_KEY: dict(
                target.metadata.get(_DELIVERY_CONTRACT_METADATA_KEY) or {}
            ),
        }
        delivery_contract = self._task_result_delivery(target)
        text = f"{map_name} 地图生成好了，用高德地图 App 扫码打开。"
        text_command_id = f"channel-reply:{target.tenant_id}:{trace_id}:amap-map-text"
        await outbound.send_text(
            target,
            text,
            ChannelSendOptions(
                trace_id=trace_id,
                source_message=source_message,
                idempotency_key=text_command_id,
                delivery_metadata={
                    "command_id": text_command_id,
                    "idempotency_key": text_command_id,
                    **delivery_contract,
                },
            ),
        )
        await self._require_scope_execution(session, phase="before_image_send")
        image_command_id = f"channel-reply:{target.tenant_id}:{trace_id}:amap-map-image"
        await outbound.send_image(
            target,
            ChannelMedia(image_path="" if qr_url else qr_path, image_url=qr_url),
            ChannelSendOptions(
                trace_id=trace_id,
                source_message=source_message,
                idempotency_key=image_command_id,
                delivery_metadata={
                    "command_id": image_command_id,
                    "idempotency_key": image_command_id,
                    **delivery_contract,
                },
            ),
        )

    def _qr_message_effects(
        self,
        session: Session,
        *,
        map_name: str,
        qr_path: str,
        qr_url: str = "",
    ) -> list[dict[str, Any]]:
        target = self._target(session)
        trace_id = str(get_trace_id() or new_trace_id())
        source_message = {
            "agent_tool": "amap_create_personal_map",
            "trace_id": trace_id,
            "session_id": target.session_id,
            "map_name": map_name,
            _DELIVERY_CONTRACT_METADATA_KEY: dict(
                target.metadata.get(_DELIVERY_CONTRACT_METADATA_KEY) or {}
            ),
        }
        delivery_contract = self._task_result_delivery(target)
        text_command_id = f"channel-reply:{target.tenant_id}:{trace_id}:amap-map-text"
        image_command_id = f"channel-reply:{target.tenant_id}:{trace_id}:amap-map-image"
        owner = "wxbot" if target.channel == "wechat" else target.channel
        base_payload = {
            "tenant_id": target.tenant_id,
            "channel": target.channel,
            "session_id": target.session_id,
            "session_name": target.session_name,
            "session_kind": target.session_kind,
            "user_id": target.user_id,
            "sender_id": target.sender_id,
            "sender_name": target.sender_name,
            "reply_to_message_id": target.reply_to_message_id,
            "trace_id": trace_id,
            "source_message": source_message,
        }
        return [
            {
                "type": "enqueue_channel_reply",
                "owner": owner,
                "idempotency_key": text_command_id,
                "payload": {
                    **base_payload,
                    "body": {
                        "type": "text",
                        "text": f"{map_name} 地图生成好了，用高德地图 App 扫码打开。",
                    },
                    "delivery": {
                        "command_id": text_command_id,
                        "idempotency_key": text_command_id,
                        **delivery_contract,
                    },
                    "command_id": text_command_id,
                },
            },
            {
                "type": "enqueue_channel_reply",
                "owner": owner,
                "idempotency_key": image_command_id,
                "payload": {
                    **base_payload,
                    "media": {
                        "image_path": "" if qr_url else qr_path,
                        "image_url": qr_url,
                    },
                    "delivery": {
                        "command_id": image_command_id,
                        "idempotency_key": image_command_id,
                        **delivery_contract,
                    },
                    "command_id": image_command_id,
                },
            },
        ]

    @staticmethod
    def _normalize_points(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        points: list[dict[str, Any]] = []
        for item in value[:16]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            lon = item.get("longitude")
            lat = item.get("latitude")
            if not name or lon is None or lat is None:
                continue
            points.append(
                {
                    "name": name,
                    "lon": float(lon),
                    "lat": float(lat),
                    "poiId": str(item.get("poi_id") or item.get("poiId") or ""),
                    "address": str(item.get("address") or ""),
                }
            )
        return points

    async def _ensure_point_poi_ids(
        self,
        points: list[dict[str, Any]],
        *,
        city: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        normalized: list[dict[str, Any]] = []
        dropped = 0
        for point in points:
            if str(point.get("poiId") or "").strip():
                normalized.append(point)
                continue
            resolved = await self._resolve_point_poi_id(point, city=city)
            if resolved is None:
                dropped += 1
                logger.warning(
                    "amap.point_without_poi_id_dropped",
                    has_name=bool(str(point.get("name") or "").strip()),
                    has_address=bool(str(point.get("address") or "").strip()),
                    city_configured=bool(str(city or "").strip()),
                )
                continue
            normalized.append(resolved)
        return normalized, dropped

    async def _resolve_point_poi_id(self, point: dict[str, Any], *, city: str = "") -> dict[str, Any] | None:
        keywords = self._point_search_keywords(point)
        if not keywords:
            return None
        result = await self._client.text_search(keywords=keywords, city=city, limit=5)
        if result.get("error"):
            logger.warning(
                "amap.point_poi_id_resolve_failed",
                has_name=bool(str(point.get("name") or "").strip()),
                has_address=bool(str(point.get("address") or "").strip()),
                city_configured=bool(str(city or "").strip()),
                has_error=True,
            )
            return None
        candidates = [
            item
            for item in result.get("items", [])
            if isinstance(item, dict) and str(item.get("poi_id") or "").strip()
        ]
        if not candidates:
            return None
        best = self._nearest_candidate(point, candidates)
        resolved = dict(point)
        resolved["poiId"] = str(best.get("poi_id") or "").strip()
        if best.get("name"):
            resolved["name"] = str(best.get("name") or resolved["name"])
        if best.get("address"):
            resolved["address"] = str(best.get("address") or resolved.get("address") or "")
        lon = best.get("longitude")
        lat = best.get("latitude")
        if lon is not None and lat is not None:
            resolved["lon"] = float(lon)
            resolved["lat"] = float(lat)
        return resolved

    @staticmethod
    def _point_search_keywords(point: dict[str, Any]) -> str:
        name = str(point.get("name") or "").strip()
        address = str(point.get("address") or "").strip()
        text = f"{name} {address}".strip()
        text = re.sub(r"[/／|｜]+", " ", text)
        text = re.sub(r"(一带|附近|周边)", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:80]

    @staticmethod
    def _nearest_candidate(point: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            lon = float(point.get("lon"))
            lat = float(point.get("lat"))
        except (TypeError, ValueError):
            return candidates[0]

        def _score(item: dict[str, Any]) -> float:
            try:
                candidate_lon = float(item.get("longitude"))
                candidate_lat = float(item.get("latitude"))
            except (TypeError, ValueError):
                return float("inf")
            return (candidate_lon - lon) ** 2 + (candidate_lat - lat) ** 2

        return min(candidates, key=_score)

    @staticmethod
    def _infer_city(arguments: dict[str, Any], *, map_name: str, line_title: str) -> str:
        explicit = str(arguments.get("city") or "").strip()
        if explicit:
            return explicit
        text = f"{map_name} {line_title}"
        match = re.search(r"([\u4e00-\u9fff]{2,4})(?:一日游|旅游|旅行|打卡|美食|景点|路线|地图)", text)
        if match:
            return match.group(1)
        return ""

    def _require_group_session(self, session: Session) -> None:
        target = self._target(session)
        if target.session_kind == "group" or target.session_id.endswith("@chatroom"):
            return
        raise ValueError("高德个人地图工具仅支持群聊或频道会话")

    @staticmethod
    def _latest_user_metadata(session: Session) -> dict[str, Any]:
        for turn in reversed(list(getattr(session, "turns", []) or [])):
            if turn.role == Role.USER:
                return dict(turn.metadata or {})
        return {}

    def _session_name(self, session: Session) -> str:
        metadata = self._latest_user_metadata(session)
        return str(
            metadata.get("session_name")
            or getattr(session, "metadata", {}).get("session_name")
            or getattr(session, "session_id", "")
            or ""
        ).strip()

    def _sender_name(self, session: Session) -> str:
        metadata = self._latest_user_metadata(session)
        return str(metadata.get("sender_name") or "").strip()

    def _sender_wxid(self, session: Session) -> str:
        metadata = self._latest_user_metadata(session)
        return str(
            metadata.get("sender_id")
            or metadata.get("sender_wxid")
            or getattr(session, "user_id", "")
            or ""
        ).strip()

    def _reply_to_msg_svr_id(self, session: Session) -> str:
        metadata = self._latest_user_metadata(session)
        return str(
            metadata.get("reply_to_message_id")
            or metadata.get("msg_svr_id")
            or metadata.get("message_id")
            or ""
        ).strip()

    def _target(self, session: Session) -> ChannelTarget:
        metadata = self._latest_user_metadata(session)
        session_contract = getattr(session, "metadata", {}).get(
            _DELIVERY_CONTRACT_METADATA_KEY
        )
        if (
            _DELIVERY_CONTRACT_METADATA_KEY not in metadata
            and isinstance(session_contract, dict)
        ):
            metadata[_DELIVERY_CONTRACT_METADATA_KEY] = dict(session_contract)
        session_id = str(getattr(session, "session_id", "") or "")
        raw_channel = getattr(session, "channel", "")
        channel = str(getattr(raw_channel, "value", raw_channel) or "")
        return ChannelTarget(
            tenant_id=str(getattr(session, "tenant_id", "") or "default"),
            channel=channel,
            session_id=session_id,
            session_name=self._session_name(session),
            session_kind=str(
                metadata.get("session_kind")
                or getattr(session, "metadata", {}).get("session_kind")
                or ("group" if session_id.endswith("@chatroom") else "")
            ),
            user_id=str(getattr(session, "user_id", "") or ""),
            sender_id=self._sender_wxid(session),
            sender_name=self._sender_name(session),
            reply_to_message_id=self._reply_to_msg_svr_id(session),
            metadata=metadata,
        )

    async def _require_scope_execution(
        self,
        session: Session,
        *,
        phase: str,
    ) -> None:
        gate = self._scope_execution_allowed
        if gate is None:
            raise RuntimeError("plugin_scope_unavailable")
        try:
            allowed = await gate(session.tenant_id, session.session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "amap.scope_execution_denied",
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                phase=phase,
                reason="gate_error",
                error_class=exc.__class__.__name__,
            )
            raise RuntimeError("plugin_scope_unavailable") from exc
        if allowed is not True:
            logger.info(
                "amap.scope_execution_denied",
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                phase=phase,
                reason="disabled",
            )
            raise RuntimeError("plugin_scope_disabled")

    async def _bind_group_delivery_contract(self, session: Session) -> None:
        target = self._target(session)
        if target.channel != "wechat" or not target.session_id.endswith("@chatroom"):
            return
        captured = target.metadata.get(_DELIVERY_CONTRACT_METADATA_KEY)
        if isinstance(captured, dict) and self._complete_delivery_contract(captured):
            return
        if self._channel_registry is None:
            raise RuntimeError("channel registry is not configured")
        outbound = self._channel_registry.require_outbound_for_target(target)
        capture = getattr(outbound, "capture_group_delivery_contract", None)
        if not callable(capture):
            return
        contract = await capture(
            target,
            source_message_id=target.reply_to_message_id,
            response_kind="tool_result",
        )
        if not isinstance(contract, dict) or not self._complete_delivery_contract(
            contract
        ):
            raise RuntimeError("amap_async_delivery_contract_unavailable")
        session.metadata[_DELIVERY_CONTRACT_METADATA_KEY] = dict(contract)

    @staticmethod
    def _complete_delivery_contract(contract: dict[str, Any]) -> bool:
        if str(contract.get("participation_status") or "") != "must_reply":
            return False
        if not str(contract.get("source_message_id") or "").strip():
            return False
        version = contract.get("participation_policy_version")
        if isinstance(version, bool) or not isinstance(version, (int, str)):
            return False
        try:
            int(version)
        except (TypeError, ValueError):
            return False
        return isinstance(contract.get("send_revalidation_enabled"), bool)

    @staticmethod
    def _task_result_delivery(target: ChannelTarget) -> dict[str, Any]:
        captured = target.metadata.get(_DELIVERY_CONTRACT_METADATA_KEY)
        delivery = dict(captured) if isinstance(captured, dict) else {}
        source_message_id = str(
            delivery.get("source_message_id") or target.reply_to_message_id or ""
        ).strip()
        delivery.update(
            {
                "participation_status": "must_reply",
                "source_message_id": source_message_id,
                "response_kind": "tool_result",
                "speech_output_kind": "ordinary",
                "speech_class": "obligation",
                "participation_reason_codes": ["direct_tool_request"],
            }
        )
        return delivery
