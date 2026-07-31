"""A11 — send-worker claim trên pending_reply (OHB-23 · design §6.9 · I13, C2).

Send-worker (approved → gửi qua OutboundChannel → sent) là loop THỨ HAI có claim: cùng
bài với debounce (§6.3/§6.9) và outbox (§6.2/R2). Không có cột claim thì:

- Không cưỡng chế được C2 giữa nhiều send-worker (double-send: cùng 1 draft approved bị
  gửi hai lần cho khách);
- Không có mốc để R5 gỡ khi worker chết sau send-thành-công nhưng trước mark 'sent'
  (draft kẹt vĩnh viễn ở 'approved', im lặng vi phạm I13).

MỘT cột nullable `sent_claimed_at` + partial index cho R5 quét. KHÔNG cần bảng mới nên
KHÔNG chạm I14 default privileges — cột thừa kế grant hiện có của `pending_reply`
(svc_seller SELECT/INSERT/UPDATE từ a1). Không có CHECK trên `status` (a0 đã cố ý để Text
tự do — 'sending' không phải giá trị mới, đường claim dùng cột thời gian riêng để giữ
nguyên semantics `status ∈ pending/approved/rejected/sent/expired`).

Partial index NGUYÊN VĂN chỗ R5 quét: `sent_claimed_at IS NOT NULL` — bảng pending_reply
lớn dần theo thời gian (spec 14 A0 giữ row cho training label §8.1), reaper 10s không
được seq-scan cả bảng chỉ để tìm vài row đang claim. Điều kiện trùng khít WHERE của R5
trong _REAP_R5_STUCK_SEND_CLAIM (db/repos.py) — sửa một bên phải sửa cả hai.

I14: chạy bằng `ohana_migrator` (BẮT BUỘC — a1). Autogen KHÔNG thấy partial index nên
viết tay bằng op.execute().

Reversible thật: cột mới toàn NULL cho row cũ, downgrade drop sạch; khi đã có traffic,
downgrade nghĩa là quay về thế giới không có send-worker (phải deploy code không claim
kèm — nếu chưa, draft approved dồn không ai gửi).

Revision ID: a11_pending_sent_claim
Revises: a10_shop_token_cap
"""

from __future__ import annotations

from alembic import op

revision = "a11_pending_sent_claim"
down_revision = "a10_shop_token_cap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pending_reply ADD COLUMN sent_claimed_at TIMESTAMPTZ")
    op.execute(
        "CREATE INDEX idx_pending_reply_send_claim ON pending_reply (sent_claimed_at) "
        "WHERE sent_claimed_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_pending_reply_send_claim")
    op.execute("ALTER TABLE pending_reply DROP COLUMN sent_claimed_at")
