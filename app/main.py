from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from .ai_chat import AIChatHandler
from .bitrix import BitrixClient, BitrixOAuthError
from .config import Settings, get_settings
from .database import Database
from .llm import LLMError, create_llm_provider
from .logging import setup_logging
from .scraper import ProductCatalog
from .storage import Storage

log = logging.getLogger("app")

app = FastAPI(title="b24-imbot-proxy", version="0.3.0")


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def _startup() -> None:
    settings = get_settings()
    setup_logging()

    app.state.settings = settings

    # --- PostgreSQL ---
    app.state.db = Database(settings.database_url)
    try:
        await app.state.db.connect()
        log.info("postgres_connected")
    except Exception as e:
        log.error("postgres_connect_failed", extra={"error": str(e)})
        app.state.db = None

    # --- Redis + Storage (with optional PG write-through) ---
    app.state.storage = Storage(settings.redis_url, db=app.state.db)

    app.state.bitrix = BitrixClient(
        domain=settings.b24_domain,
        client_id=settings.b24_client_id,
        client_secret=settings.b24_client_secret,
        redirect_uri=settings.b24_redirect_uri,
        storage=app.state.storage,
        timeout_s=settings.http_timeout_s,
        retries=settings.http_retries,
    )

    # --- Product catalog ---
    app.state.catalog = ProductCatalog(db=app.state.db)
    await app.state.catalog.load_from_db()

    # --- LLM provider (optional — gracefully degrade if not configured) ---
    app.state.llm_provider = None  # Optional[LLMProvider]
    app.state.ai_chat = None  # Optional[AIChatHandler]

    try:
        llm = create_llm_provider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            timeout_s=settings.http_timeout_s,
        )
        app.state.llm_provider = llm
        app.state.ai_chat = AIChatHandler(
            llm=llm,
            catalog=app.state.catalog,
            storage=app.state.storage,
        )
        log.info("llm_provider_enabled", extra={"provider": llm.provider_name})
    except (ValueError, LLMError) as e:
        log.warning("llm_provider_disabled", extra={"error": str(e), "hint": "check OPENAI_API_KEY and credentials"})

    # Verify OAuth token is available (non-fatal)
    try:
        await app.state.bitrix.ensure_token()
        log.info("bitrix_oauth_ok")
    except BitrixOAuthError:
        log.warning("bitrix_oauth_not_installed", extra={"hint": "open /b24/install to authorize app"})

    # --- Background scraper task ---
    # app.state.scraper_task = asyncio.create_task(_scraper_loop(settings, app.state.catalog))

    log.info(
        "startup_complete",
        extra={
            "imbot_id": settings.b24_imbot_id,
            "imbot_code": settings.b24_imbot_code,
            "event_handler": settings.b24_imbot_event_handler,
            "ai_enabled": app.state.ai_chat is not None,
            "catalog_products": len(app.state.catalog.products),
        },
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    # Cancel background scraper
    task = getattr(app.state, "scraper_task", None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    for key in ("llm_provider", "bitrix", "storage", "db"):
        obj = getattr(app.state, key, None)
        if obj:
            try:
                await obj.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Background scraper loop
# ---------------------------------------------------------------------------


async def _scraper_loop(settings: Settings, catalog: ProductCatalog) -> None:
    """Background task: full scrape daily, price/quantity refresh hourly."""
    import time as _time

    # Initial scrape if catalog is empty or stale
    await asyncio.sleep(5)  # let the app finish starting up
    try:
        if not catalog.products:
            log.info("scraper_initial_full_scrape")
            await catalog.full_scrape()
            # Invalidate AI prompt cache after scrape
            ai_chat = getattr(app.state, "ai_chat", None)
            if ai_chat:
                ai_chat.invalidate_system_prompt_cache()
    except Exception as e:
        log.error("scraper_initial_error", extra={"error": str(e)})

    while True:
        try:
            now = _time.time()

            # Full scrape if overdue
            if now - catalog.last_full_scrape >= settings.scraper_full_interval_s:
                log.info("scraper_full_scrape_start")
                await catalog.full_scrape()
                ai_chat = getattr(app.state, "ai_chat", None)
                if ai_chat:
                    ai_chat.invalidate_system_prompt_cache()
                log.info("scraper_full_scrape_done", extra={"products": len(catalog.products)})

            # Price refresh if overdue (and not just did a full scrape)
            elif now - catalog.last_price_refresh >= settings.scraper_price_interval_s:
                log.info("scraper_price_refresh_start")
                await catalog.refresh_prices()
                ai_chat = getattr(app.state, "ai_chat", None)
                if ai_chat:
                    ai_chat.invalidate_system_prompt_cache()
                log.info("scraper_price_refresh_done", extra={"products": len(catalog.products)})

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("scraper_loop_error", extra={"error": str(e)})

        # Sleep for a short interval, then re-check
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def settings_dep() -> Settings:
    return app.state.settings


def storage_dep() -> Storage:
    return app.state.storage


def bitrix_dep() -> BitrixClient:
    return app.state.bitrix


def ai_chat_dep() -> Optional[AIChatHandler]:
    return getattr(app.state, "ai_chat", None)


def catalog_dep() -> ProductCatalog:
    return app.state.catalog


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


async def _call_b24(
    bitrix: BitrixClient,
    settings: Settings,
    method: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Call a Bitrix REST method via webhook (preferred) or OAuth fallback."""
    if settings.b24_webhook_url:
        return await bitrix.call_webhook(settings.b24_webhook_url, method, params)
    return await bitrix.call(method, params)


async def _send_bot_message(
    bitrix: BitrixClient,
    settings: Settings,
    dialog_id: str,
    text: str,
) -> None:
    """Send a message from the bot to a dialog."""
    try:
        resp = await _call_b24(bitrix, settings, "imbot.message.add", {
            "BOT_ID": settings.b24_imbot_id,
            "CLIENT_ID": settings.b24_imbot_client_id,
            "DIALOG_ID": dialog_id,
            "MESSAGE": text,
        })
        log.info("imbot_msg_ok", extra={"dialog_id": dialog_id, "response": resp.get("result")})
    except Exception as e:
        log.warning("imbot_msg_failed", extra={"error": str(e), "dialog_id": dialog_id})


async def _transfer_to_operator(
    bitrix: BitrixClient,
    settings: Settings,
    dialog_id: str,
    chat_id: Optional[str],
) -> None:
    """Transfer the conversation to a free human operator.

    Uses ``imopenlines.bot.session.operator`` which requires ``CHAT_ID``
    (the internal openlines chat id, sent by Bitrix as ``TO_CHAT_ID``).
    """
    if not chat_id:
        log.warning("transfer_no_chat_id", extra={"dialog_id": dialog_id})
        await _send_bot_message(bitrix, settings, dialog_id,
                                "Не удалось перевести на оператора — отсутствует идентификатор чата.")
        return

    # Notify user first
    await _send_bot_message(bitrix, settings, dialog_id,
                            "Перевожу вас на оператора, подождите...")

    try:
        resp = await _call_b24(bitrix, settings, "imopenlines.bot.session.operator", {
            "CHAT_ID": chat_id,
        })
        log.info("transfer_to_operator_ok", extra={"chat_id": chat_id, "response": resp.get("result")})
    except Exception as e:
        log.warning("transfer_to_operator_failed", extra={"error": str(e), "chat_id": chat_id})
        await _send_bot_message(bitrix, settings, dialog_id,
                                "Не удалось перевести на оператора. Попробуйте позже.")


@app.post("/b24/imbot/events")
async def b24_imbot_events(
    request: Request,
    settings: Settings = Depends(settings_dep),
    bitrix: BitrixClient = Depends(bitrix_dep),
    ai_chat: Optional[AIChatHandler] = Depends(ai_chat_dep),
) -> Dict[str, str]:
    """Receive events from Bitrix for the UI-registered bot.

    Bitrix sends bot events as **form-encoded** POST
    (``application/x-www-form-urlencoded``), not JSON.

    Routes messages through the AI chat handler (if configured)
    or falls back to echo mode.
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
        chat_id = params.get("TO_CHAT_ID") if isinstance(params, dict) else None

        # Bitrix also sends auth context — useful for logging
        auth = payload.get("auth") if isinstance(payload, dict) else None

        log.info(
            "imbot_event_parsed",
            extra={
                "event": event,
                "dialog_id": dialog_id,
                "chat_id": chat_id,
                "msg_text": message,
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

        # --- AI-powered response (or echo fallback) ---
        if ai_chat is not None:
            reply_text, transfer = await ai_chat.handle_message(dialog_id, text)

            if transfer:
                # AI detected operator request — transfer
                await _transfer_to_operator(bitrix, settings, dialog_id, chat_id)
            else:
                await _send_bot_message(bitrix, settings, dialog_id, reply_text)
        else:
            # No AI configured — fall back to keyword check + echo
            if text.lower() in ("оператор", "operator"):
                await _transfer_to_operator(bitrix, settings, dialog_id, chat_id)
            else:
                await _send_bot_message(bitrix, settings, dialog_id, f"echo: {text}")

    except Exception as e:
        log.exception("imbot_event_handler_error", extra={"error": str(e)})

    return {"ok": "true"}


# ---------------------------------------------------------------------------
# Catalog / scraper management endpoints
# ---------------------------------------------------------------------------


@app.get("/catalog/stats")
async def catalog_stats(
    catalog: ProductCatalog = Depends(catalog_dep),
) -> Dict[str, Any]:
    """Return current catalog statistics."""
    import time as _time

    by_cat: Dict[str, int] = {}
    for p in catalog.products:
        cat = p.get("category", "unknown")
        by_cat[cat] = by_cat.get(cat, 0) + 1

    return {
        "total_products": len(catalog.products),
        "by_category": by_cat,
        "last_full_scrape": catalog.last_full_scrape,
        "last_full_scrape_ago_s": round(_time.time() - catalog.last_full_scrape) if catalog.last_full_scrape else None,
        "last_price_refresh": catalog.last_price_refresh,
        "last_price_refresh_ago_s": round(_time.time() - catalog.last_price_refresh) if catalog.last_price_refresh else None,
    }


@app.post("/catalog/scrape")
async def catalog_scrape(
    catalog: ProductCatalog = Depends(catalog_dep),
) -> Dict[str, Any]:
    """Trigger a full product scrape (manual)."""
    count = await catalog.full_scrape()
    ai_chat = getattr(app.state, "ai_chat", None)
    if ai_chat:
        ai_chat.invalidate_system_prompt_cache()
    return {"ok": "true", "products_scraped": count}


@app.get("/catalog/search")
async def catalog_search(
    q: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=50),
    catalog: ProductCatalog = Depends(catalog_dep),
) -> Dict[str, Any]:
    """Search the product catalog."""
    results = catalog.search(q, limit=limit)
    return {"query": q, "count": len(results), "products": results}


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
