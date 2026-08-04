from pathlib import Path

from fastapi.testclient import TestClient
from pwdlib import PasswordHash
from sqlmodel import Session, select

from app.config import Settings
from app.llm import SourceAnalysis
from app.main import create_app
from app.models import IngestionJob, IngestionReview, SourceEvidence, TravelSource


def configured_client(
    tmp_path: Path,
    *,
    raise_server_exceptions: bool = True,
    rag_index: object | None = None,
    llm_client: object | None = None,
) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'admin.db'}",
        uploads_dir=str(tmp_path / "uploads"),
        admin_username="admin",
        admin_password_hash=PasswordHash.recommended().hash("test-password"),
        admin_session_secret="test-session-secret",
    )
    return TestClient(
        create_app(settings=settings, rag_index=rag_index, llm_client=llm_client),
        raise_server_exceptions=raise_server_exceptions,
    )


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def submit(self, function: object, *args: object) -> None:
        self.calls.append((function, args))


class RecordingRagIndex:
    def __init__(self) -> None:
        self.indexed: list[tuple[str, str, str]] = []

    def upsert_source(self, source: TravelSource, evidence: SourceEvidence) -> None:
        self.indexed.append((source.source_id, evidence.evidence_id, evidence.full_text))


class FailingRagIndex:
    def __init__(self) -> None:
        self.attempted_source_ids: list[str] = []

    def upsert_source(self, source: TravelSource, evidence: SourceEvidence) -> None:
        self.attempted_source_ids.append(source.source_id)
        raise RuntimeError("index unavailable")


class CrawlabLlmClient:
    async def analyze_source(self, **_: object) -> SourceAnalysis:
        return SourceAnalysis(
            is_travel_related=True,
            destination="东京",
            category="play",
            normalized_tags=["拍照好看"],
        )


class FakeCrawlabResponse:
    def __init__(self, payload: dict | None = None, lines: list[str] | None = None) -> None:
        self._payload = payload or {}
        self._lines = lines or []

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload

    def iter_lines(self, *, decode_unicode: bool) -> list[str]:
        return self._lines

    def close(self) -> None:
        return None


def create_saved_source(client: TestClient) -> str:
    with Session(client.app.state.engine) as session:
        source = TravelSource(
            title="北京：个人觉得无法超越的漂亮公园",
            body_text="一份北京公园漫游的私藏清单。",
            original_url="https://xhslink.cn/o/example",
            source_platform="xiaohongshu",
            cover_image_url="https://img.example/cover.jpg",
            destination="北京",
            category="guide",
            location_name="城市公园",
            normalized_tags=["拍照好看", "亲子"],
            raw_tags=["散步"],
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        return source.source_id


def test_admin_redirects_unauthenticated_user_to_login(tmp_path: Path) -> None:
    response = configured_client(tmp_path).get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_admin_lists_crawlab_tasks_and_syncs_pages_to_rag(tmp_path: Path, monkeypatch) -> None:
    rag_index = RecordingRagIndex()
    client = configured_client(tmp_path, rag_index=rag_index, llm_client=CrawlabLlmClient())
    task_id = "a" * 24

    def fake_get(url: str, **_: object) -> FakeCrawlabResponse:
        if url.endswith("/tasks"):
            return FakeCrawlabResponse({"tasks": [{"task_id": task_id, "start_url": "https://example.com/tokyo", "page_count": 1, "failure_count": 0}]})
        return FakeCrawlabResponse(lines=['{"url":"https://example.com/tokyo","title":"东京散步","file":"page.md","markdown":"东京适合散步和拍照。"}'])

    monkeypatch.setattr("app.admin_routes.requests.get", fake_get)
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    listing = client.get("/admin/crawlab")
    synced = client.post(f"/admin/crawlab/{task_id}/sync", follow_redirects=False)

    assert listing.status_code == 200
    assert "Crawlab 抓取结果" in listing.text
    assert f'action="/admin/crawlab/{task_id}/sync"' in listing.text
    assert synced.status_code == 303
    assert len(rag_index.indexed) == 1
    with Session(client.app.state.engine) as session:
        source = session.exec(select(TravelSource)).one()
        evidence = session.exec(select(SourceEvidence).where(SourceEvidence.source_id == source.source_id)).one()
    assert source.source_platform == "crawlab"
    assert evidence.origin == "crawlab"
    assert evidence.metadata_json["crawlab_task_id"] == task_id


def test_admin_login_creates_session_only_for_correct_password(tmp_path: Path) -> None:
    client = configured_client(tmp_path)

    denied = client.post("/admin/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False)
    accepted = client.post("/admin/login", data={"username": "admin", "password": "test-password"}, follow_redirects=False)

    assert denied.status_code == 303
    assert denied.headers["location"] == "/admin/login?error=1"
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/admin"


def test_admin_returns_503_when_authentication_is_not_configured(tmp_path: Path) -> None:
    client = TestClient(create_app(settings=Settings(database_url=f"sqlite:///{tmp_path / 'missing.db'}")))

    assert client.get("/admin/login").status_code == 503


def test_logged_in_admin_dashboard_exposes_url_and_image_submission(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.get("/admin")

    assert response.status_code == 200
    assert 'action="/admin/ingestions/url"' in response.text
    assert 'action="/admin/ingestions/image"' in response.text
    assert 'href="/admin/sources"' in response.text
    assert 'class="task-workspace"' in response.text


def test_admin_submit_css_keeps_url_submit_button_on_one_line() -> None:
    stylesheet = Path("app/static/admin.css").read_text()

    assert ".url-form { min-width: 0; }" in stylesheet
    assert ".url-form input { width: 100%; min-width: 0; flex: 1 1 auto; }" in stylesheet
    assert ".url-form button { flex: 0 0 auto; white-space: nowrap; }" in stylesheet


def test_logged_in_admin_dashboard_uses_platform_logo_for_url_task(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
    with Session(client.app.state.engine) as session:
        session.add(
            IngestionJob(
                input_type="url",
                original_url="https://www.xiaoyuzhoufm.com/episode/example",
                source_platform="xiaoyuzhou",
                media_type="audio",
            )
        )
        session.commit()
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.get("/admin")

    assert response.status_code == 200
    assert 'class="task-icon task-icon-url platform-logo platform-xiaoyuzhou"' in response.text
    assert 'class="platform-icon-img" src="/static/platform-icons/xiaoyuzhou.svg"' in response.text
    assert '>链<' not in response.text


def test_logged_in_admin_dashboard_prefers_persisted_title_over_raw_url(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
    raw_url = "https://v.douyin.com/9DLOqEFBaMM/"
    with Session(client.app.state.engine) as session:
        session.add(
            IngestionJob(
                input_type="url",
                original_url=raw_url,
                source_platform="douyin",
                media_type="video",
                status="failed",
                stage="failed",
                failure_stage="audio",
                error_message="upstream audio failed",
                evidence_metadata_json={"title": "给我 5 分钟，讲明白北京怎么玩", "source_platform": "douyin"},
            )
        )
        session.commit()
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.get("/admin")

    assert "给我 5 分钟，讲明白北京怎么玩" in response.text
    assert raw_url not in response.text
    assert "媒体处理失败" in response.text


def test_logged_in_admin_dashboard_uses_existing_saved_source_title(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
    source_id = create_saved_source(client)
    raw_url = "https://xhslink.cn/o/example"
    with Session(client.app.state.engine) as session:
        session.add(
            IngestionJob(
                input_type="url",
                original_url=raw_url,
                source_id=source_id,
                source_platform="xiaohongshu",
                media_type="article",
                status="succeeded",
                stage="succeeded",
            )
        )
        session.commit()
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.get("/admin")

    assert "北京：个人觉得无法超越的漂亮公园" in response.text
    assert raw_url not in response.text


def test_logged_in_admin_lists_only_saved_results(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
    source_id = create_saved_source(client)
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.get("/admin/sources")

    assert response.status_code == 200
    assert "解析结果" in response.text
    assert "北京：个人觉得无法超越的漂亮公园" in response.text
    assert 'class="result-workspace"' in response.text
    assert f'href="/admin/sources?source_id={source_id}"' in response.text
    assert f"background-image:url('/admin/sources/{source_id}/cover')" in response.text


def test_logged_in_admin_loads_youtube_cover_directly_without_proxy(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
    with Session(client.app.state.engine) as session:
        source = TravelSource(
            title="伏见稻荷大社交通攻略",
            body_text="京都伏见稻荷大社的交通与游玩建议。",
            original_url="https://www.youtube.com/watch?v=OKFijPl39bY",
            source_platform="youtube",
            cover_image_url="https://i.ytimg.com/vi/OKFijPl39bY/maxresdefault.jpg",
            destination="京都",
            category="play",
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        source_id = source.source_id
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.get("/admin/sources")

    assert response.status_code == 200
    assert f"background-image:url('https://i.ytimg.com/vi/OKFijPl39bY/maxresdefault.jpg')" in response.text
    assert f"background-image:url('/admin/sources/{source_id}/cover')" not in response.text


def test_logged_in_admin_shows_saved_result_card(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
    source_id = create_saved_source(client)
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.get(f"/admin/sources/{source_id}")

    assert response.status_code == 200
    assert "北京：个人觉得无法超越的漂亮公园" in response.text
    assert "拍照好看" in response.text
    assert 'class="selected-result"' in response.text


def test_admin_login_uses_the_refreshed_visual_shell(tmp_path: Path) -> None:
    response = configured_client(tmp_path).get("/admin/login")

    assert response.status_code == 200
    assert 'class="login-shell"' in response.text
    assert 'href="/static/admin.css"' in response.text
    assert 'class="login-artwork"' in response.text


def test_admin_styles_define_all_workspace_shells() -> None:
    stylesheet = (Path(__file__).parents[1] / "app" / "static" / "admin.css").read_text()

    assert ".login-shell" in stylesheet
    assert ".task-workspace" in stylesheet
    assert ".result-workspace" in stylesheet


def test_logged_in_admin_can_submit_video_url(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.post("/admin/ingestions/url", data={"url": "https://youtu.be/abcdefghijk"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/ingestions/ing_")


def test_logged_in_admin_extracts_xiaohongshu_share_link_and_queues_article(tmp_path: Path) -> None:
    client = configured_client(tmp_path, raise_server_exceptions=False)
    executor = RecordingExecutor()
    client.app.state.ingestion_executor = executor
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.post(
        "/admin/ingestions/url",
        data={"url": "北京：）个人觉得无法超越的漂亮公园 北京从来不缺好... http://xhslink.cn/o/6pkJ7jUEjdv 打开【小红书】，这篇笔记值得一看~"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/ingestions/ing_")
    assert len(executor.calls) == 1


def test_logged_in_admin_can_submit_image(tmp_path: Path) -> None:
    client = configured_client(tmp_path, raise_server_exceptions=False)
    executor = RecordingExecutor()
    client.app.state.ingestion_executor = executor
    client.post("/admin/login", data={"username": "admin", "password": "test-password"})

    response = client.post(
        "/admin/ingestions/image",
        files={"file": ("note.png", b"image-bytes", "image/png")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/ingestions/ing_")
    assert len(executor.calls) == 1


def test_admin_can_accept_reviewed_ingestion(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
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
                "title": "淄博博山菜探店",
                "body_text": "淄博博山菜探店体验。",
                "destination": "淄博",
                "category": "eat",
                "location_name": "博山",
                "normalized_tags": [],
                "raw_tags": ["探店"],
            },
            evidence_text="我们现在在博山吃博山菜。",
            evidence_origin="asr",
            evidence_language="zh",
            evidence_segments=[{"start_seconds": 0, "end_seconds": 2, "text": "我们现在在博山吃博山菜。"}],
            evidence_metadata_json={"title": "淄博博山菜探店"},
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.job_id

    client.post("/admin/login", data={"username": "admin", "password": "test-password"})
    response = client.post(f"/admin/ingestions/{job_id}/review", data={"decision": "accept"}, follow_redirects=False)

    assert response.status_code == 303
    with Session(client.app.state.engine) as session:
        saved_job = session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).one()
        assert saved_job.ingest_decision == "accept"
        assert saved_job.source_id is not None
        assert session.exec(select(TravelSource)).first() is not None
        review = session.exec(select(IngestionReview).where(IngestionReview.job_id == job_id)).one()
        assert review.decision == "accept"


def test_admin_accept_syncs_saved_source_to_rag(tmp_path: Path) -> None:
    rag_index = RecordingRagIndex()
    client = configured_client(tmp_path, rag_index=rag_index)
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
                "title": "淄博博山菜探店",
                "body_text": "淄博博山菜探店体验。",
                "destination": "淄博",
                "category": "eat",
            },
            evidence_text="我们现在在博山吃博山菜。",
            evidence_origin="asr",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.job_id

    client.post("/admin/login", data={"username": "admin", "password": "test-password"})
    response = client.post(f"/admin/ingestions/{job_id}/review", data={"decision": "accept"}, follow_redirects=False)

    assert response.status_code == 303
    with Session(client.app.state.engine) as session:
        saved_job = session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).one()
        saved_evidence = session.exec(
            select(SourceEvidence).where(SourceEvidence.source_id == saved_job.source_id)
        ).one()
    assert rag_index.indexed == [
        (saved_job.source_id, saved_evidence.evidence_id, saved_evidence.full_text)
    ]


def test_admin_accept_preserves_saved_source_when_rag_sync_fails(tmp_path: Path) -> None:
    rag_index = FailingRagIndex()
    client = configured_client(tmp_path, rag_index=rag_index)
    with Session(client.app.state.engine) as session:
        job = IngestionJob(
            input_type="url",
            original_url="https://youtu.be/abcdefghijk",
            source_platform="youtube",
            media_type="video",
            status="succeeded",
            stage="succeeded",
            ingest_decision="review",
            analysis_json={"title": "博山菜", "body_text": "博山菜探店", "destination": "淄博", "category": "eat"},
            evidence_text="我们现在在博山吃博山菜。",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.job_id

    client.post("/admin/login", data={"username": "admin", "password": "test-password"})
    response = client.post(f"/admin/ingestions/{job_id}/review", data={"decision": "accept"}, follow_redirects=False)

    assert response.status_code == 303
    with Session(client.app.state.engine) as session:
        saved_job = session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).one()
        assert session.exec(select(TravelSource).where(TravelSource.source_id == saved_job.source_id)).one()
    assert rag_index.attempted_source_ids == [saved_job.source_id]


def test_admin_can_accept_legacy_review_without_saved_evidence_metadata(tmp_path: Path) -> None:
    client = configured_client(tmp_path)
    with Session(client.app.state.engine) as session:
        job = IngestionJob(
            input_type="url",
            original_url="https://youtu.be/abcdefghijk",
            source_platform="youtube",
            media_type="video",
            status="succeeded",
            stage="succeeded",
            ingest_decision="review",
            analysis_json={"title": "博山菜", "body_text": "博山菜探店", "destination": "淄博", "category": "eat"},
            evidence_text="我们现在在博山吃博山菜。",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.job_id

    client.post("/admin/login", data={"username": "admin", "password": "test-password"})
    response = client.post(f"/admin/ingestions/{job_id}/review", data={"decision": "accept"}, follow_redirects=False)

    assert response.status_code == 303
