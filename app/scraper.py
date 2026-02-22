"""Product scraper for myryba.ru (Tilda-based seafood store).

Fetches product data from Tilda Store API by first discovering ``storepart``
and ``recid`` IDs from category pages, then calling the products list endpoint.

Data is persisted to PostgreSQL via the Database layer.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
import snowballstemmer

log = logging.getLogger("app.scraper")

# Russian stemmer (singleton, thread-safe for reads)
_ru_stemmer = snowballstemmer.stemmer("russian")

BASE_URL = "https://myryba.ru"
TILDA_STORE_API = "https://store.tildacdn.com/api/getproductslist/"

# Known category slugs (order preserved)
CATEGORY_SLUGS: List[str] = [
    "aktsii",
    "ikra",
    "krevetki",
    "grebeshok",
    "krab",
    "molluski",
    "ryba",
    "vyalenaya_i_kopchenaya_productsiya",
    "polufabrikaty",
    "bakaleya",
    "podarki",
    "raznoe",
]

# Headers that mimic a normal browser to avoid 403s
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://myryba.ru/",
}


async def _fetch_category_page(
    client: httpx.AsyncClient,
    slug: str,
    *,
    retries: int = 3,
    backoff: float = 3.0,
) -> Optional[str]:
    """Fetch the HTML of a category page to extract store IDs."""
    url = f"{BASE_URL}/{slug}"
    for attempt in range(1, retries + 1):
        try:
            r = await client.get(url, headers=_HEADERS, follow_redirects=True)
            if r.status_code == 200:
                return r.text
            if r.status_code == 403:
                wait = backoff * attempt
                log.warning("category_page_403", extra={"slug": slug, "attempt": attempt, "wait_s": wait})
                await asyncio.sleep(wait)
                continue
            log.warning("category_page_error", extra={"slug": slug, "status": r.status_code})
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            wait = backoff * attempt
            log.warning("category_page_network_error", extra={"slug": slug, "error": str(e), "wait_s": wait})
            await asyncio.sleep(wait)
    return None


def _extract_store_ids(html: str) -> List[Tuple[str, str]]:
    """Extract (storepartuid, recid) pairs from Tilda page HTML.

    Tilda embeds JS like::

        recid:'1467582211',storepart:'735258288902', ...

    Both values are numeric strings.  We look for ``recid`` + ``storepart``
    pairs that appear close together in the same JS block.
    """
    pairs: List[Tuple[str, str]] = []
    seen: set[Tuple[str, str]] = set()

    # Best pattern: recid:'{digits}',storepart:'{digits}' on the same line
    combined = re.findall(
        r"recid\s*[:=]\s*['\"]?(\d{5,})['\"]?\s*[,;].*?storepart\s*[:=]\s*['\"]?(\d{5,})['\"]?",
        html,
        re.IGNORECASE,
    )
    for rid, sp in combined:
        key = (sp, rid)
        if key not in seen:
            seen.add(key)
            pairs.append(key)

    # Reverse order: storepart before recid
    combined_rev = re.findall(
        r"storepart\s*[:=]\s*['\"]?(\d{5,})['\"]?\s*[,;].*?recid\s*[:=]\s*['\"]?(\d{5,})['\"]?",
        html,
        re.IGNORECASE,
    )
    for sp, rid in combined_rev:
        key = (sp, rid)
        if key not in seen:
            seen.add(key)
            pairs.append(key)

    # Fallback: also accept hex-UUID style storepart (some Tilda themes)
    if not pairs:
        storeparts = re.findall(r"storepart\w*\s*[:=]\s*['\"]?([a-f0-9-]{8,})['\"]?", html, re.IGNORECASE)
        recids = re.findall(r"recid\s*[:=]\s*['\"]?(\d{5,})['\"]?", html, re.IGNORECASE)
        for sp, rid in zip(storeparts, recids):
            key = (sp, rid)
            if key not in seen:
                seen.add(key)
                pairs.append(key)

    return pairs


async def _fetch_products(
    client: httpx.AsyncClient,
    storepart_uid: str,
    recid: str,
    *,
    retries: int = 3,
    backoff: float = 3.0,
) -> List[Dict[str, Any]]:
    """Fetch products list from Tilda Store API."""
    params = {
        "storepartuid": storepart_uid,
        "recid": recid,
        "c": "1",
        "getparts": "true",
        "getoptions": "true",
        "slice": "1",
        "size": "500",
    }
    for attempt in range(1, retries + 1):
        try:
            r = await client.get(TILDA_STORE_API, params=params, headers=_HEADERS)
            if r.status_code == 200:
                data = r.json()
                products = data.get("products", [])
                if isinstance(products, list):
                    return products
                log.warning("tilda_api_no_products", extra={"storepart": storepart_uid, "data_keys": list(data.keys())})
                return []
            if r.status_code == 403:
                wait = backoff * attempt
                log.warning("tilda_api_403", extra={"storepart": storepart_uid, "attempt": attempt, "wait_s": wait})
                await asyncio.sleep(wait)
                continue
            log.warning("tilda_api_error", extra={"storepart": storepart_uid, "status": r.status_code})
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            wait = backoff * attempt
            log.warning("tilda_api_network", extra={"storepart": storepart_uid, "error": str(e), "wait_s": wait})
            await asyncio.sleep(wait)
    return []


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalise_product(raw: Dict[str, Any], category_slug: str) -> Dict[str, Any]:
    """Normalise a Tilda product dict into a cleaner structure."""
    editions: List[Dict[str, Any]] = []
    for ed in raw.get("editions", []):
        if isinstance(ed, dict):
            editions.append({
                "uid": ed.get("uid", ""),
                "price": ed.get("price", ""),
                "priceold": ed.get("priceold", ""),
                "sku": ed.get("sku", ""),
                "text": ed.get("text", ""),
                "quantity": ed.get("quantity", ""),
            })

    characteristics: List[Dict[str, str]] = []
    for ch in raw.get("characteristics", []):
        if isinstance(ch, dict):
            characteristics.append({
                "title": ch.get("title", ""),
                "value": ch.get("value", ""),
            })

    return {
        "uid": raw.get("uid", ""),
        "title": raw.get("title", ""),
        "sku": raw.get("sku", ""),
        "text": raw.get("text", ""),
        "descr": raw.get("descr", ""),
        "price": raw.get("price", ""),
        "priceold": raw.get("priceold", ""),
        "quantity": raw.get("quantity", ""),
        "portion": raw.get("portion", ""),
        "unit": raw.get("unit", ""),
        "mark": raw.get("mark", ""),
        "url": raw.get("url", ""),
        # gallery (image URLs) intentionally excluded — Bitrix IM chat
        # only supports plain text, so images cannot be displayed inline.
        # Product page URLs are included and customers can see photos there.
        "editions": editions,
        "characteristics": characteristics,
        "category": category_slug,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class ProductCatalog:
    """In-memory product catalog with PostgreSQL persistence."""

    def __init__(self, db=None):
        self.products: List[Dict[str, Any]] = []
        self.last_full_scrape: float = 0.0
        self.last_price_refresh: float = 0.0
        # PostgreSQL backend (app.database.Database instance)
        self._db = db
        # Pre-computed stemmed search index: list of (title_stem_words, other_stem_words)
        # Built once after load/scrape, avoids re-stemming on every search.
        self._search_index: List[Tuple[List[str], List[str]]] = []

    # --- Stemmed search index ---

    # Regex to strip punctuation that prevents proper stemming.
    # Keeps letters (incl. Cyrillic), digits, hyphens within words.
    _RE_PUNCT = re.compile(r"[^\w\-]", re.UNICODE)

    @classmethod
    def _stem_text(cls, text: str) -> str:
        """Normalize + stem text for search index.

        Applies lowercase, ё→е normalisation, strips punctuation,
        then Russian Snowball stemming.  Returns space-joined stems.
        """
        normalised = text.lower().replace("ё", "е")
        # Strip punctuation (commas, dots, etc.) that prevent stemming
        cleaned = cls._RE_PUNCT.sub(" ", normalised)
        words = cleaned.split()
        if not words:
            return ""
        return " ".join(_ru_stemmer.stemWords(words))

    def _build_search_index(self) -> None:
        """Pre-compute stemmed word lists for every product.

        Stores a parallel list of ``(title_stem_words, other_stem_words)``
        tuples (each a ``List[str]``) aligned 1-to-1 with ``self.products``.
        Called once after load/scrape/refresh so that ``search()`` doesn't
        re-stem on every query.
        """
        index: List[Tuple[List[str], List[str]]] = []
        for p in self.products:
            title_stems = self._stem_text(p.get("title", "")).split()
            other_text = " ".join([
                p.get("text", ""),
                p.get("descr", ""),
                p.get("category", ""),
                p.get("sku", ""),
                " ".join(
                    c.get("title", "") + " " + c.get("value", "")
                    for c in p.get("characteristics", [])
                ),
            ])
            other_stems = self._stem_text(other_text).split()
            index.append((title_stems, other_stems))
        self._search_index = index
        log.info("search_index_built", extra={"products": len(index)})

    # --- PostgreSQL persistence ---

    async def load_from_db(self) -> bool:
        """Load products from PostgreSQL. Returns True if loaded."""
        if not self._db:
            log.warning("catalog_no_db_configured")
            return False
        try:
            self.products = await self._db.load_all_products()
            meta = await self._db.get_scrape_meta()
            self.last_full_scrape = meta.get("last_full_scrape", 0.0)
            self.last_price_refresh = meta.get("last_price_refresh", 0.0)
            if self.products:
                self._build_search_index()
                log.info("catalog_loaded_from_db", extra={"count": len(self.products)})
                return True
            log.info("catalog_db_empty")
            return False
        except Exception as e:
            log.warning("catalog_db_load_error", extra={"error": str(e)})
            return False

    async def _save_to_db_full(self) -> None:
        """Replace all products in PostgreSQL and update scrape metadata."""
        if not self._db:
            return
        try:
            await self._db.replace_all_products(self.products)
            await self._db.set_scrape_meta(
                last_full_scrape=self.last_full_scrape,
                last_price_refresh=self.last_price_refresh,
            )
        except Exception as e:
            log.warning("catalog_db_save_error", extra={"error": str(e)})

    async def _save_to_db_prices(self) -> None:
        """Update prices in PostgreSQL and refresh metadata."""
        if not self._db:
            return
        try:
            await self._db.upsert_products(self.products)
            await self._db.set_scrape_meta(
                last_price_refresh=self.last_price_refresh,
            )
        except Exception as e:
            log.warning("catalog_db_price_save_error", extra={"error": str(e)})

    # --- Scraping ---

    async def full_scrape(self, delay_between_categories: float = 2.0) -> int:
        """Scrape all categories, replacing the entire product list.

        Returns the total number of products scraped.
        """
        all_products: List[Dict[str, Any]] = []
        seen_uids: set[str] = set()

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            for slug in CATEGORY_SLUGS:
                log.info("scraping_category", extra={"slug": slug})

                html = await _fetch_category_page(client, slug)
                if not html:
                    log.warning("scraping_category_no_html", extra={"slug": slug})
                    await asyncio.sleep(delay_between_categories)
                    continue

                pairs = _extract_store_ids(html)
                if not pairs:
                    log.warning("scraping_category_no_ids", extra={"slug": slug})
                    await asyncio.sleep(delay_between_categories)
                    continue

                category_count = 0
                for sp_uid, rec_id in pairs:
                    raw_products = await _fetch_products(client, sp_uid, rec_id)
                    for rp in raw_products:
                        uid = rp.get("uid", "")
                        if uid and uid in seen_uids:
                            continue
                        if uid:
                            seen_uids.add(uid)
                        product = _normalise_product(rp, slug)
                        all_products.append(product)
                        category_count += 1

                    # Small delay between API calls within a category
                    await asyncio.sleep(1.0)

                log.info("scraping_category_done", extra={"slug": slug, "count": category_count})
                await asyncio.sleep(delay_between_categories)

        self.products = all_products
        self.last_full_scrape = time.time()
        self.last_price_refresh = time.time()
        await self._save_to_db_full()
        self._build_search_index()

        log.info("full_scrape_complete", extra={"total_products": len(self.products)})
        return len(self.products)

    async def refresh_prices(self, delay_between_categories: float = 2.0) -> int:
        """Refresh only price and quantity for existing products.

        Fetches product data again and updates price/quantity/priceold fields
        without replacing the whole catalog.
        """
        if not self.products:
            return await self.full_scrape(delay_between_categories)

        # Build a lookup by UID for quick updates
        by_uid: Dict[str, Dict[str, Any]] = {p["uid"]: p for p in self.products if p.get("uid")}
        updated = 0

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            for slug in CATEGORY_SLUGS:
                html = await _fetch_category_page(client, slug)
                if not html:
                    await asyncio.sleep(delay_between_categories)
                    continue

                pairs = _extract_store_ids(html)
                for sp_uid, rec_id in pairs:
                    raw_products = await _fetch_products(client, sp_uid, rec_id)
                    for rp in raw_products:
                        uid = rp.get("uid", "")
                        if uid in by_uid:
                            by_uid[uid]["price"] = rp.get("price", by_uid[uid]["price"])
                            by_uid[uid]["priceold"] = rp.get("priceold", by_uid[uid]["priceold"])
                            by_uid[uid]["quantity"] = rp.get("quantity", by_uid[uid]["quantity"])
                            updated += 1
                        else:
                            # New product appeared — add it
                            product = _normalise_product(rp, slug)
                            self.products.append(product)
                            by_uid[uid] = product
                            updated += 1

                    await asyncio.sleep(1.0)
                await asyncio.sleep(delay_between_categories)

        self.last_price_refresh = time.time()
        await self._save_to_db_prices()
        self._build_search_index()
        log.info("price_refresh_complete", extra={"updated": updated})
        return updated

    # --- Search ---

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize text for search: lowercase + ё→е."""
        return text.lower().replace("ё", "е")

    @staticmethod
    def _stem_match(query_stem: str, stem_words: List[str]) -> bool:
        """Check if a stemmed query term matches any stem word.

        Matching strategy (in order):
          1. **Exact match** — stems are identical.
          2. **Prefix match** — one stem starts with the other (both ≥ 3 chars).
          3. **Shared-root match** — both stems ≥ 5 chars and share a common
             prefix of at least ``min(len(a), len(b)) - 1`` chars.  This
             handles the Snowball edge case where the nominative-singular
             keeps a suffix that oblique cases lose, e.g.
             ``гребешок`` (8) vs ``гребешк`` (7) → shared prefix ``гребеш``
             (6) ≥ ``min(7,8) - 1 = 6``.

        A minimum stem length of 3 is enforced to prevent short-stem
        false positives (e.g. ``лос`` from ``лосось`` matching unrelated
        words).
        """
        qlen = len(query_stem)
        if qlen < 3:
            return query_stem in stem_words
        for sw in stem_words:
            slen = len(sw)
            # 1) Exact
            if query_stem == sw:
                return True
            minlen = min(qlen, slen)
            if minlen < 3:
                continue
            # 2) Prefix
            if sw.startswith(query_stem) or query_stem.startswith(sw):
                return True
            # 3) Shared-root: common prefix ≥ min(len) - 1, both ≥ 5
            if minlen >= 5:
                # Find common prefix length
                common = 0
                for a, b in zip(query_stem, sw):
                    if a != b:
                        break
                    common += 1
                if common >= minlen - 1:
                    return True
        return False

    def search(self, query: str, *, limit: int = 10) -> List[Dict[str, Any]]:
        """Product search with Russian stemming, title-boost and ё→е normalisation.

        Two-pass scoring:
          1. **Stemmed match** — query terms are stemmed and matched against
             the pre-computed stem index.  Catches morphological variants
             (гребешки→гребешок, крабов→краб, креветку→креветки).
          2. **Substring fallback** — original (un-stemmed but normalised)
             terms are checked via simple ``in`` on raw text.  Catches
             partial matches that stemming might miss (e.g. SKU fragments).

        Title matches score 3 pts, other fields 1 pt.
        """
        if not query.strip():
            return []

        norm_terms = self._normalize(query).split()
        stem_terms = _ru_stemmer.stemWords(norm_terms)
        has_index = len(self._search_index) == len(self.products)

        results: List[Tuple[int, Dict[str, Any]]] = []

        for i, p in enumerate(self.products):
            score = 0

            if has_index:
                title_stem_words, other_stem_words = self._search_index[i]
                for st in stem_terms:
                    if self._stem_match(st, title_stem_words):
                        score += 3
                    elif self._stem_match(st, other_stem_words):
                        score += 1

            # Substring fallback on raw normalised text (catches SKUs, etc.)
            if score == 0:
                title = self._normalize(p.get("title", ""))
                other = self._normalize(" ".join([
                    p.get("text", ""),
                    p.get("descr", ""),
                    p.get("category", ""),
                    p.get("sku", ""),
                    " ".join(c.get("title", "") + " " + c.get("value", "")
                             for c in p.get("characteristics", [])),
                ]))
                for t in norm_terms:
                    if t in title:
                        score += 3
                    elif t in other:
                        score += 1

            if score > 0:
                results.append((score, p))

        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:limit]]

    def get_by_category(self, category: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Get products by category slug."""
        return [p for p in self.products if p.get("category") == category][:limit]

    def get_available(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Get products that are in stock (quantity > 0 or not specified)."""
        result = []
        for p in self.products:
            qty = p.get("quantity", "")
            # Tilda uses empty or "-1" for unlimited, "0" for out of stock
            if qty == "0":
                continue
            result.append(p)
            if len(result) >= limit:
                break
        return result

    def format_product_short(self, p: Dict[str, Any]) -> str:
        """Format a product for chat display (short)."""
        parts = [f"**{p.get('title', 'Без названия')}**"]
        if p.get("price"):
            price_str = f"{p['price']} ₽"
            if p.get("priceold"):
                price_str = f"~~{p['priceold']}~~ {p['price']} ₽"
            parts.append(price_str)
        if p.get("portion"):
            parts.append(str(p["portion"]))
        qty = str(p.get("quantity", ""))
        if qty == "0":
            parts.append("❌ Нет в наличии")
        elif qty and qty != "-1":
            parts.append(f"В наличии: {qty}")
        if p.get("url"):
            url = str(p["url"])
            if not url.startswith("http"):
                url = f"{BASE_URL}{url}"
            parts.append(url)
        return "\n".join(parts)

    def build_catalog_summary(self) -> str:
        """Build a lightweight category summary for the AI system prompt.

        Only includes category names, product counts and stock info — NOT
        individual product listings.  The AI receives specific product data
        via search results injected into each user message, so the system
        prompt only needs a high-level overview.

        This keeps the system prompt small (~300 tokens instead of ~4,000+).
        """
        if not self.products:
            return "Каталог товаров пуст."

        # Group by category
        by_cat: Dict[str, List[Dict[str, Any]]] = {}
        for p in self.products:
            cat = p.get("category", "other")
            by_cat.setdefault(cat, []).append(p)

        total_in_stock = sum(
            1 for p in self.products if p.get("quantity", "") != "0"
        )

        lines: List[str] = [
            f"Всего {len(self.products)} товаров ({total_in_stock} в наличии), "
            f"{len(by_cat)} категорий:",
        ]

        for cat, prods in by_cat.items():
            in_stock = sum(1 for p in prods if p.get("quantity", "") != "0")
            # Price range
            prices = []
            for p in prods:
                try:
                    prices.append(float(str(p.get("price", "0")).replace(",", ".")))
                except (ValueError, TypeError):
                    pass
            price_range = ""
            if prices:
                lo, hi = min(prices), max(prices)
                price_range = f", {lo:.0f}–{hi:.0f}₽" if lo != hi else f", {lo:.0f}₽"

            lines.append(f"- {cat}: {len(prods)} шт ({in_stock} в наличии{price_range})")

        return "\n".join(lines)
