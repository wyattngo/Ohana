"""A2 — grant hẹp cho hai đường bị a1 chặn ngoài ý muốn.

Hai lỗ phát hiện ở A4 khi chạy entrypoint dưới role thật:

1. Auth luồng A: `build_active_shop_dep` SELECT `public.shops` — svc_ohana_ai
   zero quyền trên public ⇒ mọi request authed 500. `shops` ở đây đóng vai
   `core.account_shop` của design §8.4: registry tenant, KHÔNG phải dữ liệu shop.
   Grant SELECT đúng MỘT bảng, không grant default privileges — bảng public
   tương lai vẫn ngoài tầm luồng A (I2 giữ nguyên nội dung: pending_reply,
   messages, customers… vẫn denied).

2. Wiki ingest: route admin (mount ở main_seller, role svc_seller) ghi
   `platform.embeddings` — a1 chỉ cho svc_seller USAGE trên schema platform,
   không có quyền bảng nào. Drafter luồng B (shop_kb → retrieval) cũng cần
   SELECT corpus. Grant đủ bốn quyền trên đúng MỘT bảng + sequence của nó.
   KHÔNG thêm default privileges trong platform cho svc_seller: bảng corpus
   tương lai grant tường minh khi tạo — hẹp mặc định, nới có chủ đích.

BẮT BUỘC chạy bằng role `ohana_migrator` (xem a1 — cùng lý do I14).

Gate: tests/contract/test_a2_flow_grants.py

Revision ID: a2_flow_grants
"""

from __future__ import annotations

from alembic import op

revision = "a2_flow_grants"
down_revision = "a1_platform_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # (1) — auth luồng A đọc registry tenant. CHỈ SELECT, CHỈ bảng này.
    op.execute("GRANT SELECT ON TABLE public.shops TO svc_ohana_ai")

    # (2) — ingest + RAG luồng B trên corpus. CHỈ bảng embeddings + sequence id.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE platform.embeddings TO svc_seller")
    op.execute("GRANT USAGE ON SEQUENCE platform.embeddings_id_seq TO svc_seller")


def downgrade() -> None:
    op.execute("REVOKE USAGE ON SEQUENCE platform.embeddings_id_seq FROM svc_seller")
    op.execute("REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE platform.embeddings FROM svc_seller")
    op.execute("REVOKE SELECT ON TABLE public.shops FROM svc_ohana_ai")
