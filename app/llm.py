"""LLM provider — OpenAI Chat Completions API.

Accepts messages as ``[{"role": ..., "text": ...}]``
and returns the assistant's reply as a plain string.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("app.llm")


class LLMError(RuntimeError):
    """Base exception for any LLM provider failure."""
    pass


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Common interface for chat-completion providers."""

    @abstractmethod
    async def completion(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Send messages and return the assistant's reply text.

        ``messages`` format (provider-agnostic)::

            [
                {"role": "system", "text": "You are helpful."},
                {"role": "user",   "text": "Hello!"},
            ]
        """
        ...

    @abstractmethod
    async def close(self) -> None: ...

    @property
    @abstractmethod
    def provider_name(self) -> str: ...


# ---------------------------------------------------------------------------
# OpenAI-compatible provider
# ---------------------------------------------------------------------------

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions API (works with GPT-4o, GPT-4o-mini, etc.)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o-mini",
        timeout_s: float = 60.0,
        max_tokens: int = 2000,
        temperature: float = 0.3,
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._base_url = (base_url or OPENAI_CHAT_URL).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    @property
    def provider_name(self) -> str:
        return f"openai/{self.model}"

    async def close(self) -> None:
        await self._client.aclose()

    # OpenAI uses {"role": ..., "content": ...}
    @staticmethod
    def _to_openai_messages(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        return [{"role": m["role"], "content": m["text"]} for m in messages]

    async def completion(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": self._to_openai_messages(messages),
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        log.info(
            "openai_request",
            extra={
                "model": self.model,
                "messages_count": len(messages),
                "temperature": body["temperature"],
            },
        )

        try:
            r = await self._client.post(self._base_url, json=body, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.error("openai_network_error", extra={"error": str(e)})
            raise LLMError(f"OpenAI network error: {e}") from e

        if r.status_code != 200:
            detail = r.text[:500] if r.text else "no body"
            log.error("openai_api_error", extra={"status": r.status_code, "detail": detail})
            raise LLMError(f"OpenAI API error {r.status_code}: {detail}")

        data = r.json()

        try:
            reply_text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            log.error("openai_parse_error", extra={"response": str(data)[:500]})
            raise LLMError(f"Failed to parse OpenAI response: {e}") from e

        usage = data.get("usage", {})
        log.info(
            "openai_response_ok",
            extra={
                "reply_length": len(reply_text),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
        )
        return reply_text


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_llm_provider(
    *,
    api_key: str = "",
    model: str = "gpt-4o-mini",
    base_url: str = "",
    temperature: float = 0.3,
    max_tokens: int = 2000,
    timeout_s: float = 60.0,
) -> OpenAIProvider:
    """Create an OpenAI LLM provider.

    Works with any OpenAI-compatible API (Azure, local proxy, etc.)
    by passing ``base_url``.
    """
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required")
    return OpenAIProvider(
        api_key=api_key,
        model=model,
        base_url=base_url or None,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
    )
