"""zalo_oa_tokens — credentials + secret per shop (spec 17 P0 GD0-ZALO)

Bảng này là **chỗ chờ Zalo creds** — P0 chỉ dựng schema + repo, P1 dùng `oa_secret_key`
để verify webhook signature, P2 dùng access/refresh + `SELECT ... FOR UPDATE` để refresh
token cron. Không có runtime nào ghi bảng này ở P0 (0 row) — reversible thật cho tới lúc
Wyatt/Tân seed row đầu tiên.

`shop_id` PK ⇒ MỘT OA per shop ở GĐ0 (multi-brand là schema change riêng, không thuộc scope
spec 17). `shop_id` FK CASCADE về `shops.id` — xoá shop = xoá luôn credentials, đúng ngữ
nghĩa "shop này không còn dùng dịch vụ".

Index `idx_zalo_oa_tokens_oa_id` cho P1 verify: webhook body chỉ có `sender.id`/`recipient.id`
(không có `oa_id` top-level — bẫy docs Zalo đã xác 2026-07-24), ta thử cả hai vào cột `oa_id`
để tra `oa_secret_key`. Không unique — về nguyên tắc 2 shop có thể liên kết cùng OA (test env
hoặc shared brand), constraint uniqueness là ở tầng ứng dụng (onboard) chứ không phải DB.

**Reversible thật cả schema lẫn dữ liệu ở P0** — bảng mới, 0 row (chưa wire).

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE zalo_oa_tokens (
            shop_id             TEXT PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE,
            oa_id               TEXT NOT NULL,
            access_token        TEXT NOT NULL,
            refresh_token       TEXT NOT NULL,
            access_expires_at   TIMESTAMPTZ NOT NULL,
            refresh_expires_at  TIMESTAMPTZ NOT NULL,
            oa_secret_key       TEXT NOT NULL,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_zalo_oa_tokens_oa_id ON zalo_oa_tokens(oa_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_zalo_oa_tokens_oa_id")
    op.execute("DROP TABLE IF EXISTS zalo_oa_tokens")
