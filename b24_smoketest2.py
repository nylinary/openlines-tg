#!/usr/bin/env python3
"""
Bitrix24 REST smoke-test (extended) for:
- profile/auth
- methods presence
- IM: list recent dialogs, send a test message to a chosen dialog
- OpenLines: resolve OpenLines chat by USER_CODE, extract SESSION_ID (if present), try history.get
- Connector: verify proxy-critical methods exist (register/send/update)

Key idea for your portal:
- imopenlines.session.open requires USER_CODE (not CHAT_ID/DIALOG_ID).
- In your chat payload, SESSION_ID may be encoded inside entity_data_1 (pipe-separated).
  Example: "Y|DEAL|13|N|N|7|..." -> SESSION_ID=7 (6th field).

Usage:
  export B24_BASE="https://YOUR_PORTAL.bitrix24.ru/rest/USER_ID/WEBHOOK_TOKEN"

  # Basic discovery
  python3 b24_smoketest2.py

  # Show only relevant method groups
  python3 b24_smoketest2.py --list-groups

  # List recent dialogs (including openlines if visible)
  python3 b24_smoketest2.py --recent

  # Send message to a dialog (e.g. chat31)
  python3 b24_smoketest2.py --send-dialog chat31 --send-text "hello"

  # Resolve openlines chat by USER_CODE (telegrambot|...)
  python3 b24_smoketest2.py --user-code "telegrambot|1|1096803319|29"

  # Resolve + try session.history.get (if session id found)
  python3 b24_smoketest2.py --user-code "telegrambot|1|1096803319|29" --try-history

Exit codes:
  0  OK (core checks passed; some optional calls may fail and will be reported)
  2  missing base
  3  profile failed
  4  methods failed
  10 missing proxy-critical imconnector methods
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------
# HTTP helpers (stdlib only)
# -----------------------------

@dataclass
class B24Response:
    ok: bool
    status: int
    data: Dict[str, Any]
    raw: str


def _normalize_base(base: str) -> str:
    base = base.strip()
    return base.rstrip("/")


def _endpoint(base: str, method: str) -> str:
    return f"{base}/{method}.json"


def _request(
    url: str,
    *,
    method: str = "GET",
    form: Optional[Dict[str, Any]] = None,
    timeout: int = 20,
) -> B24Response:
    headers = {
        "Accept": "application/json",
        "User-Agent": "b24-smoketest/2.0",
    }

    data_bytes = None
    if form is not None:
        pairs: List[Tuple[str, str]] = []
        for k, v in form.items():
            if isinstance(v, (list, tuple)):
                for item in v:
                    pairs.append((k, str(item)))
            else:
                pairs.append((k, str(v)))
        data_bytes = urllib.parse.urlencode(pairs).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = getattr(e, "code", 0) or 0
    except Exception as e:
        return B24Response(ok=False, status=0, data={"error": str(e)}, raw=str(e))

    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}

    ok = isinstance(data, dict) and ("error" not in data) and (200 <= status < 300)
    return B24Response(ok=ok, status=status, data=data if isinstance(data, dict) else {"result": data}, raw=raw)


def b24_get(base: str, method: str, timeout: int) -> B24Response:
    return _request(_endpoint(base, method), method="GET", timeout=timeout)


def b24_post(base: str, method: str, form: Dict[str, Any], timeout: int) -> B24Response:
    return _request(_endpoint(base, method), method="POST", form=form, timeout=timeout)


# -----------------------------
# Utilities
# -----------------------------

def fmt_err(data: Dict[str, Any]) -> str:
    err = data.get("error")
    desc = data.get("error_description")
    if err or desc:
        return f"{err or ''} {desc or ''}".strip()
    return ""


def print_check(name: str, ok: bool, details: str = "") -> None:
    status = "OK" if ok else "FAIL"
    line = f"[{status}] {name}"
    if details:
        line += f" — {details}"
    print(line)


def extract_methods(methods_resp: B24Response) -> List[str]:
    r = methods_resp.data.get("result")
    if isinstance(r, list):
        return [str(x) for x in r]
    if isinstance(r, dict) and isinstance(r.get("methods"), list):
        return [str(x) for x in r["methods"]]
    return []


def group_methods(methods: List[str]) -> Dict[str, int]:
    groups: Dict[str, int] = {}
    for m in methods:
        top = m.split(".", 1)[0] if "." in m else m
        groups[top] = groups.get(top, 0) + 1
    return dict(sorted(groups.items(), key=lambda kv: (-kv[1], kv[0])))


def filter_prefix(methods: List[str], prefix: str) -> List[str]:
    return sorted([m for m in methods if m.startswith(prefix)])


def safe_get(d: Any, path: List[str]) -> Any:
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def parse_session_id_from_entity_data_1(entity_data_1: str) -> Optional[int]:
    """
    entity_data_1 example: "Y|DEAL|13|N|N|7|1770749893|0|0|0"
    session id is often the 6th pipe-separated field -> "7"
    """
    if not entity_data_1 or "|" not in entity_data_1:
        return None
    parts = entity_data_1.split("|")
    if len(parts) < 6:
        return None
    cand = parts[5].strip()
    if cand.isdigit():
        return int(cand)
    return None


def pretty_print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# -----------------------------
# High-level tests
# -----------------------------

def test_profile(base: str, timeout: int) -> B24Response:
    r = b24_get(base, "profile", timeout)
    print_check("profile", r.ok, fmt_err(r.data))
    if r.ok:
        user_id = safe_get(r.data, ["result", "ID"])
        name = safe_get(r.data, ["result", "NAME"])
        print(f"  user: {user_id} {name}")
    return r


def test_methods(base: str, timeout: int) -> Tuple[B24Response, List[str]]:
    r = b24_get(base, "methods", timeout)
    print_check("methods", r.ok, fmt_err(r.data))
    methods = extract_methods(r) if r.ok else []
    if methods:
        print_check("methods list parse", True, f"{len(methods)} methods")
    else:
        print_check("methods list parse", False, "Could not extract methods list")
    return r, methods


def print_groups(methods: List[str]) -> None:
    g = group_methods(methods)
    print("Top-level method groups:")
    for k, v in g.items():
        print(f"  {k}: {v}")


def test_proxy_methods(methods: List[str]) -> int:
    need = ["imconnector.register", "imconnector.send.messages", "imconnector.update.messages"]
    missing = [m for m in need if m not in set(methods)]
    if missing:
        print_check("proxy methods presence", False, f"missing: {', '.join(missing)}")
        return 10
    print_check("proxy methods presence", True, "register/send/update available")
    return 0


def test_recent(base: str, timeout: int, limit: int = 20) -> Optional[List[Dict[str, Any]]]:
    """
    Lists recent dialogs. If openlines are visible to this user, you'll see chats with type=lines or id like chatNNN.
    """
    r = b24_post(base, "im.recent.list", {"SKIP_OPENLINES": 0}, timeout)
    print_check("call: im.recent.list", r.ok, fmt_err(r.data))
    if not r.ok:
        return None
    items = safe_get(r.data, ["result", "items"])
    if not isinstance(items, list):
        print_check("recent.items parse", False, "Unexpected payload shape")
        return None

    print(f"Recent dialogs (showing up to {limit}):")
    for it in items[:limit]:
        did = it.get("id")
        title = it.get("title")
        typ = it.get("type")
        chat_id = it.get("chat_id")
        print(f"  {did}  type={typ} chat_id={chat_id}  title={title}")
    return items


def test_send_dialog(base: str, timeout: int, dialog_id: str, text: str) -> B24Response:
    r = b24_post(base, "im.message.add", {"DIALOG_ID": dialog_id, "MESSAGE": text}, timeout)
    print_check(f"call: im.message.add (DIALOG_ID={dialog_id})", r.ok, fmt_err(r.data))
    if r.ok:
        msg_id = safe_get(r.data, ["result"])
        print(f"  message_id: {msg_id}")
    return r


def test_openlines_by_user_code(
    base: str,
    timeout: int,
    user_code: str,
    try_history: bool,
) -> None:
    """
    1) imopenlines.session.open(USER_CODE) -> chatId
    2) imopenlines.dialog.get(CHAT_ID) -> dialog payload
    3) Try extract SESSION_ID from entity_data_1 (portal-specific)
    4) Optionally call session.history.get(SESSION_ID)
    """
    r_open = b24_post(base, "imopenlines.session.open", {"USER_CODE": user_code}, timeout)
    print_check("call: imopenlines.session.open", r_open.ok, fmt_err(r_open.data))
    if not r_open.ok:
        return

    chat_id = safe_get(r_open.data, ["result", "chatId"])
    if not chat_id:
        print_check("session.open chatId", False, "No chatId in response")
        return

    print(f"  chatId: {chat_id}")

    r_dlg = b24_post(base, "imopenlines.dialog.get", {"CHAT_ID": chat_id}, timeout)
    print_check("call: imopenlines.dialog.get", r_dlg.ok, fmt_err(r_dlg.data))
    if not r_dlg.ok:
        return

    dlg = safe_get(r_dlg.data, ["result"])
    if not isinstance(dlg, dict):
        print_check("dialog.get parse", False, "No result object")
        return

    dialog_id = dlg.get("dialog_id")
    entity_data_1 = dlg.get("entity_data_1") or ""
    print(f"  dialog_id: {dialog_id}")
    print(f"  entity_id: {dlg.get('entity_id')}")
    print(f"  entity_data_1: {entity_data_1}")

    sess_id = parse_session_id_from_entity_data_1(entity_data_1)
    if sess_id is None:
        print_check("SESSION_ID extract", False, "Could not parse from entity_data_1 (portal-specific encoding)")
        print("  Tip: dump full dialog.get payload and search for SESSION_ID/sessionId fields.")
        return

    print_check("SESSION_ID extract", True, str(sess_id))

    if try_history:
        r_hist = b24_post(base, "imopenlines.session.history.get", {"SESSION_ID": sess_id, "LIMIT": 50}, timeout)
        print_check(f"call: imopenlines.session.history.get (SESSION_ID={sess_id})", r_hist.ok, fmt_err(r_hist.data))
        if r_hist.ok:
            # print small summary
            res = safe_get(r_hist.data, ["result"])
            if isinstance(res, dict):
                msgs = res.get("messages")
                if isinstance(msgs, list):
                    print(f"  history messages: {len(msgs)}")
        else:
            # Useful hint
            print("  If ACCESS_DENIED: add this user as operator for the line / allow access to dialogs.")


# -----------------------------
# Main
# -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("B24_BASE", ""), help="Base webhook URL")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--list-groups", action="store_true", help="Print top-level method groups and counts")
    ap.add_argument("--dump-prefix", default="", help="Dump methods with this prefix (e.g. imopenlines., imconnector., im.)")
    ap.add_argument("--recent", action="store_true", help="Call im.recent.list and print recent dialogs")
    ap.add_argument("--send-dialog", default="", help="Send message to given DIALOG_ID (e.g. chat31)")
    ap.add_argument("--send-text", default="", help="Message text for --send-dialog")
    ap.add_argument("--user-code", default="", help="OpenLines USER_CODE (e.g. telegrambot|...)")
    ap.add_argument("--try-history", action="store_true", help="With --user-code: attempt session.history.get after extracting SESSION_ID")
    ap.add_argument("--raw", action="store_true", help="Dump raw JSON responses for some calls")
    args = ap.parse_args()

    if not args.base:
        print("Missing --base or B24_BASE env var.", file=sys.stderr)
        return 2

    base = _normalize_base(args.base)

    # Core checks
    prof = test_profile(base, args.timeout)
    if not prof.ok:
        return 3

    methods_resp, methods = test_methods(base, args.timeout)
    if not methods_resp.ok or not methods:
        return 4

    if args.list_groups:
        print_groups(methods)

    if args.dump_prefix:
        ms = filter_prefix(methods, args.dump_prefix)
        print(f"Methods with prefix '{args.dump_prefix}' ({len(ms)}):")
        for m in ms:
            print("  ", m)

    # Proxy readiness check
    proxy_code = test_proxy_methods(methods)

    # Optional calls
    if args.recent:
        items = test_recent(base, args.timeout)
        if args.raw and items is not None:
            pretty_print_json(items)

    if args.send_dialog:
        if not args.send_text:
            print_check("send-dialog", False, "Provide --send-text")
        else:
            r = test_send_dialog(base, args.timeout, args.send_dialog, args.send_text)
            if args.raw:
                pretty_print_json(r.data)

    if args.user_code:
        test_openlines_by_user_code(base, args.timeout, args.user_code, args.try_history)

    # Return non-zero only if proxy-critical methods are missing
    return proxy_code


if __name__ == "__main__":
    raise SystemExit(main())