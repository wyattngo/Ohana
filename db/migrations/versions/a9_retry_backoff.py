"""A9 — backoff retry + counter poison + index cho reaper (review A5–A8, đợt P0/P1).

Bốn mảnh, cùng một chủ đề: các đường lỗi/quét mà A5–A7 dựng cơ chế nhưng review tìm ra
lỗ vận hành.

1. `outbox.next_retry_at` — job lỗi quay về pending kèm mốc `now() + 2^attempts giây`
   (`_MARK_FAILED`), claim §6.2 (đã amend doc 2026-07-30) bỏ qua row chưa tới mốc.
   Không có cột này, row lỗi là row CŨ NHẤT theo `created_at` nên bị claim lại ngay tick
   200ms kế tiếp: 5 attempts cháy trong ~1 giây, lỗi DB thoáng qua 2 giây cũng đủ giết
   một tin khách thành `dead` — trong khi backoff 2+4+8+16s cho nó sống qua blip.

2. `conversations.compose_failures` — đường compose (nơi duy nhất gọi LLM) trước A9
   không có trần thử lại: drafter nổ tất định ⇒ claim treo ⇒ R3 gỡ sau 5' ⇒ gọi LLM lại,
   ~288 lần/ngày/hội thoại độc, vĩnh viễn. Counter này là trần: compose lỗi ⇒ +1; đạt
   MAX_COMPOSE_FAILURES ⇒ worker NULL timer (dừng retry) và GIỮ counter cho vận hành
   query (quyết 2026-07-30: dừng + counter, KHÔNG park draft ESCALATE — draft body
   placeholder cần định nghĩa UX chưa có). Compose thành công ⇒ reset 0.

3. Partial index cho R3: index debounce duy nhất (a7) đòi `debounce_claimed_at IS NULL`
   — đúng phần BÙ của tập R3 cần quét ⇒ R3 seq-scan toàn bảng conversations mỗi 10s.
   Index mới chỉ chứa claim đang treo (vài row) ⇒ R3 thành index probe.

4. Partial index cho R1: `pending_reply(expires_at) WHERE status='pending'` — R1 quét
   draft quá TTL mỗi 10s, index hiện có dẫn đầu bằng shop_id nên không dùng được.

I14: chạy bằng `ohana_migrator`; cột mới thừa kế grant bảng. Reversible: cột/index mới,
downgrade drop sạch — mất lịch sử failures/backoff đang đếm, chấp nhận (downgrade = quay
về hành vi retry nóng cũ).

Revision ID: a9_retry_backoff
Revises: a8_escalation_reasons
"""

from __future__ import annotations

from alembic import op

revision = "a9_retry_backoff"
down_revision = "a8_escalation_reasons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE outbox ADD COLUMN next_retry_at TIMESTAMPTZ")
    op.execute("ALTER TABLE conversations ADD COLUMN compose_failures INT NOT NULL DEFAULT 0")
    op.execute(
        "CREATE INDEX idx_conversations_debounce_claimed ON conversations "
        "(debounce_claimed_at) WHERE debounce_claimed_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_pending_reply_ttl ON pending_reply (expires_at) WHERE status = 'pending'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_pending_reply_ttl")
    op.execute("DROP INDEX idx_conversations_debounce_claimed")
    op.execute("ALTER TABLE conversations DROP COLUMN compose_failures")
    op.execute("ALTER TABLE outbox DROP COLUMN next_retry_at")
