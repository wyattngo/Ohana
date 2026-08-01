"""A14 — bảng `assistant.message_feedback` cho R4 (ADR round2).

Cấu trúc: PK composite `(message_id, user_id)` — mỗi user rate mỗi message tối đa 1 lần
(upsert ghi đè). `rating smallint CHECK (rating IN (-1, 1))` — 2 giá trị bằng 1 byte;
CHECK enforce ở DB, KHÔNG dựa Pydantic vì repo có thể gọi thẳng bypass endpoint.

**FK `message_id → assistant.messages ON DELETE CASCADE`**: xoá message (qua CASCADE từ
conversation soft/hard-delete tương lai) ⇒ feedback theo. Không giữ feedback orphan cho
message đã biến mất.

**`user_id` là text KHÔNG FK**: khớp `assistant.conversations.user_id` — user_id sống
ở JWT sub (D7), không có bảng users trong assistant schema. Enforce scope ở endpoint
(check message thuộc conversation của caller) — không có DB constraint nào để lỡ ghi
feedback với user_id lạ, chỉ endpoint gate.

**KHÔNG grant riêng**: `ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA
assistant` từ a12 phủ. Verify: test_a14_feedback_grants trong tests/contract/.

**BẮT BUỘC chạy role `ohana_migrator`** (bẫy Alembic của skill — I14):

    DATABASE_URL="postgresql+psycopg://ohana_migrator:PW@localhost:5433/ohana" \\
        alembic upgrade head

Revision ID: a14_assistant_feedback
Revises: a13_send_dedup_log
"""

from __future__ import annotations

from alembic import op

revision = "a14_assistant_feedback"
down_revision = "a13_send_dedup_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE assistant.message_feedback ("
        "  message_id  bigint      NOT NULL REFERENCES assistant.messages(message_id) "
        "              ON DELETE CASCADE,"
        "  user_id     text        NOT NULL,"
        "  rating      smallint    NOT NULL CHECK (rating IN (-1, 1)),"
        "  note        text,"
        "  created_at  timestamptz NOT NULL DEFAULT now(),"
        "  updated_at  timestamptz NOT NULL DEFAULT now(),"
        "  PRIMARY KEY (message_id, user_id)"
        ")"
    )
    # Index cho analytics-style query "1 user đã rate bao nhiêu message tuần này" +
    # "message X có tổng rating bao nhiêu". PK đã cover access theo message+user.
    op.execute(
        "CREATE INDEX idx_assistant_feedback_user_created "
        "ON assistant.message_feedback (user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS assistant.message_feedback")
