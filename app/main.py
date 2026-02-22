from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from .ai_chat import AIChatHandler
from .bitrix import BitrixClient, BitrixError, BitrixOAuthError
from .config import Settings, get_settings
from .database import Database
from .llm import LLMError, create_llm_provider
from .logging import setup_logging
from .scraper import ProductCatalog
from .speech import SpeechToText, SpeechToTextError, is_voice_file
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
            timeout_s=settings.llm_timeout_s,
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

    # --- Speech-to-text (voice message transcription) ---
    app.state.stt = None  # Optional[SpeechToText]
    if settings.stt_enabled and settings.openai_api_key:
        try:
            app.state.stt = SpeechToText(
                api_key=settings.openai_api_key,
                model=settings.stt_model,
                timeout_s=settings.llm_timeout_s,
            )
            log.info("stt_enabled", extra={"model": settings.stt_model, "language": settings.stt_language})
        except (ValueError, Exception) as e:
            log.warning("stt_disabled", extra={"error": str(e)})
    else:
        log.info("stt_disabled", extra={"reason": "STT_ENABLED=false or no API key"})

    # Verify OAuth token is available (non-fatal)
    try:
        await app.state.bitrix.ensure_token()
        log.info("bitrix_oauth_ok")
    except BitrixOAuthError:
        log.warning("bitrix_oauth_not_installed", extra={"hint": "open /b24/install to authorize app"})

    # --- Background scraper task ---
    app.state.scraper_task = asyncio.create_task(_scraper_loop(settings, app.state.catalog))

    # --- Background session watchdog (safety net for orphaned OL sessions) ---
    app.state.watchdog_task = asyncio.create_task(_session_watchdog(settings))

    log.info(
        "startup_complete",
        extra={
            "imbot_id": settings.b24_imbot_id,
            "imbot_code": settings.b24_imbot_code,
            "event_handler": settings.b24_imbot_event_handler,
            "ai_enabled": app.state.ai_chat is not None,
            "stt_enabled": app.state.stt is not None,
            "catalog_products": len(app.state.catalog.products),
        },
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    # Cancel background tasks
    for task_name in ("scraper_task", "watchdog_task"):
        task = getattr(app.state, task_name, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    for key in ("llm_provider", "stt", "bitrix", "storage", "db"):
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
            count = await catalog.full_scrape()
            log.info("scraper_initial_full_scrape_done", extra={
                "scraped": count,
                "in_memory": len(catalog.products),
            })
            # Invalidate AI prompt cache after scrape
            ai_chat = getattr(app.state, "ai_chat", None)
            if ai_chat:
                ai_chat.invalidate_system_prompt_cache()
    except Exception:
        log.exception("scraper_initial_error")

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


async def _session_watchdog(settings: Settings) -> None:
    """Background safety-net: auto-close orphaned transferred sessions.

    Scans sessions marked as ``transferred`` in Redis.  When a transferred
    session has been idle for >2 minutes (operator likely closed it or
    we missed the ONIMBOTDELETE event), marks it as ``closed``.
    The next ``ONIMBOTJOINCHAT`` event will clear history and start fresh.

    Note: ``imopenlines.session.list`` does NOT exist in Bitrix24 REST API,
    so we rely solely on Redis state + bot lifecycle events
    (ONIMBOTJOINCHAT / ONIMBOTDELETE) for session tracking.

    Interval: every 2 minutes.
    """
    await asyncio.sleep(30)  # let startup finish

    while True:
        try:
            storage: Storage = app.state.storage

            import time as _t
            now = _t.time()
            tracked = await storage.get_all_tracked_sessions()
            for chat_id, info in tracked.items():
                state = info.get("state", "")
                ts = float(info.get("ts", "0") or "0")

                # Session was transferred to operator >2 min ago and not yet
                # marked closed → operator may have closed it without us
                # getting an ONIMBOTDELETE event.
                if state == "transferred" and ts > 0 and (now - ts) > 120:
                    await storage.mark_session_closed(chat_id)
                    log.info("watchdog_transferred_to_closed", extra={
                        "chat_id": chat_id,
                        "age_s": round(now - ts),
                    })

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("watchdog_loop_error", extra={"error": str(e)})

        await asyncio.sleep(120)  # every 2 min


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


async def _do_transfer(
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

    try:
        resp = await _call_b24(bitrix, settings, "imopenlines.bot.session.operator", {
            "CHAT_ID": chat_id,
        })
        log.info("transfer_to_operator_ok", extra={"chat_id": chat_id, "response": resp.get("result")})
    except Exception as e:
        log.warning("transfer_to_operator_failed", extra={"error": str(e), "chat_id": chat_id})
        await _send_bot_message(bitrix, settings, dialog_id,
                                "Не удалось перевести на оператора. Попробуйте позже.")


# ---------------------------------------------------------------------------
# Voice message handling
# ---------------------------------------------------------------------------


async def _extract_voice_text(
    params: Dict[str, Any],
    message_id: str,
    dialog_id: str,
    bitrix: BitrixClient,
    settings: Settings,
) -> Optional[str]:
    """Try to extract and transcribe a voice message from the event params.

    Bitrix sends file attachments in ONIMBOTMESSAGEADD under the PARAMS
    as ``FILES`` — a dict mapping file indices to file info dicts, each
    containing ``type``, ``name``, ``urlDownload``, ``size``,
    ``viewerAttrs`` (with ``viewerType``), etc.

    If no voice file is found, returns None.
    If transcription succeeds, returns the transcribed text.
    If transcription fails, returns a fallback error string.
    """
    stt: Optional[SpeechToText] = getattr(app.state, "stt", None)
    if not stt:
        return None

    # Bitrix sends FILES as a nested dict: FILES[id][name], FILES[id][urlDownload], etc.
    files_raw = params.get("FILES")
    if not files_raw or not isinstance(files_raw, dict):
        return None

    # Find first audio/voice file
    voice_url: Optional[str] = None
    voice_name: str = "voice.ogg"
    voice_mime: str = ""

    # files_raw can be {"0": {"name": ..., "urlDownload": ..., "type": ...}, "1": {...}}
    file_entries = files_raw.values() if isinstance(files_raw, dict) else []
    for f in file_entries:
        if not isinstance(f, dict):
            continue
        fname = str(f.get("name", "") or "")
        ftype = str(f.get("type", "") or "")
        # Bitrix uses urlDownload / urlShow, not "link"
        flink = str(f.get("urlDownload", "") or f.get("urlShow", "") or f.get("link", "") or "")
        # Bitrix includes viewerAttrs.viewerType == "audio" for voice/audio
        viewer_attrs = f.get("viewerAttrs") or {}
        viewer_type = str(viewer_attrs.get("viewerType", "") or "") if isinstance(viewer_attrs, dict) else ""

        log.info("voice_file_candidate", extra={
            "dialog_id": dialog_id,
            "message_id": message_id,
            "file_name": fname,
            "mime_type": ftype,
            "viewer_type": viewer_type,
            "link": flink[:100] if flink else "",
        })

        if is_voice_file(mime_type=ftype, filename=fname, viewer_type=viewer_type):
            voice_url = flink
            voice_name = fname or "voice.ogg"
            voice_mime = ftype
            break

    if not voice_url:
        return None

    log.info("voice_message_detected", extra={
        "dialog_id": dialog_id,
        "message_id": message_id,
        "file_name": voice_name,
        "mime_type": voice_mime,
        "url": voice_url[:100],
    })

    # Download the audio file
    try:
        audio_bytes = await bitrix.download_file(voice_url)
    except (BitrixError, Exception) as e:
        log.warning("voice_download_failed", extra={
            "dialog_id": dialog_id,
            "error": str(e),
            "url": voice_url[:100],
        })
        return None

    if not audio_bytes:
        log.warning("voice_download_empty", extra={"dialog_id": dialog_id})
        return None

    log.info("voice_downloaded", extra={
        "dialog_id": dialog_id,
        "size_bytes": len(audio_bytes),
        "file_name": voice_name,
    })

    # Transcribe via Whisper
    try:
        transcript = await stt.transcribe(
            audio_bytes,
            filename=voice_name,
            language=settings.stt_language,
        )
    except SpeechToTextError as e:
        log.warning("voice_transcription_failed", extra={
            "dialog_id": dialog_id,
            "error": str(e),
        })
        return "[voice_error]"

    if not transcript or not transcript.strip():
        log.info("voice_transcription_empty", extra={"dialog_id": dialog_id})
        return "[voice_empty]"

    log.info("voice_transcribed", extra={
        "dialog_id": dialog_id,
        "text_length": len(transcript),
        "text_preview": transcript[:100],
    })

    return transcript.strip()


@app.post("/b24/imbot/events")
async def b24_imbot_events(
    request: Request,
    settings: Settings = Depends(settings_dep),
    bitrix: BitrixClient = Depends(bitrix_dep),
    ai_chat: Optional[AIChatHandler] = Depends(ai_chat_dep),
    storage: Storage = Depends(storage_dep),
) -> Dict[str, str]:
    """Receive events from Bitrix for the UI-registered bot.

    Bitrix sends bot events as ``application/x-www-form-urlencoded``.

    Handled events:

    * **ONIMBOTMESSAGEADD** — user sent a message → AI response
    * **ONIMBOTJOINCHAT**   — bot (re-)added to a chat.  Happens when:
      - a new OL session starts,
      - the client writes again after operator closed the previous session
        (if Open Line is configured with "assign chatbot on repeat message").
      We clear stale conversation history so the bot starts fresh.
    * **ONIMBOTDELETE**     — bot removed from a chat.  Happens when:
      - operator takes over (after our ``imopenlines.bot.session.operator``),
      - operator closes the dialog.
      We log the removal and update session state.
    """
    try:
        content_type = request.headers.get("content-type", "")

        if "json" in content_type:
            payload: Dict[str, Any] = await request.json()
        else:
            raw_form = await request.form()
            payload = _parse_nested_form(dict(raw_form))

        log.info("imbot_event_received", extra={"payload": payload})

        event = str(payload.get("event", "")).upper()

        # Extract params — Bitrix nests them under data.PARAMS
        data = payload.get("data") if isinstance(payload, dict) else None
        params = data.get("PARAMS") if isinstance(data, dict) else None

        dialog_id = params.get("DIALOG_ID") if isinstance(params, dict) else None
        message = params.get("MESSAGE") if isinstance(params, dict) else None
        message_id = str(params.get("MESSAGE_ID", "") or "") if isinstance(params, dict) else ""
        chat_id = params.get("TO_CHAT_ID") if isinstance(params, dict) else None
        # ONIMBOTJOINCHAT sends CHAT_ID at top-level PARAMS
        if not chat_id:
            chat_id = params.get("CHAT_ID") if isinstance(params, dict) else None

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

        # ----- ONIMBOTJOINCHAT: bot (re-)joined a chat -----
        if event == "ONIMBOTJOINCHAT":
            await _handle_bot_join(storage, settings, bitrix, dialog_id, chat_id)
            return {"ok": "true"}

        # ----- ONIMBOTDELETE: bot removed from a chat -----
        if event == "ONIMBOTDELETE":
            await _handle_bot_delete(storage, dialog_id, chat_id)
            return {"ok": "true"}

        # ----- ONIMBOTMESSAGEADD: user sent a message -----
        if not isinstance(dialog_id, str) or not dialog_id:
            log.info("imbot_event_ignored", extra={"reason": "no_dialog_id"})
            return {"ok": "true"}

        if not isinstance(message, str):
            message = ""

        text = message.strip()

        # --- Voice message handling ---
        # If the message has file attachments, check for voice/audio files
        # and transcribe them.  The transcription replaces or supplements
        # the text body (which is usually empty for voice messages).
        voice_transcript: Optional[str] = None
        if isinstance(params, dict):
            voice_transcript = await _extract_voice_text(
                params, message_id, dialog_id, bitrix, settings,
            )

        if voice_transcript == "[voice_error]":
            # Transcription failed — notify user
            await _send_bot_message(
                bitrix, settings, dialog_id,
                "Не удалось распознать голосовое сообщение. "
                "Пожалуйста, напишите текстом или попробуйте записать ещё раз 🎤",
            )
            return {"ok": "true"}

        if voice_transcript == "[voice_empty]":
            await _send_bot_message(
                bitrix, settings, dialog_id,
                "Голосовое сообщение не содержит распознаваемой речи. "
                "Попробуйте ещё раз или напишите текстом 🎤",
            )
            return {"ok": "true"}

        if voice_transcript:
            # Use the transcription as the message text.
            # If there was also text in the message, combine them.
            if text:
                text = f"{text}\n\n(голосовое сообщение: {voice_transcript})"
            else:
                text = voice_transcript

            log.info("voice_used_as_text", extra={
                "dialog_id": dialog_id,
                "text_length": len(text),
            })

            # Send the recognised text back to the user so they see what
            # the bot "heard" (makes the conversation transparent).
            await _send_bot_message(
                bitrix, settings, dialog_id,
                f"🎤 Распознано: {voice_transcript}",
            )

        if not text:
            log.info("imbot_event_ignored", extra={"reason": "empty_message"})
            return {"ok": "true"}

        # Track that bot is active in this chat
        if chat_id:
            await storage.mark_session_active(chat_id)

        # --- AI-powered response (or echo fallback) ---
        if ai_chat is not None:
            reply_text, transfer = await ai_chat.handle_message(dialog_id, text)

            # Always send the reply (GPT writes a friendly message even on transfer)
            await _send_bot_message(bitrix, settings, dialog_id, reply_text)

            if transfer:
                # AI detected operator intent — hand off after sending the reply
                if chat_id:
                    await storage.mark_session_transferred(chat_id)
                await _do_transfer(bitrix, settings, dialog_id, chat_id)
        else:
            # No AI configured — fall back to keyword check + echo
            if text.lower() in ("оператор", "operator"):
                await _send_bot_message(bitrix, settings, dialog_id,
                                        "Перевожу вас на оператора, подождите... 👤")
                if chat_id:
                    await storage.mark_session_transferred(chat_id)
                await _do_transfer(bitrix, settings, dialog_id, chat_id)
            else:
                await _send_bot_message(bitrix, settings, dialog_id, f"echo: {text}")

    except Exception as e:
        log.exception("imbot_event_handler_error", extra={"error": str(e)})

    return {"ok": "true"}


# ---------------------------------------------------------------------------
# Bot lifecycle event handlers
# ---------------------------------------------------------------------------


async def _handle_bot_join(
    storage: Storage,
    settings: Settings,
    bitrix: BitrixClient,
    dialog_id: Optional[str],
    chat_id: Optional[str],
) -> None:
    """Handle ONIMBOTJOINCHAT — bot was (re-)added to a chat.

    This fires when:
    - A brand-new Open Line session starts (first client message).
    - The client writes again after the operator previously closed the dialog
      (Bitrix Open Line setting: "assign chatbot on repeat message").

    We check if this is a **returning** session (the bot was previously
    removed after a transfer) and clear stale conversation history so
    the AI starts fresh without context from the operator conversation.
    """
    log.info("bot_join_chat", extra={"dialog_id": dialog_id, "chat_id": chat_id})

    prev_state: Optional[str] = None
    if chat_id:
        prev_state = await storage.get_session_state(chat_id)
        await storage.set_session_info(
            chat_id,
            state="bot_active",
            dialog_id=dialog_id or "",
            line_id=str(settings.b24_openline_id) if settings.b24_openline_id else "",
        )

    # If the previous state was "transferred" or "closed", this is a
    # returning session after operator close → clear old AI history.
    if prev_state in ("transferred", "closed"):
        if dialog_id:
            await storage.clear_chat_history(dialog_id)
            log.info(
                "bot_rejoin_history_cleared",
                extra={
                    "dialog_id": dialog_id,
                    "chat_id": chat_id,
                    "prev_state": prev_state,
                },
            )


async def _handle_bot_delete(
    storage: Storage,
    dialog_id: Optional[str],
    chat_id: Optional[str],
) -> None:
    """Handle ONIMBOTDELETE — bot was removed from a chat.

    This fires when:
    - Our bot called ``imopenlines.bot.session.operator`` (transfer to operator).
    - The operator closed the dialog.
    - Session auto-closed by timeout.

    We mark the session as "closed" so that on the next ONIMBOTJOINCHAT
    we know to clear history and start fresh.
    """
    log.info("bot_delete_chat", extra={"dialog_id": dialog_id, "chat_id": chat_id})

    if chat_id:
        # Only overwrite if not already "transferred" (which we set ourselves)
        cur = await storage.get_session_state(chat_id)
        if cur != "transferred":
            await storage.mark_session_closed(chat_id)


# ---------------------------------------------------------------------------
# Open Line event webhook (via event.bind subscription)
# ---------------------------------------------------------------------------


@app.post("/b24/ol/events")
async def b24_ol_events(
    request: Request,
    settings: Settings = Depends(settings_dep),
    bitrix: BitrixClient = Depends(bitrix_dep),
    storage: Storage = Depends(storage_dep),
) -> Dict[str, str]:
    """Receive Open Line events subscribed via ``event.bind``.

    Listens for:
    * **ONOPENLINEMESSAGEDELETE** — fires when a session is closed/deleted.
    * **ONIMCOMMANDADD** etc.   — any other subscribed events.

    These events are separate from the bot handler events
    (ONIMBOTMESSAGEADD, ONIMBOTJOINCHAT, ONIMBOTDELETE) which come
    through ``/b24/imbot/events``.
    """
    try:
        content_type = request.headers.get("content-type", "")

        if "json" in content_type:
            payload: Dict[str, Any] = await request.json()
        else:
            raw_form = await request.form()
            payload = _parse_nested_form(dict(raw_form))

        event = str(payload.get("event", "")).upper()
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}

        log.info("ol_event_received", extra={
            "event": event,
            "data_keys": list(data.keys()) if data else [],
            "payload": payload,
        })

        # --- ONOPENLINEMESSAGEDELETE: session/message deleted (session close) ---
        if event == "ONOPENLINEMESSAGEDELETE":
            chat_id = str(data.get("CHAT_ID", "") or "")
            session_id = str(data.get("SESSION_ID", "") or "")
            operator_id = str(data.get("OPERATOR_ID", "") or "")

            log.info("ol_session_closed_event", extra={
                "chat_id": chat_id,
                "session_id": session_id,
                "operator_id": operator_id,
            })

            if chat_id:
                # Mark session as closed so next ONIMBOTJOINCHAT clears history
                await storage.mark_session_closed(chat_id)

    except Exception:
        log.exception("ol_event_handler_error")

    return {"ok": "true"}


# ---------------------------------------------------------------------------
# Event subscription management
# ---------------------------------------------------------------------------


@app.post("/b24/setup/events")
async def b24_setup_events(
    settings: Settings = Depends(settings_dep),
    bitrix: BitrixClient = Depends(bitrix_dep),
) -> Dict[str, Any]:
    """Subscribe to Bitrix24 events via ``event.bind``.

    Call this once after installing the app to register webhook handlers
    for Open Line events.  Idempotent — safe to call multiple times.

    Subscribes to:
    * ``ONOPENLINEMESSAGEDELETE`` — session closed / message deleted
    * ``ONOPENLINEMESSAGEADD``    — new OL message (for monitoring)
    """
    handler_url = settings.b24_ol_event_handler
    events_to_bind = [
        "ONOPENLINEMESSAGEDELETE",
        "ONOPENLINEMESSAGEADD",
    ]

    results: Dict[str, Any] = {}
    for event_name in events_to_bind:
        try:
            resp = await _call_b24(bitrix, settings, "event.bind", {
                "EVENT": event_name,
                "HANDLER": handler_url,
            })
            results[event_name] = {"ok": True, "result": resp.get("result")}
            log.info("event_bind_ok", extra={"event": event_name, "handler": handler_url})
        except Exception as e:
            results[event_name] = {"ok": False, "error": str(e)}
            log.warning("event_bind_failed", extra={"event": event_name, "error": str(e)})

    return {"handler_url": handler_url, "bindings": results}


@app.get("/b24/setup/events")
async def b24_list_events(
    settings: Settings = Depends(settings_dep),
    bitrix: BitrixClient = Depends(bitrix_dep),
) -> Dict[str, Any]:
    """List currently bound events via ``event.get``."""
    try:
        resp = await _call_b24(bitrix, settings, "event.get", {})
        return {"ok": True, "events": resp.get("result", [])}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.delete("/b24/setup/events")
async def b24_unbind_events(
    settings: Settings = Depends(settings_dep),
    bitrix: BitrixClient = Depends(bitrix_dep),
) -> Dict[str, Any]:
    """Unbind all event subscriptions for our handler URL."""
    handler_url = settings.b24_ol_event_handler
    events_to_unbind = [
        "ONOPENLINEMESSAGEDELETE",
        "ONOPENLINEMESSAGEADD",
    ]

    results: Dict[str, Any] = {}
    for event_name in events_to_unbind:
        try:
            resp = await _call_b24(bitrix, settings, "event.unbind", {
                "EVENT": event_name,
                "HANDLER": handler_url,
            })
            results[event_name] = {"ok": True, "result": resp.get("result")}
        except Exception as e:
            results[event_name] = {"ok": False, "error": str(e)}

    return {"handler_url": handler_url, "unbindings": results}


# ---------------------------------------------------------------------------
# Session debugging endpoint
# ---------------------------------------------------------------------------


@app.get("/b24/sessions")
async def b24_sessions(
    storage: Storage = Depends(storage_dep),
    settings: Settings = Depends(settings_dep),
) -> Dict[str, Any]:
    """Debug endpoint: list all tracked bot sessions from Redis."""
    import time as _t
    tracked = await storage.get_all_tracked_sessions()
    now = _t.time()

    sessions = []
    for chat_id, info in tracked.items():
        ts = float(info.get("ts", "0") or "0")
        sessions.append({
            "chat_id": chat_id,
            "state": info.get("state"),
            "dialog_id": info.get("dialog_id", ""),
            "user_id": info.get("user_id", ""),
            "line_id": info.get("line_id", ""),
            "age_s": round(now - ts) if ts else None,
        })

    # Sort by most recent first
    sessions.sort(key=lambda s: s.get("age_s") or 999999)

    return {
        "bot_id": settings.b24_imbot_id,
        "openline_id": settings.b24_openline_id,
        "tracked_sessions": len(sessions),
        "sessions": sessions,
    }


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
