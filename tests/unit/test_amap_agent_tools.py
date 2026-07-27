from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agent.scopes import GROUP_PERSONAL_MAP_SCOPE
from app.channel import ChannelMedia, ChannelRegistry, ChannelSendOptions, ChannelTarget
from app.common.config import Settings
from app.common.context import clear_context, set_trace_id
from app.common.types import Channel, Role, Session, Turn
from plugins.amap.agent import AMapAgentToolService, build_amap_agent_tools
from plugins.amap.client import AMapClient


async def _allow_scope(_tenant_id: str, _session_id: str) -> bool:
    return True


class _FakeAMapClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def close(self) -> None:
        self.calls.append(("close", {}))

    async def geo(self, **kwargs):
        self.calls.append(("geo", dict(kwargs)))
        return {
            "longitude": 116.397451,
            "latitude": 39.909221,
            "formatted_address": kwargs["address"],
        }

    async def text_search(self, **kwargs):
        self.calls.append(("text_search", dict(kwargs)))
        return {
            "keywords": kwargs["keywords"],
            "items": [
                {
                    "poi_id": "B1",
                    "name": "测试咖啡",
                    "longitude": 116.4,
                    "latitude": 39.9,
                    "address": "测试地址",
                }
            ],
        }

    async def regeo(self, **kwargs):
        self.calls.append(("regeo", dict(kwargs)))
        return {"location": kwargs["location"], "formatted_address": "测试地址", "pois": [], "aois": []}

    async def place_detail(self, **kwargs):
        self.calls.append(("place_detail", dict(kwargs)))
        return {"poi_id": kwargs["poi_id"], "detail": {"name": "测试咖啡"}}

    async def input_tips(self, **kwargs):
        self.calls.append(("input_tips", dict(kwargs)))
        return {"keywords": kwargs["keywords"], "tips": [{"name": "测试候选"}]}

    async def around_search(self, **kwargs):
        self.calls.append(("around_search", dict(kwargs)))
        return {"keywords": kwargs["keywords"], "items": []}

    async def route_plan(self, **kwargs):
        self.calls.append(("route_plan", dict(kwargs)))
        return {"mode": kwargs["mode"], "distance_meters": 1200}

    async def distance(self, **kwargs):
        self.calls.append(("distance", dict(kwargs)))
        return {"mode": kwargs["mode"], "results": [{"distance_meters": 1200}]}

    async def weather(self, **kwargs):
        self.calls.append(("weather", dict(kwargs)))
        return {"city": kwargs["city"], "lives": []}

    async def district(self, **kwargs):
        self.calls.append(("district", dict(kwargs)))
        return {"keywords": kwargs["keywords"], "districts": []}

    async def static_map(self, **kwargs):
        self.calls.append(("static_map", dict(kwargs)))
        return {"location": kwargs["location"], "image_path": "/tmp/static.png"}

    async def coordinate_convert(self, **kwargs):
        self.calls.append(("coordinate_convert", dict(kwargs)))
        return {"coordsys": kwargs["coordsys"], "locations": kwargs["locations"]}

    async def traffic_status(self, **kwargs):
        self.calls.append(("traffic_status", dict(kwargs)))
        return {"name": kwargs["name"], "trafficinfo": {}}

    async def bus_info(self, **kwargs):
        self.calls.append(("bus_info", dict(kwargs)))
        return {"keywords": kwargs["keywords"], "city": kwargs["city"], "buslines": []}

    async def create_personal_map(self, **kwargs):
        self.calls.append(("create_personal_map", dict(kwargs)))
        return {"schema_url": "amapuri://personal-map/demo", "line_list": kwargs["line_list"]}


class _FakeWxbotStore:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def get_session_policy(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        return {"effective_mention_sender": True}

    async def enqueue_reply(self, **kwargs) -> int:
        self.enqueued.append(dict(kwargs))
        return len(self.enqueued)


class _FakeChannelOutbound:
    def __init__(self, store: _FakeWxbotStore) -> None:
        self.store = store

    async def get_session_policy(self, target: ChannelTarget) -> dict[str, Any]:
        return await self.store.get_session_policy(target.tenant_id, target.session_id)

    async def send_text(
        self,
        target: ChannelTarget,
        text: str,
        options: ChannelSendOptions | None = None,
    ):
        options = options or ChannelSendOptions()
        return await self.store.enqueue_reply(
            tenant_id=target.tenant_id,
            session_id=target.session_id,
            session_name=target.session_name,
            sender_name=target.sender_name,
            sender_wxid=target.sender_id,
            reply_text=text,
            trace_id=options.trace_id,
            msg_type="text",
            mention_sender=True,
            reply_to_msg_svr_id=target.reply_to_message_id,
            session_kind=target.session_kind,
            source_message=options.source_message,
            delivery=options.delivery_metadata,
            command_id=options.idempotency_key,
        )

    async def send_image(
        self,
        target: ChannelTarget,
        media: ChannelMedia,
        options: ChannelSendOptions | None = None,
    ):
        options = options or ChannelSendOptions()
        return await self.store.enqueue_reply(
            tenant_id=target.tenant_id,
            session_id=target.session_id,
            session_name=target.session_name,
            sender_name=target.sender_name,
            sender_wxid=target.sender_id,
            reply_text="",
            trace_id=options.trace_id,
            msg_type="image",
            image_path=media.image_path,
            image_url=media.image_url,
            mention_sender=True,
            reply_to_msg_svr_id=target.reply_to_message_id,
            session_kind=target.session_kind,
            source_message=options.source_message,
            delivery=options.delivery_metadata,
            command_id=options.idempotency_key,
        )


async def _allow_channel_owner(_owner: str, _target: ChannelTarget) -> bool:
    return True


def _channel_registry(store: _FakeWxbotStore) -> ChannelRegistry:
    registry = ChannelRegistry(owner_gate=_allow_channel_owner)
    registry.register_outbound("wechat", _FakeChannelOutbound(store), owner="test")
    return registry


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        amap_api_key="test-key",
        amap_storage_dir=str(tmp_path),
        wxbot_media_base_url="http://media.test",
        llm_provider="fake",
    )


def _session() -> Session:
    return Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_sender",
        channel=Channel.WECHAT,
        turns=[
            Turn(
                role=Role.USER,
                session_id="room@chatroom",
                content="帮我生成地图",
                metadata={
                    "session_name": "测试群",
                    "sender_name": "小石",
                    "sender_wxid": "wxid_sender",
                    "msg_svr_id": "msg-1",
                    "_wxbot_delivery_contract": {
                        "participation_status": "must_reply",
                        "source_message_id": "msg-1",
                        "participation_policy_version": 17,
                        "send_revalidation_enabled": True,
                    },
                },
            )
        ],
    )


def test_amap_agent_tool_catalog_uses_personal_map_scope(tmp_path: Path) -> None:
    service = AMapAgentToolService(_settings(tmp_path), client=_FakeAMapClient())

    tools = build_amap_agent_tools(service)

    assert {item.scope for item in tools} == {GROUP_PERSONAL_MAP_SCOPE}
    assert all(item.metadata == {"session_kinds": ["group"]} for item in tools)
    assert {item.name for item in tools} == {
        "amap_geo",
        "amap_text_search",
        "amap_regeo",
        "amap_place_detail",
        "amap_input_tips",
        "amap_around_search",
        "amap_route_plan",
        "amap_distance",
        "amap_weather",
        "amap_district",
        "amap_static_map",
        "amap_coordinate_convert",
        "amap_traffic_status",
        "amap_bus_info",
        "amap_create_personal_map",
    }


@pytest.mark.asyncio
async def test_route_plan_resolves_addresses_before_planning(tmp_path: Path) -> None:
    fake_client = _FakeAMapClient()
    service = AMapAgentToolService(_settings(tmp_path), client=fake_client)

    result = await service.route_plan(
        _session(),
        {"origin": "人民广场", "destination": "外滩", "mode": "walking", "city": "上海"},
    )

    assert result["mode"] == "walking"
    assert [name for name, _ in fake_client.calls] == ["geo", "geo", "route_plan"]
    assert fake_client.calls[-1][1]["origin"] == "116.397451,39.909221"
    assert fake_client.calls[-1][1]["destination"] == "116.397451,39.909221"


@pytest.mark.asyncio
async def test_new_amap_query_tools_delegate_to_client(tmp_path: Path) -> None:
    fake_client = _FakeAMapClient()
    service = AMapAgentToolService(_settings(tmp_path), client=fake_client)
    session = _session()

    await service.regeo(session, {"location": "116.4, 39.9", "radius": 500})
    await service.place_detail(session, {"poi_id": "B1"})
    await service.input_tips(session, {"keywords": "中控", "city": "武汉", "location": "116.4,39.9"})
    await service.distance(
        session,
        {"origins": ["人民广场", "116.4,39.9"], "destination": "外滩", "mode": "walking", "city": "上海"},
    )
    await service.weather(session, {"city": "长沙", "extensions": "all"})
    await service.district(session, {"keywords": "武汉", "subdistrict": 2})
    await service.static_map(session, {"location": "116.4,39.9", "map_name": "测试地图"})
    await service.coordinate_convert(session, {"locations": ["116.4, 39.9"], "coordsys": "gps"})
    await service.traffic_status(session, {"name": "中山路", "city": "武汉"})
    await service.bus_info(session, {"keywords": "718", "city": "武汉", "search_type": "line"})

    call_names = [name for name, _ in fake_client.calls]
    assert call_names == [
        "regeo",
        "place_detail",
        "input_tips",
        "geo",
        "geo",
        "distance",
        "weather",
        "district",
        "static_map",
        "coordinate_convert",
        "traffic_status",
        "bus_info",
    ]
    assert fake_client.calls[0][1]["location"] == "116.4,39.9"
    assert fake_client.calls[5][1]["origins"] == ["116.397451,39.909221", "116.4,39.9"]
    assert fake_client.calls[9][1]["locations"] == ["116.4,39.9"]


@pytest.mark.asyncio
async def test_create_personal_map_enqueues_text_and_qr_image(tmp_path: Path) -> None:
    fake_client = _FakeAMapClient()
    fake_store = _FakeWxbotStore()
    service = AMapAgentToolService(
        _settings(tmp_path),
        client=fake_client,
        channel_registry=_channel_registry(fake_store),
        scope_execution_allowed=_allow_scope,
    )
    service._write_qr_image = lambda schema_url, map_name: str(tmp_path / "qr.png")  # type: ignore[method-assign]

    result = await service.create_personal_map(
        _session(),
        {
            "map_name": "周末咖啡地图",
            "points": [
                {
                    "name": "测试咖啡",
                    "longitude": 116.4,
                    "latitude": 39.9,
                    "poi_id": "B1",
                    "address": "测试地址",
                }
            ],
            "scene_type": 2,
        },
    )

    assert result["qr_image_sent"] is True
    assert result["point_count"] == 1
    assert result["suppress_final_reply"] is True
    assert len(fake_store.enqueued) == 2
    assert fake_store.enqueued[0]["msg_type"] == "text"
    assert "备用链接" not in fake_store.enqueued[0]["reply_text"]
    assert "amapuri://" not in fake_store.enqueued[0]["reply_text"]
    assert fake_store.enqueued[1]["msg_type"] == "image"
    assert fake_store.enqueued[1]["image_path"] == ""
    assert fake_store.enqueued[1]["image_url"].startswith("http://media.test/plugins/amap/files/")
    delivery = fake_store.enqueued[0]["delivery"]
    assert delivery["participation_status"] == "must_reply"
    assert delivery["source_message_id"] == "msg-1"
    assert delivery["participation_policy_version"] == 17
    assert delivery["send_revalidation_enabled"] is True
    assert delivery["speech_class"] == "obligation"
    assert result["qr_image_url"].startswith("http://media.test/plugins/amap/files/")


@pytest.mark.asyncio
async def test_create_personal_map_fails_closed_without_scope_gate(
    tmp_path: Path,
) -> None:
    fake_client = _FakeAMapClient()
    service = AMapAgentToolService(
        _settings(tmp_path),
        client=fake_client,
        channel_registry=_channel_registry(_FakeWxbotStore()),
    )

    with pytest.raises(RuntimeError, match="plugin_scope_unavailable"):
        await service.create_personal_map(
            _session(),
            {
                "map_name": "周末咖啡地图",
                "points": [
                    {
                        "name": "测试咖啡",
                        "longitude": 116.4,
                        "latitude": 39.9,
                        "poi_id": "B1",
                    }
                ],
            },
        )

    assert fake_client.calls == []


@pytest.mark.asyncio
async def test_create_personal_map_discards_result_when_scope_disabled_in_flight(
    tmp_path: Path,
) -> None:
    fake_client = _FakeAMapClient()
    fake_store = _FakeWxbotStore()
    decisions = iter((True, True, False))

    async def scope_allowed(_tenant_id: str, _session_id: str) -> bool:
        return next(decisions)

    service = AMapAgentToolService(
        _settings(tmp_path),
        client=fake_client,
        channel_registry=_channel_registry(fake_store),
        scope_execution_allowed=scope_allowed,
    )
    qr_writes = 0

    def write_qr(_schema_url: str, _map_name: str) -> str:
        nonlocal qr_writes
        qr_writes += 1
        return str(tmp_path / "qr.png")

    service._write_qr_image = write_qr  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="plugin_scope_disabled"):
        await service.create_personal_map(
            _session(),
            {
                "map_name": "周末咖啡地图",
                "points": [
                    {
                        "name": "测试咖啡",
                        "longitude": 116.4,
                        "latitude": 39.9,
                        "poi_id": "B1",
                    }
                ],
            },
        )

    assert [name for name, _kwargs in fake_client.calls] == [
        "create_personal_map"
    ]
    assert qr_writes == 0
    assert fake_store.enqueued == []


@pytest.mark.asyncio
async def test_create_personal_map_can_return_channel_reply_effects(tmp_path: Path) -> None:
    fake_client = _FakeAMapClient()
    fake_store = _FakeWxbotStore()
    service = AMapAgentToolService(
        _settings(tmp_path),
        client=fake_client,
        channel_registry=_channel_registry(fake_store),
        effect_reply_enabled=True,
        scope_execution_allowed=_allow_scope,
    )
    service._write_qr_image = lambda schema_url, map_name: str(tmp_path / "qr.png")  # type: ignore[method-assign]

    set_trace_id("trace-amap-effect")
    try:
        result = await service.create_personal_map(
            _session(),
            {
                "map_name": "周末咖啡地图",
                "points": [
                    {
                        "name": "测试咖啡",
                        "longitude": 116.4,
                        "latitude": 39.9,
                        "poi_id": "B1",
                        "address": "测试地址",
                    }
                ],
                "scene_type": 2,
            },
        )
    finally:
        clear_context()

    assert result["qr_image_sent"] is True
    assert result["suppress_final_reply"] is True
    assert fake_store.enqueued == []
    effects = result["channel_reply_effects"]
    assert len(effects) == 2
    assert [item["type"] for item in effects] == [
        "enqueue_channel_reply",
        "enqueue_channel_reply",
    ]
    assert [item["owner"] for item in effects] == ["wxbot", "wxbot"]
    assert effects[0]["idempotency_key"] == "channel-reply:demo:trace-amap-effect:amap-map-text"
    assert effects[0]["payload"]["body"]["text"] == "周末咖啡地图 地图生成好了，用高德地图 App 扫码打开。"
    effect_delivery = effects[0]["payload"]["delivery"]
    assert effect_delivery["participation_status"] == "must_reply"
    assert effect_delivery["source_message_id"] == "msg-1"
    assert effect_delivery["participation_policy_version"] == 17
    assert effect_delivery["send_revalidation_enabled"] is True
    assert effects[1]["idempotency_key"] == "channel-reply:demo:trace-amap-effect:amap-map-image"
    assert effects[1]["payload"]["media"]["image_path"] == ""
    assert effects[1]["payload"]["media"]["image_url"].startswith(
        "http://media.test/plugins/amap/files/"
    )


@pytest.mark.asyncio
async def test_create_personal_map_resolves_missing_poi_id(tmp_path: Path) -> None:
    fake_client = _FakeAMapClient()
    service = AMapAgentToolService(
        _settings(tmp_path),
        client=fake_client,
        channel_registry=_channel_registry(_FakeWxbotStore()),
        scope_execution_allowed=_allow_scope,
    )
    service._write_qr_image = lambda schema_url, map_name: str(tmp_path / "qr.png")  # type: ignore[method-assign]

    result = await service.create_personal_map(
        _session(),
        {
            "map_name": "长沙一日游",
            "line_title": "长沙一日游打卡路线",
            "city": "长沙",
            "points": [
                {
                    "name": "坡子街/火宫殿一带",
                    "longitude": 112.9771,
                    "latitude": 28.1918,
                    "address": "黄兴南路步行商业街附近",
                }
            ],
            "scene_type": 1,
        },
    )

    assert result["point_count"] == 1
    assert [name for name, _ in fake_client.calls] == ["text_search", "create_personal_map"]
    created_line_list = fake_client.calls[-1][1]["line_list"]
    assert created_line_list[0]["pointInfoList"][0]["poiId"] == "B1"


@pytest.mark.asyncio
async def test_amap_client_new_query_endpoints() -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/geocode/regeo"):
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "regeocode": {
                        "formatted_address": "武汉市洪山区测试地址",
                        "pois": [{"id": "B1", "name": "测试点", "location": "114.1,30.1"}],
                        "aois": [{"id": "A1", "name": "测试园区", "location": "114.1,30.1"}],
                    },
                },
            )
        if request.url.path.endswith("/place/detail"):
            return httpx.Response(200, json={"status": "1", "pois": [{"id": "B1", "name": "测试点"}]})
        if request.url.path.endswith("/assistant/inputtips"):
            return httpx.Response(200, json={"status": "1", "tips": [{"name": "测试候选", "id": "B2"}]})
        if request.url.path.endswith("/distance"):
            return httpx.Response(200, json={"status": "1", "results": [{"distance": "1200", "duration": "600"}]})
        if request.url.path.endswith("/weather/weatherInfo"):
            return httpx.Response(200, json={"status": "1", "lives": [{"city": "武汉"}]})
        if request.url.path.endswith("/config/district"):
            return httpx.Response(200, json={"status": "1", "districts": [{"name": "武汉市"}]})
        if request.url.path.endswith("/assistant/coordinate/convert"):
            return httpx.Response(200, json={"status": "1", "locations": "114.1,30.1"})
        if request.url.path.endswith("/traffic/status/road"):
            return httpx.Response(200, json={"status": "1", "trafficinfo": {"name": "中山路"}})
        if request.url.path.endswith("/bus/linename"):
            return httpx.Response(200, json={"status": "1", "buslines": [{"name": "718路"}]})
        raise AssertionError(f"unexpected path {request.url.path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    client = AMapClient(api_key="test-key", timeout=30, http_client=http_client)

    assert (await client.regeo(location="114.1,30.1"))["formatted_address"] == "武汉市洪山区测试地址"
    assert (await client.place_detail(poi_id="B1"))["detail"]["name"] == "测试点"
    assert (await client.input_tips(keywords="测试"))["tips"][0]["name"] == "测试候选"
    assert (await client.distance(origins=["114.1,30.1"], destination="114.2,30.2"))["results"][0]["distance_meters"] == 1200
    assert (await client.weather(city="武汉"))["lives"][0]["city"] == "武汉"
    assert (await client.district(keywords="武汉"))["districts"][0]["name"] == "武汉市"
    assert (await client.coordinate_convert(locations=["114.1,30.1"], coordsys="gps"))["locations"] == ["114.1,30.1"]
    assert (await client.traffic_status(name="中山路", city="武汉"))["trafficinfo"]["name"] == "中山路"
    assert (await client.bus_info(keywords="718", city="武汉"))["buslines"][0]["name"] == "718路"
    assert [path for path, _ in seen] == [
        "/v3/geocode/regeo",
        "/v3/place/detail",
        "/v3/assistant/inputtips",
        "/v3/distance",
        "/v3/weather/weatherInfo",
        "/v3/config/district",
        "/v3/assistant/coordinate/convert",
        "/v3/traffic/status/road",
        "/v3/bus/linename",
    ]
    await client.close()


@pytest.mark.asyncio
async def test_amap_client_static_map_writes_image(tmp_path: Path) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/staticmap"
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png-bytes")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    client = AMapClient(api_key="test-key", timeout=30, http_client=http_client)

    result = await client.static_map(
        location="114.1,30.1",
        storage_dir=str(tmp_path),
        public_base_url="http://media.test",
        map_name="测试静态图",
    )

    assert Path(result["image_path"]).read_bytes() == b"png-bytes"
    assert result["image_url"].startswith("http://media.test/plugins/amap/files/")
    await client.close()


@pytest.mark.asyncio
async def test_amap_client_never_returns_a_static_map_url_containing_the_key() -> None:
    requested = False

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    client = AMapClient(api_key="must-not-leak", timeout=30, http_client=http_client)

    result = await client.static_map(location="114.1,30.1", storage_dir="")

    assert result["error"] == "静态地图存储目录缺失"
    assert "static_map_url" not in result
    assert "must-not-leak" not in str(result)
    assert requested is False
    await client.close()


@pytest.mark.asyncio
async def test_amap_client_does_not_expose_key_or_upstream_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.amap import client as amap_client_module

    secret = "must-not-leak"
    logged: list[dict[str, Any]] = []

    class _Logger:
        def warning(self, _event: str, **kwargs: Any) -> None:
            logged.append(kwargs)

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            headers={"content-type": "text/plain"},
            text=f"bad key={secret} request",
        )

    monkeypatch.setattr(amap_client_module, "logger", _Logger())
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    client = AMapClient(api_key=secret, timeout=30, http_client=http_client)

    result = await client._get_json(
        "https://restapi.amap.com/v3/geocode/geo",
        params={"key": secret, "address": "test"},
    )

    assert result["error"] == "请求失败"
    assert result["message"] == "高德接口返回 HTTP 400"
    assert secret not in str(result)
    assert secret not in str(logged)
    assert logged == [{"method": "GET", "status_code": 400}]
    await client.close()


@pytest.mark.asyncio
async def test_amap_client_create_personal_map_reports_timeout() -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    client = AMapClient(api_key="test-key", timeout=30, http_client=http_client)

    result = await client.create_personal_map(
        map_name="测试地图",
        line_list=[
            {
                "title": "测试路线",
                "pointInfoList": [
                    {"name": "测试点", "lon": 116.4, "lat": 39.9, "poiId": "B1"},
                ],
            }
        ],
        scene_type=2,
    )

    assert result["error"] == "生成地图行程失败"
    assert result["upstream_error"] == "请求超时"
    assert "高德个人地图创建接口超时" in result["message"]
    await client.close()


def test_wxbot_image_path_converts_wsl_mount_for_windows_sdk(tmp_path: Path) -> None:
    service = AMapAgentToolService(_settings(tmp_path), client=_FakeAMapClient())

    assert service._wxbot_image_path("/mnt/c/Users/Public/agent-console-amap/qr.png") == (
        r"C:\Users\Public\agent-console-amap\qr.png"
    )
