"""All settings come from environment variables (Integration Standard point 6)."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    api_token: str = "change-me-admin-token"
    simulation_mode: bool = True
    database_url: str = "sqlite:///./incubrix.db"
    redis_url: str = ""
    job_backend: str = "inline"  # rq | inline
    s3_endpoint_url: str = ""
    s3_public_endpoint_url: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "incubrix-output"
    local_store_dir: str = "./evidence/local_store"
    callback_signing_secret: str = "change-me-callback-secret"
    mock_platforms_url: str = "http://mocks:9100"
    max_test_post_lifetime_seconds: int = 600
    unavailable_stop_after: int = 5
    rate_limit_per_minute: int = 0  # 0 = off
    public_base_url: str = "http://localhost:8000"
    http_timeout_seconds: float = 10.0
    max_inline_backoff_seconds: float = 3.0  # sleep and retry a 429 only when the reset is this short


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
