from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from .storage import Storage


log = logging.getLogger("app.bitrix")


class BitrixError(RuntimeError):
    pass


def _redact_bitrix_url(url: str) -> str:
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
        storage: Storage,
        timeout_s: float = 10.0,
        retries: int = 3,
    ):
        self.domain = domain.strip().replace("https://", "").replace("http://", "").rstrip("/")
        self.storage = storage
        self.timeout_s = timeout_s
        self.retries = retries
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    async def close(self) -> None:
        await self._client.aclose()

    # --- REST calls ---

    async def call_webhook(
        self,
        webhook_url: str,
        method: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Call a Bitrix REST method via an inbound webhook.

        ``webhook_url`` should look like ``https://b24-xxx.bitrix24.ru/rest/1/secret/``.
        """
        base = webhook_url.rstrip("/")
        url = f"{base}/{method}.json"
        form = data or {}

        try:
            log.info(
                "bitrix_webhook_request",
                extra={
                    "method": method,
                    "url": url.rsplit("/rest/", 1)[0] + "/rest/***/",
                    "body": _redact_form({k: ("" if v is None else str(v)) for k, v in form.items()}),
                },
            )
        except Exception:
            pass

        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                r = await self._client.post(url, data=form)

                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=r.request, response=r)

                payload = r.json() if r.text else {}
                if isinstance(payload, dict) and payload.get("error"):
                    raise BitrixError(f"{payload.get('error')}: {payload.get('error_description')}")

                return payload if isinstance(payload, dict) else {"result": payload}
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, BitrixError) as e:
                last_err = e
                sleep_s = min(2.0 ** (attempt - 1), 8.0)
                log.warning(
                    "bitrix_call_retry",
                    extra={"method": method, "attempt": attempt, "sleep_s": sleep_s, "error": str(e)},
                )
                await asyncio.sleep(sleep_s)

        raise BitrixError(f"Bitrix webhook call failed after retries: {method}: {last_err}")
