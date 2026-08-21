from app.config import Settings


def test_settings_ignores_compose_port_helper_variable(monkeypatch) -> None:
    monkeypatch.setenv("TRIPGUARD_BACKEND_PORT", "18080")

    settings = Settings()

    assert settings.service_name == "tripguard-mvp-backend"


def test_settings_keep_the_default_attraction_api_base_url() -> None:
    settings = Settings()

    assert settings.attraction_api_base_url == "https://x.inews.qq.com/travel/v1/admin"


def test_settings_keep_the_default_location_url() -> None:
    settings = Settings()

    assert settings.tencent_location_base_url == "https://apis.map.qq.com"
