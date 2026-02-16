from __future__ import annotations

import hashlib
import time
from typing import Optional

import redis.asyncio as redis


class Storage:
    def __init__(self, redis_url: str):
        self._redis = redis.from_url(redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._redis.aclose()

    @staticmethod
    def _h(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    async def dedupe_set(self, key: str, ttl_s: int = 24 * 3600) -> bool:
        """Returns True if the key was newly set (i.e. not seen before)."""
        # SET key value NX EX ttl
        return bool(await self._redis.set(key, "1", nx=True, ex=ttl_s))

    # --- mappings ---

    async def map_external_to_tg_chat(self, external_chat_id: str, tg_chat_id: str) -> None:
        await self._redis.hset("map:external_chat_to_tg", external_chat_id, tg_chat_id)

    async def get_tg_chat_by_external_chat(self, external_chat_id: str) -> Optional[str]:
        return await self._redis.hget("map:external_chat_to_tg", external_chat_id)

    async def map_tg_chat_to_external_chat(self, tg_chat_id: str, external_chat_id: str) -> None:
        await self._redis.hset("map:tg_chat_to_external", tg_chat_id, external_chat_id)

    async def get_external_chat_by_tg_chat(self, tg_chat_id: str) -> Optional[str]:
        return await self._redis.hget("map:tg_chat_to_external", tg_chat_id)

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
