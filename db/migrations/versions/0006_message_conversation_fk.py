"""messages: thêm conversation_id / customer_id + 2 composite FK + index history

Spec 10 Phase H0. Trước migration này `messages` là bảng DUY NHẤT trong repo không có FK —
spec 06 F0 gắn composite FK cho `conversations`/`order_drafts`/`pending_reply` rồi bỏ sót nó.

**`ADD COLUMN ... NOT NULL` không default sẽ FAIL nếu bảng có row.** Đó là hành vi ĐÚNG:
nó bắt người vận hành nhìn dữ liệu trước khi quyết backfill, thay vì im lặng nhét giá trị
bịa vào. PRE-1002 đã đo trên Postgres thật lúc viết (2026-07-20): `count(*) = 0`, nên tình
huống đó chưa tồn tại. Nếu chạy trên môi trường có dữ liệu và nó FAIL — đọc lại PRE-1002
trong spec 10, KHÔNG "sửa" bằng cách thêm DEFAULT hay hạ xuống NULLABLE: cả hai đều tạo
message mồ côi không thuộc conversation nào, đúng họ ISSUE-020.

**Về tính reversible — đọc kỹ, khác `0005`.**
`0005` reversible thật cả schema lẫn dữ liệu (thêm/gỡ constraint, không đụng row nào).
Cái này KHÔNG: `downgrade` drop cột, tức **mất liên kết conversation của mọi message đã ghi**.
Reversible về SCHEMA, không reversible về DỮ LIỆU — cùng loại cảnh báo với `0004`, dù nhẹ hơn
(0004 xoá thẳng row, cái này xoá cột). Với bảng rỗng thì vô hại; với bảng đã chạy thật thì
downgrade là mất mát không phục hồi được.

FK phải COMPOSITE `(shop_id, X)` chứ không phải FK đơn trên `X`: FK đơn chỉ đòi row được trỏ
TỒN TẠI, nên nó cho phép message của shop A trỏ conversation của shop B — lỗ R1.22 mà không
lượt review nào bắt chắc được. Dạng composite khiến chính Postgres từ chối.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_FK_CONV = "fk_messages_conversation_same_shop"
_FK_CUS = "fk_messages_customer_same_shop"
_INDEX = "idx_msg_shop_conv_created"


def upgrade() -> None:
    op.execute("ALTER TABLE messages ADD COLUMN conversation_id TEXT NOT NULL")
    op.execute("ALTER TABLE messages ADD COLUMN customer_id TEXT NOT NULL")

    op.execute(
        f"ALTER TABLE messages ADD CONSTRAINT {_FK_CONV} "
        "FOREIGN KEY (shop_id, conversation_id) REFERENCES conversations (shop_id, id)"
    )
    op.execute(
        f"ALTER TABLE messages ADD CONSTRAINT {_FK_CUS} "
        "FOREIGN KEY (shop_id, customer_id) REFERENCES customers (shop_id, id)"
    )

    # Không thay `idx_msg_shop_created`: cái đó phục vụ truy vấn theo shop, cái này phục vụ
    # đường đọc history (`WHERE shop_id=? AND conversation_id=? ORDER BY created_at DESC`).
    op.execute(f"CREATE INDEX {_INDEX} ON messages (shop_id, conversation_id, created_at)")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute(f"ALTER TABLE messages DROP CONSTRAINT IF EXISTS {_FK_CUS}")
    op.execute(f"ALTER TABLE messages DROP CONSTRAINT IF EXISTS {_FK_CONV}")
    # ⚠️ Mất dữ liệu: liên kết conversation/customer của mọi message đã ghi biến mất.
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS customer_id")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS conversation_id")
