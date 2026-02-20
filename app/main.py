from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query

from .bitrix import BitrixClient, BitrixOAuthError
from .config import Settings, get_settings
from .logging import setup_logging
from .storage import Storage

log = logging.getLogger("app")

app = FastAPI(title="b24-imbot-proxy", version="0.2.0")


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def _startup() -> None:
    settings = get_settings()
    setup_logging()

    app.state.settings = settings
    app.state.storage = Storage(settings.redis_url)

    app.state.bitrix = BitrixClient(
        domain=settings.b24_domain,
        client_id=settings.b24_client_id,
        client_secret=settings.b24_client_secret,
        redirect_uri=settings.b24_redirect_uri,
        storage=app.state.storage,
        timeout_s=settings.http_timeout_s,
        retries=settings.http_retries,
    )

    # Verify OAuth token is available (non-fatal)
    try:
        await app.state.bitrix.ensure_token()
        log.info("bitrix_oauth_ok")
    except BitrixOAuthError:
        log.warning("bitrix_oauth_not_installed", extra={"hint": "open /b24/install to authorize app"})

    log.info(
        "startup_complete",
        extra={
            "imbot_id": settings.b24_imbot_id,
            "imbot_code": settings.b24_imbot_code,
            "event_handler": settings.b24_imbot_event_handler,
        },
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    for key in ("bitrix", "storage"):
        obj = getattr(app.state, key, None)
        if obj:
            try:
                await obj.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def settings_dep() -> Settings:
    return app.state.settings


def storage_dep() -> Storage:
    return app.state.storage


def bitrix_dep() -> BitrixClient:
    return app.state.bitrix


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"ok": "true"}


# ---------------------------------------------------------------------------
# Bitrix imbot event handler
# ---------------------------------------------------------------------------


@app.post("/b24/imbot/events")
async def b24_imbot_events(
    payload: Dict[str, Any],
    settings: Settings = Depends(settings_dep),
    bitrix: BitrixClient = Depends(bitrix_dep),
) -> Dict[str, str]:
    """Receive events from Bitrix for the UI-registered bot.

    Echoes incoming message text back to the same dialog via ``im.message.add``.
    """
    log.info("imbot_event_received", extra={"payload": payload})

    # Typical event payload: data.PARAMS.DIALOG_ID + data.PARAMS.MESSAGE
    data = payload.get("data") if isinstance(payload, dict) else None
    params = data.get("PARAMS") if isinstance(data, dict) else None

    dialog_id = params.get("DIALOG_ID") if isinstance(params, dict) else None
    message = params.get("MESSAGE") if isinstance(params, dict) else None

    if not isinstance(dialog_id, str) or not dialog_id:
        log.info("imbot_event_ignored", extra={"reason": "no_dialog_id"})
        return {"ok": "true"}

    if not isinstance(message, str):
        message = ""

    text = message.strip()
    if not text:
        log.info("imbot_event_ignored", extra={"reason": "empty_message"})
        return {"ok": "true"}

    # Echo reply
    try:
        resp = await bitrix.call(
            "im.message.add",
            {"DIALOG_ID": dialog_id, "MESSAGE": f"echo: {text}"},
        )
        log.info("imbot_echo_ok", extra={"dialog_id": dialog_id, "response": resp.get("result")})
    except Exception as e:
        log.warning("imbot_echo_failed", extra={"error": str(e), "dialog_id": dialog_id})

    return {"ok": "true"}


# ---------------------------------------------------------------------------
# Bitrix OAuth install flow
# ---------------------------------------------------------------------------


@app.get("/b24/install")
async def b24_install(
    storage: Storage = Depends(storage_dep),
    settings: Settings = Depends(settings_dep),
) -> Dict[str, str]:
    """Generate Bitrix OAuth authorization URL."""
    state = secrets.token_urlsafe(24)
    await storage.dedupe_set(f"b24:oauth:state:{state}", ttl_s=10 * 60)

    url = app.state.bitrix.auth_url(state=state)
    return {"auth_url": url}


@app.get("/b24/oauth/callback")
async def b24_oauth_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    storage: Storage = Depends(storage_dep),
) -> Dict[str, str]:
    """Handle Bitrix OAuth redirect with authorization code."""
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code/state")

    fresh = await storage.dedupe_set(f"b24:oauth:state_used:{state}", ttl_s=10 * 60)
    if not fresh:
        raise HTTPException(status_code=400, detail="state already used")

    try:
        await app.state.bitrix.exchange_code(code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"oauth exchange failed: {e}")

    return {"ok": "true"}
