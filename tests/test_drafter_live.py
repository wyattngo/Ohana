"""Live smoke — `LLMDrafter` trên Together THẬT (spec 13 D0/D1).

`@pytest.mark.live` ⇒ deselect khỏi `pytest -m 'not live'`, khỏi CI, khỏi GATE_FULL. Chạy tay:

    .venv/bin/python -m pytest tests/test_drafter_live.py -m live -q

**Vì sao live là BẮT BUỘC cho D0, không phải test thường.** `tests/test_drafter.py` tiêm fake
LLM — nó chứng minh drafter RÁP prompt đúng và PARSE structured đúng, KHÔNG chứng minh model
THẬT tuân chỉ dẫn "không nói mình là AI". Cả lớp bug này (model lộ danh tính, model không gọi
`emit_reply`, tool-calling shape đổi) chỉ hiện khi có gói tin thật đi ra. Cùng lý do
`agent/persona.py:38` và `tests/test_together_live.py`.

Cần `TOGETHER_API_KEY` (live) + Postgres (fresh_db). Thiếu key ⇒ SKIP, không FAIL.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from agent.drafter import INTENT_CODES, LLMDrafter
from db.repos import ShopProfileRepo

pytestmark = pytest.mark.live

# Danh tính KHÔNG được lộ trong câu trả lời gửi khách. Cố ý KHÔNG match bare "ai" — trong tiếng
# Việt "ai" nghĩa là "who", sẽ false-positive. Match cụm/nhãn cụ thể thay vì token mơ hồ.
_LEAK = re.compile(
    r"ohana|trợ lý ảo|chatbot|\bbot\b|trí tuệ nhân tạo|mô hình ngôn ngữ|openai|together|llama|gpt",
    re.IGNORECASE,
)


def _require_key() -> None:
    if not os.environ.get("TOGETHER_API_KEY", "").strip():
        pytest.skip("TOGETHER_API_KEY chưa set — live smoke bỏ qua (không phải lỗi)")


async def _seed(engine: Any, sf: Any, *, persona: str) -> str:
    shop = f"shop_{uuid.uuid4().hex[:12]}"
    async with engine.begin() as c:
        await c.execute(
            sa.text("insert into shops (id, name, status) values (:i, :n, 'active')"),
            {"i": shop, "n": "Shop Aspecies"},
        )
    async with sf() as s:
        await ShopProfileRepo(s, shop_scope=shop).upsert(persona_md=persona, knowledge={})
        await s.commit()
    return shop


@pytest.mark.asyncio
async def test_drafter_real_model_structured_and_no_identity_leak(fresh_db) -> None:
    """Model thật: gọi `emit_reply`, trả `(text, intent∈enum, 0..1)`, KHÔNG lộ danh tính.

    Nếu model trả content thay vì gọi `emit_reply`, `LLMDrafter.draft` raise ValueError và test
    này đỏ — đó là tín hiệu ĐÚNG: nghĩa là cần ép `tool_choice`, một quyết định phải thấy trên
    output thật chứ không đoán từ fake.
    """
    _require_key()
    from agent.providers.together_client import TogetherClient

    engine, sf = await fresh_db()
    shop = await _seed(
        engine,
        sf,
        persona="Shop thời trang nữ Aspecies. Xưng 'shop', thân thiện, trả lời ngắn gọn.",
    )

    result = await LLMDrafter(TogetherClient(), sf).draft(
        shop_id=shop,
        customer_id="cust_live",
        message="shop ơi còn áo thun không ạ?",
        history=[],
    )

    assert result.text, "draft rỗng"
    assert result.intent in INTENT_CODES, f"intent {result.intent!r} ngoài enum"
    assert 0.0 <= result.confidence <= 1.0, f"confidence {result.confidence} ngoài [0,1]"
    leak = _LEAK.search(result.text)
    assert leak is None, f"draft lộ danh tính qua {leak.group(0)!r}: {result.text!r}"

    # In ra để chép vào docs/smokes/13-D0.md (OBSERVED thật, không viết "OK").
    print(
        f"\n[SMOKE 13-D0] intent={result.intent} confidence={result.confidence}\n"
        f"[SMOKE 13-D0] text={result.text!r}"
    )


@pytest.mark.asyncio
async def test_drafter_grounds_size_from_tool_not_guess(fresh_db) -> None:
    """D1: model tra `lookup_size` THẬT rồi trả size từ tool, KHÔNG đoán.

    Bảng size seed dùng token đặc biệt `XL3` cho 160cm/50kg — một size không bao giờ model tự
    đoán ra. Draft chứa `XL3` ⇒ nó ĐÃ gọi tool + đọc result (grounded). Không chứa ⇒ model
    đoán bừa, và đó là failure mode "Fact hallucination" D1 phải chặn.
    """
    _require_key()
    from agent.providers.together_client import TogetherClient
    from tools.shop_kb import build_size_tool

    engine, sf = await fresh_db()
    shop = f"shop_{uuid.uuid4().hex[:12]}"
    async with engine.begin() as c:
        await c.execute(
            sa.text("insert into shops (id, name, status) values (:i, :n, 'active')"),
            {"i": shop, "n": "Shop Aspecies"},
        )
    async with sf() as s:
        await ShopProfileRepo(s, shop_scope=shop).upsert(
            persona_md="Shop thời trang nữ Aspecies. Xưng 'shop', thân thiện.",
            knowledge={
                "size_chart": [
                    {
                        "size": "XL3",
                        "height_min_cm": 155,
                        "height_max_cm": 165,
                        "weight_min_kg": 45,
                        "weight_max_kg": 55,
                    }
                ]
            },
        )
        await s.commit()

    result = await LLMDrafter(TogetherClient(), sf, tools=[build_size_tool(sf)]).draft(
        shop_id=shop,
        customer_id="cust_live",
        message="Mình cao 160cm nặng 50kg thì shop tư vấn mặc size nào ạ?",
        history=[],
    )

    assert result.text, "draft rỗng"
    assert result.intent in INTENT_CODES
    assert _LEAK.search(result.text) is None, f"lộ danh tính: {result.text!r}"
    assert "XL3" in result.text, (
        f"draft KHÔNG chứa size từ tool (XL3) ⇒ model đoán thay vì grounded: {result.text!r}"
    )

    print(
        f"\n[SMOKE 13-D1] intent={result.intent} confidence={result.confidence}\n"
        f"[SMOKE 13-D1] text={result.text!r}"
    )
