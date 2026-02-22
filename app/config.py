from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Common
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)

    # Bitrix24 (OAuth local app)
    b24_domain: str = Field(alias="B24_DOMAIN")  # e.g. b24-gko4ik.bitrix24.ru
    b24_client_id: str = Field(alias="B24_CLIENT_ID")
    b24_client_secret: str = Field(alias="B24_CLIENT_SECRET")
    b24_redirect_uri: str = Field(alias="B24_REDIRECT_URI")  # must match app settings in Bitrix

    # Public domain where Bitrix can reach the service
    public_domain: str = Field(alias="PUBLIC_DOMAIN")

    # Bitrix imbot — registered via Bitrix UI
    b24_imbot_id: int = Field(alias="B24_IMBOT_ID")  # numeric bot ID assigned by Bitrix
    b24_imbot_code: str = Field(alias="B24_IMBOT_CODE")
    b24_imbot_name: str = Field(default="Bitrix Bot", alias="B24_IMBOT_NAME")
    b24_imbot_client_id: str = Field(default="", alias="B24_IMBOT_CLIENT_ID")

    # Inbound webhook URL for bot REST calls (avoids OAuth app mismatch).
    # Create in Bitrix: Developer resources → Inbound webhook, with "im" scope.
    # Example: https://b24-xxx.bitrix24.ru/rest/1/abc123/
    b24_webhook_url: str = Field(default="", alias="B24_WEBHOOK_URL")

    # Open Line ID — needed for session monitoring & auto-reassignment.
    # Find via Bitrix admin: Contact Center → Open Lines → line settings URL
    # contains the numeric ID, or call imopenlines.config.list.
    b24_openline_id: int = Field(default=0, alias="B24_OPENLINE_ID")

    @property
    def b24_imbot_event_handler(self) -> str:
        return f"https://{self.public_domain}/b24/imbot/events"

    @property
    def b24_ol_event_handler(self) -> str:
        """URL for Open Line event webhooks (event.bind)."""
        return f"https://{self.public_domain}/b24/ol/events"

    # LLM (OpenAI-compatible API)
    llm_temperature: float = Field(default=0.3, alias="LLM_TEMPERATURE")
    llm_max_tokens: int = Field(default=2000, alias="LLM_MAX_TOKENS")

    # OpenAI (or any OpenAI-compatible API — Azure, local proxy, etc.)
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    openai_base_url: str = Field(default="", alias="OPENAI_BASE_URL")  # custom endpoint (optional)

    # Speech-to-text (voice message transcription via OpenAI Whisper)
    stt_enabled: bool = Field(default=True, alias="STT_ENABLED")
    stt_model: str = Field(default="whisper-1", alias="STT_MODEL")
    stt_language: str = Field(default="ru", alias="STT_LANGUAGE")

    # Scraper schedule (seconds)
    scraper_full_interval_s: int = Field(default=86400, alias="SCRAPER_FULL_INTERVAL_S")  # daily
    scraper_price_interval_s: int = Field(default=3600, alias="SCRAPER_PRICE_INTERVAL_S")  # hourly

    # PostgreSQL
    database_url: str = Field(
        default="postgresql://myryba:myryba@postgres:5432/myryba",
        alias="DATABASE_URL",
    )

    # Redis
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")

    # HTTP
    http_timeout_s: float = Field(default=10.0, alias="HTTP_TIMEOUT_S")
    http_retries: int = Field(default=3, alias="HTTP_RETRIES")
    llm_timeout_s: float = Field(default=60.0, alias="LLM_TIMEOUT_S")


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
