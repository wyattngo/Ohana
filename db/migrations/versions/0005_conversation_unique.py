"""conversations: unique (shop_id, customer_id, channel, external_thread_id) NULLS NOT DISTINCT

Đóng ISSUE-017 (spec 09 C0).

KHÔNG destructive — khác `0004`. `upgrade` chỉ thêm ràng buộc, `downgrade` chỉ gỡ; không
dòng dữ liệu nào bị đụng ở cả hai chiều. Đây là migration thật sự reversible, và ghi ra đây
để không ai đọc lướt rồi áp cảnh báo của `0004` sang.

Nếu chạy trên bảng đã có row trùng, `ALTER` sẽ FAIL ồn ào. Đó là hành vi ĐÚNG: nó bắt người
vận hành nhìn dữ liệu trùng và tự quyết gộp thế nào — migration không được im lặng chọn hộ.
Lúc viết (2026-07-20) bảng có 0 row nên tình huống đó chưa tồn tại.

`NULLS NOT DISTINCT` cần PostgreSQL ≥ 15. Đã verify: local 16.14, CI `pgvector/pgvector:pg16`.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_CONSTRAINT = "uq_conversations_shop_cus_chan_thread"


def upgrade() -> None:
    # Viết SQL thô thay vì `op.create_unique_constraint`: helper của Alembic chưa có đường
    # truyền `NULLS NOT DISTINCT`, mà bỏ mệnh đề đó thì constraint không chặn được ca
    # thread_id=NULL — tức mất đúng thứ migration này sinh ra để làm.
    op.execute(
        f"ALTER TABLE conversations ADD CONSTRAINT {_CONSTRAINT} "
        "UNIQUE NULLS NOT DISTINCT (shop_id, customer_id, channel, external_thread_id)"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE conversations DROP CONSTRAINT {_CONSTRAINT}")
