from functools import lru_cache
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic import Field
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
    # Защита от перебора кода: пауза между запросами и лимиты в час на один e-mail.
    otp_resend_seconds: int = 60
    otp_codes_per_hour: int = 5
    otp_failed_per_hour: int = 10
    # Часовой пояс, по которому считается «сегодня» (вопрос дня, огонёк).
    timezone: str = "Europe/Moscow"
    radar_daily_limit: int = 30
    # Ключи шифрования личных данных в базе: "k1:<base64 32 байта>[,k0:<старый>]". В dev можно не задавать.
    data_keys: str = ""
    # Почта для кодов входа (рег.ру): smtp_host=mail.hosting.reg.ru, порт 465, ящик и пароль — в .env.
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""  # пусто — как smtp_user
    # Папка с vecher.apk и version.json для обновления приложения без магазина.
    downloads_dir: str = "downloads"
    # Токен автосборки (GitHub Actions): с ним PUT /app/release выкладывает новый APK. Пусто — выкладка выключена.
    release_token: str = ""
    # Адрес сайта для ссылок-приглашений: <public_url>/i/<код>
    public_url: str = "https://вечер.online"
    free_plans: int = 1  # сколько планов к совпадениям паре открывается без подписки
    # Push: путь к ключу сервисного аккаунта Firebase (секрет, в git не хранится).
    firebase_credentials: str = "firebase-key.json"
    scheduler_enabled: bool = True  # фоновая отправка писем, сюрпризов, напоминаний, дат
    # Durable outbox: API writes intents in its transaction; leased workers deliver them.
    outbox_enabled: bool = True
    outbox_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    outbox_max_attempts: int = Field(default=8, ge=1, le=20)
    outbox_lease_seconds: int = Field(default=90, ge=30, le=600)
    outbox_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    outbox_retry_base_seconds: float = Field(default=2.0, ge=0.1, le=60)
    outbox_retry_max_seconds: float = Field(default=300.0, ge=1, le=3600)
    # Personal invalidations via Centrifugo. Rollout is opt-in; push/polling stay available.
    realtime_enabled: bool = False
    realtime_ws_url: str = ""
    centrifugo_api_url: str = "http://centrifugo:8000/api"
    centrifugo_api_key: str = ""
    realtime_token_secret: str = ""
    realtime_token_ttl_seconds: int = Field(default=300, ge=60, le=900)
    # Приватные вложения: шифруются DATA_KEYS, никогда не раздаются как статика.
    uploads_dir: str = "uploads"
    attachment_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    attachment_pair_quota_bytes: int = Field(default=250 * 1024 * 1024, ge=1)
    attachment_pending_ttl_hours: int = Field(default=24, ge=1, le=168)
    # Coturn REST credentials: общий секрет только на сервере, клиенту — временный HMAC.
    turn_urls: list[str] = []
    stun_urls: list[str] = []
    turn_secret: str = ""
    turn_credential_ttl_seconds: int = Field(default=15000, ge=600, le=86400)
    call_ring_timeout_seconds: int = Field(default=60, ge=15, le=120)
    call_heartbeat_timeout_seconds: int = Field(default=90, ge=30, le=300)
    call_max_duration_seconds: int = Field(default=14400, ge=60, le=43200)
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
        if self.realtime_enabled:
            if len(self.realtime_token_secret) < 32 or self.realtime_token_secret == self.jwt_secret:
                raise ValueError("VECHER_REALTIME_TOKEN_SECRET: нужен отдельный случайный секрет от 32 символов")
            if len(self.centrifugo_api_key) < 32:
                raise ValueError("VECHER_CENTRIFUGO_API_KEY должен содержать от 32 символов")
            api = urlparse(self.centrifugo_api_url)
            ws = urlparse(self.realtime_ws_url)
            if api.scheme not in {"http", "https"} or not api.hostname or api.username or api.password or api.fragment or api.query:
                raise ValueError("VECHER_CENTRIFUGO_API_URL: требуется HTTP(S) URL без секретов и параметров")
            if ws.scheme not in {"ws", "wss"} or not ws.hostname or ws.username or ws.password or ws.fragment or ws.query:
                raise ValueError("VECHER_REALTIME_WS_URL: требуется WS(S) URL без секретов и параметров")
            if ws.scheme != "wss" and (not self.is_dev or ws.hostname not in {"localhost", "127.0.0.1", "::1"}):
                raise ValueError("VECHER_REALTIME_WS_URL: вне localhost-разработки требуется WSS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
