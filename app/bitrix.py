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


def _redact_bitrix_url(url: str) -> str:
    # Hide auth token in logs
    return url.replace("auth=", "auth=***")


def _redact_form(data: Dict[str, Any]) -> Dict[str, Any]:
    redacted: Dict[str, Any] = {}
    for k, v in data.items():
        if k.upper() in {"AUTH", "ACCESS_TOKEN", "REFRESH_TOKEN", "CLIENT_SECRET", "HASH"}:
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted


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
        connector_hash: str = "",
    ):
        self.domain = domain.strip().replace("https://", "").replace("http://", "").rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.storage = storage
        self.timeout_s = timeout_s
        self.retries = retries
        self.connector_hash = connector_hash
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

    async def call(
        self,
        method: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        use_bearer: bool = False,
        json_body: bool = False,
    ) -> Dict[str, Any]:
        access_token = await self.ensure_token()

        url = self._url(method, access_token) if not use_bearer else f"https://{self.domain}/rest/{method}.json"
        form_or_json = data or {}

        headers: Dict[str, str] = {}
        if use_bearer:
            headers["Authorization"] = f"Bearer {access_token}"

        # Log raw outgoing request for debugging (redacted)
        try:
            log.info(
                "bitrix_http_request",
                extra={
                    "method": method,
                    "url": _redact_bitrix_url(url),
                    "headers": {k: ("Bearer ***" if k.lower() == "authorization" else v) for k, v in headers.items()},
                    "body_type": "json" if json_body else "form",
                    "body": _redact_form({k: ("" if v is None else str(v)) for k, v in form_or_json.items()}),
                },
            )
        except Exception:
            pass

        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                if json_body:
                    r = await self._client.post(url, json=form_or_json, headers=headers)
                else:
                    r = await self._client.post(url, data=form_or_json, headers=headers)

                if r.status_code in (401, 403):
                    await self.refresh()
                    access_token = await self.ensure_token()
                    if use_bearer:
                        headers["Authorization"] = f"Bearer {access_token}"
                        url = f"https://{self.domain}/rest/{method}.json"
                    else:
                        url = self._url(method, access_token)
                    if json_body:
                        r = await self._client.post(url, json=form_or_json, headers=headers)
                    else:
                        r = await self._client.post(url, data=form_or_json, headers=headers)

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
        """Register connector (best-effort).

        Some portals deny custom provider registration for local apps and return
        CONNECTOR_ID_REQUIRED, while still allowing activate/status/send.messages
        when the connector code is already configured on the portal.

        In that case, we treat it as non-fatal and let runtime calls decide.
        """
        try:
            return await self.call(
                "imconnector.register",
                {
                    "CONNECTOR": connector,
                    "NAME": connector,
                },
            )
        except BitrixError as e:
            msg = str(e)
            if "CONNECTOR_ID_REQUIRED" in msg or "ID коннектора" in msg:
                log.warning("bitrix_register_skipped", extra={"reason": msg, "connector": connector})
                return {"error": "register_skipped", "error_description": msg}
            raise

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
        payload: Dict[str, Any] = {
            "CONNECTOR": connector,
            "LINE": line_id,
            **_encode_messages(messages),
        }
        if self.connector_hash:
            payload["HASH"] = self.connector_hash
        return await self.call("imconnector.send.messages", payload)

    async def send_status_delivery(self, connector: str, line_id: str, chat_id: str, message_id: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "CONNECTOR": connector,
            "LINE": line_id,
            "CHAT_ID": chat_id,
            "MESSAGE_ID": message_id,
        }
        if self.connector_hash:
            payload["HASH"] = self.connector_hash
        return await self.call("imconnector.send.status.delivery", payload)

    async def send_status_reading(self, connector: str, line_id: str, chat_id: str, message_id: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "CONNECTOR": connector,
            "LINE": line_id,
            "CHAT_ID": chat_id,
            "MESSAGE_ID": message_id,
        }
        if self.connector_hash:
            payload["HASH"] = self.connector_hash
        return await self.call("imconnector.send.status.reading", payload)

    async def imbot_register(
        self,
        *,
        code: str,
        name: str,
        event_handler: str,
        openline: str = "Y",
    ) -> Dict[str, Any]:
        safe_name = (name or "").strip() or code
        payload: Dict[str, Any] = {
            "CODE": code,
            "TYPE": "B",
            "EVENT_HANDLER": event_handler,
            "OPENLINE": openline,
            "PROPERTIES": {
                "NAME": safe_name,
                "LAST_NAME": "",
                "COLOR": "AQUA",
            },
        }
        # imbot.register frequently expects JSON and works reliably with Bearer auth
        return await self.call("imbot.register", payload, use_bearer=True, json_body=True)

    async def imbot_update(self, *, bot_id: str, name: Optional[str] = None, event_handler: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"BOT_ID": bot_id}
        props: Dict[str, Any] = {}
        if name is not None:
            props["NAME"] = name
        if event_handler is not None:
            payload["EVENT_HANDLER"] = event_handler
        if props:
            payload["PROPERTIES"] = props
        return await self.call("imbot.update", payload, use_bearer=True, json_body=True)


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
