"""add pending_reply table (F3 seller-copilot park path)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17

Adds the parked-draft table for spec 01 Phase 5. Every row carries `shop_id`; the composite
index `(shop_id, status, created_at)` supports the seller-inbox list query.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_reply",
        sa.Column("reply_id", sa.Text, primary_key=True),
        sa.Column("shop_id", sa.Text, nullable=False),
        sa.Column("conversation_id", sa.Text, nullable=False),
        sa.Column("customer_id", sa.Text, nullable=False),
        sa.Column("draft_text", sa.Text, nullable=False),
        sa.Column("intent", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("decided_by", sa.Text, nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_pending_shop_status_created",
        "pending_reply",
        ["shop_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_pending_shop_status_created", table_name="pending_reply")
    op.drop_table("pending_reply")
