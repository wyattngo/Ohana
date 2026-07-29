"""pending_reply +snapshot +expires_at +label — draft schema-shaping

Spec 14 Phase A0 (workflow §2.3/§2.5/§7 bước 3: "sai schema từ đầu là refactor lớn. Có
`label` field từ ngày một để nuôi §8"). Ba cột nullable trên `pending_reply`:

- `snapshot JSONB NULL`   — dữ kiện tầng-1 tại T0 (giá/tồn/order-status), để §2.5 phát hiện
                            drift lúc seller duyệt. Chỗ CHỨA; đường ghi (capture lúc draft)
                            là runtime sau.
- `expires_at TIMESTAMPTZ NULL` — TTL = min(messaging window platform, ngưỡng shop). Chỗ
                            CHỨA; tính toán + cron expiry là runtime sau.
- `label TEXT NULL`       — tín hiệu train auto-send (§8.1), KHÁC `status` (lifecycle gửi).
                            CHECK ∈ {approved,rejected,edited} ở tầng DB — hàng rào không ai
                            bypass được (psql tay / script / data-fix), khác Pydantic chỉ
                            bảo vệ đường ứng dụng.

**PRE-1402 đã đo (2026-07-21, Postgres thật):** `pending_reply` 0 row ⇒ cột nullable, KHÔNG
backfill. `downgrade` drop 3 cột: reversible về SCHEMA; khi đã có draft thật, drop `label`
làm mất training data (reversible schema, không reversible dữ liệu — cùng cảnh báo `0007`).

CHECK đặt tên `ck_pending_reply_label` khớp `db/models.py` `__table_args__` — hai nguồn phải
cùng tên constraint, nếu không alembic autogenerate về sau sẽ thấy "drift" giả.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE pending_reply
            ADD COLUMN snapshot   JSONB       NULL,
            ADD COLUMN expires_at TIMESTAMPTZ NULL,
            ADD COLUMN label      TEXT        NULL,
            ADD CONSTRAINT ck_pending_reply_label
                CHECK (label IS NULL OR label IN ('approved', 'rejected', 'edited'))
    """)


def downgrade() -> None:
    # ⚠️ Mất training data khi đã có draft thật: cột `label` biến mất.
    op.execute("""
        ALTER TABLE pending_reply
            DROP CONSTRAINT IF EXISTS ck_pending_reply_label,
            DROP COLUMN IF EXISTS label,
            DROP COLUMN IF EXISTS expires_at,
            DROP COLUMN IF EXISTS snapshot
    """)
