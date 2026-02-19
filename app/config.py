from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Common
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)

    # Connector
    # Must be one of codes returned by imconnector.list (e.g. telegrambot, vkgroup, whatsappbytwilio, ...)
    connector_code: str = Field(default="telegrambot", alias="CONNECTOR_CODE")

    # Some connectors require an extra HASH parameter for sending messages.
    b24_connector_hash: str = Field(default="", alias="B24_CONNECTOR_HASH")

    # Bitrix24 (OAuth app)
    b24_domain: str = Field(alias="B24_DOMAIN")  # e.g. b24-gko4ik.bitrix24.ru
    b24_client_id: str = Field(alias="B24_CLIENT_ID")
    b24_client_secret: str = Field(alias="B24_CLIENT_SECRET")
    # Must match app settings in Bitrix
    b24_redirect_uri: str = Field(alias="B24_REDIRECT_URI")

    # OpenLines
    b24_line_id: str = Field(alias="B24_LINE_ID")

    # Secret for /b24/handler (either header X-B24-Handler-Secret or query ?secret=)
    b24_handler_secret: str = Field(alias="B24_HANDLER_SECRET")

    public_domain: str = Field(alias="PUBLIC_DOMAIN")
    # Bitrix imbot (optional) — for echo testing inside Bitrix
    b24_imbot_code: str = Field(default="openlines_test_bot", alias="B24_IMBOT_CODE")
    b24_imbot_name: str = Field(default="OpenLines Test Bot", alias="B24_IMBOT_NAME")

    # Public URL where Bitrix can send bot events (messages, etc). Must be reachable from Bitrix.

    @property
    def b24_imbot_event_handler(self) -> str:
        return f"https://{self.public_domain}/b24/imbot/events"

    # Telegram
    tg_bot_token: str = Field(alias="TG_BOT_TOKEN")
    # Only required when using Telegram webhooks. With polling it can be empty.
    tg_webhook_secret: str = Field(default="", alias="TG_WEBHOOK_SECRET")

    # Telegram polling
    tg_use_polling: bool = Field(default=True, alias="TG_USE_POLLING")
    tg_poll_interval_s: float = Field(default=1.0, alias="TG_POLL_INTERVAL_S")

    # Redis
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")

    # HTTP
    http_timeout_s: float = Field(default=10.0, alias="HTTP_TIMEOUT_S")
    http_retries: int = Field(default=3, alias="HTTP_RETRIES")


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
