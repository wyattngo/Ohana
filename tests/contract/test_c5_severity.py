"""C5 · severity rank tất định + CHECK escalation_reasons — gate của A8 (design §9 · I10).

Ba tầng, hai tầng đầu KHÔNG cần DB (chạy cả khi thiếu DSN):

1. C5 thuần: kết quả của `decide()` chỉ phụ thuộc CỜ NÀO bật — hoán vị/tổ hợp cho cùng
   một list đã sắp theo SEVERITY_RANK. Đây là tính chất "hoán vị rule ⇒ cùng kết quả".
2. I10 cấu trúc: policy_gate KHÔNG còn khái niệm auto-send — không export nhánh, không
   threshold. Test soi bằng attr/source để một lần "khôi phục tiện tay" đỏ ngay ở CI.
3. DB CHECK (cần DSN, chạy bằng role thật): reason lạ bị `escalation_reasons_known` từ
   chối ở tầng INSERT — typo không có cửa vào training set (w§8.1).
"""

from __future__ import annotations

import inspect
import itertools
import os
import uuid
from collections.abc import Iterator
from dataclasses import fields

import psycopg
import pytest
from conftest import requires_dsn, seed_tenant, wipe_tenant

from agent import policy_gate
from agent.policy_gate import SEVERITY_RANK, DraftContext, decide

# ── 1 · C5 thuần ─────────────────────────────────────────────────────────────────────────

_FLAG_FIELDS = [f.name for f in fields(DraftContext) if f.name != "intent"]


def test_all_flag_combinations_are_rank_ordered() -> None:
    """Mọi tổ hợp cờ (2^6, kèm intent nhạy cảm hoặc không) ⇒ output đúng thứ tự rank."""
    for intent in ("faq", "refund"):
        for combo in itertools.product([False, True], repeat=len(_FLAG_FIELDS)):
            ctx = DraftContext(intent=intent, **dict(zip(_FLAG_FIELDS, combo, strict=True)))
            reasons = decide(ctx).escalation_reasons
            assert reasons == [r for r in SEVERITY_RANK if r in set(reasons)]
            assert len(reasons) == len(set(reasons))  # không trùng


def test_rank_beats_declaration_order() -> None:
    """Cùng một tập cờ, khai theo thứ tự nào cũng cùng kết quả — rank quyết, không phải
    thứ tự viết. `cost_cap` bật 'trước' vẫn xếp sau `sensitive_intent`."""
    ctx = DraftContext(intent="refund", cost_cap_hit=True, injection_detected=True)
    assert decide(ctx).escalation_reasons == [
        "sensitive_intent",
        "injection_attempt",
        "cost_cap",
    ]


def test_plain_draft_has_no_reasons() -> None:
    """Không cờ nào bật ⇒ mảng rỗng — draft thường, vẫn park, chỉ không cần ưu tiên."""
    assert decide(DraftContext(intent="faq")).escalation_reasons == []


def test_rank_matches_db_check_values() -> None:
    """SEVERITY_RANK và CHECK §5.5 phải là cùng MỘT tập bảy giá trị."""
    assert set(SEVERITY_RANK) == {
        "sensitive_intent",
        "injection_attempt",
        "data_unavailable",
        "media_content",
        "window_closed",
        "cost_cap",
        "window_unknown",
    }


# ── 2 · I10 cấu trúc ─────────────────────────────────────────────────────────────────────


def test_no_auto_send_code_path() -> None:
    """A8 xoá nhánh tự gửi — khôi phục lại (dù đổi tên nhẹ) phải đỏ ở đây trước khi kịp
    thành production bug. Soi module attrs + body của `decide` (docstring MODULE được phép
    nhắc tên nhánh cũ — để cấm nó)."""
    assert not hasattr(policy_gate, "DEFAULT_CONFIDENCE_THRESHOLD")
    assert "action" not in {f.name for f in fields(policy_gate.GateResult)}
    decide_src = inspect.getsource(policy_gate.decide).lower()
    assert "auto_send" not in decide_src
    assert "threshold" not in decide_src


# ── 3 · DB CHECK (cần DSN) ───────────────────────────────────────────────────────────────

SHOP = "c5test_shop"
CUSTOMER = "c5test_customer"
CONVERSATION = "c5test_conversation"
CHANNEL = "c5test"


@pytest.fixture
def svc_b() -> Iterator[psycopg.Connection]:
    with psycopg.connect(os.environ["MIGRATOR_DSN"], autocommit=True) as mig:
        wipe_tenant(mig, shop=SHOP, channel=CHANNEL)
        seed_tenant(mig, shop=SHOP, customer=CUSTOMER, conversation=CONVERSATION, channel=CHANNEL)
        try:
            with psycopg.connect(os.environ["SVC_B_DSN"], autocommit=True) as conn:
                yield conn
        finally:
            wipe_tenant(mig, shop=SHOP, channel=CHANNEL)


def _insert_draft(conn: psycopg.Connection, reply_id: str, reasons: list[str]) -> None:
    conn.execute(
        "INSERT INTO pending_reply (reply_id, shop_id, conversation_id, customer_id, "
        "draft_text, intent, confidence, trace_id, escalation_reasons) "
        "VALUES (%s, %s, %s, %s, 'x', 'faq', 0.5, %s, %s)",
        (reply_id, SHOP, CONVERSATION, CUSTOMER, uuid.uuid4(), reasons),
    )


@requires_dsn
def test_db_check_rejects_unknown_reason(svc_b: psycopg.Connection) -> None:
    """CHECK escalation_reasons_known · giá trị hợp lệ vào, typo bị DB từ chối."""
    _insert_draft(svc_b, "c5-ok", ["sensitive_intent", "cost_cap"])

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_draft(svc_b, "c5-typo", ["sensitive_intents"])  # thừa 's' — đúng loại typo thật


@requires_dsn
def test_db_check_accepts_full_rank(svc_b: psycopg.Connection) -> None:
    """TOÀN BỘ SEVERITY_RANK phải qua CHECK của DB THẬT — đây mới là test chống drift
    code↔DB (review A5-A8): rename một reason trong rank + model mà migration không đổi
    ⇒ đỏ ở đây, không phải CheckViolation ở compose production."""
    _insert_draft(svc_b, "c5-all", list(SEVERITY_RANK))
