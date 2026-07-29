"""Tra cứu kiến thức shop — TẤT ĐỊNH, KHÔNG đi RAG (spec 11 S2; D8/D9 đã ký).

**Vì sao size chart / bảng ship không đi vector store** — lý do AN TOÀN, không phải tối ưu:

1. `parsing/chunk.py` cắt ở `max_chars=800` sẽ cắt **giữa bảng**, chunk mất cột. Một chunk
   "155-165cm | 45-55kg" tách khỏi dòng tiêu đề "size M" là dữ liệu vô nghĩa.
2. Cosine similarity giữa `"1m6 50kg"` và một chunk toàn số gần như ngẫu nhiên.
3. Quan trọng nhất: **RAG không bao giờ nói "không biết"**. Nó luôn trả k chunk gần nhất, kể
   cả khi shop chưa khai gì — và AI sẽ trả lời khách bằng chunk gần nhất đó một cách tự tin.
   Hàm tất định trả `not_found` dứt khoát, và chính tín hiệu đó là thứ confidence-gated
   escalation (`GD0-INTENT`) cần để chuyển cho người thật.

⇒ `not_found` ở đây là **yêu cầu an toàn**, không phải chi tiết API. Nó luôn đi kèm
`success: True`: thiếu data là một câu trả lời hợp lệ ("shop chưa khai"), không phải lỗi hạ
tầng. Trộn hai thứ đó vào `success: False` sẽ làm tầng escalation không phân biệt được
"chưa biết" với "hỏng", và hai ca đó cần hai cách xử lý khác nhau.

**`shop_id` KHÔNG bao giờ nằm trong `parameters`.** Nó tới từ tham số hàm, do orchestrator
truyền xuống từ một `Identity` đã verify (xem `tools/registry.ToolHandler`). Nếu nó lọt vào
JSON schema thì LLM điền được — và model sẽ làm vậy một cách vô tình ngay khi khách nhắc tên
một shop khác trong câu hỏi.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import ShopKnowledge
from db.repos import ShopProfileRepo
from tools.registry import Tool


async def _knowledge(session: AsyncSession, shop_id: str) -> ShopKnowledge | None:
    """Đọc knowledge của shop, scope ở tầng repo. None khi shop chưa có profile.

    Parse lại bằng `ShopKnowledge` thay vì tin dict thô trong DB: row có thể được ghi từ một
    phiên bản schema cũ hơn. Validate lúc GHI (S0) là hàng rào chính; đây là hàng rào thứ hai
    cho dữ liệu đã nằm sẵn trong bảng từ trước.
    """
    profile = await ShopProfileRepo(session, shop_scope=shop_id).get()
    if profile is None:
        return None
    return ShopKnowledge.model_validate(profile.knowledge)


async def lookup_size(
    session: AsyncSession, *, shop_id: str, height_cm: int, weight_kg: int
) -> dict[str, Any]:
    """Chiều cao + cân nặng → size, theo bảng của ĐÚNG shop này.

    Khoảng ĐÓNG hai đầu (`min <= x <= max`) — biên là chỗ seller hay hiểu nhầm nhất, và
    khoảng mở sẽ tạo ra những cặp số không match dòng nào dù bảng trông như phủ hết.

    Không có dòng nào khớp ⇒ `not_found`, KHÔNG chọn dòng "gần nhất". Đoán size sai làm khách
    đổi/trả hàng, và chi phí đó thuộc về shop — nên khi không chắc thì nói không biết.
    """
    know = await _knowledge(session, shop_id)
    if know is None or not know.size_chart:
        return {"success": True, "result": "not_found", "reason": "shop chưa khai bảng size"}

    for rule in know.size_chart:
        if (
            rule.height_min_cm <= height_cm <= rule.height_max_cm
            and rule.weight_min_kg <= weight_kg <= rule.weight_max_kg
        ):
            return {"success": True, "result": rule.size}

    return {
        "success": True,
        "result": "not_found",
        "reason": f"{height_cm}cm/{weight_kg}kg không nằm trong khoảng nào của bảng size",
    }


async def lookup_shipping(session: AsyncSession, *, shop_id: str, zone: str) -> dict[str, Any]:
    """Khu vực → phí ship + thời gian giao, theo bảng của ĐÚNG shop này.

    So khớp không phân biệt hoa/thường và bỏ khoảng trắng thừa — khách gõ "q7", "Q7 ", "Q7"
    là cùng một ý. KHÔNG khớp mờ hơn thế: "Quận 7" vs "Q7" là hai chuỗi khác nhau và đoán
    bừa ở đây nghĩa là báo sai phí, tức shop chịu chênh lệch.
    """
    know = await _knowledge(session, shop_id)
    if know is None or not know.shipping_zones:
        return {"success": True, "result": "not_found", "reason": "shop chưa khai bảng ship"}

    needle = zone.strip().casefold()
    for z in know.shipping_zones:
        if z.zone.strip().casefold() == needle:
            return {
                "success": True,
                "result": "found",
                "zone": z.zone,
                "fee_vnd": z.fee_vnd,
                "eta_days": z.eta_days,
            }

    return {
        "success": True,
        "result": "not_found",
        "reason": f"shop chưa khai phí ship cho khu vực {zone!r}",
    }


_SIZE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "height_cm": {"type": "integer", "description": "Chiều cao khách, đơn vị cm."},
        "weight_kg": {"type": "integer", "description": "Cân nặng khách, đơn vị kg."},
    },
    "required": ["height_cm", "weight_kg"],
    # `False` là bắt buộc, không phải cho gọn: nó chặn LLM nhét thêm field — kể cả `shop_id`.
    "additionalProperties": False,
}

_SHIPPING_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "zone": {"type": "string", "description": "Khu vực giao hàng, ví dụ 'Q7', 'Hà Nội'."},
    },
    "required": ["zone"],
    "additionalProperties": False,
}


def build_size_tool(session_factory: async_sessionmaker[AsyncSession]) -> Tool:
    """Bọc `lookup_size` thành `Tool`. `shop_id` tới từ orchestrator, KHÔNG từ LLM."""

    async def handler(user_id: str, shop_id: str, args: dict[str, Any]) -> dict[str, Any]:
        async with session_factory() as session:
            return await lookup_size(
                session,
                shop_id=shop_id,
                height_cm=int(args["height_cm"]),
                weight_kg=int(args["weight_kg"]),
            )

    return Tool(
        name="lookup_size",
        description="Tra size áo/quần theo chiều cao và cân nặng, dùng bảng size của shop.",
        parameters=_SIZE_PARAMETERS,
        handler=handler,
        kind="read",
    )


def build_shipping_tool(session_factory: async_sessionmaker[AsyncSession]) -> Tool:
    """Bọc `lookup_shipping` thành `Tool`. `shop_id` tới từ orchestrator, KHÔNG từ LLM."""

    async def handler(user_id: str, shop_id: str, args: dict[str, Any]) -> dict[str, Any]:
        async with session_factory() as session:
            return await lookup_shipping(session, shop_id=shop_id, zone=str(args["zone"]))

    return Tool(
        name="lookup_shipping",
        description="Tra phí ship và thời gian giao theo khu vực, dùng bảng phí của shop.",
        parameters=_SHIPPING_PARAMETERS,
        handler=handler,
        kind="read",
    )
