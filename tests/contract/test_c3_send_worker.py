"""C2 send-side + R5 · send-worker claim đúng-một-lần + reaper R5 (OHB-25 · I13).

Song song `test_c2_scheduler.py` cho phía SEND. Hai tài sản cần gate contract:

* **C2 send-side** (SKIP LOCKED trên `_CLAIM_SEND`): N send-worker cùng thấy MỘT
  `pending_reply` đến `status='approved'` ⇒ đúng một bên claim được ⇒ đúng một lần gửi.
  Cơ chế: `sent_claimed_at IS NULL` trong WHERE + `FOR UPDATE SKIP LOCKED` — Postgres
  serialize UPDATE, bên thua nhận 0 row.
* **R5** (`_REAP_R5_STUCK_SEND_CLAIM`, I13): worker chết SAU `_CLAIM_SEND` nhưng TRƯỚC
  `_MARK_SENT`/`_RELEASE_SEND_CLAIM` ⇒ row kẹt ở `sent_claimed_at IS NOT NULL`,
  `status='approved'`. R5 NULL `sent_claimed_at` khi >5' để lượt claim kế nhặt lại;
  KHÔNG đổi `status` (đó là quyết định của worker sau khi thực sự gọi sender).

SQL nguyên văn chép từ `db/repos.py` (`_CLAIM_SEND` và `_REAP_R5_STUCK_SEND_CLAIM`) —
cùng lý do `test_c2_scheduler.py`: contract test canh HÌNH DẠNG câu lệnh, import từ code
sản xuất là mất khả năng bắt drift SQL. Đổi placeholder psycopg thô (`%s`) thay
`sqlalchemy.text` `:name`.

Cần 4 DSN role — xem SETUP.md §4 (dùng chung `conftest.py::requires_dsn`).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from conftest import requires_dsn, seed_tenant, wipe_tenant

pytestmark = requires_dsn

CHANNEL = "c3test"
SHOP = "c3test_shop"
CUSTOMER = "c3test_customer"
CONVERSATION = "c3test_conversation"

# `db/repos.py::_CLAIM_SEND` — placeholder đổi từ `sqlalchemy.text` không có tham số
# sang psycopg thô (không có tham số vì câu này quét toàn cluster, không scope shop).
SQL_CLAIM_SEND = """
UPDATE pending_reply SET sent_claimed_at = now()
WHERE reply_id IN (
  SELECT reply_id FROM pending_reply
   WHERE status = 'approved' AND sent_claimed_at IS NULL
   ORDER BY created_at
   FOR UPDATE SKIP LOCKED LIMIT 20
)
RETURNING reply_id, shop_id, conversation_id, customer_id, draft_text, trace_id,
          sent_claimed_at
"""

# `db/repos.py::_REAP_R5_STUCK_SEND_CLAIM` — reaper R5, cùng khuôn §6.9. Quét qua partial
# index `idx_pending_reply_send_claim` (a11); điều kiện WHERE trùng khít index.
SQL_R5 = """
UPDATE pending_reply SET sent_claimed_at = NULL
 WHERE sent_claimed_at IS NOT NULL AND sent_claimed_at < now() - interval '5 minutes'
"""


@pytest.fixture
def migrator() -> Iterator[psycopg.Connection]:
    """Dọn/seed/backdate bằng migrator — svc_seller không việc gì phải sửa được đồng hồ."""
    with psycopg.connect(os.environ["MIGRATOR_DSN"], autocommit=True) as conn:
        wipe_tenant(conn, shop=SHOP, channel=CHANNEL)
        seed_tenant(conn, shop=SHOP, customer=CUSTOMER, conversation=CONVERSATION, channel=CHANNEL)
        try:
            yield conn
        finally:
            wipe_tenant(conn, shop=SHOP, channel=CHANNEL)


@pytest.fixture
def svc_b(migrator: psycopg.Connection) -> Iterator[psycopg.Connection]:
    with psycopg.connect(os.environ["SVC_B_DSN"], autocommit=True) as conn:
        yield conn


def _seed_approved_reply(conn: psycopg.Connection, reply_id: str) -> None:
    """Draft ở trạng thái đầu vào hàng đợi gửi: `status='approved'`, chưa claim."""
    conn.execute(
        "INSERT INTO pending_reply (reply_id, shop_id, conversation_id, customer_id, "
        "draft_text, intent, confidence, trace_id, status) "
        "VALUES (%s, %s, %s, %s, 'x', 'faq', 0.5, %s, 'approved')",
        (reply_id, SHOP, CONVERSATION, CUSTOMER, uuid.uuid4()),
    )


def test_r5_reaper_frees_stuck_send_claim(
    migrator: psycopg.Connection, svc_b: psycopg.Connection
) -> None:
    """I13/R5 · send-worker claim rồi 'chết' ⇒ R5 gỡ ⇒ draft quay lại hàng đợi gửi.

    Cùng bài `test_r3_reaper_frees_stuck_claim` (bên debounce): assert theo ROW CỦA MÌNH
    thay vì rowcount toàn cục, để dữ liệu nguồn khác trên DB dùng chung không làm test
    đỏ. R5 KHÔNG đổi `status` — kiểm tra tường minh để refactor sau (vd. R5 lỡ đặt
    'failed') vỡ ở đây."""
    _seed_approved_reply(migrator, "r5-stuck")

    def _my_claim_and_status() -> tuple[object, str]:
        row = svc_b.execute(
            "SELECT sent_claimed_at, status FROM pending_reply WHERE reply_id = %s",
            ("r5-stuck",),
        ).fetchone()
        assert row is not None
        return row[0], row[1]

    # Claim bằng svc_b (giả lập worker A đang chạy send).
    claimed = svc_b.execute(SQL_CLAIM_SEND).fetchone()
    assert claimed is not None  # RETURNING 1 row (chỉ có 1 draft trong hàng đợi)
    ts, status = _my_claim_and_status()
    assert ts is not None and status == "approved"

    # Treo <5' ⇒ R5 KHÔNG đụng (không gỡ claim của worker còn có thể sống).
    svc_b.execute(SQL_R5)
    ts, status = _my_claim_and_status()
    assert ts is not None and status == "approved"

    # Worker chết 6' trước — backdate mốc claim bằng migrator.
    migrator.execute(
        "UPDATE pending_reply SET sent_claimed_at = now() - interval '6 minutes' "
        "WHERE reply_id = %s",
        ("r5-stuck",),
    )
    svc_b.execute(SQL_R5)
    ts, status = _my_claim_and_status()
    assert ts is None  # claim gỡ ⇒ draft trở lại claimable
    assert status == "approved"  # R5 KHÔNG đổi status — quyết định gửi là của worker

    # Hệ tự hồi: draft quay lại hàng đợi, worker kế claim lại được ngay.
    reclaim = svc_b.execute(SQL_CLAIM_SEND).fetchone()
    assert reclaim is not None
    assert reclaim[0] == "r5-stuck"  # reply_id (col 0 của RETURNING)


def test_c2_send_side_two_workers_one_claim(migrator: psycopg.Connection) -> None:
    """C2 send-side · hai send-worker (hai connection thật) cùng claim ⇒ đúng một thắng.

    Cơ chế: `sent_claimed_at IS NULL` trong WHERE + `FOR UPDATE SKIP LOCKED`. Bên thua
    fetchone() = None ⇒ bỏ qua ⇒ không double-send. Song song
    `test_c2_two_schedulers_one_claim` (bên debounce). Cần 1 draft trong hàng đợi;
    seed nhiều hơn 1 ⇒ RETURNING nhiều row che mất hiện tượng cần gate."""
    _seed_approved_reply(migrator, "c2-send-race")

    with (
        psycopg.connect(os.environ["SVC_B_DSN"], autocommit=True) as worker_1,
        psycopg.connect(os.environ["SVC_B_DSN"], autocommit=True) as worker_2,
    ):
        first = worker_1.execute(SQL_CLAIM_SEND).fetchone()
        second = worker_2.execute(SQL_CLAIM_SEND).fetchone()

    assert first is not None
    assert first[0] == "c2-send-race"  # bên thắng RETURNING đúng draft
    assert second is None  # bên thua fetchone() = None ⇒ bỏ qua ⇒ không double-send
