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
    callback_allow_private: bool = False  # DEV ONLY: disables the callback address checks (the demo image refuses to start with it)
    callback_allow_http: bool = False
    callback_allowed_ports: str = "443,8443"
    callback_dns_timeout: float = 3.0
    media_fetch_allow_private: bool = False  # DEV ONLY: disables the address checks for URL fetching (the demo image refuses it)
    media_fetch_allow_http: bool = True
    media_fetch_allowed_ports: str = "80,443,8080,8443"
    media_fetch_max_bytes: int = 10 * 1024 * 1024
    media_fetch_allowed_types: str = "image/png,image/jpeg,image/gif,image/webp,video/mp4,text/plain"
    upload_max_bytes: int = 25 * 1024 * 1024  # POST /files size limit
    job_timeout_seconds: int = 600  # RQ job timeout
    idempotency_claim_ttl_seconds: float = 60.0  # a claim older than this belongs to a dead request
    idempotency_wait_seconds: float = 5.0  # how long a concurrent duplicate waits for the owner before answering 409
    media_fetch_connect_timeout: float = 5.0
    media_fetch_read_timeout: float = 10.0
    media_fetch_total_timeout: float = 20.0
    media_fetch_max_redirects: int = 3
    credentials_dir: str = ""  # optional folder of secret files (one file per credentials_ref), e.g. a Docker volume
    max_inline_backoff_seconds: float = 3.0  # sleep and retry a 429 only when the reset is this short


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
