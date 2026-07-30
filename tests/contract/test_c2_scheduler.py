"""C2 · debounce claim đúng-một-lần + reaper R1–R4 — gate của A7 (design §6.3 §6.9 §6.10 · I13).

C2 (PRE-010): N scheduler cùng thấy một conversation đến hạn ⇒ ĐÚNG MỘT bên claim được ⇒
1 draft. Cơ chế là `debounce_claimed_at IS NULL` trong WHERE của §6.3 — hai connection
thật cùng bắn câu claim, Postgres serialize UPDATE trên row, bên thua nhận 0 row.

R1–R4 (I13): mọi claim phải có reaper gỡ. Mỗi test mô phỏng "worker chết giữa chừng"
bằng cách backdate mốc thời gian (bằng migrator — svc_seller không việc gì phải UPDATE
được đồng hồ), rồi chạy câu reaper bằng role runtime và chứng minh hệ tự hồi.

SQL §6.3/§6.9/§6.10 chép NGUYÊN VĂN từ design (đổi placeholder + tên theo ánh xạ) — cố ý
KHÔNG import từ `db/repos.py`, cùng lý do test_c1: contract test canh HÌNH DẠNG câu lệnh.

Cần 4 biến env DSN như test_i14 — xem SETUP.md §4.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.types.json import Jsonb

_REQUIRED = ("MIGRATOR_DSN", "SVC_A_DSN", "SVC_B_DSN", "MCP_RO_DSN")

pytestmark = pytest.mark.skipif(
    any(not os.environ.get(k) for k in _REQUIRED),
    reason="cần 4 DSN role — xem SETUP.md §4",
)

CHANNEL = "c2test"
SHOP = "c2test_shop"
CUSTOMER = "c2test_customer"
CONVERSATION = "c2test_conversation"

SQL_6_3 = """
UPDATE conversations SET debounce_claimed_at = now()
 WHERE id = %(conversation_id)s
   AND next_debounce_at <= now()
   AND debounce_claimed_at IS NULL
RETURNING id
"""

SQL_6_9 = """
UPDATE conversations SET debounce_claimed_at = NULL
 WHERE debounce_claimed_at < now() - interval '5 minutes'
"""

SQL_6_10 = """
WITH rel AS (
  UPDATE cost_reservation SET released_at = now()
   WHERE released_at IS NULL AND created_at < now() - interval '5 minutes'
  RETURNING shop_id, budget_date, tokens
)
UPDATE cost_budget b
   SET reserved_tokens = GREATEST(0, b.reserved_tokens - r.tokens)
  FROM rel r
 WHERE b.shop_id = r.shop_id AND b.budget_date = r.budget_date
"""

SQL_R1 = """
UPDATE pending_reply SET status = 'expired'
 WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < now()
"""

SQL_R2 = """
UPDATE outbox SET status = 'pending'
 WHERE status = 'processing' AND claimed_at < now() - interval '5 minutes'
"""


def _wipe(conn: psycopg.Connection) -> None:
    conn.execute(
        "DELETE FROM outbox WHERE event_id IN "
        "(SELECT event_id FROM webhook_event_log WHERE channel = %s)",
        (CHANNEL,),
    )
    conn.execute("DELETE FROM webhook_event_log WHERE channel = %s", (CHANNEL,))
    conn.execute("DELETE FROM cost_reservation WHERE shop_id = %s", (SHOP,))
    conn.execute("DELETE FROM cost_budget WHERE shop_id = %s", (SHOP,))
    conn.execute("DELETE FROM pending_reply WHERE shop_id = %s", (SHOP,))
    conn.execute("DELETE FROM messages WHERE shop_id = %s", (SHOP,))
    conn.execute("DELETE FROM conversations WHERE shop_id = %s", (SHOP,))
    conn.execute("DELETE FROM customers WHERE shop_id = %s", (SHOP,))
    conn.execute("DELETE FROM shops WHERE id = %s", (SHOP,))


@pytest.fixture
def migrator() -> Iterator[psycopg.Connection]:
    """Dọn/seed/backdate bằng migrator — svc_seller không việc gì phải sửa được đồng hồ."""
    with psycopg.connect(os.environ["MIGRATOR_DSN"], autocommit=True) as conn:
        _wipe(conn)
        conn.execute("INSERT INTO shops (id, name) VALUES (%s, 'C2 Test Shop')", (SHOP,))
        conn.execute(
            "INSERT INTO customers (id, shop_id, channel, external_id) "
            "VALUES (%s, %s, %s, 'ext-1')",
            (CUSTOMER, SHOP, CHANNEL),
        )
        conn.execute(
            "INSERT INTO conversations (id, shop_id, customer_id, channel) VALUES (%s, %s, %s, %s)",
            (CONVERSATION, SHOP, CUSTOMER, CHANNEL),
        )
        try:
            yield conn
        finally:
            _wipe(conn)


@pytest.fixture
def svc_b(migrator: psycopg.Connection) -> Iterator[psycopg.Connection]:
    with psycopg.connect(os.environ["SVC_B_DSN"], autocommit=True) as conn:
        yield conn


def _arm_debounce_past(migrator: psycopg.Connection) -> None:
    """Timer đã đến hạn từ 1 giây trước — trạng thái 'đến hạn compose'."""
    migrator.execute(
        "UPDATE conversations SET next_debounce_at = now() - interval '1 second', "
        "debounce_trace_id = %s WHERE id = %s",
        (uuid.uuid4(), CONVERSATION),
    )


def test_c2_two_schedulers_one_claim(migrator: psycopg.Connection) -> None:
    """C2 · hai 'worker' (hai connection thật) cùng claim ⇒ đúng một bên thắng."""
    _arm_debounce_past(migrator)
    with (
        psycopg.connect(os.environ["SVC_B_DSN"], autocommit=True) as worker_1,
        psycopg.connect(os.environ["SVC_B_DSN"], autocommit=True) as worker_2,
    ):
        first = worker_1.execute(SQL_6_3, {"conversation_id": CONVERSATION}).fetchone()
        second = worker_2.execute(SQL_6_3, {"conversation_id": CONVERSATION}).fetchone()
    assert first is not None
    assert second is None  # bên thua nhận 0 row ⇒ bỏ qua ⇒ đúng 1 draft


def test_r3_reaper_frees_stuck_claim(
    migrator: psycopg.Connection, svc_b: psycopg.Connection
) -> None:
    """I13/R3 · claim rồi 'worker chết' ⇒ §6.9 gỡ ⇒ conversation claim lại được."""
    _arm_debounce_past(migrator)
    assert svc_b.execute(SQL_6_3, {"conversation_id": CONVERSATION}).fetchone() is not None

    # Treo dưới 5' ⇒ reaper KHÔNG đụng (không gỡ claim của worker đang sống).
    assert svc_b.execute(SQL_6_9).rowcount == 0

    # Worker chết 6' trước — backdate mốc claim bằng migrator.
    migrator.execute(
        "UPDATE conversations SET debounce_claimed_at = now() - interval '6 minutes' WHERE id = %s",
        (CONVERSATION,),
    )
    assert svc_b.execute(SQL_6_9).rowcount == 1
    # Timer vẫn còn (chưa compose) ⇒ hệ tự hồi: claim lại được ngay, không im lặng vĩnh viễn.
    assert svc_b.execute(SQL_6_3, {"conversation_id": CONVERSATION}).fetchone() is not None


def test_r4_reaper_releases_stuck_reservation(
    migrator: psycopg.Connection, svc_b: psycopg.Connection
) -> None:
    """I13/R4 · reserve rồi LLM treo quá 5' ⇒ §6.10 release ⇒ reserved_tokens về đúng."""
    svc_b.execute(
        "INSERT INTO cost_budget (shop_id, budget_date, cap_tokens) VALUES (%s, CURRENT_DATE, 100)",
        (SHOP,),
    )
    svc_b.execute(
        "WITH upd AS (UPDATE cost_budget SET reserved_tokens = reserved_tokens + 60 "
        "WHERE shop_id = %(shop)s AND budget_date = CURRENT_DATE "
        "AND reserved_tokens + actual_tokens + 60 <= cap_tokens "
        "RETURNING shop_id, budget_date) "
        "INSERT INTO cost_reservation (shop_id, budget_date, tokens, trace_id) "
        "SELECT shop_id, budget_date, 60, %(trace)s FROM upd",
        {"shop": SHOP, "trace": uuid.uuid4()},
    )

    # Chưa quá 5' ⇒ R4 không đụng — reservation của lượt LLM đang chạy phải được yên.
    svc_b.execute(SQL_6_10)
    (reserved,) = svc_b.execute(
        "SELECT reserved_tokens FROM cost_budget WHERE shop_id = %s", (SHOP,)
    ).fetchone()
    assert reserved == 60

    migrator.execute(
        "UPDATE cost_reservation SET created_at = now() - interval '6 minutes' WHERE shop_id = %s",
        (SHOP,),
    )
    svc_b.execute(SQL_6_10)
    reserved, released = svc_b.execute(
        "SELECT b.reserved_tokens, r.released_at IS NOT NULL FROM cost_budget b "
        "JOIN cost_reservation r ON r.shop_id = b.shop_id "
        "WHERE b.shop_id = %s",
        (SHOP,),
    ).fetchone()
    assert (reserved, released) == (0, True)

    # Double-release: chạy lại không kéo âm (GREATEST) và không release gì thêm.
    svc_b.execute(SQL_6_10)
    (reserved,) = svc_b.execute(
        "SELECT reserved_tokens FROM cost_budget WHERE shop_id = %s", (SHOP,)
    ).fetchone()
    assert reserved == 0


def test_r2_reaper_requeues_stuck_outbox(
    migrator: psycopg.Connection, svc_b: psycopg.Connection
) -> None:
    """I13/R2 · outbox kẹt processing quá 5' ⇒ về pending — đóng lỗ đã khai ở A5."""
    svc_b.execute(
        "WITH ins AS (INSERT INTO webhook_event_log "
        "(channel, platform_msg_id, shop_id, raw_event, trace_id) "
        "VALUES (%(channel)s, 'r2-msg', %(shop)s, %(raw)s, %(trace)s) "
        "ON CONFLICT (channel, platform_msg_id) DO NOTHING "
        "RETURNING event_id, shop_id, trace_id) "
        "INSERT INTO outbox (event_id, shop_id, payload, trace_id) "
        "SELECT event_id, shop_id, %(payload)s, trace_id FROM ins",
        {
            "channel": CHANNEL,
            "shop": SHOP,
            "raw": Jsonb({}),
            "payload": Jsonb({}),
            "trace": uuid.uuid4(),
        },
    )
    svc_b.execute(
        "UPDATE outbox SET status='processing', claimed_at=now(), attempts=attempts+1 "
        "WHERE shop_id = %s",
        (SHOP,),
    )

    assert svc_b.execute(SQL_R2).rowcount == 0  # đang trong hạn — worker có thể còn sống

    migrator.execute(
        "UPDATE outbox SET claimed_at = now() - interval '6 minutes' WHERE shop_id = %s",
        (SHOP,),
    )
    assert svc_b.execute(SQL_R2).rowcount == 1
    (status,) = svc_b.execute("SELECT status FROM outbox WHERE shop_id = %s", (SHOP,)).fetchone()
    assert status == "pending"  # attempts giữ nguyên — §6.2 mới là chỗ đếm lần thử


def test_r1_reaper_expires_overdue_drafts(
    migrator: psycopg.Connection, svc_b: psycopg.Connection
) -> None:
    """I13/R1 · draft quá TTL ⇒ expired; draft không TTL (chưa wire) ⇒ để yên."""
    for reply_id, expires in (("r1-overdue", "now() - interval '1 minute'"), ("r1-nottl", "NULL")):
        svc_b.execute(
            "INSERT INTO pending_reply (reply_id, shop_id, conversation_id, customer_id, "
            "draft_text, intent, confidence, trace_id, expires_at) "
            f"VALUES (%s, %s, %s, %s, 'x', 'faq', 0.5, %s, {expires})",
            (reply_id, SHOP, CONVERSATION, CUSTOMER, uuid.uuid4()),
        )

    assert svc_b.execute(SQL_R1).rowcount == 1
    rows = dict(
        svc_b.execute(
            "SELECT reply_id, status FROM pending_reply WHERE shop_id = %s", (SHOP,)
        ).fetchall()
    )
    assert rows == {"r1-overdue": "expired", "r1-nottl": "pending"}


# Câu finish của repo (KHÔNG phải SQL nguyên văn design — đây là test HÀNH VI, khớp
# db/repos.py::_FINISH_DEBOUNCE): timer chỉ bị xoá khi VẪN là giá trị đã claim (echo).
SQL_FINISH = """
UPDATE conversations
   SET debounce_claimed_at = NULL,
       next_debounce_at = CASE WHEN next_debounce_at = %(due_at)s
                               THEN NULL ELSE next_debounce_at END
 WHERE id = %(conversation_id)s
"""


def test_finish_preserves_timer_set_mid_compose(
    migrator: psycopg.Connection, svc_b: psycopg.Connection
) -> None:
    """Review A5-A8 #1 · tin đến GIỮA lúc compose dời timer — finish không được nuốt nó,
    kể cả khi timer mới cũng đã quá hạn lúc finish (compose LLM thường > 5s)."""
    _arm_debounce_past(migrator)
    (due_at,) = svc_b.execute(
        "SELECT next_debounce_at FROM conversations WHERE id = %s", (CONVERSATION,)
    ).fetchone()
    assert svc_b.execute(SQL_6_3, {"conversation_id": CONVERSATION}).fetchone() is not None

    # Tin B đến giữa lúc compose: outbox loop dời timer. Mô phỏng ca ÁC nhất — timer mới
    # cũng đã quá hạn tại thời điểm finish (compose lâu hơn DEBOUNCE_DELAY_SECONDS).
    migrator.execute(
        "UPDATE conversations SET next_debounce_at = now() - interval '1 millisecond', "
        "debounce_trace_id = %s WHERE id = %s",
        (uuid.uuid4(), CONVERSATION),
    )

    svc_b.execute(SQL_FINISH, {"conversation_id": CONVERSATION, "due_at": due_at})
    new_timer, claimed = svc_b.execute(
        "SELECT next_debounce_at, debounce_claimed_at FROM conversations WHERE id = %s",
        (CONVERSATION,),
    ).fetchone()
    assert claimed is None  # claim thả
    assert new_timer is not None  # timer của tin B SỐNG — sẽ được compose ở lượt sau

    # Đối chứng: không có tin mới ⇒ finish với đúng echo xoá timer như trước.
    (due_at2,) = svc_b.execute(
        "SELECT next_debounce_at FROM conversations WHERE id = %s", (CONVERSATION,)
    ).fetchone()
    svc_b.execute(SQL_6_3, {"conversation_id": CONVERSATION})
    svc_b.execute(SQL_FINISH, {"conversation_id": CONVERSATION, "due_at": due_at2})
    (cleared,) = svc_b.execute(
        "SELECT next_debounce_at FROM conversations WHERE id = %s", (CONVERSATION,)
    ).fetchone()
    assert cleared is None
