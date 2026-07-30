"""C1 · dedup ở TẦNG DB — gate của A5 (design §9 · PRE-010 C1 · I7).

Ba lớp chống-nhân-đôi, mỗi lớp một test, chạy bằng ĐÚNG role runtime (svc_seller) để chứng
minh luôn grant a1/I14 đủ cho đường ghi thật:

1. §6.1 — platform retry cùng `(channel, platform_msg_id)` ⇒ lần 2 là no-op TRONG MỘT câu:
   không thêm row sổ, không enqueue lại. Đây chính là I7 — tách thành hai INSERT thì test
   này vẫn xanh ở bảng sổ mà đỏ ở outbox (draft đôi im lặng).
2. §6.2 — một row pending chỉ claim được ĐÚNG MỘT lần; claim xong row mang
   `status='processing'`, `attempts=1`.
3. C1 messages — worker double-process (R2 requeue) ⇒ `UNIQUE (conversation_id,
   platform_msg_id)` biến lần ghi thứ hai thành no-op.

SQL §6.1/§6.2 ở đây chép NGUYÊN VĂN từ design §6 (đổi placeholder theo psycopg) — cố ý
KHÔNG import từ `db/repos.py`: contract test canh HÌNH DẠNG câu lệnh mà DB thấy; repo đổi
hình dạng thì test này phải đỏ, import chung là tự chấm bài mình.

Cần 4 biến env DSN như test_i14 — xem SETUP.md §4.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from conftest import requires_dsn, seed_tenant, wipe_tenant
from psycopg.types.json import Jsonb

pytestmark = requires_dsn

# Định danh test cố định + dọn trước-và-sau ⇒ chạy lặp trên cùng DB vẫn tất định.
CHANNEL = "c1test"
SHOP = "c1test_shop"
CUSTOMER = "c1test_customer"
CONVERSATION = "c1test_conversation"

SQL_6_1 = """
WITH ins AS (
  INSERT INTO webhook_event_log (channel, platform_msg_id, shop_id, raw_event, trace_id)
  VALUES (%(channel)s, %(platform_msg_id)s, %(shop_id)s, %(raw_event)s, %(trace_id)s)
  ON CONFLICT (channel, platform_msg_id) DO NOTHING
  RETURNING event_id, shop_id, trace_id
)
INSERT INTO outbox (event_id, shop_id, payload, trace_id)
SELECT event_id, shop_id, %(payload)s, trace_id FROM ins
RETURNING outbox_id
"""

SQL_6_2 = """
UPDATE outbox SET status='processing', claimed_at=now(), attempts=attempts+1
WHERE outbox_id IN (
  SELECT outbox_id FROM outbox
   WHERE status='pending'
     AND (next_retry_at IS NULL OR next_retry_at <= now())
   ORDER BY created_at
   FOR UPDATE SKIP LOCKED LIMIT 20
)
RETURNING outbox_id, status, attempts
"""


@pytest.fixture
def migrator() -> Iterator[psycopg.Connection]:
    """Connection dọn dẹp/seed — svc_seller cố ý KHÔNG có DELETE (a1), nên dọn bằng migrator."""
    with psycopg.connect(os.environ["MIGRATOR_DSN"], autocommit=True) as conn:
        wipe_tenant(conn, shop=SHOP, channel=CHANNEL)
        try:
            yield conn
        finally:
            wipe_tenant(conn, shop=SHOP, channel=CHANNEL)


@pytest.fixture
def svc_b(migrator: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Role runtime luồng B — mọi thao tác ghi trong file này đi bằng role THẬT."""
    with psycopg.connect(os.environ["SVC_B_DSN"], autocommit=True) as conn:
        yield conn


def _deliver(conn: psycopg.Connection, *, msg_id: str = "msg-1") -> int | None:
    row = conn.execute(
        SQL_6_1,
        {
            "channel": CHANNEL,
            "platform_msg_id": msg_id,
            "shop_id": SHOP,
            "raw_event": Jsonb({"raw": True}),
            "payload": Jsonb({"text": "xin chào"}),
            "trace_id": uuid.uuid4(),
        },
    ).fetchone()
    return None if row is None else int(row[0])


def test_6_1_retry_is_noop_in_one_statement(svc_b: psycopg.Connection) -> None:
    """I7 · lần giao thứ hai: 0 row trả về, KHÔNG thêm sổ, KHÔNG enqueue lại."""
    assert _deliver(svc_b) is not None
    assert _deliver(svc_b) is None  # platform retry ⇒ CTE rỗng ⇒ không có outbox_id

    (seen,) = svc_b.execute(
        "SELECT count(*) FROM webhook_event_log WHERE channel = %s", (CHANNEL,)
    ).fetchone()
    (queued,) = svc_b.execute("SELECT count(*) FROM outbox WHERE shop_id = %s", (SHOP,)).fetchone()
    assert (seen, queued) == (1, 1)


def _claim_ids(conn: psycopg.Connection) -> dict[int, tuple[str, int]]:
    """Chạy §6.2, trả {outbox_id: (status, attempts)}. Câu claim tự nó KHÔNG scope theo
    shop (đúng hành vi worker thật) — test lọc theo id CỦA MÌNH khi assert, để row pending
    của nguồn khác trên DB dùng chung không làm assert đỏ (review A5-A8)."""
    return {r[0]: (r[1], r[2]) for r in conn.execute(SQL_6_2).fetchall()}


def test_6_2_claim_exactly_once(svc_b: psycopg.Connection) -> None:
    """§6.2 · claim đánh dấu processing + attempts=1; claim lại không thấy row của mình."""
    oid = _deliver(svc_b)
    assert oid is not None

    claimed = _claim_ids(svc_b)
    assert claimed.get(oid) == ("processing", 1)

    assert oid not in _claim_ids(svc_b)  # hết pending ⇒ lượt sau không thấy row này nữa


def test_6_2_backoff_row_waits_out_its_delay(
    migrator: psycopg.Connection, svc_b: psycopg.Connection
) -> None:
    """§6.2 (amend a9) · row đang trong backoff KHÔNG bị claim; hết hạn chờ thì claim được.

    Không có điều kiện này, row lỗi là row CŨ NHẤT nên bị claim lại ngay tick 200ms kế
    tiếp — 5 attempts cháy trong ~1 giây và lỗi thoáng qua cũng thành dead (review A5-A8).
    """
    oid = _deliver(svc_b)
    migrator.execute(
        "UPDATE outbox SET next_retry_at = now() + interval '1 hour' WHERE shop_id = %s",
        (SHOP,),
    )
    assert oid not in _claim_ids(svc_b)  # đang chờ backoff — không claim

    migrator.execute(
        "UPDATE outbox SET next_retry_at = now() - interval '1 second' WHERE shop_id = %s",
        (SHOP,),
    )
    assert oid in _claim_ids(svc_b)  # hết hạn chờ — claim bình thường


def test_c1_worker_double_process_writes_one_message(
    migrator: psycopg.Connection, svc_b: psycopg.Connection
) -> None:
    """C1 · ghi tin khách hai lần cùng khoá (job requeue) ⇒ đúng 1 row messages."""
    # Seed cây tenant bằng migrator — test này canh khoá C1, không canh grant seed-path.
    seed_tenant(migrator, shop=SHOP, customer=CUSTOMER, conversation=CONVERSATION, channel=CHANNEL)

    insert = (
        "INSERT INTO messages "
        "(shop_id, conversation_id, customer_id, role, content, platform_msg_id) "
        "VALUES (%s, %s, %s, 'user', 'xin chào', %s) "
        "ON CONFLICT (conversation_id, platform_msg_id) DO NOTHING"
    )
    for _ in range(2):
        svc_b.execute(insert, (SHOP, CONVERSATION, CUSTOMER, "msg-1"))

    (count,) = svc_b.execute(
        "SELECT count(*) FROM messages WHERE conversation_id = %s", (CONVERSATION,)
    ).fetchone()
    assert count == 1


def test_flow_a_cannot_touch_queue(migrator: psycopg.Connection) -> None:
    """I2/I14 · bảng mới của A5 nằm ngoài tầm luồng A — không cần GRANT tay nào."""
    with psycopg.connect(os.environ["SVC_A_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM outbox").fetchall()
