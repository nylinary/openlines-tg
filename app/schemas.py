from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


# --- Telegram (subset) ---


class TgUser(BaseModel):
    id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None


class TgChat(BaseModel):
    id: int
    type: Optional[str] = None


class TgMessage(BaseModel):
    message_id: int
    from_user: Optional[TgUser] = Field(default=None, alias="from")
    chat: TgChat
    text: Optional[str] = None


class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[TgMessage] = None


# --- Bitrix connector events (very loose / defensive) ---


class BitrixEventEnvelope(BaseModel):
    # Bitrix can send different shapes; keep raw
    event: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    payload: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    # accept everything
    model_config = {"extra": "allow"}
