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

    model_config = SettingsConfigDict(env_file=".env", env_prefix="TRIPGUARD_")
