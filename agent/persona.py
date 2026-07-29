"""Persona → system-prompt fragment (spec 11 Phase S0/S2).

**Vì sao persona là MỘT cột `persona_md`, không phải bảy cột rời** (PRE-1101, Wyatt ký
2026-07-20; thay thế D10). Bảy field rời cho phép UI bind từng ô, nhưng cả bảy đằng nào cũng
nối vào CÙNG một prompt — nên cap từng field không chặn được tổng, mà tổng mới là thứ ngân
sách token quan tâm. Một cột thì cap được ở tầng CỘT, ở đúng một chỗ, và không trôi.

**Ngân sách prompt là CHUNG, không phải của riêng module này.** Cap persona (2000) và cap
history (`agent.orchestrator.HISTORY_MAX_CHARS` = 4000) cộng lại ≈ 6000 ký tự ≈ 1800 token,
CHƯA kể system prompt nền + tool schema. Hai số này ⚠️ **chưa đo tokenizer thật**
(ISSUE-022 + ISSUE-023) — chúng suy từ ước lượng ký tự→token tiếng Việt ≈ 3.3. Đổi một
trong hai thì phải nghĩ tới cái kia; đừng nâng riêng lẻ.
"""

from __future__ import annotations

# Cap CỨNG cho phần persona ráp vào prompt. Thi hành ở BA nơi, có chủ ý:
#   - `db.repos.ShopProfileRepo.upsert` → thông báo lỗi đọc được cho người ghi
#   - CHECK constraint trên cột          → thứ raw SQL / script / psql tay không lách được
#   - hàm dưới đây                       → chốt chặn cuối trước khi vào prompt
# Ba lớp vì chúng bảo vệ ba đường vào khác nhau, không phải vì thừa.
PERSONA_MAX_CHARS = 2000


def build_persona_prompt(persona_md: str | None, *, shop_display_name: str) -> str:
    """Ráp persona của shop thành đoạn system prompt cho AI Seller.

    Hàm THUẦN: không chạm DB, không chạm LLM, không đọc config. Nhờ vậy nó test được bằng
    assertion tất định thay vì LLM-as-judge — cùng lý do D9 bắt size-chart đi hàm tra cứu
    thay vì RAG.

    `persona_md` rỗng/None ⇒ trả đoạn mặc định trung tính. KHÔNG raise: một shop chưa điền
    persona vẫn phải trả lời được khách, chỉ là bằng giọng chung. Raise ở đây sẽ biến "chưa
    cấu hình xong" thành "hỏng".

    ⚠️ **Hàm này KHÔNG đảm bảo AI không lộ danh tính.** Nó chỉ đảm bảo phần TẤT ĐỊNH —
    chuỗi ta ráp vào prompt không mang tên "Ohana" sang. Việc model có tuân chỉ dẫn hay
    không là câu hỏi hành vi, phải đo bằng `-m live` + eval trên OUTPUT thật (`GD0-DRAFTER`
    acceptance). Đừng đọc test của hàm này rộng hơn thế.
    """
    body = (persona_md or "").strip()
    if len(body) > PERSONA_MAX_CHARS:
        raise ValueError(
            f"persona_md {len(body)} ký tự, vượt cap {PERSONA_MAX_CHARS} — "
            "cắt ở tầng ghi, đừng cắt ở đây (cắt lúc build sẽ im lặng làm mất nội dung "
            "seller tưởng đã lưu)"
        )

    if not body:
        body = "Bạn trả lời khách hàng thay cho shop, ngắn gọn, lịch sự, bằng tiếng Việt."

    # Chỉ dẫn danh tính đặt SAU persona của shop: nếu seller vô tình viết gì đó mâu thuẫn,
    # dòng cuối là dòng model đọc gần câu trả lời nhất. Đây là phòng thủ mềm — gate cứng
    # nằm ở test regex trên output của `GD0-DRAFTER`.
    return (
        f'Bạn là trợ lý trả lời tin nhắn khách hàng của shop "{shop_display_name}".\n\n'
        f"{body}\n\n"
        "Luôn xưng danh là shop. TUYỆT ĐỐI không nhắc tới nền tảng, công cụ, hay nhà cung "
        "cấp AI nào đứng sau. Không nói mình là AI, bot, hay trợ lý ảo."
    )
