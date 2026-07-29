from pathlib import Path

from fastapi.testclient import TestClient
from pwdlib import PasswordHash
from sqlmodel import Session

from app.config import Settings
from app.main import create_app
from app.models import TravelSource


def configured_client(tmp_path: Path, *, raise_server_exceptions: bool = True) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'admin.db'}",
        uploads_dir=str(tmp_path / "uploads"),
        admin_username="admin",
        admin_password_hash=PasswordHash.recommended().hash("test-password"),
        admin_session_secret="test-session-secret",
    )
    return TestClient(create_app(settings=settings), raise_server_exceptions=raise_server_exceptions)


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def submit(self, function: object, *args: object) -> None:
        self.calls.append((function, args))


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
