"""A1 — schema platform + phân quyền luồng A/B.

Cưỡng chế I2, I14, I15.

BẮT BUỘC chạy bằng role `ohana_migrator`:

    DATABASE_URL=postgresql+psycopg://ohana_migrator:PW@localhost:5432/ohana \\
        alembic upgrade head

`ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator` chỉ phủ bảng DO ROLE ĐÓ tạo.
Chạy alembic bằng role khác (kể cả owner `ohana`) thì mọi bảng sinh ra sau đó
rơi ra ngoài I2 — và không có gì báo lỗi.

Điều kiện: `scripts/bootstrap_roles.py` đã chạy.

Revision ID: a1_platform_grants
"""

from __future__ import annotations

from alembic import op

revision = "a1_platform_grants"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Luồng A chỉ cần corpus (workflow §1: không chạm dữ liệu shop) ────────
    # Di cư MỘT bảng thay vì mười. Chín bảng còn lại ở `public`, không đụng.
    op.execute("CREATE SCHEMA IF NOT EXISTS platform")
    op.execute("ALTER TABLE embeddings SET SCHEMA platform")

    # ── I2 · luồng A KHÔNG có quyền nào trên `public` ────────────────────────
    # Đó là toàn bộ nội dung của I2: SELECT * FROM pending_reply từ
    # svc_ohana_ai ⇒ permission denied ở tầng DB, không phải ở tầng code review.
    op.execute("GRANT USAGE ON SCHEMA platform TO svc_ohana_ai")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA platform TO svc_ohana_ai")

    # ── Luồng B ──────────────────────────────────────────────────────────────
    op.execute("GRANT USAGE ON SCHEMA public, platform TO svc_seller")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO svc_seller")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO svc_seller")

    # ── I15 · MCP đọc tất cả, ghi không gì ───────────────────────────────────
    op.execute("GRANT USAGE ON SCHEMA public, platform TO mcp_readonly")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public, platform TO mcp_readonly")

    # ── I14 · BẢNG TƯƠNG LAI ─────────────────────────────────────────────────
    # `ALL TABLES` ở trên chỉ phủ bảng ĐANG tồn tại. Thiếu khối này thì mọi bảng
    # của A5/A6 (outbox, cost_budget, cost_reservation…) rơi ra ngoài — I2 hỏng
    # im lặng ở bảng thứ N+1. Gate: tests/contract/test_i14_default_privileges.py
    for statement in (
        "ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA platform "
        "GRANT SELECT ON TABLES TO svc_ohana_ai",
        "ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE ON TABLES TO svc_seller",
        "ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA public "
        "GRANT USAGE ON SEQUENCES TO svc_seller",
        "ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA public, platform "
        "GRANT SELECT ON TABLES TO mcp_readonly",
    ):
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE platform.embeddings SET SCHEMA public")
    op.execute("DROP SCHEMA IF EXISTS platform")
    # Role sống ở cấp cluster — gỡ bằng bootstrap_roles, không phải ở đây.
