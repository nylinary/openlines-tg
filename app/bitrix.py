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

    # --- File downloads ---

    async def download_file(self, url: str) -> bytes:
        """Download a file by URL (e.g. Bitrix disk file link).

        Bitrix file URLs may require auth.  If the URL is on the same
        domain, an access token query param is appended automatically.

        Bitrix disk download URLs often contain ``humanRE=1`` which causes
        the server to return an HTML wrapper page instead of raw bytes.
        We strip this parameter before downloading.
        """
        try:
            download_url = url

            # Strip humanRE=1 — it makes Bitrix return an HTML page
            # instead of the raw file content
            download_url = download_url.replace("&humanRE=1", "").replace("humanRE=1&", "").replace("?humanRE=1", "?")

            # If it's a Bitrix domain URL, ensure auth token is present.
            # Note: The AJAX download endpoint (/bitrix/services/main/ajax.php)
            # does NOT support REST API auth tokens — it requires session
            # cookies. The auth= parameter only works on /rest/ endpoints.
            # We still try it as a best-effort attempt; the fallback
            # download_file_by_id() via disk.file.get is more reliable.
            if self.domain in download_url and "auth=" not in download_url:
                try:
                    token = await self.ensure_token()
                    separator = "&" if "?" in download_url else "?"
                    download_url = f"{download_url}{separator}auth={token}"
                except Exception:
                    pass  # try without auth

            r = await self._client.get(download_url, follow_redirects=True)
            r.raise_for_status()

            content_type = r.headers.get("content-type", "")
            content_length = len(r.content)

            log.info("bitrix_file_downloaded", extra={
                "url": _redact_bitrix_url(download_url)[:120],
                "status": r.status_code,
                "content_type": content_type,
                "content_length": content_length,
            })

            # Bitrix may return an HTML page instead of the file
            # (e.g. login page, error page, humanRE wrapper)
            if "text/html" in content_type:
                log.warning("bitrix_file_download_html", extra={
                    "url": _redact_bitrix_url(download_url)[:120],
                    "content_length": content_length,
                    "body_preview": r.text[:300] if r.text else "",
                })
                raise BitrixError(
                    f"Bitrix returned HTML instead of file (content-type: {content_type})"
                )

            return r.content
        except BitrixError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
            log.error("bitrix_file_download_error", extra={
                "url": _redact_bitrix_url(url)[:120],
                "error": str(e),
            })
            raise BitrixError(f"File download failed: {e}") from e

    async def download_file_by_id(
        self,
        file_id: str,
        *,
        webhook_url: Optional[str] = None,
    ) -> bytes:
        """Download a Bitrix disk file by its ID using the REST API.

        Uses ``disk.file.get`` to retrieve file metadata including a
        direct download URL, then downloads the actual content.
        This is more reliable than using ``urlDownload`` from chat
        file attachments, which may return HTML wrapper pages.

        Args:
            file_id: Bitrix disk file ID.
            webhook_url: If provided, use inbound webhook instead of OAuth.
                         The webhook must have ``disk`` scope.
        """
        try:
            # Get file info with direct download link
            if webhook_url:
                resp = await self.call_webhook(webhook_url, "disk.file.get", {"id": file_id})
            else:
                resp = await self.call("disk.file.get", {"id": file_id})
            result = resp.get("result", {})
            if not isinstance(result, dict):
                raise BitrixError(f"disk.file.get returned unexpected result: {resp}")

            download_url = str(result.get("DOWNLOAD_URL", "") or "")
            if not download_url:
                raise BitrixError(f"disk.file.get returned no DOWNLOAD_URL for file {file_id}")

            log.info("bitrix_disk_file_get_ok", extra={
                "file_id": file_id,
                "name": result.get("NAME", ""),
                "size": result.get("SIZE", ""),
                "download_url": _redact_bitrix_url(download_url)[:120],
            })

            return await self.download_file(download_url)
        except BitrixError:
            raise
        except Exception as e:
            raise BitrixError(f"disk.file.get download failed for {file_id}: {e}") from e

    async def download_file_by_event_token(
        self,
        file_id: str,
        *,
        access_token: str,
        domain: str,
    ) -> bytes:
        """Download a Bitrix disk file using an access token from an event.

        Bitrix event payloads include an ``auth`` block with a fresh
        access token scoped to the app.  This method uses that token
        to call ``disk.file.get`` on the event's domain, then downloads
        the file via the returned ``DOWNLOAD_URL``.

        This is the most reliable way to download IM chat file attachments
        because:
        - The AJAX ``urlDownload`` requires browser session cookies.
        - The webhook may not have ``disk`` scope or file-level access.
        - The event token is scoped to the app that received the event
          and runs in the user context that has access to the chat file.
        """
        domain = domain.strip().replace("https://", "").replace("http://", "").rstrip("/")
        url = f"https://{domain}/rest/disk.file.get.json?auth={access_token}"

        try:
            log.info("bitrix_event_token_disk_file_get", extra={
                "file_id": file_id,
                "domain": domain,
            })

            r = await self._client.post(url, data={"id": file_id})
            payload = r.json() if r.text else {}

            if isinstance(payload, dict) and payload.get("error"):
                raise BitrixError(
                    f"disk.file.get via event token: "
                    f"{payload.get('error')}: {payload.get('error_description', '')}"
                )

            result = payload.get("result", {})
            if not isinstance(result, dict):
                raise BitrixError(f"disk.file.get via event token: unexpected result: {payload}")

            download_url = str(result.get("DOWNLOAD_URL", "") or "")
            if not download_url:
                raise BitrixError(
                    f"disk.file.get via event token: no DOWNLOAD_URL for file {file_id}"
                )

            log.info("bitrix_event_token_file_ok", extra={
                "file_id": file_id,
                "name": result.get("NAME", ""),
                "size": result.get("SIZE", ""),
                "download_url": _redact_bitrix_url(download_url)[:120],
            })

            # Download the actual file content from DOWNLOAD_URL
            r2 = await self._client.get(download_url, follow_redirects=True)
            r2.raise_for_status()

            content_type = r2.headers.get("content-type", "")
            if "text/html" in content_type:
                raise BitrixError(
                    f"DOWNLOAD_URL returned HTML (content-type: {content_type})"
                )

            return r2.content

        except BitrixError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
            raise BitrixError(f"Event token file download failed for {file_id}: {e}") from e

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

    async def call_webhook(
        self,
        webhook_url: str,
        method: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Call a Bitrix REST method via an inbound webhook (no OAuth needed).

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
