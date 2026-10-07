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
    session_ttl_hours: int = 24 * 7  # абсолютный срок жизни сессии
    session_idle_minutes: int = 12 * 60  # выход после стольких минут бездействия
    cookie_secure: bool = False
    force_https: bool = False

    # Защита от перебора пароля: блокировка учётной записи...
    login_max_attempts: int = 5
    login_lock_minutes: int = 15
    # ...и ограничение частоты запросов с одного IP (за окно в минутах)
    login_rate_limit: int = 20
    login_rate_window_minutes: int = 5
    register_rate_limit: int = 5
    register_rate_window_minutes: int = 60

    # Сколько доверенных прокси стоит перед приложением (у Amvera — входной прокси).
    # IP клиента для лимитов берётся из X-Forwarded-For на столько позиций справа:
    # левые значения клиент может подделать. 0 — не доверять X-Forwarded-For вовсе.
    trusted_proxy_hops: int = 1

    # Максимальный размер тела запроса (импорт выписки ~200 тыс. символов ≈ 1,2 МБ в форме)
    max_body_bytes: int = 2 * 1024 * 1024

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
    def cookie_prefix(self) -> str:
        """По HTTPS — префикс __Host-: cookie нельзя подменить с поддомена или по HTTP."""
        return "__Host-" if self.cookie_secure else ""

    @property
    def session_cookie(self) -> str:
        return self.cookie_prefix + self.session_cookie_name

    @property
    def csrf_cookie(self) -> str:
        return self.cookie_prefix + "fa_csrf"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def validate_for_runtime(self) -> None:
        if self.is_production and not self.cookie_secure:
            raise RuntimeError("В production COOKIE_SECURE должен быть true")


@lru_cache
def get_settings() -> Settings:
    return Settings()
