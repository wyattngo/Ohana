"""Cổng chính sách draft (spec 01 Sub-task E → A8 · design C5 · I10).

**Phase 1 KHÔNG có nhánh tự gửi — mọi draft đều park chờ seller duyệt.** I10 được cưỡng
chế bằng KHÔNG TỒN TẠI code path: `decide()` không trả `action`, không có threshold, và
orchestrator không nhận sender. Khôi phục nhánh AUTO_SEND là vi phạm bị cấm tường minh
(design §10) — mở lại chỉ khi w§8.1 có label thật + noise floor ≥85%.

Việc của gate bây giờ: biến ngữ cảnh draft thành `escalation_reasons: list[str]` — lý do
draft này cần seller chú ý — sắp theo severity rank TẤT ĐỊNH. Inbox sort trên field này
(§7: ESCALATE lên đầu), và `CHECK escalation_reasons_known` ở DB (§5.5) từ chối mọi giá
trị ngoài danh sách — typo ở đây nổ lúc INSERT, không đầu độc training set (w§8.1).

`SEVERITY_RANK` chính là bảng precedence cũ, thăng cấp thành dữ liệu (C5): kết quả chỉ
phụ thuộc CỜ NÀO bật, không phụ thuộc thứ tự ai kiểm tra trước — hoán vị rule cho cùng
output, và `tests/contract/test_c5_severity.py` canh đúng tính chất đó.
"""

from __future__ import annotations

from dataclasses import dataclass

# Rank cao → thấp: an toàn/tin cậy trước, vận hành sau (Wyatt ký 2026-07-30, lượt duyệt
# A8). ⚠️ Thứ tự chốt tại A8 để có ràng buộc tất định từ đầu — B4 (rules + severity, gate
# C5 của §11 bước 4) tinh chỉnh lại khi bảng rules thật đổ bộ. Tuple, không set: THỨ TỰ
# là nội dung của C5.
#
# Bảy giá trị này khớp NGUYÊN VĂN `CHECK escalation_reasons_known` (§5.5 + migration a8)
# — thêm/bớt một bên mà quên bên kia thì contract test DB đỏ ngay.
SEVERITY_RANK: tuple[str, ...] = (
    "sensitive_intent",
    "injection_attempt",
    "data_unavailable",
    "media_content",
    "window_closed",
    "window_unknown",
    "cost_cap",
)

# Blocklist giữ nguyên từ spec 01 — frozenset để một `.discard()` lạc trong refactor sau
# này raise lúc chạy thay vì lặng lẽ chọc thủng gate.
SENSITIVE_INTENTS: frozenset[str] = frozenset(
    {"complaint", "refund", "price_negotiation", "specific_order"}
)


@dataclass(frozen=True)
class DraftContext:
    """Ngữ cảnh một draft tại thời điểm qua gate.

    Các cờ default False vì pipeline hôm nay mới cấp được `intent` — B7 wire dần từng cờ
    (data_unavailable khi API tầng-1 fail, cost_cap_hit khi §6.5 trả None, window_* từ
    §6.8, injection từ tầng PII/C0, media từ parse). Cờ chưa wire = không bao giờ bật =
    không bao giờ phát reason tương ứng — an toàn theo chiều thiếu, không theo chiều bịa.
    """

    intent: str  # code — so với SENSITIVE_INTENTS
    injection_detected: bool = False
    data_unavailable: bool = False
    has_media: bool = False
    window_closed: bool = False
    window_unknown: bool = False
    cost_cap_hit: bool = False


@dataclass(frozen=True)
class GateResult:
    # Sắp theo SEVERITY_RANK, dedup — ghi thẳng vào `pending_reply.escalation_reasons`.
    # Rỗng = draft thường: vẫn PARK (phase 1 không có nhánh nào khác), chỉ là không cần
    # seller ưu tiên.
    escalation_reasons: list[str]


def decide(ctx: DraftContext) -> GateResult:
    """Cờ ngữ cảnh → danh sách lý do escalate, thứ tự tất định theo SEVERITY_RANK.

    Map cờ rồi CHIẾU QUA RANK — không if-chain theo thứ tự viết code, nên hoán vị chỗ nào
    bật cờ trước cũng cùng kết quả (C5). `active[reason]` truy cập THẲNG (không .get):
    reason có trong RANK mà thiếu trong map ⇒ KeyError ngay lượt decide đầu tiên — desync
    giữa hai cấu trúc chết to thay vì âm thầm không bao giờ phát reason đó (review A5-A8).
    Không có nhánh trả "gửi": hàm này không quyết gửi hay không — phase 1 không ai gửi,
    nó chỉ quyết seller nhìn cái gì trước.
    """
    active = {
        "sensitive_intent": ctx.intent in SENSITIVE_INTENTS,
        "injection_attempt": ctx.injection_detected,
        "data_unavailable": ctx.data_unavailable,
        "media_content": ctx.has_media,
        "window_closed": ctx.window_closed,
        "window_unknown": ctx.window_unknown,
        "cost_cap": ctx.cost_cap_hit,
    }
    return GateResult(escalation_reasons=[r for r in SEVERITY_RANK if active[r]])
