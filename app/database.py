"""PostgreSQL storage layer using SQLAlchemy async ORM.

Provides the :class:`Database` wrapper that manages an async engine +
session factory and exposes high-level methods for products, scrape
metadata and chat history.

Schema migrations are handled by **Alembic** (see ``alembic/`` directory).
On first run without Alembic, :meth:`Database.connect` will create tables
automatically via ``Base.metadata.create_all``.
"""
from __future__ import annotations

import logging
import re as _re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base, ChatMessage, Product, ScrapeMeta

log = logging.getLogger("app.database")

# Seed the single-row scrape_meta table.
_SEED_SCRAPE_META_SQL = """\
INSERT INTO scrape_meta (id) VALUES (1) ON CONFLICT DO NOTHING;
"""


class Database:
    """Async SQLAlchemy wrapper — drop-in replacement for the raw asyncpg version.

    Public API is identical: ``__init__(dsn)``, ``connect()``, ``close()``,
    plus CRUD methods for products, scrape meta, and chat messages.
    """

    def __init__(self, dsn: str) -> None:
        # SQLAlchemy requires ``postgresql+asyncpg://`` scheme
        sa_url = dsn
        if sa_url.startswith("postgresql://"):
            sa_url = sa_url.replace("postgresql://", "postgresql+asyncpg://", 1)

        self._dsn = dsn
        self._engine = create_async_engine(
            sa_url,
            pool_size=10,
            max_overflow=5,
            echo=False,
        )
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def connect(self) -> None:
        """Create tables (if needed) and seed metadata row."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text(_SEED_SCRAPE_META_SQL))
        log.info("pg_connected_and_migrated", extra={"dsn": _mask_dsn(self._dsn)})

    async def close(self) -> None:
        await self._engine.dispose()
        log.info("pg_engine_disposed")

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    async def upsert_products(self, products: List[Dict[str, Any]]) -> int:
        """Insert or update products.  Returns the number of rows affected."""
        if not products:
            return 0

        async with self._session_factory() as session:
            async with session.begin():
                for p in products:
                    values = _product_dict_to_row(p)
                    stmt = (
                        pg_insert(Product)
                        .values(**values)
                        .on_conflict_do_update(
                            index_elements=["uid"],
                            set_={k: v for k, v in values.items() if k != "uid"},
                        )
                    )
                    await session.execute(stmt)

        log.info("pg_products_upserted", extra={"count": len(products)})
        return len(products)

    async def replace_all_products(self, products: List[Dict[str, Any]]) -> int:
        """Replace the entire products table (used after full scrape).

        Uses a two-step approach: DELETE all, then bulk-INSERT.
        Products are inserted in batches to avoid excessively large
        statements and to surface per-batch errors.
        """
        if not products:
            return 0

        BATCH = 50
        rows = [_product_dict_to_row(p) for p in products]

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(delete(Product))

                for i in range(0, len(rows), BATCH):
                    batch = rows[i : i + BATCH]
                    stmt = pg_insert(Product).values(batch)
                    await session.execute(stmt)

        log.info("pg_products_replaced", extra={"count": len(products)})
        return len(products)

    async def load_all_products(self) -> List[Dict[str, Any]]:
        """Load all products as dicts (same shape as the JSON catalog)."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Product).order_by(Product.category, Product.title)
            )
            rows = result.scalars().all()

        products = [r.to_dict() for r in rows]
        log.info("pg_products_loaded", extra={"count": len(products)})
        return products

    async def update_prices(self, updates: List[Dict[str, str]]) -> int:
        """Batch-update price/priceold/quantity by UID."""
        if not updates:
            return 0

        count = 0
        async with self._session_factory() as session:
            async with session.begin():
                for u in updates:
                    uid = u.get("uid")
                    if not uid:
                        continue
                    values: Dict[str, Any] = {}
                    if u.get("price"):
                        values["price"] = u["price"]
                    if u.get("priceold"):
                        values["priceold"] = u["priceold"]
                    if u.get("quantity"):
                        values["quantity"] = u["quantity"]
                    if values:
                        await session.execute(
                            update(Product).where(Product.uid == uid).values(**values)
                        )
                        count += 1

        log.info("pg_prices_updated", extra={"count": count})
        return count

    async def search_fts(self, query: str, *, limit: int = 10) -> List[Dict[str, Any]]:
        """Full-text search using PostgreSQL ``ts_query`` with Russian config."""
        if not query.strip():
            return []

        normalised = query.lower().replace("ё", "е")
        words = normalised.split()
        ts_terms = " & ".join(f"{w}:*" for w in words if w)
        if not ts_terms:
            return []

        ts_query = func.to_tsquery("russian", ts_terms)
        rank = func.ts_rank_cd(Product.fts, ts_query, 32).label("rank")

        async with self._session_factory() as session:
            result = await session.execute(
                select(Product, rank)
                .where(Product.fts.op("@@")(ts_query))
                .order_by(rank.desc())
                .limit(limit)
            )
            rows = result.all()

        return [row[0].to_dict() for row in rows]

    # ------------------------------------------------------------------
    # Scrape metadata
    # ------------------------------------------------------------------

    async def get_scrape_meta(self) -> Dict[str, float]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ScrapeMeta).where(ScrapeMeta.id == 1)
            )
            meta = result.scalar_one_or_none()

        if meta:
            return {
                "last_full_scrape": meta.last_full_scrape,
                "last_price_refresh": meta.last_price_refresh,
            }
        return {"last_full_scrape": 0.0, "last_price_refresh": 0.0}

    async def set_scrape_meta(
        self,
        *,
        last_full_scrape: Optional[float] = None,
        last_price_refresh: Optional[float] = None,
    ) -> None:
        values: Dict[str, Any] = {}
        if last_full_scrape is not None:
            values["last_full_scrape"] = last_full_scrape
        if last_price_refresh is not None:
            values["last_price_refresh"] = last_price_refresh
        if not values:
            return

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(ScrapeMeta).where(ScrapeMeta.id == 1).values(**values)
                )

    # ------------------------------------------------------------------
    # Chat history
    # ------------------------------------------------------------------

    async def append_chat_message(self, dialog_id: str, role: str, text_content: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                session.add(ChatMessage(dialog_id=dialog_id, role=role, text=text_content))

    async def get_chat_history(
        self, dialog_id: str, *, limit: int = 20
    ) -> List[Dict[str, str]]:
        subq = (
            select(ChatMessage)
            .where(ChatMessage.dialog_id == dialog_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
            .subquery()
        )
        async with self._session_factory() as session:
            result = await session.execute(
                select(subq.c.role, subq.c.text)
                .order_by(subq.c.created_at.asc())
            )
            rows = result.all()

        return [{"role": r.role, "text": r.text} for r in rows]

    async def clear_chat_history(self, dialog_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(ChatMessage).where(ChatMessage.dialog_id == dialog_id)
                )

    async def cleanup_old_chats(self, *, max_age_hours: int = 24) -> int:
        """Delete chat messages older than ``max_age_hours``."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    delete(ChatMessage).where(ChatMessage.created_at < cutoff)
                )
                deleted = result.rowcount  # type: ignore[assignment]

        if deleted:
            log.info("pg_old_chats_cleaned", extra={"deleted": deleted})
        return deleted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _product_dict_to_row(p: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a catalog product dict to a flat dict for SQLAlchemy insert.

    All Text columns are explicitly cast to ``str`` because the Tilda API
    sometimes returns numeric values (e.g. ``uid`` as ``int``, ``portion``
    as ``int``) which asyncpg rejects for VARCHAR parameters.
    """
    def _s(key: str, default: str = "") -> str:
        v = p.get(key, default)
        return str(v) if v is not None else default

    return {
        "uid": _s("uid"),
        "title": _s("title"),
        "sku": _s("sku"),
        "text": _s("text"),
        "descr": _s("descr"),
        "price": _s("price"),
        "priceold": _s("priceold"),
        "quantity": _s("quantity"),
        "portion": _s("portion"),
        "unit": _s("unit"),
        "mark": _s("mark"),
        "url": _s("url"),
        "editions": p.get("editions", []),
        "characteristics": p.get("characteristics", []),
        "category": _s("category"),
    }


def _mask_dsn(dsn: str) -> str:
    """Mask password in DSN for safe logging."""
    return _re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)
