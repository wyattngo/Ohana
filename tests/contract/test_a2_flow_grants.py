"""Gate của migration a2 — grant hẹp cho auth luồng A + ingest luồng B.

Chứng minh hai chiều: đường được MỞ hoạt động, và mở KHÔNG rộng hơn một bảng.
`shops` là registry tenant (vai `core.account_shop` §8.4) — luồng A đọc để
xác thực; mọi bảng dữ liệu shop khác vẫn denied (test_i14 canh phần đó).

Cùng cơ chế env với test_i14: cần 4 DSN role, thiếu thì skip.
"""

from __future__ import annotations

import os

import psycopg
import pytest

_REQUIRED = ("SVC_A_DSN", "SVC_B_DSN")

pytestmark = pytest.mark.skipif(
    any(not os.environ.get(k) for k in _REQUIRED),
    reason="cần DSN role — xem SETUP.md §4",
)


def test_flow_a_can_read_shops_registry() -> None:
    """a2(1) · auth luồng A: SELECT `shops` phải chạy — không thì mọi request authed 500."""
    with psycopg.connect(os.environ["SVC_A_DSN"]) as conn:
        conn.execute("SELECT id, status FROM shops").fetchall()


def test_flow_a_cannot_write_shops() -> None:
    """a2(1) chỉ mở SELECT: tạo/sửa tenant không phải việc của luồng A."""
    with psycopg.connect(os.environ["SVC_A_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO shops (id, name, status) VALUES ('x', 'x', 'active')")


def test_flow_b_can_write_corpus() -> None:
    """a2(2) · ingest: svc_seller INSERT được `platform.embeddings` (rollback, không để rác)."""
    with psycopg.connect(os.environ["SVC_B_DSN"]) as conn:
        conn.execute(
            "INSERT INTO platform.embeddings (shop_id, namespace, chunk) "
            "VALUES ('probe-a2', 'probe', 'x')"
        )
        conn.rollback()


def test_flow_a_cannot_write_corpus() -> None:
    """Luồng A đọc corpus (a1) nhưng KHÔNG ghi — ingest thuộc route admin luồng B."""
    with psycopg.connect(os.environ["SVC_A_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO platform.embeddings (shop_id, namespace, chunk) "
                "VALUES ('probe-a2', 'probe', 'x')"
            )
