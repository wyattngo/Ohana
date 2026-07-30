"""I8 · cost cap pre-charge atomic — gate của A6 (design §6.5 §6.5b · I8, I13).

Chạy bằng role runtime thật (svc_seller) như test_c1: chứng minh luôn grant a1/I14 đủ
cho đường ghi cost. SQL §6.5/§6.5b chép NGUYÊN VĂN từ design (đổi placeholder theo
psycopg) — cố ý KHÔNG import từ `db/repos.py`, cùng lý do đã ghi ở test_c1_dedup: contract
test canh HÌNH DẠNG câu lệnh mà DB thấy; repo đổi hình dạng thì test này phải đỏ.

Nội dung I8 nằm ở chỗ điều kiện cap sống TRONG WHERE của UPDATE: vượt trần ⇒ CTE rỗng ⇒
INSERT reservation không chạy — 0 row, KHÔNG có reservation mồ côi, KHÔNG có reserved
lệch. Check-rồi-update không tái lập được tính chất đó dưới race.

Cần 4 biến env DSN như test_i14 — xem SETUP.md §4.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from conftest import requires_dsn, wipe_tenant

pytestmark = requires_dsn

SHOP = "i8test_shop"
CHANNEL = "i8test"
CAP = 100

SQL_6_5 = """
WITH upd AS (
  UPDATE cost_budget
     SET reserved_tokens = reserved_tokens + %(tokens)s
   WHERE shop_id = %(shop_id)s AND budget_date = CURRENT_DATE
     AND reserved_tokens + actual_tokens + %(tokens)s <= cap_tokens
  RETURNING shop_id, budget_date
)
INSERT INTO cost_reservation (shop_id, budget_date, tokens, trace_id)
SELECT shop_id, budget_date, %(tokens)s, %(trace_id)s FROM upd
RETURNING reservation_id
"""

SQL_6_5B = """
WITH rel AS (
  UPDATE cost_reservation SET released_at = now()
   WHERE reservation_id = %(reservation_id)s AND released_at IS NULL
  RETURNING shop_id, budget_date, tokens
)
UPDATE cost_budget b
   SET reserved_tokens = b.reserved_tokens - rel.tokens,
       actual_tokens   = b.actual_tokens   + %(actual_tokens)s
  FROM rel
 WHERE b.shop_id = rel.shop_id AND b.budget_date = rel.budget_date
"""


@pytest.fixture
def migrator() -> Iterator[psycopg.Connection]:
    """Dọn/seed bằng migrator — svc_seller cố ý không có DELETE (a1)."""
    with psycopg.connect(os.environ["MIGRATOR_DSN"], autocommit=True) as conn:
        wipe_tenant(conn, shop=SHOP, channel=CHANNEL)
        conn.execute("INSERT INTO shops (id, name) VALUES (%s, 'I8 Test Shop')", (SHOP,))
        try:
            yield conn
        finally:
            wipe_tenant(conn, shop=SHOP, channel=CHANNEL)


@pytest.fixture
def svc_b(migrator: psycopg.Connection) -> Iterator[psycopg.Connection]:
    with psycopg.connect(os.environ["SVC_B_DSN"], autocommit=True) as conn:
        yield conn


def _seed_budget(conn: psycopg.Connection) -> None:
    # ensure_today của CostRepo — đường provisioning, chạy bằng role runtime luôn.
    conn.execute(
        "INSERT INTO cost_budget (shop_id, budget_date, cap_tokens) "
        "VALUES (%s, CURRENT_DATE, %s) ON CONFLICT (shop_id, budget_date) DO NOTHING",
        (SHOP, CAP),
    )


def _reserve(conn: psycopg.Connection, tokens: int) -> int | None:
    row = conn.execute(
        SQL_6_5, {"shop_id": SHOP, "tokens": tokens, "trace_id": uuid.uuid4()}
    ).fetchone()
    return None if row is None else int(row[0])


def _budget(conn: psycopg.Connection) -> tuple[int, int]:
    return conn.execute(
        "SELECT reserved_tokens, actual_tokens FROM cost_budget "
        "WHERE shop_id = %s AND budget_date = CURRENT_DATE",
        (SHOP,),
    ).fetchone()


def test_reserve_within_cap(svc_b: psycopg.Connection) -> None:
    """§6.5 · trong trần: có reservation_id, reserved tăng đúng, reservation chưa release."""
    _seed_budget(svc_b)
    rid = _reserve(svc_b, 60)
    assert rid is not None
    assert _budget(svc_b) == (60, 0)
    (released,) = svc_b.execute(
        "SELECT released_at FROM cost_reservation WHERE reservation_id = %s", (rid,)
    ).fetchone()
    assert released is None


def test_reserve_over_cap_leaves_no_orphan(svc_b: psycopg.Connection) -> None:
    """I8 · vượt trần: 0 row VÀ không có reservation mồ côi, sổ không nhúc nhích."""
    _seed_budget(svc_b)
    assert _reserve(svc_b, 60) is not None
    assert _reserve(svc_b, 60) is None  # 60+60 > 100 ⇒ CTE rỗng ⇒ INSERT không chạy

    assert _budget(svc_b) == (60, 0)
    (count,) = svc_b.execute(
        "SELECT count(*) FROM cost_reservation WHERE shop_id = %s", (SHOP,)
    ).fetchone()
    assert count == 1


def test_reserve_without_budget_row_fails_closed(svc_b: psycopg.Connection) -> None:
    """§6.5 · chưa provisioning ⇒ None — im lặng không-tốn-tiền, không phải không-giới-hạn."""
    assert _reserve(svc_b, 1) is None


def test_reconcile_swaps_reserved_for_actual(svc_b: psycopg.Connection) -> None:
    """§6.5b · release + cộng token THẬT (khác số ước lượng); lần hai là no-op."""
    _seed_budget(svc_b)
    rid = _reserve(svc_b, 60)

    cur = svc_b.execute(SQL_6_5B, {"reservation_id": rid, "actual_tokens": 37})
    assert cur.rowcount == 1
    assert _budget(svc_b) == (0, 37)  # ước lượng 60 trả lại, số thật 37 vào sổ

    cur = svc_b.execute(SQL_6_5B, {"reservation_id": rid, "actual_tokens": 37})
    assert cur.rowcount == 0  # guard released_at IS NULL ⇒ double-reconcile không cộng đôi
    assert _budget(svc_b) == (0, 37)


def test_flow_a_cannot_read_cost(migrator: psycopg.Connection) -> None:
    """I2/I14 · bảng cost của A6 nằm ngoài tầm luồng A — không cần GRANT tay nào."""
    with psycopg.connect(os.environ["SVC_A_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM cost_budget").fetchall()
