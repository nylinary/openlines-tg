"""AI-powered chat handler for the Bitrix imbot.

Combines:
- OpenAI for generating responses
- Product catalog for search / recommendations
- Conversation history from Redis
- FAQ knowledge base
- "оператор" keyword → transfer to live operator
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .llm import LLMError, LLMProvider
from .scraper import ProductCatalog
from .storage import Storage

log = logging.getLogger("app.ai_chat")

# ---------------------------------------------------------------------------
# Markdown stripping — Bitrix IM doesn't render Markdown so we remove it
# ---------------------------------------------------------------------------

_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")          # **bold**
_RE_ITALIC_STAR = re.compile(r"\*(.+?)\*")        # *italic*
_RE_ITALIC_UNDER = re.compile(r"(?<!\w)_(.+?)_(?!\w)")  # _italic_
_RE_STRIKE = re.compile(r"~~(.+?)~~")             # ~~strike~~
_RE_INLINE_CODE = re.compile(r"`(.+?)`")          # `code`
_RE_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)   # ### heading
_RE_LINK = re.compile(r"\[([^\]]+)]\(([^)]+)\)")  # [text](url)


def _strip_markdown(text: str) -> str:
    """Remove Markdown formatting that Bitrix IM cannot render."""
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_ITALIC_STAR.sub(r"\1", text)
    text = _RE_ITALIC_UNDER.sub(r"\1", text)
    text = _RE_STRIKE.sub(r"\1", text)
    text = _RE_INLINE_CODE.sub(r"\1", text)
    text = _RE_HEADING.sub("", text)
    # [text](url) → text (url)  — keep the URL visible
    text = _RE_LINK.sub(r"\1 (\2)", text)
    return text


# Maximum history messages to include in the prompt (pairs of user+assistant)
MAX_HISTORY_MESSAGES = 20

# Maximum products to include in search results sent to GPT
MAX_SEARCH_RESULTS_FOR_GPT = 8

# System prompt template
SYSTEM_PROMPT = """Ты — AI-консультант интернет-магазина морепродуктов myryba.ru (МояРыба).
Твоя задача — помогать покупателям с выбором продуктов, отвечать на вопросы о товарах, наличии, ценах, доставке и хранении.

Правила:
1. Отвечай дружелюбно, кратко и по делу. Используй эмодзи уместно.
2. Если покупатель спрашивает о конкретном товаре — используй данные из [Результаты поиска по каталогу], прикреплённые к его сообщению. Давай точную информацию: цена, наличие, ссылка.
3. Если запрос неоднозначный — задай уточняющий вопрос.
4. Если товара нет в наличии — предложи аналоги из той же категории.
5. Всегда указывай цену в рублях и ссылку на товар, если есть.
6. Не придумывай товары, которых нет в каталоге. Если не нашёл — так и скажи.
7. Формат ответа: только обычный текст. НЕ используй Markdown-разметку (жирный, курсив, заголовки, списки с *, ссылки []() и т.д.) — Bitrix IM её не поддерживает.

ВАЖНО — перевод на оператора:
Если покупатель хочет поговорить с живым человеком, оператором, менеджером, оформить заказ, купить, сделать заказ, или любым другим образом выражает намерение, что ему нужен реальный человек, а не бот — начни свой ответ СТРОГО с метки [TRANSFER] (именно так, в квадратных скобках, в самом начале сообщения, отдельной строкой).
После метки [TRANSFER] напиши дружелюбное сообщение клиенту, что переводишь его на оператора.
Примеры ситуаций для [TRANSFER]: "хочу заказать", "можно оформить?", "давайте закажу", "есть живой человек?", "хочу купить", "а можно с менеджером поговорить?", "сделайте заказ", "оформите доставку" и т.п.
Если покупатель просто спрашивает о товарах, ценах, наличии — это НЕ повод для перевода, отвечай сам.

FAQ — частые вопросы:
- Адрес: Москва (точный адрес уточняйте у оператора или на сайте myryba.ru)
- Режим работы: ежедневно (точные часы уточняйте на сайте)
- Доставка: доставка по Москве и МО, подробности на сайте myryba.ru
- Оплата: наличные, карта, онлайн-оплата
- Хранение: морепродукты хранить в морозильной камере при -18°C, размороженные — в холодильнике до 24ч
- Срок годности: зависит от продукта, обычно 6-12 месяцев для замороженных, смотрите упаковку
- Возврат: по закону о защите прав потребителей, свяжитесь с оператором

Доступные категории товаров:
{catalog_summary}

Полная информация о товарах приходит в [Результаты поиска по каталогу] в каждом сообщении пользователя. Используй именно эти данные для ответов.
"""


class AIChatHandler:
    """Handles incoming chat messages with AI-powered responses."""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        catalog: ProductCatalog,
        storage: Storage,
    ):
        self.gpt = llm
        self.catalog = catalog
        self.storage = storage
        self._system_prompt_cache: Optional[str] = None
        self._system_prompt_product_count: int = 0

    def _get_system_prompt(self) -> str:
        """Build system prompt with current catalog data (cached)."""
        current_count = len(self.catalog.products)
        if self._system_prompt_cache is None or self._system_prompt_product_count != current_count:
            summary = self.catalog.build_catalog_summary()
            self._system_prompt_cache = SYSTEM_PROMPT.format(catalog_summary=summary)
            self._system_prompt_product_count = current_count
            log.info("system_prompt_rebuilt", extra={"product_count": current_count, "prompt_length": len(self._system_prompt_cache)})
        return self._system_prompt_cache

    def invalidate_system_prompt_cache(self) -> None:
        """Force rebuild of system prompt (after catalog update)."""
        self._system_prompt_cache = None

    # --- Intent detection (lightweight, before GPT) ---

    _TRANSFER_TAG_RE = re.compile(r"^\s*\[TRANSFER\]\s*", re.IGNORECASE)

    @staticmethod
    def detect_product_search(text: str) -> Optional[str]:
        """Try to extract a product search query from the message.

        Returns the search query string if it looks like a product search, None otherwise.
        """
        lower = text.lower().strip()
        # Explicit search patterns
        search_patterns = [
            r"(?:найди|покажи|ищу|поищи|есть ли|есть|в наличии)\s+(.+)",
            r"(?:хочу|хотел бы|интересует|нужн[аы]?)\s+(.+)",
            r"(?:сколько стоит|цена|почём|почем)\s+(.+)",
            r"(?:расскажи про|что за|что такое)\s+(.+)",
        ]
        for pattern in search_patterns:
            m = re.search(pattern, lower)
            if m:
                return m.group(1).strip()
        return None

    # --- Conversation history ---

    async def _get_history(self, dialog_id: str) -> List[Dict[str, str]]:
        """Retrieve conversation history from Redis."""
        return await self.storage.get_chat_history(dialog_id, limit=MAX_HISTORY_MESSAGES)

    async def _save_message(self, dialog_id: str, role: str, text: str) -> None:
        """Save a message to conversation history in Redis."""
        await self.storage.append_chat_message(dialog_id, role, text)

    # --- Main handler ---

    async def handle_message(
        self,
        dialog_id: str,
        user_text: str,
    ) -> Tuple[str, bool]:
        """Process a user message and generate a response.

        Returns:
            (response_text, transfer_to_operator)
        """
        # 1. Search for relevant products to enrich the GPT context
        search_query = self.detect_product_search(user_text)
        product_context = ""

        if search_query:
            found = self.catalog.search(search_query, limit=MAX_SEARCH_RESULTS_FOR_GPT)
            if found:
                product_context = self._format_search_results(found)
        else:
            # Even without explicit search, try finding products mentioned in the text
            found = self.catalog.search(user_text, limit=5)
            if found:
                product_context = self._format_search_results(found)

        # 2. Build messages for GPT
        system_prompt = self._get_system_prompt()

        messages: List[Dict[str, str]] = [
            {"role": "system", "text": system_prompt},
        ]

        # Add conversation history
        history = await self._get_history(dialog_id)
        messages.extend(history)

        # Add product search context if found
        user_message = user_text
        if product_context:
            user_message = (
                f"{user_text}\n\n"
                f"[Результаты поиска по каталогу для контекста — используй эту информацию в ответе]:\n"
                f"{product_context}"
            )

        messages.append({"role": "user", "text": user_message})

        # 3. Call LLM
        try:
            reply = await self.gpt.completion(messages)
        except LLMError as e:
            log.error("ai_chat_llm_error", extra={"dialog_id": dialog_id, "error": str(e)})
            reply = (
                "Извините, произошла техническая ошибка. "
                "Попробуйте ещё раз или напишите «оператор» для связи с менеджером."
            )

        # 3b. Strip Markdown — GPT may still produce it despite the prompt
        reply = _strip_markdown(reply)

        # 4. Detect [TRANSFER] tag in GPT reply → operator transfer
        transfer = False
        if self._TRANSFER_TAG_RE.search(reply):
            transfer = True
            reply = self._TRANSFER_TAG_RE.sub("", reply).strip()
            log.info("ai_transfer_detected", extra={"dialog_id": dialog_id})

        # 5. Save messages to history
        await self._save_message(dialog_id, "user", user_text)
        await self._save_message(dialog_id, "assistant", reply)

        return (reply, transfer)

    def _format_search_results(self, products: List[Dict[str, Any]]) -> str:
        """Format product search results for inclusion in GPT context."""
        lines: List[str] = []
        for p in products:
            parts = [p.get("title", "?")]
            if p.get("price"):
                price_str = f"{p['price']}₽"
                if p.get("priceold"):
                    price_str += f" (было {p['priceold']}₽)"
                parts.append(price_str)
            if p.get("portion"):
                parts.append(str(p["portion"]))
            qty = p.get("quantity", "")
            if qty == "0":
                parts.append("нет в наличии")
            else:
                parts.append("в наличии")
            if p.get("url"):
                url = p["url"]
                if not url.startswith("http"):
                    url = f"https://myryba.ru{url}"
                parts.append(url)
            if p.get("characteristics"):
                chars = "; ".join(f"{c['title']}: {c['value']}" for c in p["characteristics"] if c.get("title"))
                if chars:
                    parts.append(f"({chars})")
            lines.append(" | ".join(parts))
        return "\n".join(lines)
