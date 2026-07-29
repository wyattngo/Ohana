"""Tạo role cấp cluster cho Ohana BE.

Chạy MỘT LẦN mỗi cluster Postgres, TRƯỚC `alembic upgrade head`.

Vì sao không nằm trong Alembic: role là đối tượng cấp *cluster*, grant là cấp
*database*. Nhét CREATE ROLE vào migration thì restore dump sang cluster mới sẽ
mất role trong khi bảng `alembic_version` vẫn báo revision đã chạy — hỏng theo
kiểu không ai thấy cho tới lúc service không kết nối được.

Idempotent: chạy lại sẽ cập nhật mật khẩu, không lỗi.

    SUPERUSER_DSN=postgresql://ohana:PW@localhost:5432/ohana \\
        python scripts/bootstrap_roles.py
"""

from __future__ import annotations

import os
import sys

import psycopg

# role → tên biến env chứa mật khẩu
ROLES: dict[str, str] = {
    "ohana_migrator": "MIGRATOR_PW",  # role DUY NHẤT được tạo bảng
    "svc_ohana_ai": "SVC_A_PW",  # luồng A — chỉ thấy schema platform
    "svc_seller": "SVC_B_PW",  # luồng B
    "mcp_readonly": "MCP_RO_PW",  # MCP soi DB, chỉ đọc
}


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> int:
    try:
        dsn = os.environ["SUPERUSER_DSN"]
    except KeyError:
        print("thiếu SUPERUSER_DSN", file=sys.stderr)
        return 1

    missing = [env for env in ROLES.values() if not os.environ.get(env)]
    if missing:
        print(f"thiếu biến env: {', '.join(missing)}", file=sys.stderr)
        return 1

    with psycopg.connect(dsn, autocommit=True) as conn:
        for role, env_key in ROLES.items():
            literal = _sql_literal(os.environ[env_key])
            conn.execute(
                f"""
                DO $$ BEGIN
                  EXECUTE format('CREATE ROLE {role} LOGIN PASSWORD %L', {literal});
                EXCEPTION WHEN duplicate_object THEN
                  EXECUTE format('ALTER ROLE {role} LOGIN PASSWORD %L', {literal});
                END $$;
                """
            )
            print(f"role ok: {role}")

        # ohana_migrator phải tạo được bảng trong public — đó là tiền đề của
        # `ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator` ở revision A1.
        conn.execute("GRANT CREATE, USAGE ON SCHEMA public TO ohana_migrator")
        # ... và tạo được SCHEMA mới (A1: `CREATE SCHEMA platform`) — quyền này
        # nằm ở cấp database, GRANT trên schema public không phủ.
        row = conn.execute("SELECT current_database()").fetchone()
        if row is None:  # current_database() luôn trả 1 row — nhánh này chỉ để mypy
            raise RuntimeError("SELECT current_database() không trả row nào")
        dbname = row[0]
        conn.execute(f'GRANT CREATE ON DATABASE "{dbname}" TO ohana_migrator')
        conn.execute("GRANT ohana_migrator TO CURRENT_USER")
        print(f"grant ok: ohana_migrator CREATE ON public + DATABASE {dbname}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
