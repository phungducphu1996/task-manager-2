from functools import lru_cache
import re

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'Team Task Manager API'
    database_url: str = 'postgresql+psycopg://postgres:postgres@localhost:5432/team_task_manager'
    db_schema: str | None = None
    app_timezone: str = 'Asia/Ho_Chi_Minh'
    cors_origins: str = 'http://localhost:5173'
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_storage_bucket: str = 'task-attachments'
    supabase_signed_url_expires_seconds: int = 60 * 60 * 24
    auth_secret_key: str = 'dev-change-me'
    auth_token_expires_minutes: int = 60 * 24 * 7
    notify_timezone: str = 'Asia/Ho_Chi_Minh'
    notify_internal_token: str | None = None
    zalo_worker_url: str | None = None
    zalo_worker_token: str | None = None
    zalo_shared_secret: str | None = None
    zalo_group_id: str | None = None
    notification_retry_delays_seconds: str = '5,30,120'
    notification_delivery_batch_limit: int = 100
    notification_http_timeout_seconds: float = 10.0
    notification_max_retries: int = 3

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(',') if origin.strip()]

    @property
    def normalized_db_schema(self) -> str | None:
        if not self.db_schema:
            return None
        schema = self.db_schema.strip()
        if not schema:
            return None
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', schema):
            raise ValueError('DB_SCHEMA is invalid. Use letters, numbers, and underscores only.')
        return schema

    @property
    def notification_retry_delays(self) -> list[int]:
        values: list[int] = []
        for raw in self.notification_retry_delays_seconds.split(','):
            token = raw.strip()
            if not token:
                continue
            if not token.isdigit():
                raise ValueError('NOTIFICATION_RETRY_DELAYS_SECONDS must contain comma-separated integers.')
            values.append(int(token))
        if not values:
            return [5, 30, 120]
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()
