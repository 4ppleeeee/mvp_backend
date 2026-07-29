from app.config import Settings


def test_settings_ignores_compose_port_helper_variable(monkeypatch) -> None:
    monkeypatch.setenv("TRIPGUARD_BACKEND_PORT", "18080")

    settings = Settings()

    assert settings.service_name == "tripguard-mvp-backend"
