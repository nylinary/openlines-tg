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
