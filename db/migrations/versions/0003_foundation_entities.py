"""add customers / conversations / order_drafts + composite tenant FKs

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-18

Spec 06 Phase F0. Lands the three core commerce entities that spec 03 assumed existed but
never created, and retro-fits foreign keys onto `pending_reply`'s two orphan columns.

Identity type is TEXT throughout (PRE-F01, signed by Wyatt 2026-07-18) — matching the
existing `shop_id Text` on messages/embeddings/pending_reply. Spec 03 §8 originally drafted
these as UUID, which could not have worked: a UUID foreign key cannot reference a TEXT column.

Every FK here is COMPOSITE on `(shop_id, <child_id>)` rather than a plain single-column FK.
A plain FK asserts only "the referenced row exists"; the composite form additionally pins it
to the SAME shop, so Postgres rejects a shop A row that points at a shop B row. That is the
difference between tenant isolation as a convention and tenant isolation as a constraint.
Each parent therefore needs UNIQUE (shop_id, id) for the composite FK to have a target.

`pending_reply` FKs are added unconditionally because PRE-F02 verified the table holds 0
rows in every environment this migration will touch. If that ever stops being true, this
migration needs a backfill step BEFORE the constraints — do not assume.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("shop_id", sa.Text, nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Target for the composite FKs below. Redundant-looking against the PK, load-bearing.
        sa.UniqueConstraint("shop_id", "id", name="uq_customers_shop_id"),
        # One customer row per (shop, channel, channel-side user id).
        sa.UniqueConstraint("shop_id", "channel", "external_id", name="uq_customers_shop_chan_ext"),
    )
    op.create_index("idx_customer_shop_created", "customers", ["shop_id", "created_at"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("shop_id", sa.Text, nullable=False),
        sa.Column("customer_id", sa.Text, nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("external_thread_id", sa.Text, nullable=True),
        # Zalo 48h reactive window lives here from the start (spec 03 Phase 10 planned to
        # ALTER a conversations table that had never been created).
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_status", sa.Text, nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("shop_id", "id", name="uq_conversations_shop_id"),
        sa.ForeignKeyConstraint(
            ["shop_id", "customer_id"],
            ["customers.shop_id", "customers.id"],
            name="fk_conversations_customer_same_shop",
        ),
    )
    op.create_index("idx_conv_shop_last_inbound", "conversations", ["shop_id", "last_inbound_at"])

    op.create_table(
        "order_drafts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("shop_id", sa.Text, nullable=False),
        sa.Column("conversation_id", sa.Text, nullable=False),
        sa.Column("customer_id", sa.Text, nullable=False),
        sa.Column("items", postgresql.JSONB, nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=True),
        # Never defaults to anything implying confirmation — guardrail §1.3.
        sa.Column("status", sa.Text, nullable=False, server_default="draft"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["shop_id", "conversation_id"],
            ["conversations.shop_id", "conversations.id"],
            name="fk_order_drafts_conversation_same_shop",
        ),
        sa.ForeignKeyConstraint(
            ["shop_id", "customer_id"],
            ["customers.shop_id", "customers.id"],
            name="fk_order_drafts_customer_same_shop",
        ),
    )
    op.create_index(
        "idx_od_shop_status_created", "order_drafts", ["shop_id", "status", "created_at"]
    )

    # Retro-fit the two orphan columns (PRE-F02: table verified empty).
    op.create_foreign_key(
        "fk_pending_reply_conversation_same_shop",
        "pending_reply",
        "conversations",
        ["shop_id", "conversation_id"],
        ["shop_id", "id"],
    )
    op.create_foreign_key(
        "fk_pending_reply_customer_same_shop",
        "pending_reply",
        "customers",
        ["shop_id", "customer_id"],
        ["shop_id", "id"],
    )


def downgrade() -> None:
    # Reverse order: drop the constraints that point INTO these tables before the tables.
    op.drop_constraint("fk_pending_reply_customer_same_shop", "pending_reply", type_="foreignkey")
    op.drop_constraint(
        "fk_pending_reply_conversation_same_shop", "pending_reply", type_="foreignkey"
    )

    op.drop_index("idx_od_shop_status_created", table_name="order_drafts")
    op.drop_table("order_drafts")

    op.drop_index("idx_conv_shop_last_inbound", table_name="conversations")
    op.drop_table("conversations")

    op.drop_index("idx_customer_shop_created", table_name="customers")
    op.drop_table("customers")
