"""Policy-gate escalation ranking (spec 01 Sub-task E → A8 · design C5 · I10).

Pure function tests — no DB, no HTTP. A8: auto_send + threshold đã xoá cùng nhánh code —
I10. `decide()` không còn trả `action`: phase 1 mọi draft đều PARK, gate chỉ biến ngữ
cảnh draft thành `escalation_reasons` sắp theo `SEVERITY_RANK` tất định. Rỗng = draft
thường (vẫn park), không rỗng = seller cần nhìn trước.

Test cũ đã XOÁ vì code path tương ứng không tồn tại nữa (A8: auto_send + threshold đã
xoá cùng nhánh code — I10):
  - test_low_confidence_parks_even_for_safe_intent (confidence không còn gate gì)
  - test_auto_disabled_for_intent_parks_even_at_high_confidence (shop opt-in đã xoá)
  - test_high_confidence_safe_intent_auto_enabled_sends (nhánh auto_send đã xoá)
  - test_default_threshold_conservative (DEFAULT_CONFIDENCE_THRESHOLD đã xoá)

Ý định GIỮ từ suite cũ: sensitive intent (4 mã) LUÔN được flag — trước đây là "always
park", giờ là "always mang reason `sensitive_intent`" — cùng một lưới an toàn spec §4.
"""

from __future__ import annotations

import pytest

from agent.policy_gate import (
    SENSITIVE_INTENTS,
    SEVERITY_RANK,
    DraftContext,
    decide,
)


def test_sensitive_intent_always_flagged() -> None:
    """Blocklist là bất biến sống sót từ suite cũ: 4 mã nhạy cảm LUÔN phát reason
    `sensitive_intent`, kể cả khi mọi cờ khác tắt. Regress ở đây nghĩa là một khiếu
    nại/hoàn tiền trôi xuống đáy inbox như draft thường."""
    for intent in ("complaint", "refund", "price_negotiation", "specific_order"):
        result = decide(DraftContext(intent=intent))
        assert result.escalation_reasons == ["sensitive_intent"], (
            f"{intent} phải phát đúng ['sensitive_intent'], got {result.escalation_reasons!r}"
        )


def test_safe_intent_no_flags_yields_empty_reasons() -> None:
    """Intent thường + không cờ nào bật ⇒ reasons rỗng. Rỗng KHÔNG nghĩa là gửi —
    phase 1 vẫn park — chỉ là seller không cần ưu tiên."""
    result = decide(DraftContext(intent="general_qa"))
    assert result.escalation_reasons == []


def test_multiple_flags_ordered_by_severity_rank() -> None:
    """Nhiều cờ bật ⇒ reasons sắp ĐÚNG thứ tự SEVERITY_RANK (C5: thứ tự là dữ liệu,
    không phụ thuộc thứ tự ai kiểm tra trước). Bật TẤT CẢ ⇒ đúng nguyên bảng rank."""
    all_on = decide(
        DraftContext(
            intent="refund",  # sensitive
            injection_detected=True,
            data_unavailable=True,
            has_media=True,
            window_closed=True,
            window_unknown=True,
            cost_cap_hit=True,
        )
    )
    assert all_on.escalation_reasons == list(SEVERITY_RANK)

    # Tập con không kề nhau trong rank — vẫn giữ thứ tự rank, không phải thứ tự khai cờ.
    subset = decide(DraftContext(intent="general_qa", cost_cap_hit=True, injection_detected=True))
    assert subset.escalation_reasons == ["injection_attempt", "cost_cap"]


def test_severity_rank_is_exhaustive_and_deterministic() -> None:
    """SEVERITY_RANK là tuple 7 giá trị khớp CHECK `escalation_reasons_known` (§5.5).
    Tuple (không set): THỨ TỰ là nội dung của C5 — đổi thứ tự là đổi hành vi sort inbox."""
    assert isinstance(SEVERITY_RANK, tuple)
    assert len(SEVERITY_RANK) == 7
    assert len(set(SEVERITY_RANK)) == 7, "rank có giá trị trùng — dedup sẽ mơ hồ"
    assert SEVERITY_RANK[0] == "sensitive_intent", "an toàn/tin cậy phải đứng đầu rank"


def test_sensitive_intents_frozen() -> None:
    """The blocklist is a frozenset — attempts to mutate it at runtime raise. Prevents a
    later refactor from swapping in a Python set that a stray line could `.discard()`."""
    assert isinstance(SENSITIVE_INTENTS, frozenset)
    assert {"complaint", "refund", "price_negotiation", "specific_order"} <= SENSITIVE_INTENTS
    with pytest.raises(AttributeError):
        SENSITIVE_INTENTS.add("safe_intent")  # type: ignore[attr-defined]
