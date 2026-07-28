from pathlib import Path

from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from app.config import Settings
from app.main import create_app


def configured_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'admin.db'}",
        uploads_dir=str(tmp_path / "uploads"),
        admin_username="admin",
        admin_password_hash=PasswordHash.recommended().hash("test-password"),
        admin_session_secret="test-session-secret",
    )
    return TestClient(create_app(settings=settings))


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
