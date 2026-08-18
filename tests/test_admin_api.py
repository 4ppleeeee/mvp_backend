from pathlib import Path

import requests
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import Settings
from app.main import create_app
from app.models import IngestionJob, SourceEvidence, TravelSource


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def submit(self, function: object, *args: object) -> None:
        self.calls.append((function, args))


def make_client(tmp_path: Path, *, max_upload_bytes: int = 512 * 1024 * 1024) -> tuple[TestClient, RecordingExecutor]:
    app = create_app(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'admin-api.db'}",
            uploads_dir=str(tmp_path / "uploads"),
            ingestion_temp_dir=str(tmp_path / "ingestion"),
            ingestion_max_upload_bytes=max_upload_bytes,
            admin_api_enabled=True,
            admin_allowed_origins="https://admin-test.example",
        )
    )
    executor = RecordingExecutor()
    app.state.ingestion_executor = executor
    return TestClient(app), executor


def make_poi_client(tmp_path: Path) -> TestClient:
    app = create_app(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'poi-admin-api.db'}",
            uploads_dir=str(tmp_path / "uploads"),
            ingestion_temp_dir=str(tmp_path / "ingestion"),
            admin_api_enabled=True,
            crawlab_results_api_url="https://crawlab.internal",
            crawlab_api_token="crawlab-test-token",
            tencent_location_api_key="tencent-test-key",
            tencent_location_base_url="https://location.example",
            attraction_api_base_url="https://attraction.internal",
        )
    )
    return TestClient(app)


class UpstreamJsonResponse:
    def __init__(self, body: object) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.body


class UpstreamHttpErrorResponse(UpstreamJsonResponse):
    def __init__(self, status_code: int, body: object) -> None:
        super().__init__(body)
        self.status_code = status_code

    def raise_for_status(self) -> None:
        import requests

        raise requests.HTTPError(response=self)


class InvalidJsonResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        raise ValueError("invalid upstream JSON")


def test_attraction_routes_proxy_list_detail_create_and_update(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    calls: list[tuple[str, str, object]] = []

    def attraction_request(method: str, url: str, **kwargs: object) -> UpstreamJsonResponse:
        calls.append((method, url, kwargs.get("json")))
        if url.endswith("/attraction/batchGet"):
            return UpstreamJsonResponse({"code": 0, "data": {"items": [{"attractionId": "attr-123", "name": "故宫博物院"}]}})
        return UpstreamJsonResponse({"code": 0, "data": {"attractionId": "attr-123", "name": "故宫博物院"}})

    monkeypatch.setattr("requests.request", attraction_request)

    listed = client.get("/admin-api/poi/attractions?cursor=next&pageSize=5")
    detail = client.get("/admin-api/poi/attractions/attr-123")
    created = client.post("/admin-api/poi/attractions", json={"poiId": "poi-123", "attrInfo": {"name": "故宫博物院"}})
    updated = client.post("/admin-api/poi/attractions/attr-123", json={"attrInfo": {"name": "故宫博物院"}, "baseInfo": {"status": 1}})

    assert [response.status_code for response in (listed, detail, created, updated)] == [200, 200, 200, 200]
    assert listed.json()["data"]["items"] == [{"attractionId": "attr-123", "poiId": "", "name": "故宫博物院", "status": None}]
    assert calls == [
        ("POST", "https://attraction.internal/attraction/batchGet", {"cursor": "next", "direction": 0, "pageSize": 5}),
        ("POST", "https://attraction.internal/attraction/get", {"attractionId": "attr-123"}),
        ("POST", "https://attraction.internal/attraction/create", {"poiId": "poi-123", "attrInfo": {"name": "故宫博物院"}}),
        ("POST", "https://attraction.internal/attraction/update", {"attractionId": "attr-123", "attrInfo": {"name": "故宫博物院"}, "baseInfo": {"status": 1}}),
    ]


def test_attraction_routes_normalize_snake_case_upstream_payload(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)

    def attraction_request(method: str, url: str, **kwargs: object) -> UpstreamJsonResponse:
        if url.endswith("/attraction/batchGet"):
            return UpstreamJsonResponse({"code": 0, "data": {"items": [
                {"attraction_id": "ATTR-1", "poi_id": "poi-1", "name": "故宫", "status": 0}
            ], "total_count": 1}})
        return UpstreamJsonResponse({"code": 0, "data": {
            "attraction_id": "ATTR-1", "poi_id": "poi-1",
            "attr_info": {"name": "故宫", "cityName": "北京"}, "status": 0,
        }})

    monkeypatch.setattr("requests.request", attraction_request)

    assert client.get("/admin-api/poi/attractions").json()["data"]["items"][0] == {
        "attractionId": "ATTR-1", "poiId": "poi-1", "name": "故宫", "status": 0,
    }
    detail = client.get("/admin-api/poi/attractions/ATTR-1").json()["data"]
    assert detail["attrInfo"] == {"name": "故宫", "cityName": "北京"}
    assert detail["baseInfo"] == {"status": 0}
    assert detail["raw"]["attr_info"]["name"] == "故宫"


def add_review_job(client: TestClient) -> str:
    with Session(client.app.state.engine) as session:
        job = IngestionJob(
            input_type="url",
            original_url="https://youtu.be/abcdefghijk",
            source_platform="youtube",
            media_type="video",
            status="succeeded",
            stage="succeeded",
            ingest_decision="review",
            analysis_json={
                "title": "博山菜探店",
                "body_text": "淄博博山菜探店体验。",
                "destination": "淄博",
                "category": "eat",
            },
            evidence_text="我们现在在博山吃博山菜。",
            evidence_origin="asr",
            evidence_language="zh",
            evidence_segments=[{"start_seconds": 0, "end_seconds": 2, "text": "我们现在在博山吃博山菜。"}],
            evidence_metadata_json={"title": "元数据标题", "thumbnail_url": "https://images.example/cover.png"},
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job.job_id


def test_admin_api_lists_title_first_tasks_without_a_legacy_session(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    job_id = add_review_job(client)

    response = client.get("/admin-api/tasks")

    assert response.status_code == 200
    task = response.json()["tasks"][0]
    assert task["job_id"] == job_id
    assert task["display"]["title"] == "博山菜探店"
    assert task["display"]["status"] == "已完成"


def test_enabled_admin_api_replaces_the_legacy_admin_html_route(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)

    legacy_page = client.get("/admin", follow_redirects=False)
    tasks = client.get("/admin-api/tasks")

    assert legacy_page.status_code == 404
    assert tasks.status_code == 200
    assert tasks.json() == {"tasks": []}


def test_admin_api_is_disabled_until_explicitly_enabled_for_the_test_or_gateway_environment(tmp_path: Path) -> None:
    app = create_app(settings=Settings(database_url=f"sqlite:///{tmp_path / 'disabled.db'}"))

    response = TestClient(app).get("/admin-api/tasks")

    assert response.status_code == 404


def test_admin_api_cors_allows_browser_credentials_from_the_configured_console_origin(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)

    response = client.options(
        "/admin-api/tasks",
        headers={
            "Origin": "https://admin-test.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://admin-test.example"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_admin_api_submits_url_and_image_jobs(tmp_path: Path) -> None:
    client, executor = make_client(tmp_path)

    url_response = client.post("/admin-api/tasks/url", json={"url": "https://youtu.be/abcdefghijk"})
    image_response = client.post(
        "/admin-api/tasks/image",
        files={"file": ("note.png", b"image-bytes", "image/png")},
    )

    assert url_response.status_code == 202
    assert url_response.json()["job_id"].startswith("ing_")
    assert url_response.json()["status"] == "queued"
    assert image_response.status_code == 202
    assert image_response.json()["job_id"].startswith("ing_")
    assert image_response.json()["status"] == "queued"
    assert len(executor.calls) == 2


def test_admin_api_discards_the_job_when_an_image_upload_fails(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, max_upload_bytes=4)

    response = client.post("/admin-api/tasks/image", files={"file": ("big.png", b"12345", "image/png")})
    with Session(client.app.state.engine) as session:
        jobs = session.exec(select(IngestionJob)).all()

    assert response.status_code == 413
    assert jobs == []


def test_admin_api_returns_task_detail_and_accepts_review(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    job_id = add_review_job(client)

    detail = client.get(f"/admin-api/tasks/{job_id}")
    review = client.post(f"/admin-api/tasks/{job_id}/review", json={"decision": "accept", "reason": "内容可信"})

    assert detail.status_code == 200
    assert detail.json()["task"]["job_id"] == job_id
    assert detail.json()["evidence"]["full_text"] == "我们现在在博山吃博山菜。"
    assert review.status_code == 200
    assert review.json()["task"]["ingest_decision"] == "accept"
    assert review.json()["task"]["source_id"].startswith("src_")


def test_admin_api_lists_and_reads_sources_with_evidence(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    job_id = add_review_job(client)
    client.post(f"/admin-api/tasks/{job_id}/review", json={"decision": "accept"})
    with Session(client.app.state.engine) as session:
        job = session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).one()
        source_id = job.source_id

    listed = client.get("/admin-api/sources")
    detail = client.get(f"/admin-api/sources/{source_id}")

    assert listed.status_code == 200
    assert listed.json()["sources"][0]["source_id"] == source_id
    assert detail.status_code == 200
    assert detail.json()["source"]["title"] == "博山菜探店"
    assert detail.json()["evidence"]["origin"] == "asr"


def test_admin_api_proxies_only_a_validated_source_cover(tmp_path: Path, monkeypatch) -> None:
    client, _ = make_client(tmp_path)
    with Session(client.app.state.engine) as session:
        source = TravelSource(
            title="安全封面",
            body_text="正文",
            original_url="https://example.com/article",
            source_platform="web",
            cover_image_url="https://images.example/cover.png",
            destination="北京",
            category="sight",
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        source_id = source.source_id
    monkeypatch.setattr(
        "app.admin_api._read_pinned_cover_response",
        lambda url: (200, {"content-type": "image/png"}, b"safe-image"),
        raising=False,
    )

    response = client.get(f"/admin-api/sources/{source_id}/cover")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"safe-image"


def test_admin_api_uses_a_pinned_transport_for_source_covers(tmp_path: Path, monkeypatch) -> None:
    client, _ = make_client(tmp_path)
    with Session(client.app.state.engine) as session:
        source = TravelSource(
            title="固定地址封面",
            body_text="正文",
            original_url="https://example.com/article",
            source_platform="web",
            cover_image_url="https://images.example/cover.png",
            destination="北京",
            category="sight",
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        source_id = source.source_id
    monkeypatch.setattr(
        "app.admin_api._read_pinned_cover_response",
        lambda url: (200, {"content-type": "image/png"}, b"pinned-image"),
        raising=False,
    )
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unverified request")))

    response = client.get(f"/admin-api/sources/{source_id}/cover")

    assert response.status_code == 200
    assert response.content == b"pinned-image"


def test_admin_api_never_connects_to_a_private_cover_address(tmp_path: Path, monkeypatch) -> None:
    client, _ = make_client(tmp_path)
    with Session(client.app.state.engine) as session:
        source = TravelSource(
            title="私网封面",
            body_text="正文",
            original_url="https://example.com/article",
            source_platform="web",
            cover_image_url="https://images.example/cover.png",
            destination="北京",
            category="sight",
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        source_id = source.source_id
    monkeypatch.setattr(
        "app.admin_api.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    monkeypatch.setattr(
        "app.admin_api.socket.create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("private connection attempted")),
    )

    response = client.get(f"/admin-api/sources/{source_id}/cover")

    assert response.status_code == 502


def test_admin_api_uses_the_configured_cors_origin_allowlist(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)

    response = client.options(
        "/admin-api/tasks",
        headers={"Origin": "https://admin-test.example", "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://admin-test.example"


def test_poi_location_suggestions_are_normalized_without_exposing_the_tencent_key(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    calls: list[dict[str, object]] = []

    def suggestion_get(url: str, **kwargs: object) -> UpstreamJsonResponse:
        calls.append({"url": url, **kwargs})
        return UpstreamJsonResponse(
            {
                "status": 0,
                "data": [
                    {
                        "id": "poi-123",
                        "title": "故宫博物院",
                        "address": "景山前街4号",
                        "category": "旅游景点",
                        "ad_info": {"province": "北京市", "city": "北京市", "district": "东城区"},
                        "location": {"lat": 39.916, "lng": 116.397},
                    }
                ],
            }
        )

    monkeypatch.setattr("requests.get", suggestion_get)

    response = client.post("/admin-api/poi/location/suggest", json={"keyword": "故宫", "region": "北京"})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "keyword": "故宫",
            "candidates": [
                {
                    "poiId": "poi-123",
                    "poiKey": "tencent_map:poi-123",
                    "name": "故宫博物院",
                    "address": "景山前街4号",
                    "province": "北京市",
                    "city": "北京市",
                    "district": "东城区",
                    "category": "旅游景点",
                    "latitude": 39.916,
                    "longitude": 116.397,
                }
            ],
        },
    }
    assert calls == [
        {
            "url": "https://location.example/ws/place/v1/suggestion",
            "params": {"key": "tencent-test-key", "keyword": "故宫", "region": "北京"},
            "timeout": (3, 10),
        }
    ]
    assert "tencent-test-key" not in response.text


def test_poi_crawlab_routes_forward_the_contract_with_server_side_bearer_auth(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    calls: list[dict[str, object]] = []

    def crawlab_request(method: str, url: str, **kwargs: object) -> UpstreamJsonResponse:
        calls.append({"method": method, "url": url, **kwargs})
        if url.endswith("/poi-crawls/crawl-123"):
            return UpstreamJsonResponse({"crawlTaskId": "crawl-123", "sources": [{"nativeTaskId": "native-123"}]})
        return UpstreamJsonResponse({"upstream": url.rsplit("/api/v1", 1)[1]})

    monkeypatch.setattr("requests.request", crawlab_request)

    submitted = client.post("/admin-api/poi/crawls", json={"poi": {"poiId": "poi-123"}, "sourceUrls": ["https://example.com"]})
    listed = client.get("/admin-api/poi/crawls?poiKey=tencent_map:poi-123&status=running&offset=2&limit=4")
    status_response = client.get("/admin-api/poi/crawls/crawl-123/status")
    pages = client.get("/admin-api/poi/tasks/native-123/pages?crawlTaskId=crawl-123&offset=3&limit=5")
    searched = client.post("/admin-api/poi/tasks/native-123/search?crawlTaskId=crawl-123", json={"query": "宫殿", "limit": 6})

    assert submitted.status_code == 202
    for response in (listed, status_response, pages, searched):
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert "crawlab-test-token" not in response.text
    assert calls == [
        {
            "method": "POST",
            "url": "https://crawlab.internal/api/v1/poi-crawls",
            "json": {"poi": {"poiId": "poi-123"}, "sourceUrls": ["https://example.com"]},
            "headers": {"Authorization": "Bearer crawlab-test-token"},
            "timeout": (3, 60),
        },
        {
            "method": "GET",
            "url": "https://crawlab.internal/api/v1/poi-crawls",
            "params": {"poiKey": "tencent_map:poi-123", "status": "running", "offset": 2, "limit": 4},
            "headers": {"Authorization": "Bearer crawlab-test-token"},
            "timeout": (3, 60),
        },
        {
            "method": "GET",
            "url": "https://crawlab.internal/api/v1/poi-crawls/crawl-123",
            "headers": {"Authorization": "Bearer crawlab-test-token"},
            "timeout": (3, 60),
        },
        {
            "method": "GET",
            "url": "https://crawlab.internal/api/v1/poi-crawls/crawl-123",
            "headers": {"Authorization": "Bearer crawlab-test-token"},
            "timeout": (3, 60),
        },
        {
            "method": "GET",
            "url": "https://crawlab.internal/api/v1/tasks/native-123/pages",
            "params": {"offset": 3, "limit": 5},
            "headers": {"Authorization": "Bearer crawlab-test-token"},
            "timeout": (3, 60),
        },
        {
            "method": "GET",
            "url": "https://crawlab.internal/api/v1/poi-crawls/crawl-123",
            "headers": {"Authorization": "Bearer crawlab-test-token"},
            "timeout": (3, 60),
        },
        {
            "method": "POST",
            "url": "https://crawlab.internal/api/v1/tasks/native-123/search",
            "json": {"query": "宫殿", "limit": 6},
            "headers": {"Authorization": "Bearer crawlab-test-token"},
            "timeout": (3, 60),
        },
    ]


def test_poi_routes_reject_invalid_requests_and_report_unconfigured_services_safely(tmp_path: Path) -> None:
    app = create_app(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'poi-unconfigured.db'}",
            admin_api_enabled=True,
            crawlab_api_token="private-crawlab-token",
            tencent_location_api_key="private-tencent-key",
        )
    )
    client = TestClient(app)

    empty_keyword = client.post("/admin-api/poi/location/suggest", json={"keyword": ""})
    unconfigured_crawlab = client.get("/admin-api/poi/crawls")

    assert empty_keyword.status_code == 400
    assert unconfigured_crawlab.status_code == 503
    assert "private-crawlab-token" not in unconfigured_crawlab.text
    assert "private-tencent-key" not in empty_keyword.text


def test_poi_crawl_submission_preserves_the_upstream_accepted_status(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    monkeypatch.setattr("requests.request", lambda *args, **kwargs: UpstreamJsonResponse({"crawlTaskId": "crawl-123"}))

    response = client.post("/admin-api/poi/crawls", json={"poi": {"poiId": "poi-123"}, "sourceUrls": ["https://example.com"]})

    assert response.status_code == 202
    assert response.json() == {"ok": True, "data": {"crawlTaskId": "crawl-123"}}


def test_poi_crawl_status_is_available_at_the_canonical_aggregate_route(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    calls: list[str] = []

    def crawlab_request(method: str, url: str, **kwargs: object) -> UpstreamJsonResponse:
        calls.append(f"{method} {url}")
        return UpstreamJsonResponse({"crawlTaskId": "crawl-123", "sources": []})

    monkeypatch.setattr("requests.request", crawlab_request)

    response = client.get("/admin-api/poi/crawls/crawl-123")

    assert response.status_code == 200
    assert response.json()["data"]["crawlTaskId"] == "crawl-123"
    assert calls == ["GET https://crawlab.internal/api/v1/poi-crawls/crawl-123"]


def test_poi_native_reads_verify_the_aggregate_source_before_forwarding(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    calls: list[str] = []

    def crawlab_request(method: str, url: str, **kwargs: object) -> UpstreamJsonResponse:
        calls.append(f"{method} {url}")
        if url.endswith("/poi-crawls/crawl-123"):
            return UpstreamJsonResponse({"crawlTaskId": "crawl-123", "sources": [{"nativeTaskId": "native-123"}]})
        return UpstreamJsonResponse({"task_id": "native-123"})

    monkeypatch.setattr("requests.request", crawlab_request)

    pages = client.get("/admin-api/poi/tasks/native-123/pages?crawlTaskId=crawl-123&offset=0&limit=2")
    searched = client.post("/admin-api/poi/tasks/native-123/search?crawlTaskId=crawl-123", json={"query": "宫殿", "limit": 2})

    assert pages.status_code == 200
    assert searched.status_code == 200
    assert calls == [
        "GET https://crawlab.internal/api/v1/poi-crawls/crawl-123",
        "GET https://crawlab.internal/api/v1/tasks/native-123/pages",
        "GET https://crawlab.internal/api/v1/poi-crawls/crawl-123",
        "POST https://crawlab.internal/api/v1/tasks/native-123/search",
    ]


def test_poi_native_reads_reject_a_task_not_confirmed_by_its_aggregate(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    calls: list[str] = []

    def crawlab_request(method: str, url: str, **kwargs: object) -> UpstreamJsonResponse:
        calls.append(f"{method} {url}")
        return UpstreamJsonResponse({"crawlTaskId": "crawl-123", "sources": [{"nativeTaskId": "other-native"}]})

    monkeypatch.setattr("requests.request", crawlab_request)

    response = client.get("/admin-api/poi/tasks/native-123/pages?crawlTaskId=crawl-123")

    assert response.status_code == 404
    assert response.json()["detail"] == "POI crawl source was not found"
    assert calls == ["GET https://crawlab.internal/api/v1/poi-crawls/crawl-123"]


def test_poi_crawlab_client_errors_preserve_structured_feedback_without_secrets(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    monkeypatch.setattr(
        "requests.request",
        lambda *args, **kwargs: UpstreamHttpErrorResponse(
            400,
            {"code": "INVALID_SOURCE_URL", "message": "source URL must be public HTTP(S)"},
        ),
    )

    response = client.post("/admin-api/poi/crawls", json={"poi": {"poiId": "poi-123"}, "sourceUrls": ["bad"]})

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "INVALID_SOURCE_URL", "message": "source URL must be public HTTP(S)"}}
    assert "crawlab-test-token" not in response.text


def test_poi_search_rejects_a_boolean_limit_without_contacting_crawlab(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    monkeypatch.setattr(
        "requests.request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Crawlab should not be called")),
    )

    response = client.post("/admin-api/poi/tasks/native-123/search?crawlTaskId=crawl-123", json={"query": "宫殿", "limit": True})

    assert response.status_code == 422


def test_poi_search_rejects_a_query_longer_than_500_characters_without_contacting_crawlab(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    monkeypatch.setattr(
        "requests.request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Crawlab should not be called")),
    )

    response = client.post(
        "/admin-api/poi/tasks/native-123/search?crawlTaskId=crawl-123",
        json={"query": "x" * 501, "limit": 1},
    )

    assert response.status_code == 422


def test_poi_crawlab_failures_map_to_safe_gateway_errors_without_credentials(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    failures: list[tuple[object, int]] = [
        (requests.Timeout("upstream timeout"), 504),
        (requests.ConnectionError("upstream connection failed"), 502),
        (InvalidJsonResponse(), 502),
        (UpstreamHttpErrorResponse(500, {"message": "upstream failure"}), 502),
    ]

    for upstream, expected_status in failures:
        def crawlab_request(*args: object, _upstream: object = upstream, **kwargs: object) -> object:
            if isinstance(_upstream, BaseException):
                raise _upstream
            return _upstream

        monkeypatch.setattr("requests.request", crawlab_request)
        response = client.post("/admin-api/poi/crawls", json={"poi": {"poiId": "poi-123"}, "sourceUrls": ["https://example.com"]})

        assert response.status_code == expected_status
        assert "crawlab-test-token" not in response.text
        assert "tencent-test-key" not in response.text
