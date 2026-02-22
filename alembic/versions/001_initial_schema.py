"""initial_schema

Revision ID: 001
Revises: 
Create Date: 2026-02-22 07:44:53.157539

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Products ---
    op.create_table(
        "products",
        sa.Column("uid", sa.String(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("sku", sa.Text(), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("descr", sa.Text(), nullable=False, server_default=""),
        sa.Column("price", sa.Text(), nullable=False, server_default=""),
        sa.Column("priceold", sa.Text(), nullable=False, server_default=""),
        sa.Column("quantity", sa.Text(), nullable=False, server_default=""),
        sa.Column("portion", sa.Text(), nullable=False, server_default=""),
        sa.Column("unit", sa.Text(), nullable=False, server_default=""),
        sa.Column("mark", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("editions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("characteristics", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("category", sa.Text(), nullable=False, server_default=""),
        sa.Column("fts", postgresql.TSVECTOR(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_products_category", "products", ["category"])
    op.create_index("idx_products_fts", "products", ["fts"], postgresql_using="gin")

    # FTS trigger
    op.execute("""
        CREATE OR REPLACE FUNCTION products_fts_update() RETURNS trigger AS $$
        BEGIN
            NEW.fts :=
                setweight(to_tsvector('russian', coalesce(NEW.title, '')), 'A') ||
                setweight(to_tsvector('russian', coalesce(NEW.descr, '')), 'B') ||
                setweight(to_tsvector('russian', coalesce(NEW.text, '')), 'C') ||
                setweight(to_tsvector('russian', coalesce(NEW.category, '')), 'D');
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_products_fts
            BEFORE INSERT OR UPDATE ON products
            FOR EACH ROW EXECUTE FUNCTION products_fts_update();
    """)

    # --- Scrape metadata (single-row) ---
    op.create_table(
        "scrape_meta",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column("last_full_scrape", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_price_refresh", sa.Float(), nullable=False, server_default="0"),
        sa.CheckConstraint("id = 1", name="scrape_meta_single_row"),
    )
    op.execute("INSERT INTO scrape_meta (id) VALUES (1) ON CONFLICT DO NOTHING")

    # --- Chat messages ---
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("dialog_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_chat_messages_dialog", "chat_messages", ["dialog_id", "created_at"])


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_products_fts ON products")
    op.execute("DROP FUNCTION IF EXISTS products_fts_update()")
    op.drop_table("chat_messages")
    op.drop_table("scrape_meta")
    op.drop_table("products")
