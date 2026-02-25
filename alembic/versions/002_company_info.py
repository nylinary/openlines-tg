"""add company_info table

Revision ID: 002
Revises: 001
Create Date: 2026-02-25 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_info",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column("company_name", sa.Text(), nullable=False, server_default="МояРыба"),
        sa.Column("address", sa.Text(), nullable=False, server_default=""),
        sa.Column("phone", sa.Text(), nullable=False, server_default=""),
        sa.Column("email", sa.Text(), nullable=False, server_default=""),
        sa.Column("website", sa.Text(), nullable=False, server_default="https://myryba.ru"),
        sa.Column("working_hours", sa.Text(), nullable=False, server_default=""),
        sa.Column("delivery_info", sa.Text(), nullable=False, server_default=""),
        sa.Column("payment_info", sa.Text(), nullable=False, server_default=""),
        sa.Column("extra_faq", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="company_info_single_row"),
    )
    # Seed the single row so there is always something to read/edit
    op.execute(
        "INSERT INTO company_info (id) VALUES (1) ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("company_info")
