"""I14 · bảng TƯƠNG LAI phải được phủ bởi default privileges.

Gate đầu tiên của A1. Nó chứng minh cơ chế cưỡng chế mạnh nhất trong design —
I2 — vẫn đúng với bảng tạo SAU lần GRANT ban đầu.

`GRANT ... ON ALL TABLES` chỉ phủ bảng đang tồn tại. Không có
`ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator`, test này đỏ — và nếu không
có test này, I2 sẽ hỏng im lặng ở bảng thứ N+1 của đời dự án.

Cần 4 biến env, mỗi cái là DSN của một role:
    MIGRATOR_DSN  SVC_A_DSN  SVC_B_DSN  MCP_RO_DSN
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

PROBE = "public.probe_i14"

_REQUIRED = ("MIGRATOR_DSN", "SVC_A_DSN", "SVC_B_DSN", "MCP_RO_DSN")

pytestmark = pytest.mark.skipif(
    any(not os.environ.get(k) for k in _REQUIRED),
    reason="cần 4 DSN role — xem SETUP.md §4",
)


@pytest.fixture
def probe_table() -> Iterator[None]:
    """Bảng MỚI, tạo bằng ohana_migrator SAU lần GRANT ở A1."""
    with psycopg.connect(os.environ["MIGRATOR_DSN"], autocommit=True) as conn:
        conn.execute(f"CREATE TABLE {PROBE} (id int PRIMARY KEY)")
        try:
            yield
        finally:
            conn.execute(f"DROP TABLE IF EXISTS {PROBE}")


def test_flow_a_cannot_read_new_table(probe_table: None) -> None:
    """I2 · luồng A không thấy dữ liệu shop — kể cả bảng vừa tạo."""
    with psycopg.connect(os.environ["SVC_A_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT * FROM {PROBE}").fetchall()


def test_flow_b_can_read_new_table(probe_table: None) -> None:
    """I14 · luồng B đọc được bảng mới mà KHÔNG cần GRANT thủ công."""
    with psycopg.connect(os.environ["SVC_B_DSN"]) as conn:
        assert conn.execute(f"SELECT * FROM {PROBE}").fetchall() == []


def test_mcp_can_read_but_not_write(probe_table: None) -> None:
    """I15 · MCP soi được, sửa không được."""
    with psycopg.connect(os.environ["MCP_RO_DSN"]) as conn:
        assert conn.execute(f"SELECT * FROM {PROBE}").fetchall() == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"INSERT INTO {PROBE}(id) VALUES (1)")


def test_flow_a_cannot_read_existing_shop_data() -> None:
    """I2 · bảng đang có cũng ngoài tầm luồng A."""
    with psycopg.connect(os.environ["SVC_A_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM pending_reply").fetchall()
