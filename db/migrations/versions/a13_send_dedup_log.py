"""A13 — send_log dedup cho send-worker Tầng 3 (OHB-24 · chống double-send).

Đóng gap exactly-once của `app/worker_seller.py::send_one` (verify main `8aba78b`):
crash sau `sender.send()` OK nhưng trước `mark_sent` ⇒ R5 NULL `sent_claimed_at` ⇒
worker khác re-claim ⇒ gửi lại. SKIP LOCKED chỉ chặn concurrent claim, không chặn
crash-reap-resend. Bảng dedup PK trên `reply_id` là atomic barrier: INSERT ON CONFLICT
DO NOTHING RETURNING lúc trước send() ⇒ lần hai (sau crash) reserve trả rowcount=0 ⇒
send_one skip sender.

**KHÔNG FK về `pending_reply(reply_id)`**: retention khác — pending_reply có thể xoá
cho training label (w§8.1) trong khi sent_log giữ audit lâu hơn. FK gây CASCADE mất
audit; giữ độc lập là ĐÚNG.

**KHÔNG grant `svc_ohana_ai`**: Tầng 3 (luồng B) scope. Cùng lý do OHB-27/D2 giữ I2 —
send-worker chạy dưới `svc_seller`, bảng này chỉ svc_seller đọc/ghi.

**Grant DELETE tường minh cho svc_seller**: a1 default privileges public là
SELECT/INSERT/UPDATE — thiếu DELETE. Send_one flow gọi `SentLogRepo.rollback` (DELETE)
khi sender.send() raise để lượt kế retry được. Grant CHỈ bảng này, KHÔNG ALTER DEFAULT
PRIVILEGES thêm DELETE cho toàn public (đó là quyết định wider, không thuộc scope OHB-24).

**BẮT BUỘC chạy role `ohana_migrator`** (bẫy Alembic của skill — I14).

Gate: tests/test_ohb24_send_idempotency.py (2 test: dedup chặn double-send, rollback
sau exception cho retry).

Revision ID: a13_send_dedup_log
Revises: a12_assistant_schema
"""

from __future__ import annotations

from alembic import op

revision = "a13_send_dedup_log"
down_revision = "a12_assistant_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Bảng dedup ──────────────────────────────────────────────────────────────
    # `reply_id text PK`: PK là toàn bộ nội dung dedup — INSERT ON CONFLICT DO NOTHING
    # RETURNING dựa vào constraint này. `shop_id`/`customer_id` để audit; sent_at
    # server_default cho immutable timeline.
    op.execute("""
        CREATE TABLE sent_log (
          reply_id     text        PRIMARY KEY,
          shop_id      text        NOT NULL,
          customer_id  text        NOT NULL,
          sent_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    # Index cho query vận hành "shop X đã sent tin nào hôm nay" — không dùng ở hot path.
    op.execute("CREATE INDEX idx_sent_log_shop_sent ON sent_log (shop_id, sent_at DESC)")

    # ── Grant DELETE tường minh (a1 default thiếu) ──────────────────────────────
    # SELECT/INSERT/UPDATE tự thừa kế qua ALTER DEFAULT PRIVILEGES của a1. Chỉ DELETE
    # cần thêm, và chỉ bảng này — KHÔNG ALTER DEFAULT PRIVILEGES thêm DELETE cho public
    # (over-broad, chưa cần cho bảng khác).
    op.execute("GRANT DELETE ON TABLE sent_log TO svc_seller")


def downgrade() -> None:
    # DROP TABLE dọn cả index + grant cascade.
    op.execute("DROP TABLE IF EXISTS sent_log")
