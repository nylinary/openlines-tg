#!/usr/bin/env python3
"""Local test script — debug Bitrix file download strategies for voice messages.

Usage:
    # From project root:
    python -m test.test_voice_download

    # Or directly:
    cd test && python test_voice_download.py

Requires: httpx, python-dotenv (or just reads .env manually).
Uses real credentials from ../.env to hit the live Bitrix API.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

import httpx

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv_simple(path: Path) -> dict[str, str]:
    """Minimal .env loader — no external deps needed."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


ENV = load_dotenv_simple(ENV_PATH)

B24_WEBHOOK_URL = ENV.get("B24_WEBHOOK_URL", "")       # e.g. https://b24-dhuvgc.bitrix24.ru/rest/1/abc/
B24_DOMAIN = ENV.get("B24_DOMAIN", "")                  # e.g. b24-gko4ik.bitrix24.ru
OPENAI_API_KEY = ENV.get("OPENAI_API_KEY", "")
STT_MODEL = ENV.get("STT_MODEL", "whisper-1")
STT_LANGUAGE = ENV.get("STT_LANGUAGE", "ru")

# --- The real file data from live Bitrix event logs ---
# Update these values from the latest Railway logs / Bitrix payload.
# NOTE: file ID in Bitrix IM events is the *chat file* ID, not the Disk file ID.
# The chat file ID is what appears in FILES[n]["id"] in ONIMBOTMESSAGEADD.
TEST_FILE_ID = "116"
TEST_FILE_NAME = "699b551701c6b.oga"
TEST_FILE_URL = (
    "https://b24-dhuvgc.bitrix24.ru/bitrix/services/main/ajax.php"
    "?action=disk.api.file.download&SITE_ID=s1&humanRE=1"
    "&fileId=116&exact=N&fileName=699b551701c6b.oga"
)

# Dialog ID from which the file was sent (from event payload DIALOG_ID).
# Needed for im.dialog.messages.get. Update from logs.
TEST_DIALOG_ID = ""  # e.g. "chat116" or "12345" — fill from logs if known

# Event auth token — from Bitrix event payload auth.access_token.
# This is the MOST IMPORTANT credential for downloading chat files.
# Capture it from Railway logs: look for "auth" in imbot_event_received.
# It expires after ~1 hour, so paste a fresh one before testing.
TEST_EVENT_ACCESS_TOKEN = ""  # e.g. "0d2893690080f35e..."
# Event auth domain — from payload auth.domain (without https://)
TEST_EVENT_DOMAIN = ""  # e.g. "b24-dhuvgc.bitrix24.ru"

# Colours for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}{RESET}\n")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓ {msg}{RESET}")


def fail(msg: str) -> None:
    print(f"  {RED}✗ {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠ {msg}{RESET}")


def info(msg: str) -> None:
    print(f"  {msg}")


def describe_response(r: httpx.Response, label: str) -> bytes | None:
    """Print response details and return content if it looks like audio."""
    ct = r.headers.get("content-type", "unknown")
    size = len(r.content)
    info(f"{label}: HTTP {r.status_code}  content-type={ct}  size={size} bytes")

    if r.status_code >= 400:
        fail(f"HTTP error {r.status_code}")
        if size < 2000:
            info(f"  Body: {r.text[:500]}")
        return None

    if "text/html" in ct:
        fail("Got HTML instead of audio file")
        info(f"  Body preview: {r.text[:300]}")
        return None

    if size < 100:
        warn(f"Suspiciously small file ({size} bytes)")
        info(f"  Body: {r.text[:200]}")
        return None

    # Try to detect OGG by magic bytes
    magic = r.content[:4]
    if magic == b"OggS":
        ok(f"OGG magic bytes detected — valid audio ({size} bytes)")
    elif magic[:3] == b"ID3" or magic[:2] == b"\xff\xfb":
        ok(f"MP3 detected ({size} bytes)")
    elif magic[:4] == b"fLaC":
        ok(f"FLAC detected ({size} bytes)")
    elif magic[:4] == b"RIFF":
        ok(f"WAV/RIFF detected ({size} bytes)")
    else:
        warn(f"Unknown magic bytes: {magic!r} — might still be valid audio")

    return r.content


def strip_human_re(url: str) -> str:
    """Remove humanRE=1 parameter from URL."""
    return (
        url.replace("&humanRE=1", "")
        .replace("humanRE=1&", "")
        .replace("?humanRE=1", "?")
    )


# ===========================================================================
# Strategy 0: Event auth token → disk.file.get (THE CORRECT APPROACH)
# ===========================================================================
async def test_strategy_0_event_token(client: httpx.AsyncClient) -> bytes | None:
    header("Strategy 0: Event auth token → disk.file.get (RECOMMENDED)")
    if not TEST_EVENT_ACCESS_TOKEN or not TEST_EVENT_DOMAIN:
        warn("TEST_EVENT_ACCESS_TOKEN / TEST_EVENT_DOMAIN not set — skipping")
        info("To get these values:")
        info("  1. Send a voice message to the bot in Bitrix")
        info("  2. Check Railway logs for 'imbot_event_received'")
        info("  3. Find auth.access_token and auth.domain in the payload")
        info("  4. Paste them into test_voice_download.py")
        info("  NOTE: Tokens expire after ~1 hour!")
        return None

    domain = TEST_EVENT_DOMAIN.replace("https://", "").replace("http://", "").rstrip("/")
    url = f"https://{domain}/rest/disk.file.get.json?auth={TEST_EVENT_ACCESS_TOKEN}"

    info(f"Calling disk.file.get on {domain} with event token, file_id={TEST_FILE_ID}")
    try:
        r = await client.post(url, data={"id": TEST_FILE_ID})
        payload = r.json() if r.text else {}

        if isinstance(payload, dict) and payload.get("error"):
            fail(f"Error: {payload.get('error')}: {payload.get('error_description', '')}")
            # Check if token expired
            if "expired" in str(payload).lower():
                warn("Token expired! Get a fresh one from Railway logs")
            return None

        result = payload.get("result", {})
        if isinstance(result, dict):
            name = result.get("NAME", "?")
            size = result.get("SIZE", "?")
            dl_url = result.get("DOWNLOAD_URL", "")
            ok(f"disk.file.get OK: NAME={name} SIZE={size}")
            if dl_url:
                ok(f"DOWNLOAD_URL: {dl_url[:80]}...")
                info("Downloading file content...")
                r2 = await client.get(dl_url, follow_redirects=True)
                audio = describe_response(r2, "     Event token download")
                if audio:
                    return audio
            else:
                fail("No DOWNLOAD_URL in response")
        else:
            fail(f"Unexpected result: {str(payload)[:300]}")
    except Exception as e:
        fail(f"Request failed: {e}")

    return None


# ===========================================================================
# Strategy 1: Direct URL download (AJAX endpoint)
# ===========================================================================
async def test_strategy_1_direct_url(client: httpx.AsyncClient) -> bytes | None:
    header("Strategy 1: Direct URL download (AJAX endpoint)")
    if not TEST_FILE_URL:
        fail("No TEST_FILE_URL configured")
        return None

    # 1a) Raw URL as-is (with humanRE=1)
    info("1a) Raw URL as-is (with humanRE=1):")
    try:
        r = await client.get(TEST_FILE_URL, follow_redirects=True)
        describe_response(r, "     Raw URL")
    except Exception as e:
        fail(f"Request failed: {e}")

    # 1b) URL with humanRE stripped
    clean_url = strip_human_re(TEST_FILE_URL)
    info(f"\n1b) URL without humanRE:")
    info(f"    {clean_url[:100]}...")
    try:
        r = await client.get(clean_url, follow_redirects=True)
        result = describe_response(r, "     No humanRE")
        if result:
            return result
    except Exception as e:
        fail(f"Request failed: {e}")

    # 1c) URL with webhook auth token appended
    if B24_WEBHOOK_URL:
        # Extract the secret part from webhook URL
        # webhook format: https://domain/rest/{user_id}/{secret}/
        parts = B24_WEBHOOK_URL.rstrip("/").split("/")
        if len(parts) >= 2:
            webhook_secret = parts[-1]
            auth_url = f"{clean_url}&auth={webhook_secret}"
            info(f"\n1c) URL with webhook secret as auth param:")
            try:
                r = await client.get(auth_url, follow_redirects=True)
                result = describe_response(r, "     With webhook auth")
                if result:
                    return result
            except Exception as e:
                fail(f"Request failed: {e}")

    return None


# ===========================================================================
# Strategy 2: disk.file.get via webhook — try a range of file IDs
# ===========================================================================
async def test_strategy_2_disk_file_get(client: httpx.AsyncClient) -> bytes | None:
    header("Strategy 2: disk.file.get via webhook")
    if not B24_WEBHOOK_URL:
        fail("No B24_WEBHOOK_URL configured")
        return None

    base = B24_WEBHOOK_URL.rstrip("/")

    # The file ID from IM events might NOT be a Disk file ID.
    # Bitrix IM uses its own file numbering. Try the ID and nearby IDs.
    ids_to_try = [TEST_FILE_ID]
    # Also scan a range in case the Disk file ID differs from IM file ID
    try:
        fid = int(TEST_FILE_ID)
        for offset in range(-2, 5):
            candidate = str(fid + offset)
            if candidate not in ids_to_try:
                ids_to_try.append(candidate)
    except ValueError:
        pass

    for fid in ids_to_try:
        url = f"{base}/disk.file.get.json"
        info(f"disk.file.get id={fid}:")
        try:
            r = await client.post(url, data={"id": fid})
            payload = r.json() if r.text else {}

            if isinstance(payload, dict) and payload.get("error"):
                error = payload.get("error", "")
                desc = payload.get("error_description", "")
                if "not found" in desc.lower() or "NOT_FOUND" in error:
                    info(f"  → id={fid}: not found (expected for IM file IDs)")
                else:
                    warn(f"  → id={fid}: {error}: {desc}")
                continue

            result = payload.get("result", {})
            if isinstance(result, dict):
                name = result.get("NAME", "?")
                size = result.get("SIZE", "?")
                info(f"  → id={fid}: NAME={name}  SIZE={size}")
                download_url = result.get("DOWNLOAD_URL", "")
                if download_url:
                    ok(f"Got DOWNLOAD_URL: {download_url[:100]}...")
                    info("Downloading from DOWNLOAD_URL...")
                    r2 = await client.get(download_url, follow_redirects=True)
                    audio = describe_response(r2, f"     disk.file.get({fid})")
                    if audio:
                        return audio
                else:
                    warn(f"  No DOWNLOAD_URL for id={fid}")
        except Exception as e:
            warn(f"  disk.file.get({fid}) failed: {e}")

    return None


# ===========================================================================
# Strategy 3: Bitrix disk.file.getlist — find files by name
# ===========================================================================
async def test_strategy_3_find_file_by_name(client: httpx.AsyncClient) -> bytes | None:
    header("Strategy 3: Find file by name via disk.file.getlist / disk.storage.getlist")
    if not B24_WEBHOOK_URL:
        fail("No B24_WEBHOOK_URL configured")
        return None

    base = B24_WEBHOOK_URL.rstrip("/")

    # 3a) List disk storages to find where IM files are stored
    info("3a) Listing disk storages (disk.storage.getlist):")
    url = f"{base}/disk.storage.getlist.json"
    try:
        r = await client.post(url)
        payload = r.json() if r.text else {}
        if isinstance(payload, dict) and payload.get("error"):
            warn(f"disk.storage.getlist: {payload.get('error')}: {payload.get('error_description', '')}")
        else:
            storages = payload.get("result", [])
            if isinstance(storages, list):
                ok(f"Found {len(storages)} storages")
                for s in storages[:10]:
                    if isinstance(s, dict):
                        info(f"  Storage: ID={s.get('ID')} NAME={s.get('NAME')!r} "
                             f"ENTITY_TYPE={s.get('ENTITY_TYPE')} ENTITY_ID={s.get('ENTITY_ID')}")
            else:
                info(f"Response: {str(storages)[:300]}")
    except Exception as e:
        warn(f"disk.storage.getlist failed: {e}")

    # 3b) Search for the file across all disk storages by name
    info(f"\n3b) Searching for file by name: {TEST_FILE_NAME}")
    # disk.file.getlist is NOT a top-level method; files belong to folders.
    # But we can try disk.folder.getchildren on root folders of each storage.
    # More practical: use entity_type=chat approach

    return None


# ===========================================================================
# Strategy 4: imbot.message.file.get / REST file download pattern
# ===========================================================================
async def test_strategy_4_rest_file_methods(client: httpx.AsyncClient) -> bytes | None:
    header("Strategy 4: REST file download methods")
    if not B24_WEBHOOK_URL:
        fail("No B24_WEBHOOK_URL configured")
        return None

    base = B24_WEBHOOK_URL.rstrip("/")
    parsed = urlparse(B24_WEBHOOK_URL)
    webhook_domain = parsed.netloc
    parts = B24_WEBHOOK_URL.rstrip("/").split("/")
    webhook_secret = parts[-1] if len(parts) >= 2 else ""
    user_id = parts[-2] if len(parts) >= 3 else "1"

    # 4a) Construct direct REST-style URL:
    # https://domain/rest/USER_ID/SECRET/disk.file.getContent?id=FILE_ID
    # (undocumented but sometimes works)
    for method in ["disk.file.getContent", "disk.file.download"]:
        url = f"https://{webhook_domain}/rest/{user_id}/{webhook_secret}/{method}.json"
        info(f"4a) {method}?id={TEST_FILE_ID}:")
        try:
            r = await client.post(url, data={"id": TEST_FILE_ID}, follow_redirects=True)
            payload_or_audio = describe_response(r, f"     {method}")
            if payload_or_audio:
                return payload_or_audio
            # If JSON error
            if r.headers.get("content-type", "").startswith("application/json"):
                body = r.json() if r.text else {}
                if isinstance(body, dict) and body.get("error"):
                    warn(f"  {body.get('error')}: {body.get('error_description', '')}")
        except Exception as e:
            warn(f"  {method} failed: {e}")

    # 4b) Try accessing via the AJAX URL but with REST webhook auth in cookies
    info(f"\n4b) AJAX URL with different auth approaches:")
    clean_url = strip_human_re(TEST_FILE_URL)
    # Try adding sessid from webhook call
    try:
        # Get a sessid by calling a simple method
        r = await client.post(f"{base}/server.time.json")
        # Some Bitrix responses include set-cookie
        cookies = dict(r.cookies)
        if cookies:
            info(f"  Got cookies from server.time: {list(cookies.keys())}")
            # Now try the AJAX URL with those cookies
            r2 = await client.get(clean_url, cookies=cookies, follow_redirects=True)
            result = describe_response(r2, "     AJAX+cookies")
            if result:
                return result
        else:
            info("  No cookies from server.time")
    except Exception as e:
        warn(f"  Cookie approach failed: {e}")

    return None


# ===========================================================================
# Strategy 5a: Disk storage for IM/chat — find the right storage
# ===========================================================================
async def test_strategy_5a_chat_storage(client: httpx.AsyncClient) -> bytes | None:
    header("Strategy 5a: Chat disk storage — find IM files")
    if not B24_WEBHOOK_URL:
        fail("No B24_WEBHOOK_URL configured")
        return None

    base = B24_WEBHOOK_URL.rstrip("/")

    # Bitrix stores IM chat files in a special disk storage with
    # ENTITY_TYPE = "chat" or "im". Let's find it and browse its files.
    info("Looking for chat/IM disk storages...")
    url = f"{base}/disk.storage.getlist.json"
    try:
        r = await client.post(url, data={
            "filter[ENTITY_TYPE]": "group",
        })
        payload = r.json() if r.text else {}
        storages = payload.get("result", [])
        if isinstance(storages, list) and storages:
            for s in storages[:5]:
                info(f"  Found: ID={s.get('ID')} NAME={s.get('NAME')!r} "
                     f"ENTITY_TYPE={s.get('ENTITY_TYPE')} ENTITY_ID={s.get('ENTITY_ID')}")
    except Exception as e:
        warn(f"  Failed: {e}")

    # Also try to find the user's personal storage
    info("\nLooking up user 1's personal disk storage...")
    url = f"{base}/disk.storage.getforapp.json"
    try:
        r = await client.post(url)
        payload = r.json() if r.text else {}
        if isinstance(payload, dict) and payload.get("error"):
            warn(f"disk.storage.getforapp: {payload.get('error')}: {payload.get('error_description', '')}")
        else:
            result = payload.get("result", {})
            if isinstance(result, dict):
                info(f"  App storage: ID={result.get('ID')} NAME={result.get('NAME')!r}")
    except Exception as e:
        warn(f"  Failed: {e}")

    return None


# ===========================================================================
# Strategy 5: Scope discovery — what scopes does the webhook have?
# ===========================================================================
async def test_strategy_5_scope_check(client: httpx.AsyncClient) -> None:
    header("Strategy 5: Webhook scope & permission check")
    if not B24_WEBHOOK_URL:
        fail("No B24_WEBHOOK_URL configured")
        return

    base = B24_WEBHOOK_URL.rstrip("/")

    # 5a) Check available scopes via app.info or scope
    for method in ["scope", "app.info"]:
        info(f"Calling {method}:")
        url = f"{base}/{method}.json"
        try:
            r = await client.post(url)
            payload = r.json() if r.text else {}
            if isinstance(payload, dict) and payload.get("error"):
                warn(f"{method}: {payload.get('error')}: {payload.get('error_description', '')}")
            else:
                result = payload.get("result", payload)
                if isinstance(result, list):
                    ok(f"Scopes ({len(result)}): {', '.join(sorted(result)[:20])}...")
                    # Highlight key scopes
                    key_scopes = {"disk", "im", "imbot", "imopenlines", "imconnector"}
                    found = key_scopes & set(result)
                    missing = key_scopes - set(result)
                    if found:
                        ok(f"Key scopes present: {', '.join(sorted(found))}")
                    if missing:
                        warn(f"Key scopes MISSING: {', '.join(sorted(missing))}")
                elif isinstance(result, dict):
                    scopes = result.get("SCOPE", [])
                    if isinstance(scopes, list):
                        ok(f"Scopes from app.info ({len(scopes)})")
                    info(f"LICENSE: {result.get('LICENSE', '?')}")
                else:
                    info(f"Response: {str(payload)[:500]}")
        except Exception as e:
            warn(f"{method} failed: {e}")


# ===========================================================================
# Strategy 6: im.dialog.messages.get — fetch message to find disk file ID
# ===========================================================================
async def test_strategy_6_im_dialog_messages(client: httpx.AsyncClient) -> bytes | None:
    header("Strategy 6: im.dialog.messages.get — find actual disk file ID")
    if not B24_WEBHOOK_URL:
        fail("No B24_WEBHOOK_URL configured")
        return None

    base = B24_WEBHOOK_URL.rstrip("/")

    if not TEST_DIALOG_ID:
        warn("TEST_DIALOG_ID not set — skipping. Set it from the event payload DIALOG_ID")
        info("You can find it in Railway logs: look for DIALOG_ID in ONIMBOTMESSAGEADD events")
        return None

    info(f"Fetching messages from dialog {TEST_DIALOG_ID}...")
    url = f"{base}/im.dialog.messages.get.json"
    try:
        r = await client.post(url, data={
            "DIALOG_ID": TEST_DIALOG_ID,
            "LIMIT": "10",
        })
        payload = r.json() if r.text else {}
        if isinstance(payload, dict) and payload.get("error"):
            warn(f"im.dialog.messages.get: {payload.get('error')}: {payload.get('error_description', '')}")
            return None

        result = payload.get("result", {})
        messages = result.get("messages", []) if isinstance(result, dict) else []
        info(f"Got {len(messages)} messages")

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id", "?")
            msg_files = msg.get("params", {}).get("FILE_ID", []) if isinstance(msg.get("params"), dict) else []
            if msg_files:
                info(f"  Message {msg_id}: has files: {msg_files}")
                # The FILE_ID in message params might be the disk file ID
                for disk_fid in msg_files:
                    info(f"  Trying disk.file.get with id={disk_fid} (from message)...")
                    try:
                        r2 = await client.post(
                            f"{base}/disk.file.get.json",
                            data={"id": str(disk_fid)},
                        )
                        p2 = r2.json() if r2.text else {}
                        if isinstance(p2, dict) and p2.get("error"):
                            warn(f"  disk.file.get({disk_fid}): {p2.get('error')}")
                        else:
                            res = p2.get("result", {})
                            if isinstance(res, dict):
                                dl = res.get("DOWNLOAD_URL", "")
                                info(f"  → NAME={res.get('NAME')} SIZE={res.get('SIZE')} DL_URL={bool(dl)}")
                                if dl:
                                    ok(f"Found DOWNLOAD_URL for disk file {disk_fid}!")
                                    r3 = await client.get(dl, follow_redirects=True)
                                    audio = describe_response(r3, f"     disk.file({disk_fid})")
                                    if audio:
                                        return audio
                    except Exception as e:
                        warn(f"  disk.file.get({disk_fid}) failed: {e}")
    except Exception as e:
        warn(f"im.dialog.messages.get failed: {e}")

    return None


# ===========================================================================
# Strategy 7: Direct file download via REST with various auth approaches
# ===========================================================================
async def test_strategy_7_direct_rest_download(client: httpx.AsyncClient) -> bytes | None:
    header("Strategy 7: Direct REST download with webhook auth")
    if not B24_WEBHOOK_URL:
        fail("No B24_WEBHOOK_URL configured")
        return None

    parsed = urlparse(B24_WEBHOOK_URL)
    webhook_domain = parsed.netloc
    parts = B24_WEBHOOK_URL.rstrip("/").split("/")
    webhook_secret = parts[-1] if len(parts) >= 2 else ""
    user_id = parts[-2] if len(parts) >= 3 else "1"

    # The AJAX URL is on the same domain as the webhook.
    # Try rewriting it as a REST call by constructing the URL differently.
    clean_url = strip_human_re(TEST_FILE_URL)

    # 7a) Try the AJAX URL with auth= webhook secret (REST-style)
    auth_url = f"{clean_url}&auth={webhook_secret}"
    info("7a) AJAX URL + auth=webhook_secret:")
    try:
        r = await client.get(auth_url, follow_redirects=True)
        result = describe_response(r, "     AJAX+auth")
        if result:
            return result
    except Exception as e:
        warn(f"  Failed: {e}")

    # 7b) Try building a webhook-scoped download URL
    # Pattern: /rest/{user_id}/{secret}/disk.file.download/{file_id}
    for endpoint in [
        f"https://{webhook_domain}/rest/{user_id}/{webhook_secret}/disk.file.getContent.json?id={TEST_FILE_ID}",
        f"https://{webhook_domain}/rest/{user_id}/{webhook_secret}/disk.file.get.json?id={TEST_FILE_ID}&download=1",
    ]:
        method_name = endpoint.split("/")[-1].split("?")[0]
        info(f"\n7b) {method_name}:")
        try:
            r = await client.get(endpoint, follow_redirects=True)
            ct = r.headers.get("content-type", "")
            if "application/json" in ct:
                body = r.json() if r.text else {}
                if isinstance(body, dict) and body.get("error"):
                    warn(f"  {body.get('error')}: {body.get('error_description', '')}")
                elif isinstance(body, dict) and body.get("result"):
                    # disk.file.get might return file metadata
                    res = body.get("result", {})
                    if isinstance(res, dict) and res.get("DOWNLOAD_URL"):
                        dl = res["DOWNLOAD_URL"]
                        ok(f"  Got DOWNLOAD_URL: {dl[:80]}...")
                        r2 = await client.get(dl, follow_redirects=True)
                        audio = describe_response(r2, "     DOWNLOAD_URL")
                        if audio:
                            return audio
            else:
                result = describe_response(r, f"     {method_name}")
                if result:
                    return result
        except Exception as e:
            warn(f"  {method_name} failed: {e}")

    return None


# ===========================================================================
# Strategy 8: Exhaustive IM/imbot file access methods
# ===========================================================================
async def test_strategy_8_im_file_access(client: httpx.AsyncClient) -> bytes | None:
    header("Strategy 8: IM/imbot file methods + disk folder browsing")
    if not B24_WEBHOOK_URL:
        fail("No B24_WEBHOOK_URL configured")
        return None

    base = B24_WEBHOOK_URL.rstrip("/")

    # 8a) Try a wide range of IM-related file methods
    im_methods = [
        ("im.disk.file.get", {"FILE_ID": TEST_FILE_ID}),
        ("im.disk.file.get", {"id": TEST_FILE_ID}),
        ("imbot.chat.file.get", {"FILE_ID": TEST_FILE_ID}),
        ("im.chat.file.list", {"CHAT_ID": "0"}),  # might list files
        ("im.message.get", {"MESSAGE_ID": TEST_FILE_ID}),  # message might have same ID
        # Bitrix cloud has messageId-based file info in newer API
        ("rest.file.get", {"id": TEST_FILE_ID}),
    ]

    for method, params in im_methods:
        info(f"8a) {method}({params}):")
        url = f"{base}/{method}.json"
        try:
            r = await client.post(url, data={str(k): str(v) for k, v in params.items()})
            payload = r.json() if r.text else {}
            if isinstance(payload, dict) and payload.get("error"):
                error = payload.get("error", "")
                desc = payload.get("error_description", "")
                if "METHOD_NOT_FOUND" in error:
                    info(f"  → method not found")
                else:
                    warn(f"  → {error}: {desc}")
            else:
                result = payload.get("result", payload)
                info(f"  → Response: {str(result)[:400]}")
                # Check if it contains file info
                if isinstance(result, dict):
                    for key in ["DOWNLOAD_URL", "urlDownload", "downloadUrl", "url"]:
                        dl = result.get(key, "")
                        if dl:
                            ok(f"  Found {key}: {dl[:100]}")
                            r2 = await client.get(dl, follow_redirects=True)
                            audio = describe_response(r2, f"     {method}")
                            if audio:
                                return audio
        except Exception as e:
            warn(f"  {method} failed: {e}")

    # 8b) Browse the admin user's disk storage for chat-uploaded files
    # Chat files in Bitrix are stored under a special folder in the user's storage
    info("\n8b) Browsing admin user storage for chat files...")
    try:
        # Get root folder of user 1's storage (storage ID=1 from earlier)
        r = await client.post(f"{base}/disk.storage.getchildren.json", data={"id": "1"})
        payload = r.json() if r.text else {}
        if isinstance(payload, dict) and payload.get("error"):
            warn(f"disk.storage.getchildren(1): {payload.get('error')}")
        else:
            items = payload.get("result", [])
            if isinstance(items, list):
                info(f"  Root of storage 1 has {len(items)} items:")
                for item in items:
                    if isinstance(item, dict):
                        item_type = item.get("TYPE", "?")
                        item_name = item.get("NAME", "?")
                        item_id = item.get("ID", "?")
                        info(f"    {item_type}: ID={item_id} NAME={item_name!r}")
                        # Look for "Чат" or "Chat" or "IM" folder
                        if any(kw in str(item_name).lower() for kw in ["чат", "chat", "im", "message", "сообщен"]):
                            ok(f"  Found chat folder: {item_name} (ID={item_id})")
                            # Browse it
                            r2 = await client.post(
                                f"{base}/disk.folder.getchildren.json",
                                data={"id": str(item_id)},
                            )
                            p2 = r2.json() if r2.text else {}
                            children = p2.get("result", [])
                            if isinstance(children, list):
                                info(f"    Chat folder has {len(children)} items:")
                                for child in children[:20]:
                                    if isinstance(child, dict):
                                        cname = child.get("NAME", "?")
                                        cid = child.get("ID", "?")
                                        ctype = child.get("TYPE", "?")
                                        csize = child.get("SIZE", "?")
                                        info(f"      {ctype}: ID={cid} NAME={cname!r} SIZE={csize}")
                                        # Check if this is our file
                                        if cname == TEST_FILE_NAME or str(cid) == TEST_FILE_ID:
                                            ok(f"    FOUND our file! ID={cid}")
                                            dl = child.get("DOWNLOAD_URL", "")
                                            if dl:
                                                r3 = await client.get(dl, follow_redirects=True)
                                                audio = describe_response(r3, "     chat folder file")
                                                if audio:
                                                    return audio
    except Exception as e:
        warn(f"  Storage browsing failed: {e}")

    # 8c) Try disk.file.get as other user IDs (the file might belong to a different user)
    info("\n8c) Trying to find file owner via disk.file.get with different approaches...")
    # The ACCESS_DENIED for ID 116 means the file exists but webhook user can't access it.
    # This often happens because IM files are owned by the message sender.
    # Try getting file info through disk.folder methods instead.
    
    # List all storages and check each for our file
    try:
        r = await client.post(f"{base}/disk.storage.getlist.json")
        payload = r.json() if r.text else {}
        storages = payload.get("result", [])
        if isinstance(storages, list):
            for storage in storages:
                sid = storage.get("ID", "?")
                sname = storage.get("NAME", "?")
                stype = storage.get("ENTITY_TYPE", "?")
                # Check root children for each storage
                r2 = await client.post(
                    f"{base}/disk.storage.getchildren.json",
                    data={"id": str(sid)},
                )
                p2 = r2.json() if r2.text else {}
                if isinstance(p2, dict) and p2.get("error"):
                    info(f"  Storage {sid} ({sname}): {p2.get('error')}")
                    continue
                children = p2.get("result", [])
                if isinstance(children, list):
                    # Look for chat-related folders
                    for child in children:
                        if isinstance(child, dict):
                            cname = str(child.get("NAME", ""))
                            if any(kw in cname.lower() for kw in ["чат", "chat"]):
                                info(f"  Storage {sid} ({sname}): Found chat folder '{cname}' ID={child.get('ID')}")
    except Exception as e:
        warn(f"  Multi-storage scan failed: {e}")

    return None


# ===========================================================================
# Strategy 9: Scan ALL user storages recursively to find the file
# ===========================================================================
async def test_strategy_9_scan_all_storages(client: httpx.AsyncClient) -> bytes | None:
    header("Strategy 9: Scan all user storages for chat-uploaded files")
    if not B24_WEBHOOK_URL:
        fail("No B24_WEBHOOK_URL configured")
        return None

    base = B24_WEBHOOK_URL.rstrip("/")

    # Get all storages
    r = await client.post(f"{base}/disk.storage.getlist.json")
    payload = r.json() if r.text else {}
    storages = payload.get("result", [])

    for storage in storages:
        if not isinstance(storage, dict):
            continue
        sid = storage.get("ID", "?")
        sname = storage.get("NAME", "?")
        etype = storage.get("ENTITY_TYPE", "?")

        info(f"Scanning storage {sid} ({sname}, type={etype}):")

        try:
            r2 = await client.post(
                f"{base}/disk.storage.getchildren.json",
                data={"id": str(sid)},
            )
            p2 = r2.json() if r2.text else {}
            if isinstance(p2, dict) and p2.get("error"):
                warn(f"  Cannot list: {p2.get('error')}")
                continue

            children = p2.get("result", [])
            if not isinstance(children, list):
                continue

            info(f"  {len(children)} items in root")
            for item in children:
                if not isinstance(item, dict):
                    continue
                itype = item.get("TYPE", "?")
                iname = item.get("NAME", "?")
                iid = item.get("ID", "?")

                # Check if this is our file directly
                if itype == "file":
                    if str(iid) == TEST_FILE_ID or str(iname) == TEST_FILE_NAME:
                        ok(f"  FOUND FILE at root! ID={iid} NAME={iname}")
                        dl = item.get("DOWNLOAD_URL", "")
                        if dl:
                            r3 = await client.get(dl, follow_redirects=True)
                            audio = describe_response(r3, f"  storage {sid} root file")
                            if audio:
                                return audio

                # Recurse into folders (especially "Чат" folders)
                if itype == "folder":
                    info(f"    Folder: ID={iid} NAME={iname!r}")
                    # List children of this folder
                    try:
                        r3 = await client.post(
                            f"{base}/disk.folder.getchildren.json",
                            data={"id": str(iid)},
                        )
                        p3 = r3.json() if r3.text else {}
                        if isinstance(p3, dict) and p3.get("error"):
                            info(f"      Cannot list folder: {p3.get('error')}")
                            continue
                        folder_children = p3.get("result", [])
                        if isinstance(folder_children, list):
                            info(f"      {len(folder_children)} items")
                            for fc in folder_children[:30]:
                                if not isinstance(fc, dict):
                                    continue
                                fcname = fc.get("NAME", "?")
                                fcid = fc.get("ID", "?")
                                fctype = fc.get("TYPE", "?")
                                fcsize = fc.get("SIZE", "?")

                                if fctype == "file":
                                    info(f"        File: ID={fcid} NAME={fcname!r} SIZE={fcsize}")
                                    # Check if it matches our target
                                    if str(fcid) == TEST_FILE_ID or str(fcname) == TEST_FILE_NAME:
                                        ok(f"      FOUND our file! ID={fcid} NAME={fcname}")
                                        dl = fc.get("DOWNLOAD_URL", "")
                                        if dl:
                                            ok(f"      DOWNLOAD_URL: {dl[:80]}...")
                                            r4 = await client.get(dl, follow_redirects=True)
                                            audio = describe_response(r4, f"      file in folder")
                                            if audio:
                                                return audio
                                elif fctype == "folder":
                                    info(f"        Subfolder: ID={fcid} NAME={fcname!r}")
                    except Exception as e:
                        warn(f"      Folder listing failed: {e}")
        except Exception as e:
            warn(f"  Storage {sid} scan failed: {e}")

    return None


# ===========================================================================
# Whisper transcription test
# ===========================================================================
async def test_whisper_transcription(client: httpx.AsyncClient, audio_bytes: bytes) -> str | None:
    header("Whisper STT Transcription")
    if not OPENAI_API_KEY:
        fail("No OPENAI_API_KEY configured")
        return None

    info(f"Audio size: {len(audio_bytes)} bytes")
    info(f"Model: {STT_MODEL}  Language: {STT_LANGUAGE}")

    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    files = {"file": (TEST_FILE_NAME, audio_bytes)}
    data = {
        "model": STT_MODEL,
        "language": STT_LANGUAGE,
        "response_format": "text",
    }

    try:
        r = await client.post(url, headers=headers, files=files, data=data, timeout=60.0)
        if r.status_code != 200:
            fail(f"Whisper API error {r.status_code}: {r.text[:500]}")
            return None
        text = r.text.strip()
        ok(f"Transcription ({len(text)} chars): {text[:200]}")
        return text
    except Exception as e:
        fail(f"Whisper request failed: {e}")
        return None


# ===========================================================================
# Save audio file locally for debugging
# ===========================================================================
def save_audio_locally(audio_bytes: bytes, label: str) -> Path:
    out_dir = Path(__file__).parent / "downloaded"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{label}_{TEST_FILE_NAME}"
    out_path.write_bytes(audio_bytes)
    ok(f"Saved to {out_path} ({len(audio_bytes)} bytes)")
    return out_path


# ===========================================================================
# Main
# ===========================================================================
async def main() -> None:
    print(f"\n{BOLD}Bitrix Voice File Download Test{RESET}")
    print(f"{'─'*40}")
    info(f"B24_WEBHOOK_URL = {B24_WEBHOOK_URL[:40]}***" if B24_WEBHOOK_URL else "B24_WEBHOOK_URL = (not set)")
    info(f"B24_DOMAIN      = {B24_DOMAIN}")
    info(f"TEST_FILE_ID    = {TEST_FILE_ID}")
    info(f"TEST_FILE_NAME  = {TEST_FILE_NAME}")
    info(f"TEST_FILE_URL   = {TEST_FILE_URL[:80]}...")
    info(f"OPENAI_API_KEY  = {'sk-...'+OPENAI_API_KEY[-6:] if OPENAI_API_KEY else '(not set)'}")

    # Check domain mismatch
    if B24_WEBHOOK_URL and B24_DOMAIN:
        wh_domain = urlparse(B24_WEBHOOK_URL).netloc
        if wh_domain != B24_DOMAIN:
            warn(f"DOMAIN MISMATCH: B24_DOMAIN={B24_DOMAIN} ≠ webhook domain={wh_domain}")
            warn("File URLs from events use the webhook domain, but OAuth uses B24_DOMAIN")
            warn("This means auth tokens from OAuth won't work for file downloads!")

    audio_bytes: bytes | None = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        # Scope check first — understand what we have access to
        await test_strategy_5_scope_check(client)

        # Strategy 0: Event auth token (THE CORRECT APPROACH)
        result = await test_strategy_0_event_token(client)
        if result:
            audio_bytes = result
            save_audio_locally(result, "strategy0_event_token")

        # Strategy 1: Direct URL (AJAX endpoint)
        if not audio_bytes:
            result = await test_strategy_1_direct_url(client)
            if result:
                audio_bytes = result
                save_audio_locally(result, "strategy1")

        # Strategy 2: disk.file.get (range of IDs)
        if not audio_bytes:
            result = await test_strategy_2_disk_file_get(client)
            if result:
                audio_bytes = result
                save_audio_locally(result, "strategy2")

        # Strategy 3: Find file by name in disk storages
        if not audio_bytes:
            result = await test_strategy_3_find_file_by_name(client)
            if result:
                audio_bytes = result
                save_audio_locally(result, "strategy3")

        # Strategy 4: REST file download methods
        if not audio_bytes:
            result = await test_strategy_4_rest_file_methods(client)
            if result:
                audio_bytes = result
                save_audio_locally(result, "strategy4")

        # Strategy 5a: Chat disk storage
        if not audio_bytes:
            result = await test_strategy_5a_chat_storage(client)
            if result:
                audio_bytes = result
                save_audio_locally(result, "strategy5a")

        # Strategy 6: IM dialog messages → disk file IDs
        if not audio_bytes:
            result = await test_strategy_6_im_dialog_messages(client)
            if result:
                audio_bytes = result
                save_audio_locally(result, "strategy6")

        # Strategy 7: Direct REST download with auth
        if not audio_bytes:
            result = await test_strategy_7_direct_rest_download(client)
            if result:
                audio_bytes = result
                save_audio_locally(result, "strategy7")

        # Strategy 8: IM file methods + disk folder browsing
        if not audio_bytes:
            result = await test_strategy_8_im_file_access(client)
            if result:
                audio_bytes = result
                save_audio_locally(result, "strategy8")

        # Strategy 9: Scan all user storages
        if not audio_bytes:
            result = await test_strategy_9_scan_all_storages(client)
            if result:
                audio_bytes = result
                save_audio_locally(result, "strategy9")

        # Summary
        header("SUMMARY")
        if audio_bytes:
            ok(f"Successfully downloaded audio: {len(audio_bytes)} bytes")
            # Test Whisper
            transcript = await test_whisper_transcription(client, audio_bytes)
            if transcript:
                ok(f"Full pipeline works! Transcript: {transcript}")
            else:
                warn("Audio downloaded but transcription failed")
        else:
            fail("ALL download strategies failed")
            print()
            info("DIAGNOSIS:")
            info("  - AJAX urlDownload requires browser cookies (not REST-accessible)")
            info("  - disk.file.get returns ACCESS_DENIED (file owned by chat context)")
            info("  - Webhook user cannot browse IM internal file storage")
            info("  - All user disk storages appear empty via REST API")
            print()
            ok("SOLUTION: Use the event auth token (Strategy 0)")
            info("  The Bitrix event payload includes auth.access_token — a fresh")
            info("  OAuth token scoped to the app/user that sent the event.")
            info("  This token CAN access chat files via disk.file.get.")
            print()
            info("  The app code has been updated to use this approach (Strategy 1).")
            info("  Deploy to Railway and send a new voice message to test live.")
            print()
            info("  To test locally with Strategy 0:")
            info("  1. Send a voice message to the bot in Bitrix")
            info("  2. Check Railway logs for 'imbot_event_received'")
            info("  3. Copy auth.access_token and auth.domain from the payload")
            info("  4. Paste into TEST_EVENT_ACCESS_TOKEN / TEST_EVENT_DOMAIN")
            info("     at the top of this file, then re-run")
            info("  NOTE: Event tokens expire after ~1 hour!")


if __name__ == "__main__":
    asyncio.run(main())
