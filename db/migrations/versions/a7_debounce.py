"""A7 — debounce scheduler trên conversations (adopt-plan §4 A7 · design §5.5 §6.3 · I13, C2).

Ba cột + một partial index để `conversations` thành hàng đợi compose có coalesce:

- `next_debounce_at` — timer: tin mới ĐẨY LÙI nó (w§2.2 coalesce), loop debounce chỉ
  compose khi timer đến hạn ⇒ cụm tin gõ liên tiếp thành MỘT draft, không phải N draft.
- `debounce_claimed_at` — khoá claim của §6.3: `IS NULL` trong WHERE là toàn bộ nội dung
  C2 (N scheduler ⇒ đúng 1 draft). Claim này CÓ reaper gỡ (R3 §6.9) — I13.
- `debounce_trace_id` — KHÔNG có trong design §5.5 (lỗ doc, Wyatt ký 2026-07-30 lượt duyệt
  A7; bổ sung doc kiểu Q4): khi compose dọn từ outbox loop sang debounce loop, trace G6
  phải có chỗ đi qua conversation — không có cột này thì `draft.trace_id` hoặc phải bịa
  (đứt chuỗi webhook→draft, test trace §9 không viết được) hoặc phải mò outbox theo
  payload jsonb (không index). Batch coalesce nhiều tin ⇒ draft mang trace của TIN CUỐI —
  draft là câu trả lời cho cụm tin mà tin cuối khép lại.

Partial index NGUYÊN VĂN design §5.5 — đường quét của loop debounce; điều kiện index
trùng khít WHERE của `due_conversations` để quét là index-only, không seq-scan bảng
conversation lớn mỗi 500ms.

R1 (draft hết TTL → 'expired') KHÔNG cần đổi schema: `pending_reply.status` là Text không
CHECK; 'expired' là giá trị mới do reaper ghi, model docstring đã khai sẵn từ spec 01.

I14: chạy bằng `ohana_migrator` (BẮT BUỘC — xem a1); cột mới trên bảng cũ thừa kế grant
đang có của bảng (svc_seller SELECT/INSERT/UPDATE trên conversations từ a1).

Reversible thật: cột mới toàn NULL cho row cũ, downgrade drop sạch; khi đã có traffic,
downgrade xoá timer đang chờ — conversation nào đang đợi compose sẽ im lặng (chấp nhận:
downgrade A7 nghĩa là quay về thế giới compose-trực-tiếp của A5, phải deploy worker cũ kèm).

Revision ID: a7_debounce
Revises: a6_cost_tables
"""

from __future__ import annotations

from alembic import op

revision = "a7_debounce"
down_revision = "a6_cost_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE conversations ADD COLUMN next_debounce_at TIMESTAMPTZ")
    op.execute("ALTER TABLE conversations ADD COLUMN debounce_claimed_at TIMESTAMPTZ")
    op.execute("ALTER TABLE conversations ADD COLUMN debounce_trace_id UUID")
    op.execute(
        "CREATE INDEX idx_conversations_debounce_due ON conversations (next_debounce_at) "
        "WHERE next_debounce_at IS NOT NULL AND debounce_claimed_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_conversations_debounce_due")
    op.execute("ALTER TABLE conversations DROP COLUMN debounce_trace_id")
    op.execute("ALTER TABLE conversations DROP COLUMN debounce_claimed_at")
    op.execute("ALTER TABLE conversations DROP COLUMN next_debounce_at")
