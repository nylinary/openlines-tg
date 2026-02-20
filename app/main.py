from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request

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


def _parse_nested_form(form: Dict[str, str]) -> Dict[str, Any]:
    """Parse flat form keys like ``data[PARAMS][DIALOG_ID]`` into a nested dict.

    Bitrix sends bot events as ``application/x-www-form-urlencoded`` with keys
    such as ``data[PARAMS][DIALOG_ID]=123``.  This helper reconstructs the
    nested structure so the rest of the code can work with a normal dict.
    """
    import re

    result: Dict[str, Any] = {}
    key_re = re.compile(r"\[([^\]]*)\]")

    for raw_key, value in form.items():
        # Split "data[PARAMS][DIALOG_ID]" → root="data", parts=["PARAMS","DIALOG_ID"]
        bracket_pos = raw_key.find("[")
        if bracket_pos == -1:
            result[raw_key] = value
            continue

        root = raw_key[:bracket_pos]
        parts = key_re.findall(raw_key)

        cur: Any = result
        keys = [root, *parts]
        for i, k in enumerate(keys[:-1]):
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = value

    return result


@app.post("/b24/imbot/events")
async def b24_imbot_events(
    request: Request,
    settings: Settings = Depends(settings_dep),
    bitrix: BitrixClient = Depends(bitrix_dep),
) -> Dict[str, str]:
    """Receive events from Bitrix for the UI-registered bot.

    Bitrix sends bot events as **form-encoded** POST
    (``application/x-www-form-urlencoded``), not JSON.

    Echoes incoming message text back via ``imbot.message.add``.
    """
    try:
        content_type = request.headers.get("content-type", "")

        if "json" in content_type:
            payload: Dict[str, Any] = await request.json()
        else:
            # form-encoded (the normal Bitrix bot event format)
            raw_form = await request.form()
            payload = _parse_nested_form(dict(raw_form))

        log.info("imbot_event_received", extra={"payload": payload})

        event = payload.get("event", "")

        # Extract params — Bitrix nests them under data.PARAMS
        data = payload.get("data") if isinstance(payload, dict) else None
        params = data.get("PARAMS") if isinstance(data, dict) else None

        dialog_id = params.get("DIALOG_ID") if isinstance(params, dict) else None
        message = params.get("MESSAGE") if isinstance(params, dict) else None

        # Bitrix also sends auth context — useful for logging
        auth = payload.get("auth") if isinstance(payload, dict) else None

        log.info(
            "imbot_event_parsed",
            extra={
                "event": event,
                "dialog_id": dialog_id,
                "message": message,
                "auth_application_token": (auth.get("application_token") if isinstance(auth, dict) else None),
            },
        )

        if not isinstance(dialog_id, str) or not dialog_id:
            log.info("imbot_event_ignored", extra={"reason": "no_dialog_id"})
            return {"ok": "true"}

        if not isinstance(message, str):
            message = ""

        text = message.strip()
        if not text:
            log.info("imbot_event_ignored", extra={"reason": "empty_message"})
            return {"ok": "true"}

        # Echo reply using imbot.message.add (requires BOT_ID + CLIENT_ID)
        try:
            resp = await bitrix.call(
                "imbot.message.add",
                {
                    "BOT_ID": settings.b24_imbot_id,
                    "CLIENT_ID": settings.b24_imbot_client_id,
                    "DIALOG_ID": dialog_id,
                    "MESSAGE": f"echo: {text}",
                },
            )
            log.info("imbot_echo_ok", extra={"dialog_id": dialog_id, "response": resp.get("result")})
        except Exception as e:
            log.warning("imbot_echo_failed", extra={"error": str(e), "dialog_id": dialog_id})

    except Exception as e:
        log.exception("imbot_event_handler_error", extra={"error": str(e)})

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
