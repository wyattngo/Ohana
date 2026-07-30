"""A6 — cost_budget + cost_reservation (adopt-plan §4 A6 · design §5.6 · I8, I13).

Hai bảng cho cost cap pre-charge — cơ chế duy nhất đứng giữa "bug đầu tiên" và "shop bị
bill trắng" (OHB-6). Tên phẳng ở `public` theo adopt-plan §3 (schema `seller` để sau);
`shop_id TEXT` theo convention codebase (design: bigint — đổi kiểu là việc của đợt di cư
core/seller).

`cost_budget` — sổ cái ngày: `PK (shop_id, budget_date)`, mỗi shop mỗi ngày đúng một row.
Hai cột đếm tách bạch: `reserved_tokens` (đang giữ chỗ, chưa biết số thật) và
`actual_tokens` (đã reconcile). Điều kiện cap của §6.5 đọc CẢ HAI —
`reserved + actual + $2 <= cap` — nằm TRONG WHERE của UPDATE, không bao giờ check-rồi-update.
KHÁC `outbox`: có FK về `shops` — ngân sách là dữ liệu tenant thật, không có ca
sentinel/pre-verify như đường webhook (PRE-1104 không áp ở đây), shop phải tồn tại trước
khi có ngân sách.

`cost_reservation` — I13: reservation phải có DANH TÍNH, không chỉ là một con số tổng
trong `cost_budget`. Không có bảng này thì LLM timeout ⇒ `reserved_tokens` rò rỉ ⇒ shop
bị khoá tới nửa đêm, và reaper không có cách nào biết cái nào treo. `trace_id` NOT NULL
nối tiếp G6 (webhook→outbox→draft→reservation cùng một trace). Partial index
`(created_at) WHERE released_at IS NULL` là đường quét của reaper R4 (§6.10, đổ bộ A7).
Composite FK `(shop_id, budget_date)` → `cost_budget` đúng design: reservation không bao
giờ trỏ vào một ngày ngân sách không tồn tại.

Đường ghi DUY NHẤT là `db/repos.py::CostRepo` — §6.5 (reserve) và §6.5b (reconcile)
nguyên văn, mỗi cái một câu. Row ngân sách ngày do `ensure_today` tạo idempotent; §6.5
là UPDATE-first nên thiếu row ⇒ 0 row ⇒ coi như chạm trần (fail-closed, không gọi LLM).

I14: chạy bằng `ohana_migrator` (BẮT BUỘC — xem a1) thì cả hai bảng + sequence rơi đúng
default privileges: svc_seller SELECT/INSERT/UPDATE, svc_ohana_ai zero, mcp_readonly SELECT.

Reversible thật ở GĐ0: chưa có caller nào ghi (wire pipeline là B7), downgrade drop sạch.

Revision ID: a6_cost_tables
Revises: a5_outbox_trace
"""

from __future__ import annotations

from alembic import op

revision = "a6_cost_tables"
down_revision = "a5_outbox_trace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE cost_budget (
            shop_id         TEXT   NOT NULL REFERENCES shops (id),
            budget_date     DATE   NOT NULL,
            cap_tokens      BIGINT NOT NULL,
            reserved_tokens BIGINT NOT NULL DEFAULT 0,
            actual_tokens   BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (shop_id, budget_date)
        )
    """)
    op.execute("""
        CREATE TABLE cost_reservation (
            reservation_id bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            shop_id     TEXT NOT NULL,
            budget_date DATE NOT NULL,
            tokens      INT  NOT NULL,
            trace_id    UUID NOT NULL,
            released_at TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (shop_id, budget_date)
                REFERENCES cost_budget (shop_id, budget_date)
        )
    """)
    op.execute(
        "CREATE INDEX idx_cost_reservation_unreleased ON cost_reservation (created_at) "
        "WHERE released_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE cost_reservation")
    op.execute("DROP TABLE cost_budget")
