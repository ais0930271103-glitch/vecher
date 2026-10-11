from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings come from environment variables (or backend/.env in dev)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="VECHER_", extra="ignore")

    env: str = "dev"  # dev | staging | prod
    database_url: str = "sqlite+aiosqlite:///./dev.db"
    jwt_secret: str = "dev-only-secret-change-me-in-env-file-0000"
    jwt_ttl_days: int = 30
    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    # Часовой пояс, по которому считается «сегодня» (вопрос дня, огонёк).
    timezone: str = "Europe/Moscow"
    radar_daily_limit: int = 30
    free_plans: int = 1  # сколько планов к совпадениям паре открывается без подписки
    cors_origins: list[str] = ["http://localhost:3000"]
    # В dev разрешаем любой localhost-порт: flutter run -d chrome каждый раз берёт случайный порт.
    cors_dev_regex: str = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @model_validator(mode="after")
    def _strong_secret_outside_dev(self) -> "Settings":
        if self.env != "dev" and (len(self.jwt_secret) < 32 or "change-me" in self.jwt_secret):
            raise ValueError("VECHER_JWT_SECRET должен быть случайной строкой длиной от 32 символов")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
