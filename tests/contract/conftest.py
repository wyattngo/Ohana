"""Hạ tầng dùng chung cho contract tests (review A5-A8: 4 file tự chép skipif + _wipe +
seed tree — A9 thêm bảng FK mới là phải sửa 4 chỗ, sót một là teardown nổ FK hoặc rác row
làm rerun flaky trên DB dùng chung).

Ba mảnh, mỗi file import từ đây (pytest prepend thư mục test vào sys.path — `from conftest
import ...` chạy được, không cần package):

- `requires_dsn` — skipif chung: cần 4 DSN role, xem SETUP.md §4.
- `wipe_tenant` — dọn MỌI bảng theo đúng thứ tự FK, lọc theo (shop, channel) của file.
  Bảng mới có FK vào cây tenant ⇒ thêm MỘT dòng ở đây, mọi file hưởng.
- `seed_tenant` — cây shop → customer → conversation tối thiểu cho FK.

Chạy bằng MIGRATOR (dọn/seed/backdate) — svc_seller cố ý không có DELETE (a1) và không
việc gì phải sửa được đồng hồ.
"""

from __future__ import annotations

import os

import psycopg
import pytest

DSN_KEYS = ("MIGRATOR_DSN", "SVC_A_DSN", "SVC_B_DSN", "MCP_RO_DSN")

requires_dsn = pytest.mark.skipif(
    any(not os.environ.get(k) for k in DSN_KEYS),
    reason="cần 4 DSN role — xem SETUP.md §4",
)


def wipe_tenant(conn: psycopg.Connection, *, shop: str, channel: str) -> None:
    """Xoá sạch dấu vết một tenant test — thứ tự theo FK, từ lá về gốc."""
    conn.execute(
        "DELETE FROM outbox WHERE event_id IN "
        "(SELECT event_id FROM webhook_event_log WHERE channel = %s)",
        (channel,),
    )
    conn.execute("DELETE FROM webhook_event_log WHERE channel = %s", (channel,))
    conn.execute("DELETE FROM cost_reservation WHERE shop_id = %s", (shop,))
    conn.execute("DELETE FROM cost_budget WHERE shop_id = %s", (shop,))
    # OHB-24 · sent_log dedup — xoá trước pending_reply (không FK nhưng cùng cây tenant).
    conn.execute("DELETE FROM sent_log WHERE shop_id = %s", (shop,))
    conn.execute("DELETE FROM pending_reply WHERE shop_id = %s", (shop,))
    conn.execute("DELETE FROM messages WHERE shop_id = %s", (shop,))
    conn.execute("DELETE FROM conversations WHERE shop_id = %s", (shop,))
    conn.execute("DELETE FROM customers WHERE shop_id = %s", (shop,))
    conn.execute("DELETE FROM shops WHERE id = %s", (shop,))


def seed_tenant(
    conn: psycopg.Connection, *, shop: str, customer: str, conversation: str, channel: str
) -> None:
    """Cây tenant tối thiểu để mọi FK trong test đứng được."""
    conn.execute("INSERT INTO shops (id, name) VALUES (%s, 'Contract Test Shop')", (shop,))
    conn.execute(
        "INSERT INTO customers (id, shop_id, channel, external_id) VALUES (%s, %s, %s, 'ext-1')",
        (customer, shop, channel),
    )
    conn.execute(
        "INSERT INTO conversations (id, shop_id, customer_id, channel) VALUES (%s, %s, %s, %s)",
        (conversation, shop, customer, channel),
    )
