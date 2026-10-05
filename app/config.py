"""Настройки приложения. Все секреты берутся только из переменных окружения / .env."""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="development", description="development | production | test")
    database_url: str = "postgresql+psycopg://fin_acc:fin_acc@localhost:5434/fin_acc"

    # Cookie сессии. В production должны быть secure=True (HTTPS).
    session_cookie_name: str = "fa_session"
    session_ttl_hours: int = 24 * 7
    cookie_secure: bool = False
    force_https: bool = False

    # Защита от перебора пароля
    login_max_attempts: int = 5
    login_lock_minutes: int = 15

    # Создание первого администратора при старте (если администраторов ещё нет)
    admin_email: str | None = None
    admin_password: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        # Хостинги часто отдают postgres:// или postgresql:// — приводим к драйверу psycopg3
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix) :]
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def validate_for_runtime(self) -> None:
        if self.is_production and not self.cookie_secure:
            raise RuntimeError("В production COOKIE_SECURE должен быть true")


@lru_cache
def get_settings() -> Settings:
    return Settings()
