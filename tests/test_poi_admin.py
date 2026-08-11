from pathlib import Path

from sqlmodel import Session, select

from app.models import PoiCrawlRecord
from tests.test_admin import configured_client


def test_poi_console_exposes_collection_and_remote_review_flows(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.get("/admin/poi")

    assert response.status_code == 200
    assert "抓取入库" in response.text
    assert "内容校审" in response.text
    assert "/location/suggest" in response.text
    assert "/attractions" in response.text


def test_poi_crawl_submission_persists_poi_to_task_link(tmp_path: Path, monkeypatch) -> None:
    client = configured_client(tmp_path)
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    def fake_call(self, method, path, **kwargs):
        assert method == "POST"
        assert path == "/poi-crawls"
        return {"crawlTaskId": "crawl-1", "status": "queued"}

    monkeypatch.setattr("app.poi_routes.CrawlabClient.call", fake_call)
    response = client.post(
        "/admin/poi/api/crawls",
        json={
            "poiProvider": "tencent_map",
            "poiId": "923456",
            "poiKey": "tencent_map:923456",
            "poi": {"name": "故宫博物院", "city": "北京"},
            "sourceUrls": ["https://example.com/guide"],
        },
    )

    assert response.status_code == 200
    with Session(client.app.state.engine) as session:
        record = session.exec(select(PoiCrawlRecord)).one()
        assert record.crawl_task_id == "crawl-1"
        assert record.poi_id == "923456"
        assert record.sync_status == "draft"


def test_poi_create_maps_draft_to_remote_attr_info(tmp_path: Path, monkeypatch) -> None:
    client = configured_client(tmp_path)
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})
    with Session(client.app.state.engine) as session:
        session.add(
            PoiCrawlRecord(
                crawl_task_id="crawl-1",
                poi_id="923456",
                poi_key="tencent_map:923456",
                poi_name="故宫博物院",
                poi_json={"city": "北京"},
            )
        )
        session.commit()

    calls = []

    def fake_create(self, *, poi_id, attr_info):
        calls.append({"poi_id": poi_id, "attr_info": attr_info})
        return {"attractionId": "ATTR-1"}

    monkeypatch.setattr("app.poi_routes.AttractionClient.create", fake_create)
    response = client.post(
        "/admin/poi/api/crawls/crawl-1/create",
        json={"draft": {"name": "故宫博物院", "city_name": "北京", "description": "故宫介绍", "tags": ["博物馆"]}},
    )

    assert response.status_code == 200
    assert calls == [{"poi_id": "923456", "attr_info": {"name": "故宫博物院", "cityName": "北京", "description": "故宫介绍", "tags": ["博物馆"], "countryName": "中国", "currencyCode": "CNY"}}]
    with Session(client.app.state.engine) as session:
        record = session.exec(select(PoiCrawlRecord)).one()
        assert record.attraction_id == "ATTR-1"
        assert record.sync_status == "created"
