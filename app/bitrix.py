from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from .storage import Storage


log = logging.getLogger("app.bitrix")


class BitrixError(RuntimeError):
    pass


class BitrixOAuthError(BitrixError):
    pass


class BitrixClient:
    def __init__(
        self,
        *,
        domain: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        storage: Storage,
        timeout_s: float = 10.0,
        retries: int = 3,
    ):
        self.domain = domain.strip().replace("https://", "").replace("http://", "").rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.storage = storage
        self.timeout_s = timeout_s
        self.retries = retries
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    async def close(self) -> None:
        await self._client.aclose()

    # --- OAuth ---

    def auth_url(self, *, state: str) -> str:
        # User will open it in browser and approve app installation.
        return (
            f"https://{self.domain}/oauth/authorize/?client_id={self.client_id}"
            f"&redirect_uri={httpx.QueryParams({'redirect_uri': self.redirect_uri})['redirect_uri']}"
            f"&state={state}"
        )

    async def exchange_code(self, code: str) -> None:
        url = f"https://{self.domain}/oauth/token/"
        params = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "code": code,
        }
        r = await self._client.get(url, params=params)
        data = r.json() if r.text else {}
        if r.status_code >= 400 or not isinstance(data, dict) or data.get("error"):
            raise BitrixOAuthError(f"oauth_exchange_failed: {data}")

        await self.storage.set_b24_tokens(
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "")),
            expires_in=int(data.get("expires_in", 0) or 0),
        )

    async def refresh(self) -> None:
        refresh_token = await self.storage.get_b24_refresh_token()
        if not refresh_token:
            raise BitrixOAuthError("no_refresh_token")

        url = f"https://{self.domain}/oauth/token/"
        params = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
        }
        r = await self._client.get(url, params=params)
        data = r.json() if r.text else {}
        if r.status_code >= 400 or not isinstance(data, dict) or data.get("error"):
            raise BitrixOAuthError(f"oauth_refresh_failed: {data}")

        await self.storage.set_b24_tokens(
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", refresh_token)),
            expires_in=int(data.get("expires_in", 0) or 0),
        )

    async def ensure_token(self) -> str:
        if await self.storage.b24_token_is_expiring(skew_s=90):
            await self.refresh()
        token = await self.storage.get_b24_access_token()
        if not token:
            raise BitrixOAuthError("no_access_token")
        return token

    # --- REST calls ---

    def _url(self, method: str, access_token: str) -> str:
        return f"https://{self.domain}/rest/{method}.json?auth={access_token}"

    async def call(self, method: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        access_token = await self.ensure_token()
        url = self._url(method, access_token)
        form = data or {}

        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                r = await self._client.post(url, data=form)
                if r.status_code in (401, 403):
                    # Token might be expired/revoked; try one refresh.
                    await self.refresh()
                    access_token = await self.ensure_token()
                    url = self._url(method, access_token)
                    r = await self._client.post(url, data=form)

                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=r.request, response=r)

                payload = r.json() if r.text else {}
                if isinstance(payload, dict) and payload.get("error"):
                    raise BitrixError(f"{payload.get('error')}: {payload.get('error_description')}")

                return payload if isinstance(payload, dict) else {"result": payload}
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, BitrixError, BitrixOAuthError) as e:
                last_err = e
                sleep_s = min(2.0 ** (attempt - 1), 8.0)
                log.warning(
                    "bitrix_call_retry",
                    extra={
                        "method": method,
                        "attempt": attempt,
                        "sleep_s": sleep_s,
                        "error": str(e),
                    },
                )
                await asyncio.sleep(sleep_s)

        raise BitrixError(f"Bitrix call failed after retries: {method}: {last_err}")

    async def register(self, connector: str) -> Dict[str, Any]:
        return await self.call(
            "imconnector.register",
            {
                "CONNECTOR": connector,
                "NAME": connector,
            },
        )

    async def activate(self, connector: str, line_id: str) -> Dict[str, Any]:
        return await self.call(
            "imconnector.activate",
            {
                "CONNECTOR": connector,
                "LINE": line_id,
                "ACTIVE": "Y",
            },
        )

    async def status(self, connector: str, line_id: str) -> Dict[str, Any]:
        return await self.call(
            "imconnector.status",
            {
                "CONNECTOR": connector,
                "LINE": line_id,
            },
        )

    async def send_messages(self, connector: str, line_id: str, messages: list[Dict[str, Any]]) -> Dict[str, Any]:
        return await self.call(
            "imconnector.send.messages",
            {
                "CONNECTOR": connector,
                "LINE": line_id,
                **_encode_messages(messages),
            },
        )

    async def send_status_delivery(self, connector: str, line_id: str, chat_id: str, message_id: str) -> Dict[str, Any]:
        return await self.call(
            "imconnector.send.status.delivery",
            {
                "CONNECTOR": connector,
                "LINE": line_id,
                "CHAT_ID": chat_id,
                "MESSAGE_ID": message_id,
            },
        )

    async def send_status_reading(self, connector: str, line_id: str, chat_id: str, message_id: str) -> Dict[str, Any]:
        return await self.call(
            "imconnector.send.status.reading",
            {
                "CONNECTOR": connector,
                "LINE": line_id,
                "CHAT_ID": chat_id,
                "MESSAGE_ID": message_id,
            },
        )


def _encode_messages(messages: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Encode MESSAGES[] to x-www-form-urlencoded style keys.

    Bitrix endpoints often expect fields like:
      MESSAGES[0][user][id]=...
      MESSAGES[0][chat][id]=...
      MESSAGES[0][message][id]=...
      MESSAGES[0][message][text]=...

    This encoder handles dict nesting.
    """

    def walk(prefix: str, obj: Any, out: Dict[str, Any]) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(f"{prefix}[{k}]", v, out)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(f"{prefix}[{i}]", v, out)
        else:
            out[prefix] = "" if obj is None else str(obj)

    out: Dict[str, Any] = {}
    walk("MESSAGES", messages, out)
    return out
