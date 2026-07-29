"""initial tenant-first schema (messages, embeddings)

Revision ID: 0001
Revises:
Create Date: 2026-07-17

Phase 2 lands only the two tables the tenant-isolation gate exercises. Wider tables
(shops, sellers, customers, conversations, pending_reply) land in Phase 5 alongside the
copilot/policy_gate work — one concern per migration (R6 db pair).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("shop_id", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_msg_shop_created", "messages", ["shop_id", "created_at"])

    op.create_table(
        "embeddings",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("shop_id", sa.Text, nullable=False),
        sa.Column("namespace", sa.Text, nullable=False),
        sa.Column("source_ref", sa.Text, nullable=True),
        sa.Column("chunk", sa.Text, nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_emb_shop_ns", "embeddings", ["shop_id", "namespace"])


def downgrade() -> None:
    op.drop_index("idx_emb_shop_ns", table_name="embeddings")
    op.drop_table("embeddings")
    op.drop_index("idx_msg_shop_created", table_name="messages")
    op.drop_table("messages")
