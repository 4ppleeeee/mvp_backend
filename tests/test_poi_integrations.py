from __future__ import annotations

from typing import Any

from app.config import Settings
from app.poi_integrations import AttractionClient, TencentLocationClient


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self) -> dict[str, Any]:
        return self._payload


def test_tencent_location_suggestion_normalizes_poi_id(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(
            {
                "status": 0,
                "data": [
                    {
                        "id": 923456,
                        "title": "故宫博物院",
                        "address": "北京市东城区景山前街4号",
                        "ad_info": {"province": "北京市", "city": "北京市", "district": "东城区"},
                        "location": {"lat": 39.9163, "lng": 116.3972},
                    }
                ],
            }
        )

    monkeypatch.setattr("app.poi_integrations.requests.get", fake_get)
    result = TencentLocationClient(Settings(tencent_location_api_key="secret")).suggest("故宫", "北京")

    assert result == [
        {
            "poiId": "923456",
            "poiKey": "tencent_map:923456",
            "name": "故宫博物院",
            "address": "北京市东城区景山前街4号",
            "province": "北京市",
            "city": "北京市",
            "district": "东城区",
            "category": "",
            "latitude": 39.9163,
            "longitude": 116.3972,
        }
    ]
    assert calls[0]["params"] == {"key": "secret", "keyword": "故宫", "region": "北京"}


def test_attraction_client_unwraps_batch_and_preserves_cursor(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse(
            {
                "code": 0,
                "msg": "success",
                "data": {
                    "items": [{"attractionId": "ATTR-1", "poiId": "923456", "name": "故宫博物院"}],
                    "next_cursor": "next-1",
                    "prev_cursor": "",
                    "total_count": 1,
                },
            }
        )

    monkeypatch.setattr("app.poi_integrations.requests.request", fake_request)
    result = AttractionClient(Settings()).batch_get(cursor="", direction=0, page_size=10)

    assert result["items"][0]["poiId"] == "923456"
    assert result["next_cursor"] == "next-1"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/attraction/batchGet")
    assert calls[0]["json"] == {"cursor": "", "direction": 0, "pageSize": 10}


def test_attraction_client_create_sends_poi_id_and_attr_info(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse({"code": 0, "msg": "success", "data": {"attractionId": "ATTR-1"}})

    monkeypatch.setattr("app.poi_integrations.requests.request", fake_request)
    result = AttractionClient(Settings()).create(
        poi_id="923456",
        attr_info={"name": "故宫博物院", "cityName": "北京", "tags": ["博物馆"]},
    )

    assert result["attractionId"] == "ATTR-1"
    assert calls[0]["json"] == {
        "poiId": 923456,
        "attrInfo": {"name": "故宫博物院", "cityName": "北京", "tags": ["博物馆"]},
    }
