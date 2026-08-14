from pathlib import Path

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


def test_admin_api_is_disabled_until_explicitly_enabled_for_the_test_or_gateway_environment(tmp_path: Path) -> None:
    app = create_app(settings=Settings(database_url=f"sqlite:///{tmp_path / 'disabled.db'}"))

    response = TestClient(app).get("/admin-api/tasks")

    assert response.status_code == 404


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
