from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.common.logging import get_logger
from app.common.safe_url import OutboundURLPolicy
from app.egress.safe_http import safe_http_request

logger = get_logger(__name__)
_KEY_QUERY_RE = re.compile(r"([?&]key=)[^&\s]+", re.IGNORECASE)
_AMAP_MAX_JSON_BYTES = 2 * 1024 * 1024
_AMAP_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_AMAP_JSON_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "text/plain",
)


def _error(error: str, message: str) -> dict[str, Any]:
    return {"error": error, "message": message}


def _redact_secret(value: object, secret: str) -> str:
    text = str(value or "")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return _KEY_QUERY_RE.sub(r"\1[REDACTED]", text)


def _redact_secret_payload(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_secret_payload(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret_payload(item, secret) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secret_payload(item, secret) for item in value)
    if isinstance(value, str):
        return _redact_secret(value, secret)
    return value


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_location(value: str) -> tuple[float | None, float | None]:
    parts = str(value or "").split(",", 1)
    if len(parts) != 2:
        return None, None
    return _as_float(parts[0]), _as_float(parts[1])


def _amap_policy(
    url: str,
    *,
    timeout: float,
    max_response_bytes: int,
    content_types: tuple[str, ...],
    max_redirects: int,
) -> OutboundURLPolicy:
    hostname = str(httpx.URL(url).host or "").strip().lower().rstrip(".")
    return OutboundURLPolicy(
        require_https=True,
        allowed_hosts=frozenset({hostname}) if hostname else frozenset(),
        max_redirects=max(0, int(max_redirects)),
        max_response_bytes=max(1, int(max_response_bytes)),
        timeout_seconds=max(0.1, float(timeout)),
        allowed_response_content_types=content_types,
    )


class AMapClient:
    def __init__(
        self,
        *,
        api_key: str,
        timeout: float = 15.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.timeout = float(timeout or 15.0)
        self._http_client = http_client
        self.base_url = "https://restapi.amap.com/v3"
        self.wia_base_url = "https://restapi.amap.com"

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def geo(self, *, address: str, city: str = "") -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        params = {"key": self.api_key, "address": address}
        if city:
            params["city"] = city
        result = await self._get_json(f"{self.base_url}/geocode/geo", params=params)
        if result.get("status") != "1" or int(result.get("count") or 0) <= 0:
            return _error("无法找到该地址", str(result.get("info") or "未知错误"))
        first = (result.get("geocodes") or [{}])[0]
        lon, lat = _split_location(str(first.get("location") or ""))
        return {
            "longitude": lon,
            "latitude": lat,
            "formatted_address": str(first.get("formatted_address") or address),
        }

    async def text_search(
        self,
        *,
        keywords: str,
        city: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        params = {
            "key": self.api_key,
            "keywords": keywords,
            "offset": max(1, min(int(limit or 10), 20)),
            "page": 1,
        }
        if city:
            params["city"] = city
        result = await self._get_json(f"{self.base_url}/place/text", params=params)
        if result.get("status") != "1":
            return _error("搜索失败", str(result.get("info") or "未知错误"))
        return {
            "keywords": keywords,
            "city": city,
            "items": [self._normalize_poi(item) for item in result.get("pois", [])],
        }

    async def regeo(
        self,
        *,
        location: str,
        radius: int = 1000,
        extensions: str = "all",
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        params = {
            "key": self.api_key,
            "location": location,
            "radius": max(0, min(int(radius or 1000), 3000)),
            "extensions": self._normalize_extensions(extensions),
        }
        result = await self._get_json(f"{self.base_url}/geocode/regeo", params=params)
        if result.get("status") != "1":
            return _error("逆地理编码失败", str(result.get("info") or "未知错误"))
        regeocode = result.get("regeocode") or {}
        return {
            "location": location,
            "formatted_address": str(regeocode.get("formatted_address") or ""),
            "address_component": regeocode.get("addressComponent") or {},
            "pois": [self._normalize_poi(item) for item in (regeocode.get("pois") or [])],
            "aois": [self._normalize_aoi(item) for item in (regeocode.get("aois") or [])],
        }

    async def place_detail(self, *, poi_id: str) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        result = await self._get_json(
            f"{self.base_url}/place/detail",
            params={"key": self.api_key, "id": poi_id},
        )
        if result.get("status") != "1":
            return _error("POI 详情查询失败", str(result.get("info") or "未知错误"))
        pois = result.get("pois") or []
        if not pois:
            return _error("POI 详情查询失败", "未找到该 POI")
        return {"poi_id": poi_id, "detail": self._normalize_poi_detail(pois[0])}

    async def input_tips(
        self,
        *,
        keywords: str,
        city: str = "",
        citylimit: bool = False,
        location: str = "",
        datatype: str = "all",
        limit: int = 10,
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        params: dict[str, Any] = {
            "key": self.api_key,
            "keywords": keywords,
            "citylimit": "true" if citylimit else "false",
            "datatype": datatype or "all",
        }
        if city:
            params["city"] = city
        if location:
            params["location"] = location
        result = await self._get_json(f"{self.base_url}/assistant/inputtips", params=params)
        if result.get("status") != "1":
            return _error("输入提示查询失败", str(result.get("info") or "未知错误"))
        tips = [item for item in result.get("tips", []) if isinstance(item, dict)]
        return {
            "keywords": keywords,
            "city": city,
            "tips": [self._normalize_tip(item) for item in tips[: max(1, min(int(limit or 10), 20))]],
        }

    async def around_search(
        self,
        *,
        keywords: str,
        location: str,
        radius: int = 1000,
        types: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        params: dict[str, Any] = {
            "key": self.api_key,
            "keywords": keywords,
            "location": location,
            "radius": max(1, min(int(radius or 1000), 50000)),
            "offset": max(1, min(int(limit or 10), 20)),
            "page": 1,
        }
        if types:
            params["types"] = types
        result = await self._get_json(f"{self.base_url}/place/around", params=params)
        if result.get("status") != "1":
            return _error("周边搜索失败", str(result.get("info") or "未知错误"))
        return {
            "keywords": keywords,
            "location": location,
            "radius": params["radius"],
            "items": [self._normalize_poi(item) for item in result.get("pois", [])],
        }

    async def route_plan(
        self,
        *,
        origin: str,
        destination: str,
        mode: str = "driving",
        city: str = "",
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        normalized_mode = str(mode or "driving").strip().lower()
        endpoint = {
            "walking": "walking",
            "driving": "driving",
            "transit": "transit/integrated",
        }.get(normalized_mode)
        if endpoint is None:
            return _error("不支持的路线模式", "mode 仅支持 walking、driving、transit")
        params: dict[str, Any] = {
            "key": self.api_key,
            "origin": origin,
            "destination": destination,
        }
        if normalized_mode == "transit":
            params["city"] = city or "北京"
        result = await self._get_json(f"{self.base_url}/direction/{endpoint}", params=params)
        if result.get("status") != "1":
            return _error("路径规划失败", str(result.get("info") or "未知错误"))
        return self._summarize_route_result(result, mode=normalized_mode, origin=origin, destination=destination)

    async def distance(
        self,
        *,
        origins: list[str],
        destination: str,
        mode: str = "driving",
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        normalized_mode = str(mode or "driving").strip().lower()
        type_code = {"straight": "0", "driving": "1", "walking": "3"}.get(normalized_mode)
        if type_code is None:
            return _error("不支持的距离模式", "mode 仅支持 straight、driving、walking")
        normalized_origins = [str(item).strip() for item in origins if str(item or "").strip()][:100]
        result = await self._get_json(
            f"{self.base_url}/distance",
            params={
                "key": self.api_key,
                "origins": "|".join(normalized_origins),
                "destination": destination,
                "type": type_code,
            },
        )
        if result.get("status") != "1":
            return _error("距离测量失败", str(result.get("info") or "未知错误"))
        return {
            "mode": normalized_mode,
            "destination": destination,
            "results": [self._normalize_distance(item) for item in result.get("results", [])],
        }

    async def weather(self, *, city: str, extensions: str = "base") -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        normalized_extensions = str(extensions or "base").strip().lower()
        if normalized_extensions not in {"base", "all"}:
            normalized_extensions = "base"
        result = await self._get_json(
            f"{self.base_url}/weather/weatherInfo",
            params={"key": self.api_key, "city": city, "extensions": normalized_extensions},
        )
        if result.get("status") != "1":
            return _error("天气查询失败", str(result.get("info") or "未知错误"))
        return {
            "city": city,
            "extensions": normalized_extensions,
            "lives": result.get("lives") or [],
            "forecasts": result.get("forecasts") or [],
        }

    async def district(
        self,
        *,
        keywords: str = "",
        subdistrict: int = 1,
        extensions: str = "base",
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        params = {
            "key": self.api_key,
            "keywords": keywords,
            "subdistrict": max(0, min(int(subdistrict or 1), 3)),
            "extensions": self._normalize_extensions(extensions),
        }
        result = await self._get_json(f"{self.base_url}/config/district", params=params)
        if result.get("status") != "1":
            return _error("行政区域查询失败", str(result.get("info") or "未知错误"))
        return {"keywords": keywords, "districts": result.get("districts") or []}

    async def static_map(
        self,
        *,
        location: str,
        zoom: int = 12,
        size: str = "750*500",
        markers: str = "",
        labels: str = "",
        paths: str = "",
        traffic: bool = False,
        storage_dir: str = "",
        public_base_url: str = "",
        map_name: str = "amap-static-map",
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        params: dict[str, Any] = {
            "key": self.api_key,
            "location": location,
            "zoom": max(1, min(int(zoom or 12), 17)),
            "size": self._normalize_static_map_size(size),
            "traffic": "1" if traffic else "0",
        }
        if markers:
            params["markers"] = markers
        if labels:
            params["labels"] = labels
        if paths:
            params["paths"] = paths
        url = f"{self.base_url}/staticmap"
        if not storage_dir:
            # A signed upstream URL contains the API key in its query string.
            # Never return that URL to a tool caller, transcript, or audit sink.
            return _error(
                "静态地图存储目录缺失",
                "请配置受控的 AMAP_STORAGE_DIR 后再生成静态地图。",
            )
        image = await self._get_bytes(url, params=params)
        if image.get("error"):
            return image
        path = self._write_static_map_image(
            bytes(image.get("content") or b""),
            storage_dir=storage_dir,
            map_name=map_name,
        )
        return {
            "location": location,
            "image_path": path,
            "image_url": self._public_file_url(path, public_base_url=public_base_url),
        }

    async def coordinate_convert(
        self,
        *,
        locations: list[str],
        coordsys: str = "gps",
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        normalized_coordsys = str(coordsys or "gps").strip().lower()
        if normalized_coordsys not in {"gps", "mapbar", "baidu", "autonavi"}:
            return _error("不支持的坐标系", "coordsys 仅支持 gps、mapbar、baidu、autonavi")
        normalized_locations = [str(item).strip() for item in locations if str(item or "").strip()][:40]
        result = await self._get_json(
            f"{self.base_url}/assistant/coordinate/convert",
            params={
                "key": self.api_key,
                "locations": "|".join(normalized_locations),
                "coordsys": normalized_coordsys,
            },
        )
        if result.get("status") != "1":
            return _error("坐标转换失败", str(result.get("info") or "未知错误"))
        converted = str(result.get("locations") or "")
        return {
            "coordsys": normalized_coordsys,
            "locations": converted.split(";") if converted else [],
        }

    async def traffic_status(
        self,
        *,
        name: str,
        city: str = "",
        adcode: str = "",
        level: int = 5,
        extensions: str = "base",
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        params: dict[str, Any] = {
            "key": self.api_key,
            "name": name,
            "level": max(1, min(int(level or 5), 6)),
            "extensions": self._normalize_extensions(extensions),
        }
        if city:
            params["city"] = city
        if adcode:
            params["adcode"] = adcode
        result = await self._get_json(f"{self.base_url}/traffic/status/road", params=params)
        if result.get("status") != "1":
            return _error("交通态势查询失败", str(result.get("info") or "未知错误"))
        return {"name": name, "trafficinfo": result.get("trafficinfo") or {}}

    async def bus_info(
        self,
        *,
        keywords: str,
        city: str,
        search_type: str = "line",
        offset: int = 10,
        page: int = 1,
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        normalized_search_type = str(search_type or "line").strip().lower()
        endpoint = {"line": "linename", "stop": "stopname"}.get(normalized_search_type)
        if endpoint is None:
            return _error("不支持的公交查询类型", "search_type 仅支持 line、stop")
        result = await self._get_json(
            f"{self.base_url}/bus/{endpoint}",
            params={
                "key": self.api_key,
                "keywords": keywords,
                "city": city,
                "offset": max(1, min(int(offset or 10), 20)),
                "page": max(1, int(page or 1)),
                "extensions": "all",
            },
        )
        if result.get("status") != "1":
            return _error("公交信息查询失败", str(result.get("info") or "未知错误"))
        return {
            "search_type": normalized_search_type,
            "keywords": keywords,
            "city": city,
            "buslines": result.get("buslines") or [],
            "busstops": result.get("busstops") or [],
        }

    async def create_personal_map(
        self,
        *,
        map_name: str,
        line_list: list[dict[str, Any]],
        scene_type: int = 1,
    ) -> dict[str, Any]:
        if missing := self._missing_key_error():
            return missing
        normalized_scene_type = int(scene_type or 1)
        if normalized_scene_type not in {1, 2, 3}:
            normalized_scene_type = 1
        payload = {
            "channel": "60000001",
            "orgName": map_name,
            "lineList": line_list,
            "sceneType": normalized_scene_type,
        }
        result = await self._post_json(
            f"{self.wia_base_url}/rest/wia/mcp/schema",
            params={"key": self.api_key, "source": "personal-map"},
            json_payload=payload,
        )
        if result.get("code") == 1 and result.get("result") is True:
            schema_url = str((result.get("data") or {}).get("schemaUrl") or "")
            if not schema_url:
                return _error("生成地图行程失败", "未返回有效的行程链接")
            return {
                "map_name": map_name,
                "scene_type": normalized_scene_type,
                "schema_url": schema_url,
                "line_list": line_list,
            }
        if result.get("error"):
            upstream_error = str(result.get("error") or "请求失败")
            upstream_message = str(result.get("message") or upstream_error)
            if upstream_error == "请求超时":
                message = (
                    f"高德个人地图创建接口超时（{self.timeout:.0f}s），POI 已找到，"
                    "可稍后重试生成地图。"
                )
            else:
                message = f"高德个人地图创建接口请求失败：{upstream_message}"
            return {
                "error": "生成地图行程失败",
                "message": message,
                "upstream_error": upstream_error,
                "upstream_message": upstream_message,
            }
        return _error("生成地图行程失败", str(result.get("message") or result.get("info") or "未知错误"))

    async def _get_json(self, url: str, *, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await safe_http_request(
                self._client,
                "GET",
                str(httpx.URL(url, params=params)),
                headers={"Accept": "application/json"},
                policy=_amap_policy(
                    url,
                    timeout=self.timeout,
                    max_response_bytes=_AMAP_MAX_JSON_BYTES,
                    content_types=_AMAP_JSON_CONTENT_TYPES,
                    max_redirects=2,
                ),
            )
            response.raise_for_status()
            return dict(_redact_secret_payload(response.json(), self.api_key))
        except httpx.TimeoutException as exc:
            logger.warning(
                "amap.http_timeout",
                method="GET",
                timeout_seconds=self.timeout,
                exception_type=type(exc).__name__,
            )
            return _error("请求超时", f"高德接口请求超时（{self.timeout:.0f}s）")
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "amap.http_status_error",
                method="GET",
                status_code=exc.response.status_code,
            )
            return _error("请求失败", f"高德接口返回 HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            logger.warning(
                "amap.http_error",
                method="GET",
                exception_type=type(exc).__name__,
            )
            return _error("请求失败", "高德接口连接失败")
        except ValueError as exc:
            logger.warning(
                "amap.invalid_json",
                method="GET",
                exception_type=type(exc).__name__,
            )
            return _error("请求失败", "高德接口返回非 JSON")

    async def _post_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        json_payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = await safe_http_request(
                self._client,
                "POST",
                str(httpx.URL(url, params=params)),
                json=json_payload,
                headers={"Content-Type": "application/json"},
                policy=_amap_policy(
                    url,
                    timeout=self.timeout,
                    max_response_bytes=_AMAP_MAX_JSON_BYTES,
                    content_types=_AMAP_JSON_CONTENT_TYPES,
                    max_redirects=0,
                ),
            )
            response.raise_for_status()
            return dict(_redact_secret_payload(response.json(), self.api_key))
        except httpx.TimeoutException as exc:
            logger.warning(
                "amap.http_timeout",
                method="POST",
                timeout_seconds=self.timeout,
                exception_type=type(exc).__name__,
                point_count=sum(
                    len(line.get("pointInfoList") or [])
                    for line in json_payload.get("lineList", [])
                    if isinstance(line, dict)
                ),
            )
            return _error("请求超时", f"高德接口请求超时（{self.timeout:.0f}s）")
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "amap.http_status_error",
                method="POST",
                status_code=exc.response.status_code,
            )
            return _error("请求失败", f"高德接口返回 HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            logger.warning(
                "amap.http_error",
                method="POST",
                exception_type=type(exc).__name__,
            )
            return _error("请求失败", "高德接口连接失败")
        except ValueError as exc:
            logger.warning(
                "amap.invalid_json",
                method="POST",
                exception_type=type(exc).__name__,
            )
            return _error("请求失败", "高德接口返回非 JSON")

    async def _get_bytes(self, url: str, *, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await safe_http_request(
                self._client,
                "GET",
                str(httpx.URL(url, params=params)),
                headers={"Accept": "image/*, application/json"},
                policy=_amap_policy(
                    url,
                    timeout=self.timeout,
                    max_response_bytes=_AMAP_MAX_IMAGE_BYTES,
                    content_types=("image/", *_AMAP_JSON_CONTENT_TYPES),
                    max_redirects=2,
                ),
            )
            response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "")
            if "image" not in content_type:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {
                        "message": _redact_secret(response.text[:500], self.api_key)
                    }
                payload = _redact_secret_payload(payload, self.api_key)
                return _error("静态地图生成失败", str(payload.get("info") or payload.get("message") or "未知错误"))
            return {"content": response.content}
        except httpx.TimeoutException as exc:
            logger.warning(
                "amap.http_timeout",
                method="GET",
                timeout_seconds=self.timeout,
                exception_type=type(exc).__name__,
            )
            return _error("请求超时", f"高德接口请求超时（{self.timeout:.0f}s）")
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "amap.http_status_error",
                method="GET",
                status_code=exc.response.status_code,
            )
            return _error("请求失败", f"高德接口返回 HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            logger.warning(
                "amap.http_error",
                method="GET",
                exception_type=type(exc).__name__,
            )
            return _error("请求失败", "高德接口连接失败")

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            )
        return self._http_client

    def _missing_key_error(self) -> dict[str, Any] | None:
        if self.api_key:
            return None
        return _error(
            "API Key 缺失",
            "未配置 AMAP_API_KEY，请通过环境变量或挂载 Secret Provider 注入高德 Web 服务 API Key。",
        )

    @staticmethod
    def _normalize_poi(item: dict[str, Any]) -> dict[str, Any]:
        lon, lat = _split_location(str(item.get("location") or ""))
        return {
            "poi_id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "longitude": lon,
            "latitude": lat,
            "address": str(item.get("address") or ""),
            "tel": str(item.get("tel") or ""),
            "distance": str(item.get("distance") or ""),
        }

    @staticmethod
    def _normalize_poi_detail(item: dict[str, Any]) -> dict[str, Any]:
        normalized = AMapClient._normalize_poi(item)
        normalized.update(
            {
                "type": str(item.get("type") or ""),
                "typecode": str(item.get("typecode") or ""),
                "cityname": str(item.get("cityname") or ""),
                "adname": str(item.get("adname") or ""),
                "business_area": str(item.get("business_area") or ""),
                "photos": item.get("photos") or [],
            }
        )
        return normalized

    @staticmethod
    def _normalize_tip(item: dict[str, Any]) -> dict[str, Any]:
        lon, lat = _split_location(str(item.get("location") or ""))
        return {
            "name": str(item.get("name") or ""),
            "district": str(item.get("district") or ""),
            "adcode": str(item.get("adcode") or ""),
            "poi_id": str(item.get("id") or ""),
            "address": str(item.get("address") or ""),
            "longitude": lon,
            "latitude": lat,
            "typecode": str(item.get("typecode") or ""),
        }

    @staticmethod
    def _normalize_aoi(item: dict[str, Any]) -> dict[str, Any]:
        lon, lat = _split_location(str(item.get("location") or ""))
        return {
            "name": str(item.get("name") or ""),
            "aoi_id": str(item.get("id") or ""),
            "adcode": str(item.get("adcode") or ""),
            "longitude": lon,
            "latitude": lat,
            "area": str(item.get("area") or ""),
            "distance": str(item.get("distance") or ""),
            "type": str(item.get("type") or ""),
        }

    @staticmethod
    def _normalize_distance(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "origin_id": str(item.get("origin_id") or ""),
            "destination_id": str(item.get("dest_id") or ""),
            "distance_meters": _as_float(item.get("distance")),
            "duration_seconds": _as_float(item.get("duration")),
        }

    @staticmethod
    def _normalize_extensions(value: str) -> str:
        normalized = str(value or "all").strip().lower()
        return normalized if normalized in {"base", "all"} else "all"

    @staticmethod
    def _normalize_static_map_size(value: str) -> str:
        match = re.match(r"^\s*(\d{1,4})\s*[x*]\s*(\d{1,4})\s*$", str(value or ""))
        if not match:
            return "750*500"
        width = max(1, min(int(match.group(1)), 1024))
        height = max(1, min(int(match.group(2)), 1024))
        return f"{width}*{height}"

    @staticmethod
    def _write_static_map_image(content: bytes, *, storage_dir: str, map_name: str) -> str:
        if not content:
            raise ValueError("静态地图图片为空")
        directory = Path(storage_dir)
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9一-鿿._-]+", "-", map_name).strip("-")
        safe_name = safe_name or "static-map"
        path = directory / f"amap-static-{safe_name}-{uuid.uuid4().hex}.png"
        path.write_bytes(content)
        return str(path)

    @staticmethod
    def _public_file_url(path: str, *, public_base_url: str) -> str:
        base_url = str(public_base_url or "").strip().rstrip("/")
        if not base_url:
            return ""
        file_name = Path(path).name
        if not file_name:
            return ""
        return f"{base_url}/plugins/amap/files/{quote(file_name)}"

    @staticmethod
    def _summarize_route_result(
        result: dict[str, Any],
        *,
        mode: str,
        origin: str,
        destination: str,
    ) -> dict[str, Any]:
        route = result.get("route") or {}
        if mode == "transit":
            transits = route.get("transits") or []
            first = transits[0] if transits else {}
            return {
                "mode": mode,
                "origin": origin,
                "destination": destination,
                "distance_meters": _as_float(first.get("distance")),
                "duration_seconds": _as_float(first.get("duration")),
                "cost": str(first.get("cost") or ""),
                "segments": first.get("segments") or [],
            }
        paths = route.get("paths") or []
        first = paths[0] if paths else {}
        return {
            "mode": mode,
            "origin": origin,
            "destination": destination,
            "distance_meters": _as_float(first.get("distance")),
            "duration_seconds": _as_float(first.get("duration")),
            "steps": first.get("steps") or [],
        }
