"""A5 — bảng `outbox`: hàng đợi sự kiện inbound (design §5.4, ánh xạ tên phẳng).

DDL viết tay mirror `db/models.py::Outbox` — autogenerate không thấy partial index
và CHECK (bẫy Alembic đã ghi trong skill). Đọc docstring model để hiểu ba chỗ lệch
design-đích (FK compound, payload đã parse thay raw_event vì O8, payload nullable).

BẮT BUỘC chạy bằng `ohana_migrator` (I14): default privileges của a1 tự phủ bảng
này cho svc_seller (SELECT/INSERT/UPDATE — đủ cho webhook ghi + worker claim,
KHÔNG có DELETE: queue không xoá row, chỉ đổi status) và mcp_readonly (SELECT).
svc_ohana_ai zero quyền — outbox là luồng B thuần (I2).

Gate: tests/test_outbox_a5.py + tests/contract/test_i14_default_privileges.py

Revision ID: a5_outbox
"""

from __future__ import annotations

from alembic import op

revision = "a5_outbox"
down_revision = "a2_flow_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE outbox (
          outbox_id       bigserial PRIMARY KEY,
          channel         text        NOT NULL,
          platform_msg_id text        NOT NULL,
          shop_id         text        NOT NULL REFERENCES shops (id),
          payload         jsonb,
          status          text        NOT NULL DEFAULT 'pending'
                          CONSTRAINT ck_outbox_status
                          CHECK (status IN ('pending', 'processing', 'done', 'failed')),
          attempts        int         NOT NULL DEFAULT 0,
          claimed_at      timestamptz,
          last_error      text,
          trace_id        uuid        NOT NULL,
          created_at      timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT fk_outbox_event
            FOREIGN KEY (channel, platform_msg_id)
            REFERENCES webhook_event_log (channel, platform_msg_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_outbox_pending_created ON outbox (created_at) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE INDEX idx_outbox_processing_claimed ON outbox (claimed_at) "
        "WHERE status = 'processing'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE outbox")
