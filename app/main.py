from __future__ import annotations

import asyncio
import secrets
import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from .bitrix import BitrixClient, BitrixError, BitrixOAuthError
from .config import Settings, get_settings
from .logging import setup_logging
from .schemas import BitrixEventEnvelope, TelegramUpdate
from .storage import Storage
from .telegram import TelegramClient, TelegramError

log = logging.getLogger("app")

app = FastAPI(title="tg-b24-openlines-proxy", version="0.1.0")


def _tg_names(update: TelegramUpdate) -> str:
    msg = update.message
    if not msg or not msg.from_user:
        return ""
    first = msg.from_user.first_name or ""
    last = msg.from_user.last_name or ""
    return (first + " " + last).strip()


def _build_b24_message(update: TelegramUpdate) -> Tuple[str, str, str, Dict[str, Any]]:
    assert update.message and update.message.text is not None

    tg_user_id = str(update.message.from_user.id) if update.message.from_user else "0"
    tg_chat_id = str(update.message.chat.id)
    tg_message_id = str(update.message.message_id)

    external_user_id = f"tg:{tg_user_id}"
    external_chat_id = f"tg:{tg_chat_id}"
    external_message_id = f"tg:{tg_chat_id}:{tg_message_id}"

    user_name = _tg_names(update)

    msg = {
        "user": {"id": external_user_id, "name": user_name or external_user_id},
        "chat": {"id": external_chat_id, "name": external_chat_id},
        "message": {"id": external_message_id, "text": update.message.text},
    }
    return tg_chat_id, external_chat_id, external_message_id, msg


async def _process_tg_update(settings: Settings, storage: Storage, bitrix: BitrixClient, update: TelegramUpdate) -> None:
    if not update.message or update.message.text is None:
        log.info("tg_update_ignored", extra={"update_id": update.update_id})
        return

    # Dedupe (strict idempotency)
    dedupe_key = f"dedupe:tg:update_id:{update.update_id}"
    fresh = await storage.dedupe_set(dedupe_key, ttl_s=48 * 3600)
    if not fresh:
        log.info("tg_update_duplicate", extra={"update_id": update.update_id})
        return

    tg_chat_id, external_chat_id, external_message_id, b24_msg = _build_b24_message(update)

    # store mapping used
    await storage.map_external_to_tg_chat(external_chat_id, tg_chat_id)
    await storage.map_tg_chat_to_external_chat(tg_chat_id, external_chat_id)

    try:
        resp = await bitrix.send_messages(settings.connector_code, settings.b24_line_id, [b24_msg])
        log.info(
            "b24_send_messages_ok",
            extra={
                "external_chat_id": external_chat_id,
                "external_message_id": external_message_id,
                "bitrix": resp.get("result"),
            },
        )
    except BitrixError as e:
        log.error(
            "b24_send_messages_failed",
            extra={"error": str(e), "external_chat_id": external_chat_id, "request": b24_msg},
        )


async def _tg_polling_loop(settings: Settings, storage: Storage, bitrix: BitrixClient, telegram: TelegramClient) -> None:
    # Disable webhook so Telegram starts delivering updates via polling.
    try:
        await telegram.delete_webhook(drop_pending_updates=False)
        log.info("tg_delete_webhook_ok")
    except Exception as e:
        log.warning("tg_delete_webhook_failed", extra={"error": str(e)})

    offset: Optional[int] = None
    while True:
        try:
            resp = await telegram.get_updates(offset=offset, timeout=25, allowed_updates=["message"])  # type: ignore[arg-type]
            result = resp.get("result")
            if isinstance(result, list):
                for item in result:
                    try:
                        upd = TelegramUpdate.model_validate(item)
                    except Exception as e:
                        log.warning("tg_update_parse_failed", extra={"error": str(e), "item": item})
                        continue

                    await _process_tg_update(settings, storage, bitrix, upd)
                    offset = upd.update_id + 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("tg_polling_error", extra={"error": str(e)})
            await asyncio.sleep(2.0)

        await asyncio.sleep(max(settings.tg_poll_interval_s, 0.0))


def _extract_b24_outgoing(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Best-effort parser for Bitrix connector callbacks.

    Returns {external_chat_id, text, message_id?}
    """
    candidates = []

    # Common nesting patterns
    for root in (payload, payload.get("data"), payload.get("payload"), payload.get("result")):
        if isinstance(root, dict):
            candidates.append(root)

    def pick(d: Dict[str, Any], keys: list[str]) -> Optional[Any]:
        cur: Any = d
        for k in keys:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur

    for d in candidates:
        text = (
            pick(d, ["message", "text"])
            or pick(d, ["MESSAGE", "text"])
            or pick(d, ["message", "MESSAGE"])
            or d.get("text")
            or d.get("MESSAGE")
        )
        chat_id = (
            pick(d, ["chat", "id"])
            or d.get("chat_id")
            or d.get("CHAT_ID")
            or d.get("dialog_id")
            or d.get("DIALOG_ID")
        )
        msg_id = pick(d, ["message", "id"]) or d.get("message_id") or d.get("MESSAGE_ID")

        if isinstance(text, str) and text and isinstance(chat_id, str) and chat_id:
            out: Dict[str, str] = {"external_chat_id": chat_id, "text": text}
            if isinstance(msg_id, str) and msg_id:
                out["message_id"] = msg_id
            return out

    return None


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
        connector_hash=settings.b24_connector_hash,
    )
    app.state.telegram = TelegramClient(settings.tg_bot_token, timeout_s=settings.http_timeout_s, retries=settings.http_retries)

    # Register/activate only when OAuth token is installed.
    try:
        await app.state.bitrix.ensure_token()

        # imbot register/update for echo testing (best-effort)
        if settings.b24_imbot_event_handler:
            try:
                resp = await app.state.bitrix.imbot_register(
                    code=settings.b24_imbot_code,
                    name=settings.b24_imbot_name,
                    event_handler=settings.b24_imbot_event_handler,
                    openline="Y",
                )
                bot_id = str((resp.get("result") or {}).get("BOT_ID") or "")
                if bot_id:
                    log.info("b24_imbot_registered", extra={"bot_id": bot_id, "code": settings.b24_imbot_code})
                else:
                    log.warning("b24_imbot_register_no_bot_id", extra={"response": resp})
            except Exception as e:
                log.warning(
                    "b24_imbot_register_failed",
                    extra={"error": str(e), "event_handler": settings.b24_imbot_event_handler, "code": settings.b24_imbot_code},
                )
        else:
            log.info("b24_imbot_skipped", extra={"reason": "B24_IMBOT_EVENT_HANDLER not set"})

        try:
            await app.state.bitrix.register(settings.connector_code)
            log.info("bitrix_connector_registered", extra={"connector": settings.connector_code})
        except Exception as e:
            log.warning("bitrix_register_failed", extra={"error": str(e)})

        try:
            await app.state.bitrix.activate(settings.connector_code, settings.b24_line_id)
            log.info(
                "bitrix_connector_activated",
                extra={"connector": settings.connector_code, "line": settings.b24_line_id},
            )
        except Exception as e:
            log.warning("bitrix_activate_failed", extra={"error": str(e)})
    except BitrixOAuthError:
        log.warning("bitrix_oauth_not_installed", extra={"hint": "open /b24/install to authorize app"})

    if settings.tg_use_polling:
        app.state.tg_polling_task = asyncio.create_task(
            _tg_polling_loop(settings, app.state.storage, app.state.bitrix, app.state.telegram)
        )
        log.info("tg_polling_started", extra={"interval_s": settings.tg_poll_interval_s})


@app.on_event("shutdown")
async def _shutdown() -> None:
    task = getattr(app.state, "tg_polling_task", None)
    if task:
        task.cancel()
        try:
            await task
        except Exception:
            pass

    for key in ("telegram", "bitrix", "storage"):
        obj = getattr(app.state, key, None)
        if obj:
            try:
                await obj.close()
            except Exception:
                pass


def settings_dep() -> Settings:
    return app.state.settings


def storage_dep() -> Storage:
    return app.state.storage


def bitrix_dep() -> BitrixClient:
    return app.state.bitrix


def telegram_dep() -> TelegramClient:
    return app.state.telegram


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"ok": "true"}


@app.post("/b24/handler")
async def b24_handler(
    envelope: BitrixEventEnvelope,
    x_b24_handler_secret: Optional[str] = Header(default=None, alias="X-B24-Handler-Secret"),
    secret: Optional[str] = Query(default=None),
    settings: Settings = Depends(settings_dep),
    storage: Storage = Depends(storage_dep),
    bitrix: BitrixClient = Depends(bitrix_dep),
    telegram: TelegramClient = Depends(telegram_dep),
) -> Dict[str, str]:
    # Secret check
    if (x_b24_handler_secret or secret) != settings.b24_handler_secret:
        raise HTTPException(status_code=401, detail="invalid bitrix handler secret")

    raw = envelope.model_dump()
    log.info("b24_event_received", extra={"payload": raw})

    extracted = _extract_b24_outgoing(raw)
    if not extracted:
        log.info("b24_event_unhandled", extra={"reason": "could_not_extract"})
        return {"ok": "true"}

    external_chat_id = extracted["external_chat_id"]
    text = extracted["text"]
    message_id = extracted.get("message_id") or ""

    # Dedupe (event-level)
    dkey = f"dedupe:b24:{external_chat_id}:{message_id or hash(text)}"
    fresh = await storage.dedupe_set(dkey, ttl_s=48 * 3600)
    if not fresh:
        log.info("b24_event_duplicate", extra={"external_chat_id": external_chat_id, "message_id": message_id})
        return {"ok": "true"}

    tg_chat_id = await storage.get_tg_chat_by_external_chat(external_chat_id)
    if not tg_chat_id:
        log.warning("no_tg_chat_mapping", extra={"external_chat_id": external_chat_id})
        return {"ok": "true"}

    try:
        tg_resp = await telegram.send_message(tg_chat_id, text)
        log.info("tg_send_ok", extra={"external_chat_id": external_chat_id, "tg": tg_resp.get("result")})
    except TelegramError as e:
        log.error("tg_send_failed", extra={"error": str(e), "tg_chat_id": tg_chat_id})
        return {"ok": "true"}

    # Best-effort statuses
    try:
        await bitrix.send_status_delivery(settings.connector_code, settings.b24_line_id, external_chat_id, message_id)
    except Exception as e:
        log.warning("b24_status_delivery_failed", extra={"error": str(e)})
    try:
        await bitrix.send_status_reading(settings.connector_code, settings.b24_line_id, external_chat_id, message_id)
    except Exception as e:
        log.warning("b24_status_reading_failed", extra={"error": str(e)})

    return {"ok": "true"}


# --- Bitrix OAuth install flow ---

@app.get("/b24/install")
async def b24_install(storage: Storage = Depends(storage_dep), settings: Settings = Depends(settings_dep)) -> Dict[str, str]:
    # Generate one-time state token for CSRF protection
    state = secrets.token_urlsafe(24)
    await storage.dedupe_set(f"b24:oauth:state:{state}", ttl_s=10 * 60)

    url = app.state.bitrix.auth_url(state=state)
    return {"auth_url": url}


@app.get("/b24/oauth/callback")
async def b24_oauth_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    settings: Settings = Depends(settings_dep),
    storage: Storage = Depends(storage_dep),
) -> Dict[str, str]:
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code/state")

    # Validate state (best-effort)
    fresh = await storage.dedupe_set(f"b24:oauth:state_used:{state}", ttl_s=10 * 60)
    if not fresh:
        raise HTTPException(status_code=400, detail="state already used")

    try:
        await app.state.bitrix.exchange_code(code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"oauth exchange failed: {e}")

    # After install, attempt connector registration/activation immediately.
    try:
        await app.state.bitrix.register(settings.connector_code)
        await app.state.bitrix.activate(settings.connector_code, settings.b24_line_id)
    except Exception as e:
        log.warning("bitrix_post_install_register_failed", extra={"error": str(e)})

    return {"ok": "true"}


@app.post("/b24/imbot/events")
async def b24_imbot_events(
    payload: Dict[str, Any],
    settings: Settings = Depends(settings_dep),
    bitrix: BitrixClient = Depends(bitrix_dep),
) -> Dict[str, str]:
    """Bitrix imbot event handler.

    For testing: echoes incoming message text back to the same dialog.

    Configure Bitrix bot EVENT_HANDLER to point here.
    """
    # Typical event payload contains `data[PARAMS][DIALOG_ID]` + `data[PARAMS][MESSAGE]`.
    data = payload.get("data") if isinstance(payload, dict) else None
    params = data.get("PARAMS") if isinstance(data, dict) else None

    dialog_id = params.get("DIALOG_ID") if isinstance(params, dict) else None
    message = params.get("MESSAGE") if isinstance(params, dict) else None

    if not isinstance(dialog_id, str) or not dialog_id:
        log.info("imbot_event_ignored", extra={"reason": "no_dialog_id"})
        return {"ok": "true"}

    if not isinstance(message, str):
        message = ""

    # Avoid echo loops for empty/system messages
    text = message.strip()
    if not text:
        return {"ok": "true"}

    try:
        await bitrix.call("im.message.add", {"DIALOG_ID": dialog_id, "MESSAGE": f"echo: {text}"})
    except Exception as e:
        log.warning("imbot_echo_failed", extra={"error": str(e)})

    return {"ok": "true"}
