"""webhook_event_log — idempotency ledger cho inbound webhook

Spec 14 Phase B0 (workflow §2.1 ràng buộc #2: "Idempotent tại DB. Unique constraint trên
`(channel, platform_msg_id)`. Không dựa vào cache"). Đây là keystone #1 của workflow, phần
INTERNAL — bảng + constraint KHÔNG cần Zalo creds (creds chỉ cho signature-verify = GD0-ZALO
external). Vì vậy tách khỏi spec 03 Phase 2 (SUPERSEDED, PRE-1403 Wyatt ký 2026-07-21).

PK compound `(channel, platform_msg_id)` là cơ chế chống-nhân-đôi: Zalo/FB retry cùng payload
⇒ `INSERT ... ON CONFLICT DO NOTHING` từ chối bản sao ở tầng DB. `messages` cố ý KHÔNG
idempotent (spec 10 H1) — dedup sống ở ĐÂY, không ở đó.

⚠️ `shop_id` KHÔNG FK về `shops`: idempotency là biên giới NỀN-TẢNG, không phải dữ liệu
tenant; `shop_id` khi wire runtime có thể là sentinel/pre-verify chưa là shop thật (cùng lý do
`embeddings._platform`, spec 11 PRE-1104). Lưu để audit, không ràng buộc.

**Reversible thật cả schema lẫn dữ liệu ở GĐ0** — bảng mới, chưa có row (B0 chưa wire runtime),
`downgrade` drop sạch. Khi đã có traffic thật thì `downgrade` xoá toàn bộ sổ idempotency ⇒
reversible về SCHEMA, không về khả-năng-chống-trùng của các event đã ghi.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE webhook_event_log (
            channel         TEXT NOT NULL,
            platform_msg_id TEXT NOT NULL,
            shop_id         TEXT NOT NULL,
            received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (channel, platform_msg_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webhook_event_log")
