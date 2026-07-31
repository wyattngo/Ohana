"""D2 · isolation gate hai chiều cho schema `assistant` (Bước 2 Phase 2.1 · OHB-27).

Chứng minh D2 (grant WRITE svc_ohana_ai trong `assistant`) KHÔNG phá I2:

- svc_ohana_ai INSERT/SELECT được `assistant.*` — D2 mở đường ghi luồng A.
- svc_ohana_ai vẫn DENIED `public.*` (I2 cho seller data — test lại ở đây để
  đảm bảo grant `assistant` KHÔNG rò sang `public`).
- svc_seller DENIED `assistant.*` — luồng B không thấy memory user.
- mcp_readonly DENIED `assistant.*` — memory user không phải scope MCP.

I14 cho bảng tương lai trong `assistant`: probe table tạo SAU migration a12
vẫn được `ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA assistant`
phủ. Đây là song song test_i14_default_privileges nhưng cho schema mới.

Cần 4 biến env DSN như tests khác (SETUP.md §4):
    MIGRATOR_DSN  SVC_A_DSN  SVC_B_DSN  MCP_RO_DSN
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest
from conftest import requires_dsn

pytestmark = requires_dsn

PROBE = "assistant.probe_a12"


@pytest.fixture
def probe_table() -> Iterator[None]:
    """Bảng MỚI trong schema `assistant`, tạo bằng ohana_migrator SAU migration a12.

    Cùng lý do test_i14_default_privileges: bảng phải tạo sau lần GRANT ban đầu
    để chứng minh ALTER DEFAULT PRIVILEGES phủ đúng — grant tay thì test này
    không đo được gì có ý nghĩa với I14.
    """
    with psycopg.connect(os.environ["MIGRATOR_DSN"], autocommit=True) as conn:
        conn.execute(f"CREATE TABLE {PROBE} (id int PRIMARY KEY, note text)")
        try:
            yield
        finally:
            conn.execute(f"DROP TABLE IF EXISTS {PROBE}")


def test_flow_a_can_write_assistant(probe_table: None) -> None:
    """D2 · svc_ohana_ai INSERT/SELECT được `assistant.*`.

    Bảng probe tạo sau migration a12 ⇒ nếu ALTER DEFAULT PRIVILEGES thiếu, INSERT
    sẽ raise InsufficientPrivilege — đây là gate cho I14 của schema mới.
    """
    with psycopg.connect(os.environ["SVC_A_DSN"], autocommit=True) as conn:
        conn.execute(f"INSERT INTO {PROBE} (id, note) VALUES (1, 'ok')")
        assert conn.execute(f"SELECT id, note FROM {PROBE}").fetchall() == [(1, "ok")]


def test_flow_a_still_denied_public_shop_data() -> None:
    """I2 KHÔNG rò sau D2 · svc_ohana_ai vẫn không đọc được `public.pending_reply`.

    Nếu test này đỏ ⇒ grant `assistant` làm hỏng gì đó ở `public` — phải điều
    tra ngay (có thể ai đó thêm nhầm `GRANT ... ON ALL TABLES IN SCHEMA public`
    vào a12). D2 phải scoped `assistant` bằng SQL, KHÔNG bằng review.
    """
    with psycopg.connect(os.environ["SVC_A_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM pending_reply").fetchall()


def test_flow_b_denied_assistant(probe_table: None) -> None:
    """I2-symmetric · svc_seller không thấy memory user (bảng mới trong `assistant`).

    Luồng B (seller copilot) không có nghiệp vụ đọc user memory Tầng 2 — grant
    có chủ đích khi cần cross-cutting, KHÔNG mặc định.

    `autocommit=True`: hai statement độc lập — không autocommit, statement đầu raise
    InsufficientPrivilege làm transaction fail, statement thứ hai raise
    InFailedSqlTransaction (che mất tín hiệu grant thật). Cùng lý do
    `test_flow_a_can_write_assistant` dùng autocommit."""
    with psycopg.connect(os.environ["SVC_B_DSN"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT * FROM {PROBE}").fetchall()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"INSERT INTO {PROBE}(id) VALUES (99)")


def test_mcp_denied_assistant(probe_table: None) -> None:
    """MCP scoped cho shop data (public/platform), KHÔNG cho user memory (assistant).

    Nếu design đổi (vd muốn MCP query memory để debug) ⇒ grant MCP có chủ đích
    ở migration riêng, KHÔNG mở mặc định qua ALTER DEFAULT PRIVILEGES."""
    with psycopg.connect(os.environ["MCP_RO_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT * FROM {PROBE}").fetchall()
