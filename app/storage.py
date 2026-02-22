from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Dict, List, Optional

import redis.asyncio as redis

if TYPE_CHECKING:
    from .database import Database

log = logging.getLogger("app.storage")


class Storage:
    def __init__(self, redis_url: str, *, db: Optional["Database"] = None):
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._db: Optional["Database"] = db

    async def close(self) -> None:
        await self._redis.aclose()

    @staticmethod
    def _h(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    async def dedupe_set(self, key: str, ttl_s: int = 24 * 3600) -> bool:
        """Returns True if the key was newly set (i.e. not seen before)."""
        # SET key value NX EX ttl
        return bool(await self._redis.set(key, "1", nx=True, ex=ttl_s))

    # --- Bitrix OAuth token storage ---

    async def set_b24_tokens(self, *, access_token: str, refresh_token: str, expires_in: int) -> None:
        # Store as hash + set separate expiry timestamp.
        now = int(time.time())
        expires_at = now + max(int(expires_in), 0)
        await self._redis.hset(
            "b24:oauth",
            mapping={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": str(expires_at),
            },
        )

    async def get_b24_access_token(self) -> Optional[str]:
        token = await self._redis.hget("b24:oauth", "access_token")
        return token or None

    async def get_b24_refresh_token(self) -> Optional[str]:
        token = await self._redis.hget("b24:oauth", "refresh_token")
        return token or None

    async def get_b24_expires_at(self) -> Optional[int]:
        v = await self._redis.hget("b24:oauth", "expires_at")
        if not v:
            return None
        try:
            return int(v)
        except ValueError:
            return None

    async def b24_token_is_expiring(self, *, skew_s: int = 60) -> bool:
        exp = await self.get_b24_expires_at()
        if not exp:
            return True
        return int(time.time()) >= (exp - skew_s)

    # --- Chat conversation history ---

    _CHAT_HISTORY_PREFIX = "chat:history:"
    _CHAT_HISTORY_TTL = 24 * 3600  # 24 hours

    async def append_chat_message(self, dialog_id: str, role: str, text: str) -> None:
        """Append a message to the conversation history for a dialog.

        Writes to Redis (primary, fast) and PostgreSQL (durable backup).
        """
        key = f"{self._CHAT_HISTORY_PREFIX}{dialog_id}"
        entry = json.dumps({"role": role, "text": text}, ensure_ascii=False)
        await self._redis.rpush(key, entry)
        await self._redis.expire(key, self._CHAT_HISTORY_TTL)
        # Keep history bounded
        await self._redis.ltrim(key, -60, -1)
        # Write-through to PostgreSQL
        if self._db:
            try:
                await self._db.append_chat_message(dialog_id, role, text)
            except Exception as e:
                log.warning("pg_chat_write_error", extra={"error": str(e)})

    async def get_chat_history(self, dialog_id: str, *, limit: int = 20) -> List[Dict[str, str]]:
        """Retrieve the last ``limit`` messages from conversation history.

        Reads from Redis first; falls back to PostgreSQL if Redis has no data.
        """
        key = f"{self._CHAT_HISTORY_PREFIX}{dialog_id}"
        raw_items = await self._redis.lrange(key, -limit, -1)
        result: List[Dict[str, str]] = []
        for raw in raw_items:
            try:
                entry = json.loads(raw)
                if isinstance(entry, dict) and "role" in entry and "text" in entry:
                    result.append({"role": entry["role"], "text": entry["text"]})
            except (json.JSONDecodeError, TypeError):
                continue
        # Fallback to PostgreSQL if Redis is empty (e.g. after restart)
        if not result and self._db:
            try:
                result = await self._db.get_chat_history(dialog_id, limit=limit)
            except Exception as e:
                log.warning("pg_chat_read_error", extra={"error": str(e)})
        return result

    async def clear_chat_history(self, dialog_id: str) -> None:
        """Clear conversation history for a dialog."""
        key = f"{self._CHAT_HISTORY_PREFIX}{dialog_id}"
        await self._redis.delete(key)

    # --- Bot session state tracking ---

    _SESSION_PREFIX = "bot:session:"
    _SESSION_TTL = 7 * 24 * 3600  # 7 days

    async def _ensure_session_hash(self, key: str) -> None:
        """If the key exists as a non-hash type (legacy string), delete it first.

        Old code stored session state as a plain Redis string via
        ``redis.set(key, "transferred")``.  The new code uses hashes.
        Calling HSET/HGET on a string key raises WRONGTYPE, so we need
        to detect and migrate by deleting the old key.
        """
        try:
            key_type = await self._redis.type(key)
        except Exception:
            return
        if key_type not in ("hash", "none"):
            log.info("session_key_migrated", extra={"key": key, "old_type": key_type})
            await self._redis.delete(key)

    async def set_session_info(
        self,
        chat_id: str,
        *,
        state: str,
        dialog_id: str = "",
        user_id: str = "",
        line_id: str = "",
    ) -> None:
        """Store full session info as a Redis hash."""
        key = f"{self._SESSION_PREFIX}{chat_id}"
        await self._ensure_session_hash(key)
        mapping: Dict[str, str] = {"state": state, "ts": str(int(time.time()))}
        if dialog_id:
            mapping["dialog_id"] = dialog_id
        if user_id:
            mapping["user_id"] = user_id
        if line_id:
            mapping["line_id"] = line_id
        await self._redis.hset(key, mapping=mapping)
        await self._redis.expire(key, self._SESSION_TTL)

    async def mark_session_transferred(self, chat_id: str) -> None:
        """Mark that the bot transferred this chat to an operator."""
        key = f"{self._SESSION_PREFIX}{chat_id}"
        await self._ensure_session_hash(key)
        await self._redis.hset(key, mapping={"state": "transferred", "ts": str(int(time.time()))})
        await self._redis.expire(key, self._SESSION_TTL)

    async def mark_session_active(self, chat_id: str) -> None:
        """Mark that the bot is active in this chat."""
        key = f"{self._SESSION_PREFIX}{chat_id}"
        await self._ensure_session_hash(key)
        await self._redis.hset(key, mapping={"state": "bot_active", "ts": str(int(time.time()))})
        await self._redis.expire(key, self._SESSION_TTL)

    async def mark_session_closed(self, chat_id: str) -> None:
        """Mark that the bot was removed from this chat (session closed or operator took over)."""
        key = f"{self._SESSION_PREFIX}{chat_id}"
        await self._ensure_session_hash(key)
        await self._redis.hset(key, mapping={"state": "closed", "ts": str(int(time.time()))})
        await self._redis.expire(key, self._SESSION_TTL)

    async def get_session_state(self, chat_id: str) -> Optional[str]:
        """Get the current session state: 'bot_active', 'transferred', 'closed', or None."""
        key = f"{self._SESSION_PREFIX}{chat_id}"
        try:
            val = await self._redis.hget(key, "state")
            return val or None
        except Exception:
            # Legacy string key — read its value and migrate
            try:
                val = await self._redis.get(key)
                if val:
                    await self._redis.delete(key)
                    await self._redis.hset(key, mapping={"state": val, "ts": str(int(time.time()))})
                    await self._redis.expire(key, self._SESSION_TTL)
                    return val
            except Exception:
                pass
            return None

    async def get_session_info(self, chat_id: str) -> Dict[str, str]:
        """Get full session info hash."""
        key = f"{self._SESSION_PREFIX}{chat_id}"
        try:
            data = await self._redis.hgetall(key)
            return data if isinstance(data, dict) else {}
        except Exception:
            # Legacy string key
            return {}

    async def get_all_tracked_sessions(self) -> Dict[str, Dict[str, str]]:
        """Scan all tracked sessions. Returns {chat_id: {state, dialog_id, ...}}."""
        result: Dict[str, Dict[str, str]] = {}
        prefix = self._SESSION_PREFIX
        async for key in self._redis.scan_iter(match=f"{prefix}*", count=100):
            chat_id = key[len(prefix):]
            try:
                data = await self._redis.hgetall(key)
                if isinstance(data, dict) and data.get("state"):
                    result[chat_id] = data
                    continue
            except Exception:
                pass
            # Legacy string key — try to read and migrate
            try:
                val = await self._redis.get(key)
                if val and isinstance(val, str):
                    await self._redis.delete(key)
                    mapping = {"state": val, "ts": str(int(time.time()))}
                    await self._redis.hset(key, mapping=mapping)
                    await self._redis.expire(key, self._SESSION_TTL)
                    result[chat_id] = mapping
                    log.info("session_key_migrated", extra={"chat_id": chat_id, "old_state": val})
            except Exception:
                pass
        return result
