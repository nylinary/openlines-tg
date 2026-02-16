from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx


log = logging.getLogger("app.telegram")


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, bot_token: str, *, timeout_s: float = 10.0, retries: int = 3):
        self.bot_token = bot_token
        self.timeout_s = timeout_s
        self.retries = retries
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    async def close(self) -> None:
        await self._client.aclose()

    def verify_secret(self, header_value: Optional[str], expected: str) -> None:
        if not header_value or header_value != expected:
            raise TelegramError("invalid_telegram_secret")

    async def _call(self, method: str, *, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"

        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                r = await self._client.post(url, json=json)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
                payload = r.json() if r.text else {}
                if not isinstance(payload, dict) or not payload.get("ok"):
                    raise TelegramError(str(payload))
                return payload
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, TelegramError) as e:
                last_err = e
                sleep_s = min(2.0 ** (attempt - 1), 8.0)
                log.warning(
                    "telegram_call_retry",
                    extra={"method": method, "attempt": attempt, "sleep_s": sleep_s, "error": str(e)},
                )
                await asyncio.sleep(sleep_s)

        raise TelegramError(f"Telegram call failed after retries: {method}: {last_err}")

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> Dict[str, Any]:
        return await self._call("deleteWebhook", json={"drop_pending_updates": drop_pending_updates})

    async def get_updates(
        self,
        *,
        offset: Optional[int] = None,
        timeout: int = 25,
        allowed_updates: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates is not None:
            payload["allowed_updates"] = allowed_updates
        return await self._call("getUpdates", json=payload)

    async def send_message(self, chat_id: str, text: str) -> Dict[str, Any]:
        return await self._call("sendMessage", json={"chat_id": chat_id, "text": text})
