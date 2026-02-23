#!/usr/bin/env python3
"""
Real-data integration test for the voice-message download pipeline.

Tests the exact flow used in production:
  1. im.disk.file.save  — copy chat-only file into webhook user's Disk
  2. disk.file.get      — get DOWNLOAD_URL for the saved disk file
  3. Download bytes     — GET the DOWNLOAD_URL
  4. Whisper STT        — transcribe the audio bytes

Usage (from project root):
    python -m test.test_voice_pipeline

    # Or with a specific file ID from the latest event logs:
    TEST_FILE_ID=118 python -m test.test_voice_pipeline

Requires:
    pip install httpx
    ../.env with B24_WEBHOOK_URL and OPENAI_API_KEY

How to get a fresh FILE_ID:
    1. Send a voice message from Telegram/WhatsApp to your Bitrix OpenLine.
    2. Check Railway logs for the `imbot_event_received` message.
    3. Find  data.PARAMS.FILES.<ID>.id  — that number is the FILE_ID.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


_env = _load_env(ENV_PATH)

# Config — override any of these via environment variables
B24_WEBHOOK_URL: str = os.environ.get("B24_WEBHOOK_URL", _env.get("B24_WEBHOOK_URL", ""))
OPENAI_API_KEY: str  = os.environ.get("OPENAI_API_KEY",  _env.get("OPENAI_API_KEY",  ""))
STT_MODEL: str       = os.environ.get("STT_MODEL",       _env.get("STT_MODEL",       "whisper-1"))
STT_LANGUAGE: str    = os.environ.get("STT_LANGUAGE",    _env.get("STT_LANGUAGE",    "ru"))

# The IM chat file ID from the ONIMBOTMESSAGEADD event (FILES[n].id).
# Example from last event log:  FILES["118"]["id"] == "118"
TEST_FILE_ID: str = "118"

# ---------------------------------------------------------------------------
# Terminal colours
# ---------------------------------------------------------------------------
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'='*60}\n  {title}\n{'='*60}{RESET}\n")

def ok(msg: str)   -> None: print(f"  {GREEN}✓ {msg}{RESET}")
def fail(msg: str) -> None: print(f"  {RED}✗ {msg}{RESET}")
def warn(msg: str) -> None: print(f"  {YELLOW}⚠ {msg}{RESET}")
def info(msg: str) -> None: print(f"  {msg}")


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

async def call_webhook(
    client: httpx.AsyncClient,
    webhook_url: str,
    method: str,
    data: dict | None = None,
) -> dict:
    """POST to Bitrix inbound webhook, return parsed JSON."""
    url = webhook_url.rstrip("/") + f"/{method}.json"
    r = await client.post(url, data=data or {})
    try:
        payload = r.json()
    except Exception:
        fail(f"{method}: non-JSON response  HTTP {r.status_code}")
        info(f"  Body: {r.text[:400]}")
        return {}

    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"{method}: {payload['error']}: {payload.get('error_description', '')}")

    return payload if isinstance(payload, dict) else {"result": payload}


# ---------------------------------------------------------------------------
# Step 0 — sanity-check configuration
# ---------------------------------------------------------------------------

def check_config() -> bool:
    header("Step 0: Configuration check")
    ok_flag = True

    if B24_WEBHOOK_URL:
        ok(f"B24_WEBHOOK_URL: {B24_WEBHOOK_URL.rsplit('/rest/', 1)[0]}/rest/***/")
    else:
        fail("B24_WEBHOOK_URL not set — add it to .env")
        ok_flag = False

    if OPENAI_API_KEY:
        ok(f"OPENAI_API_KEY: {OPENAI_API_KEY[:8]}...")
    else:
        warn("OPENAI_API_KEY not set — STT step will be skipped")

    if TEST_FILE_ID:
        ok(f"TEST_VOICE_FILE_ID: {TEST_FILE_ID}")
    else:
        fail(
            "TEST_VOICE_FILE_ID not set!\n"
            "  Add  TEST_VOICE_FILE_ID=<id>  to .env or export it.\n"
            "  How to find it:\n"
            "    1. Send a voice message to the bot in Bitrix\n"
            "    2. Check Railway logs for `imbot_event_received`\n"
            "    3. Look for: data.PARAMS.FILES.<key>.id"
        )
        ok_flag = False

    return ok_flag


# ---------------------------------------------------------------------------
# Step 1 — webhook connectivity
# ---------------------------------------------------------------------------

async def test_webhook(client: httpx.AsyncClient) -> bool:
    header("Step 1: Webhook connectivity (server.time)")
    try:
        payload = await call_webhook(client, B24_WEBHOOK_URL, "server.time")
        ts = payload.get("result", "?")
        ok(f"server.time OK — server timestamp: {ts}")
        return True
    except Exception as e:
        fail(f"Webhook not reachable: {e}")
        return False


# ---------------------------------------------------------------------------
# Step 2 — im.disk.file.save
# ---------------------------------------------------------------------------

async def test_im_disk_file_save(client: httpx.AsyncClient) -> str | None:
    header(f"Step 2: im.disk.file.save (FILE_ID={TEST_FILE_ID})")
    info("Copies the chat-only file into the webhook user's Disk storage.")
    info("Docs: https://apidocs.bitrix24.com/api-reference/chats/files/im-disk-file-save.html")

    try:
        payload = await call_webhook(client, B24_WEBHOOK_URL, "im.disk.file.save", {"FILE_ID": TEST_FILE_ID})
    except Exception as e:
        fail(f"im.disk.file.save failed: {e}")
        return None

    result = payload.get("result", {})
    info(f"Raw result: {str(result)[:400]}")

    # im.disk.file.save returns one of:
    #   {"folder": {"id": ...}, "file": {"id": 134, "name": "..."}}  ← current Bitrix
    #   {"ID": 134, "NAME": "..."}                                    ← older format
    #   134                                                            ← scalar
    disk_id: str | None = None
    if isinstance(result, dict):
        # New format: nested under "file"
        file_obj = result.get("file") or result.get("FILE") or {}
        if isinstance(file_obj, dict):
            disk_id = str(file_obj.get("id") or file_obj.get("ID") or "")
            if disk_id:
                ok(f"Disk file ID: {disk_id}  NAME={file_obj.get('name','?')}")
        # Old flat format
        if not disk_id:
            disk_id = str(result.get("ID") or result.get("id") or "")
            if disk_id:
                ok(f"Disk file ID: {disk_id}  NAME={result.get('NAME','?')}")
        if not disk_id:
            fail("Could not extract disk file ID from result — check raw result above")
    elif result:
        disk_id = str(result)
        ok(f"Disk file ID (scalar): {disk_id}")
    else:
        fail("Empty result from im.disk.file.save")

    return disk_id


# ---------------------------------------------------------------------------
# Step 3 — disk.file.get → DOWNLOAD_URL
# ---------------------------------------------------------------------------

async def test_disk_file_get(client: httpx.AsyncClient, disk_file_id: str) -> str | None:
    header(f"Step 3: disk.file.get (id={disk_file_id})")

    try:
        payload = await call_webhook(client, B24_WEBHOOK_URL, "disk.file.get", {"id": disk_file_id})
    except Exception as e:
        fail(f"disk.file.get failed: {e}")
        return None

    result = payload.get("result", {})
    if not isinstance(result, dict):
        fail(f"Unexpected result type: {type(result)}  value: {str(result)[:200]}")
        return None

    dl_url = str(result.get("DOWNLOAD_URL") or "")
    name   = result.get("NAME", "?")
    size   = result.get("SIZE", "?")

    if dl_url:
        ok(f"NAME={name}  SIZE={size}")
        ok(f"DOWNLOAD_URL: {dl_url[:90]}...")
    else:
        fail(f"No DOWNLOAD_URL in result: {str(result)[:300]}")

    return dl_url or None


# ---------------------------------------------------------------------------
# Step 4 — download audio bytes
# ---------------------------------------------------------------------------

async def test_download_audio(client: httpx.AsyncClient, dl_url: str) -> bytes | None:
    header("Step 4: Download audio bytes")

    try:
        r = await client.get(dl_url, follow_redirects=True)
    except Exception as e:
        fail(f"GET failed: {e}")
        return None

    ct   = r.headers.get("content-type", "unknown")
    size = len(r.content)
    info(f"HTTP {r.status_code}  content-type={ct}  size={size} bytes")

    if r.status_code >= 400:
        fail(f"HTTP error {r.status_code}")
        info(f"Body: {r.text[:300]}")
        return None

    if "text/html" in ct:
        fail("Got HTML instead of audio")
        info(f"Body preview: {r.text[:300]}")
        return None

    if size < 100:
        warn(f"File is suspiciously small ({size} bytes)")
        info(f"Body: {r.text[:200]}")
        return None

    magic = r.content[:4]
    if magic == b"OggS":
        ok(f"OGG magic bytes ✓  ({size} bytes)")
    elif magic[:3] in (b"ID3", b"\xff\xfb"):
        ok(f"MP3 detected  ({size} bytes)")
    elif magic == b"RIFF":
        ok(f"WAV/RIFF detected  ({size} bytes)")
    else:
        warn(f"Unknown magic {magic!r} — may still be valid  ({size} bytes)")

    return r.content


# ---------------------------------------------------------------------------
# Step 5 — Whisper transcription
# ---------------------------------------------------------------------------

async def test_whisper(client: httpx.AsyncClient, audio_bytes: bytes, filename: str = "voice.ogg") -> str | None:
    header("Step 5: Whisper transcription")

    if not OPENAI_API_KEY:
        warn("OPENAI_API_KEY not set — skipping")
        return None

    info(f"Sending {len(audio_bytes)} bytes ({filename}) to Whisper ({STT_MODEL}, lang={STT_LANGUAGE}) …")

    try:
        r = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (filename, audio_bytes)},
            data={"model": STT_MODEL, "language": STT_LANGUAGE, "response_format": "text"},
            timeout=60.0,
        )
    except Exception as e:
        fail(f"Whisper request failed: {e}")
        return None

    if r.status_code != 200:
        fail(f"Whisper API error {r.status_code}: {r.text[:300]}")
        return None

    transcript = r.text.strip()
    ok(f"Transcript ({len(transcript)} chars):")
    print(f"\n    {BOLD}{transcript}{RESET}\n")
    return transcript


# ---------------------------------------------------------------------------
# Bonus — im.disk.folder.get (lists all chat files in webhook user's IM folder)
# ---------------------------------------------------------------------------

async def test_im_disk_folder(client: httpx.AsyncClient) -> None:
    header("Bonus: im.disk.folder.get (webhook user's IM files folder)")
    info("Docs: https://apidocs.bitrix24.com/api-reference/chats/files/im-disk-folder-get.html")
    # im.disk.folder.get lists the webhook user's own IM storage folder (no CHAT_ID needed).
    # If it still fails, it's non-critical — just informational.
    try:
        # Try without params first (personal folder)
        payload = await call_webhook(client, B24_WEBHOOK_URL, "im.disk.folder.get")
        result = payload.get("result", {})
        if isinstance(result, dict):
            ok(f"Folder ID={result.get('id') or result.get('ID')} NAME={result.get('name') or result.get('NAME')!r}")
            info(f"  Full: {str(result)[:400]}")
        else:
            info(f"Result: {str(result)[:300]}")
    except Exception as e:
        warn(f"im.disk.folder.get failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print(f"\n{BOLD}Voice Pipeline Integration Test{RESET}")
    print(f"Using .env from: {ENV_PATH}\n")

    if not check_config():
        sys.exit(1)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:

        # Step 1: connectivity
        if not await test_webhook(client):
            sys.exit(1)

        # Bonus: list IM disk folder (informational)
        await test_im_disk_folder(client)

        # Step 2: copy chat file → disk
        disk_file_id = await test_im_disk_file_save(client)
        if not disk_file_id:
            fail("\nPipeline stopped at Step 2.")
            sys.exit(1)

        # Step 3: get download URL
        dl_url = await test_disk_file_get(client, disk_file_id)
        if not dl_url:
            fail("\nPipeline stopped at Step 3.")
            sys.exit(1)

        # Step 4: download bytes
        audio = await test_download_audio(client, dl_url)
        if not audio:
            fail("\nPipeline stopped at Step 4.")
            sys.exit(1)

        # Step 5: transcribe
        transcript = await test_whisper(client, audio, filename=f"voice_{TEST_FILE_ID}.oga")

        # Summary
        header("Summary")
        ok(f"Chat file ID  : {TEST_FILE_ID}")
        ok(f"Disk file ID  : {disk_file_id}")
        ok(f"Audio size    : {len(audio)} bytes")
        if transcript:
            ok(f"Transcript    : {transcript[:120]}")
            ok("Pipeline PASSED ✓")
        else:
            warn("Transcription skipped (no OPENAI_API_KEY) — but download pipeline PASSED ✓")


if __name__ == "__main__":
    asyncio.run(main())
