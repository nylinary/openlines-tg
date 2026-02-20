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

    @property
    def b24_imbot_event_handler(self) -> str:
        return f"https://{self.public_domain}/b24/imbot/events"

    # Redis
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")

    # HTTP
    http_timeout_s: float = Field(default=10.0, alias="HTTP_TIMEOUT_S")
    http_retries: int = Field(default=3, alias="HTTP_RETRIES")


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
