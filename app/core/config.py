from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "local"
    database_url: str = "postgresql+psycopg://bp_app:bp_app_dev@localhost:5432/batchpilot"
    migration_database_url: str = "postgresql+psycopg://bp_owner:bp_owner_dev@localhost:5432/batchpilot"
    # Cross-tenant jobs only (ledger reconciliation, outbox worker): role with BYPASSRLS.
    ops_database_url: str = "postgresql+psycopg://bp_ops:bp_ops_dev@localhost:5432/batchpilot"

    secret_key: str = "dev-only-change-me"
    session_cookie_name: str = "bp_session"
    session_days: int = 30
    cookie_secure: bool = False

    otp_length: int = 6
    otp_ttl_minutes: int = 5
    otp_max_per_window: int = 3  # per phone per 15 min
    otp_max_attempts: int = 5
    dev_otp_echo: bool = True

    # SMS provider: console (log only) | android_gateway (free: your phone's SIM via
    # "SMS Gateway for Android") | bulksmsbd (paid local gateway, for production).
    sms_provider: str = "console"
    sms_gateway_url: str = "https://api.sms-gate.app/3rdparty/v1/message"
    sms_gateway_username: str = ""
    sms_gateway_password: str = ""
    bulksmsbd_api_key: str = ""
    bulksmsbd_sender_id: str = ""

    cors_origins: str = "http://localhost:3000"

    business_tz: str = "Asia/Dhaka"
    # Guardians never get SMS outside this window (08:00–21:00 Asia/Dhaka).
    quiet_start_hour: int = 21
    quiet_end_hour: int = 8

    # An assistant may reverse their own payment within this many minutes.
    assistant_reverse_minutes: int = 10

    @property
    def is_local(self) -> bool:
        return self.env in ("local", "dev", "test")


@lru_cache
def get_settings() -> Settings:
    return Settings()
