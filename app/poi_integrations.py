from __future__ import annotations

from typing import Any

import requests

from app.config import Settings


class PoiIntegrationError(RuntimeError):
    pass


def _json_response(response: requests.Response, *, label: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        detail = response.text.strip()
        suffix = f"：{detail[:300]}" if detail else ""
        raise PoiIntegrationError(f"{label} 请求失败（HTTP {response.status_code}）{suffix}") from exc
    except (requests.RequestException, ValueError) as exc:
        raise PoiIntegrationError(f"{label} 请求失败") from exc
    if not isinstance(payload, dict):
        raise PoiIntegrationError(f"{label} 返回格式无效")
    return payload


class TencentLocationClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def suggest(self, keyword: str, region: str = "") -> list[dict[str, Any]]:
        if not self._settings.tencent_location_api_key:
            raise PoiIntegrationError("腾讯位置服务未配置")
        try:
            response = requests.get(
                f"{self._settings.tencent_location_base_url.rstrip('/')}/ws/place/v1/suggestion",
                params={
                    "key": self._settings.tencent_location_api_key,
                    "keyword": keyword,
                    "region": region or None,
                },
                timeout=(3, 10),
            )
        except requests.RequestException as exc:
            raise PoiIntegrationError("腾讯位置服务请求失败") from exc
        payload = _json_response(response, label="腾讯位置服务")
        if payload.get("status") != 0:
            raise PoiIntegrationError(str(payload.get("message") or "腾讯位置服务请求失败"))
        return [self._normalize(item) for item in (payload.get("data") or [])[:5] if isinstance(item, dict)]

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        ad = item.get("ad_info") if isinstance(item.get("ad_info"), dict) else {}
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        poi_id = str(item.get("id") or "")
        return {
            "poiId": poi_id,
            "poiKey": f"tencent_map:{poi_id}",
            "name": item.get("title") or "",
            "address": item.get("address") or "",
            "province": ad.get("province") or "",
            "city": ad.get("city") or "",
            "district": ad.get("district") or "",
            "category": item.get("category") or "",
            "latitude": location.get("lat"),
            "longitude": location.get("lng"),
        }


class AttractionClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.attraction_api_base_url.rstrip("/")

    def batch_get(self, *, cursor: str = "", direction: int = 0, page_size: int = 10) -> dict[str, Any]:
        return self._post("/attraction/batchGet", {"cursor": cursor, "direction": direction, "pageSize": page_size})

    def get(self, attraction_id: str) -> dict[str, Any]:
        return self._post("/attraction/get", {"attractionId": attraction_id})

    def create(self, *, poi_id: str, attr_info: dict[str, Any]) -> dict[str, Any]:
        # Tencent POI ids can exceed JavaScript's safe integer range. Keep the
        # identifier as a string across the JSON boundary to avoid precision loss.
        return self._post("/attraction/create", {"poiId": str(poi_id), "attrInfo": attr_info})

    def update(self, *, attraction_id: str, attr_info: dict[str, Any], status: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"attractionId": attraction_id, "attrInfo": attr_info}
        if status is not None:
            payload["baseInfo"] = {"status": status}
        return self._post("/attraction/update", payload)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.request(
                "POST",
                f"{self._base_url}{path}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=(3, 30),
            )
        except requests.RequestException as exc:
            raise PoiIntegrationError("景点后台请求失败") from exc
        body = _json_response(response, label="景点后台")
        if body.get("code") not in (None, 0):
            code = body.get("code")
            message = body.get("msg") or "景点后台返回失败"
            raise PoiIntegrationError(f"景点后台返回失败（code {code}）：{message}")
        data = body.get("data")
        return data if isinstance(data, dict) else body


class CrawlabClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.crawlab_results_api_url.rstrip("/")

    def call(self, method: str, path: str, *, payload: object | None = None, params: dict[str, object] | None = None) -> Any:
        if not self._settings.crawlab_api_token:
            raise PoiIntegrationError("Crawlab API 未配置")
        try:
            response = requests.request(
                method,
                f"{self._base_url}/api/v1{path}",
                json=payload,
                params=params,
                headers={"Authorization": f"Bearer {self._settings.crawlab_api_token}"},
                timeout=(3, 60),
            )
        except requests.RequestException as exc:
            raise PoiIntegrationError("Crawlab API 请求失败") from exc
        return _json_response(response, label="Crawlab API")
