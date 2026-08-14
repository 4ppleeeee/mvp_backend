from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "tripguard-mvp-backend"
    public_base_url: str = "http://127.0.0.1:18080"
    database_url: str = "sqlite:///./tripguard.db"
    uploads_dir: str = "./uploads"
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "gemma4:latest"
    llm_max_tokens: int = 220
    request_timeout_seconds: float = 240.0
    ingestion_temp_dir: str = "./ingestion-tmp"
    ingestion_max_attempts: int = 2
    ingestion_max_upload_bytes: int = 512 * 1024 * 1024
    media_proxy_url: str | None = None
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    video_keyframes_enabled: bool = False
    video_frame_interval_seconds: int = 6
    video_grid_rows: int = 2
    video_grid_columns: int = 2
    admin_username: str | None = None
    admin_password_hash: str | None = None
    admin_session_secret: str | None = None
    admin_api_enabled: bool = False
    admin_allowed_origins: str = ""
    crawlab_results_api_url: str | None = None
    crawlab_api_token: str | None = None
    tencent_location_api_key: str | None = None
    tencent_location_base_url: str | None = None

    @property
    def admin_cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.admin_allowed_origins.split(",") if origin.strip()]

    model_config = SettingsConfigDict(env_file=".env", env_prefix="TRIPGUARD_", extra="ignore")
