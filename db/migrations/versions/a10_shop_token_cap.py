"""A10 — nguồn cap thật cho pre-charge (B6 wire, đóng khoảng "B7 quyết" của A6).

`shops.daily_token_cap` — trần token/ngày mỗi shop, nguồn DUY NHẤT cho
`CostRepo.ensure_today(cap_tokens=...)` (docstring A6 cố ý không đặt hằng số ở repo để
khỏi thành nguồn sự thật thứ hai — cột này chính là nguồn thứ nhất đó).

Đặt trên `shops` chứ KHÔNG dựng bảng `shop_config` riêng (design §5.3): một cột cho một
số, bảng config tổng quát là kiến trúc cho nhu cầu chưa có. Khi shop_config thật land
thì migrate cột này sang, một UPDATE.

DEFAULT 200000 (Wyatt duyệt 2026-07-30, lượt duyệt spec B6): ~25 lượt draft/ngày với
reserve 8k — trần an toàn cho MVP, shop thật chỉnh bằng UPDATE khi vận hành.

I14: chạy bằng `ohana_migrator`; cột mới thừa kế grant bảng (svc_seller sẵn SELECT).
Reversible: downgrade drop cột — mất cap đã chỉnh tay, chấp nhận (quay về thế giới
chưa wire cost).

Revision ID: a10_shop_token_cap
Revises: a9_retry_backoff
"""

from __future__ import annotations

from alembic import op

revision = "a10_shop_token_cap"
down_revision = "a9_retry_backoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE shops ADD COLUMN daily_token_cap INT NOT NULL DEFAULT 200000")


def downgrade() -> None:
    op.execute("ALTER TABLE shops DROP COLUMN daily_token_cap")
