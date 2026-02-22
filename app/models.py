"""SQLAlchemy ORM models for the product catalog and chat history."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all models."""
    pass


class Product(Base):
    __tablename__ = "products"

    uid: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    sku: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    descr: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    price: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    priceold: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    quantity: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    portion: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    unit: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    mark: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    url: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    editions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    characteristics: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    category: Mapped[str] = mapped_column(Text, nullable=False, server_default="", index=True)

    # Full-text search vector — populated by DB trigger
    fts: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_products_fts", "fts", postgresql_using="gin"),
    )

    def to_dict(self) -> dict:
        """Convert to the dict format used by ProductCatalog."""
        return {
            "uid": self.uid,
            "title": self.title,
            "sku": self.sku,
            "text": self.text,
            "descr": self.descr,
            "price": self.price,
            "priceold": self.priceold,
            "quantity": self.quantity,
            "portion": self.portion,
            "unit": self.unit,
            "mark": self.mark,
            "url": self.url,
            "editions": self.editions if isinstance(self.editions, list) else [],
            "characteristics": self.characteristics if isinstance(self.characteristics, list) else [],
            "category": self.category,
        }


class ScrapeMeta(Base):
    __tablename__ = "scrape_meta"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, server_default="1"
    )
    last_full_scrape: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    last_price_refresh: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")

    __table_args__ = (
        CheckConstraint("id = 1", name="scrape_meta_single_row"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dialog_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_chat_messages_dialog", "dialog_id", "created_at"),
    )
