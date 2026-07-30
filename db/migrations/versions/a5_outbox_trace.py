"""A5 — outbox queue + trace_id xuyên suốt + khoá C1 trên messages.

Ba việc trong MỘT revision (adopt-plan §4 A5 · design §5.4 §5.5 §6.1 §6.2 · I7, G6, C1):

1. `webhook_event_log` (≙ `seller.webhook_seen` design §5.4 — ánh xạ adopt-plan §1):
   - `event_id` identity + UNIQUE — đích FK cho `outbox`. PK compound
     `(channel, platform_msg_id)` GIỮ NGUYÊN làm conflict target của §6.1; design để PK là
     `event_id` + UNIQUE cặp, chức năng tương đương, không đập PK đang có.
   - `raw_event` JSONB NOT NULL — §6.1 lưu payload thô để worker re-derive khi cần, tin
     khách không mất dù worker chết trước khi ghi `messages`. Backfill `'{}'` cho row cũ
     (bảng B0 chưa wire runtime nên thực tế rỗng). ⚠️ O8 (design §12): retention/quyền xoá
     NĐ13 cho PII thô trong cột này còn treo — chính sách trước khi ra sandbox.
   - `trace_id` UUID NOT NULL, KHÔNG default — §6.1 phải cấp tường minh (G6: trace sinh tại
     webhook, xuyên webhook→outbox→draft; `llm_turn` nối nốt khi bảng đó đổ bộ).

2. `outbox` — queue duy nhất của luồng B (I7 · §10: KHÔNG Redis/RabbitMQ/SQS). Nằm ở
   `public` với tên phẳng theo adopt-plan §3 (schema `seller` để sau, khi có lý do độc lập).
   Hai chỗ lệch design §5.4 CÓ CHỦ ĐÍCH:
   - `shop_id TEXT` (design: bigint) — toàn codebase đang dùng Text id, đổi kiểu là việc
     của đợt di cư core/seller, không phải của A5.
   - KHÔNG FK về `shops` (design: REFERENCES core.shop) — §6.1 copy `shop_id` từ
     `webhook_event_log`, nơi shop_id có thể là sentinel/pre-verify chưa là shop thật
     (cùng lý do PRE-1104 đã ghi ở model `WebhookEventLog`). FK ở đây sẽ làm §6.1 nổ
     đúng ca mà sổ idempotency được thiết kế để chịu.
   Hai partial index đúng nguyên văn design §5.4 — phục vụ §6.2 (claim pending theo
   created_at) và reaper R2 (quét processing kẹt theo claimed_at).

3. C1 (PRE-010, design §9): `messages.platform_msg_id` + UNIQUE
   `(conversation_id, platform_msg_id)`. Đây là lớp dedup THỨ HAI, khác tầng với
   `webhook_event_log`: sổ webhook chặn retry của PLATFORM, khoá này chặn double-process
   của WORKER (R2 reset row `processing` kẹt về `pending` ⇒ job chạy lại ⇒ ghi message
   lần hai — ON CONFLICT trên khoá này biến lần hai thành no-op). NULL là distinct trong
   UNIQUE của Postgres ⇒ message assistant/seller/system (không có platform id) không bị
   ràng, row cũ (cột mới toàn NULL) không conflict.

4. `pending_reply.trace_id` UUID NOT NULL (G6) — backfill `gen_random_uuid()` cho row cũ:
   trace bịa cho dữ liệu lịch sử là chấp nhận được (không có webhook gốc để nối), nhưng
   KHÔNG đặt default để mọi row MỚI phải mang trace thật từ đường ingest.

I14: chạy bằng `ohana_migrator` (xem a1 — BẮT BUỘC) thì `outbox` + cột mới rơi đúng vào
default privileges: svc_seller SELECT/INSERT/UPDATE (đủ cho §6.1/§6.2, không DELETE — queue
không xoá row, chỉ đổi status), svc_ohana_ai zero, mcp_readonly SELECT.

Reversible thật ở GĐ0: các bảng chưa có traffic (webhook chưa mount), downgrade drop sạch
cột/bảng mới. Khi có traffic thật, downgrade xoá queue + trace — reversible về SCHEMA thôi.

Revision ID: a5_outbox_trace
Revises: a2_flow_grants
"""

from __future__ import annotations

from alembic import op

revision = "a5_outbox_trace"
down_revision = "a2_flow_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1 · webhook_event_log: event_id + raw_event + trace_id ──────────────────────────
    op.execute(
        "ALTER TABLE webhook_event_log ADD COLUMN event_id BIGINT GENERATED ALWAYS AS IDENTITY"
    )
    op.execute(
        "ALTER TABLE webhook_event_log "
        "ADD CONSTRAINT uq_webhook_event_log_event_id UNIQUE (event_id)"
    )
    op.execute("ALTER TABLE webhook_event_log ADD COLUMN raw_event JSONB")
    op.execute("UPDATE webhook_event_log SET raw_event = '{}'::jsonb WHERE raw_event IS NULL")
    op.execute("ALTER TABLE webhook_event_log ALTER COLUMN raw_event SET NOT NULL")
    op.execute("ALTER TABLE webhook_event_log ADD COLUMN trace_id UUID")
    op.execute("UPDATE webhook_event_log SET trace_id = gen_random_uuid() WHERE trace_id IS NULL")
    op.execute("ALTER TABLE webhook_event_log ALTER COLUMN trace_id SET NOT NULL")

    # ── 2 · outbox — shape design §5.4, tên phẳng ở public ───────────────────────────────
    op.execute("CREATE TYPE outbox_status AS ENUM ('pending','processing','done','dead')")
    op.execute("""
        CREATE TABLE outbox (
            outbox_id  BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            event_id   BIGINT NOT NULL REFERENCES webhook_event_log (event_id),
            shop_id    TEXT   NOT NULL,
            payload    JSONB  NOT NULL,
            status     outbox_status NOT NULL DEFAULT 'pending',
            attempts   INT    NOT NULL DEFAULT 0,
            claimed_at TIMESTAMPTZ,
            last_error TEXT,
            trace_id   UUID   NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_outbox_pending ON outbox (created_at) WHERE status = 'pending'")
    op.execute(
        "CREATE INDEX idx_outbox_processing ON outbox (claimed_at) WHERE status = 'processing'"
    )

    # ── 3 · C1: khoá dedup ở tầng message ────────────────────────────────────────────────
    op.execute("ALTER TABLE messages ADD COLUMN platform_msg_id TEXT")
    op.execute(
        "ALTER TABLE messages ADD CONSTRAINT uq_messages_conv_platform_msg "
        "UNIQUE (conversation_id, platform_msg_id)"
    )

    # ── 4 · trace vào draft (G6) ─────────────────────────────────────────────────────────
    op.execute("ALTER TABLE pending_reply ADD COLUMN trace_id UUID")
    op.execute("UPDATE pending_reply SET trace_id = gen_random_uuid() WHERE trace_id IS NULL")
    op.execute("ALTER TABLE pending_reply ALTER COLUMN trace_id SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE pending_reply DROP COLUMN trace_id")
    op.execute("ALTER TABLE messages DROP CONSTRAINT uq_messages_conv_platform_msg")
    op.execute("ALTER TABLE messages DROP COLUMN platform_msg_id")
    op.execute("DROP TABLE outbox")
    op.execute("DROP TYPE outbox_status")
    op.execute("ALTER TABLE webhook_event_log DROP COLUMN trace_id")
    op.execute("ALTER TABLE webhook_event_log DROP COLUMN raw_event")
    # DROP COLUMN kéo theo UNIQUE constraint + identity sequence của nó.
    op.execute("ALTER TABLE webhook_event_log DROP COLUMN event_id")
