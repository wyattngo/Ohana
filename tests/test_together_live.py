"""Live smoke — Together API THẬT (spec 07 §10, scope-extension G1 2026-07-19).

`@pytest.mark.live` ⇒ bị deselect khỏi `pytest -m 'not live'`, tức khỏi CI và khỏi mọi
GATE_FULL. Chạy tay khi cần:

    .venv/bin/python -m pytest tests/test_together_live.py -m live -q

**Vì sao file này tồn tại.** G0 qua 3 vòng review, 90 test xanh, mypy sạch — và vẫn ship một
client gọi Together bằng `gpt-4o-mini`. Không có gì bắt được, vì mọi test đều tiêm fake client
và fake không quan tâm model id có thật hay không. Cả lớp lỗi này — model id sai, endpoint
sai, key hết hạn, response shape đổi — chỉ hiện ra khi có gói tin thật đi ra ngoài.

Test ở đây cố ý ÍT và RẺ: một lượt hỏi đáp ngắn, `max_tokens` nhỏ. Mục tiêu là "đường ống có
thông không", không phải đo chất lượng model.

Cần `TOGETHER_API_KEY` trong env (hoặc `.env` đã export). Thiếu key ⇒ SKIP, không FAIL: người
không có key vẫn chạy được suite mà không thấy đỏ giả.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live


def _require_key() -> str:
    key = os.environ.get("TOGETHER_API_KEY", "").strip()
    if not key:
        pytest.skip("TOGETHER_API_KEY chưa set — live smoke bỏ qua (không phải lỗi)")
    return key


@pytest.mark.asyncio
async def test_together_answers_a_real_request() -> None:
    """Một lượt hỏi đáp thật: model id hợp lệ, key hợp lệ, response có nội dung + usage.

    Đây là test lẽ ra phải tồn tại từ G0. Nó bắt được CHÍNH XÁC lỗi đã lọt: nếu
    `_default_model` rơi về model của provider khác, Together trả 404 và test này đỏ ngay.
    """
    _require_key()
    from agent.providers.together_client import TogetherClient

    client = TogetherClient()
    step = await client.step(
        [
            {"role": "system", "content": "Trả lời cực ngắn bằng tiếng Việt."},
            {"role": "user", "content": "Nói đúng một từ: xin chào"},
        ],
        max_tokens=32,
    )

    assert step.content, "Together trả về nội dung rỗng"
    assert step.usage, "thiếu usage — không đo được cost"
    assert step.usage.get("prompt_tokens", 0) > 0
    assert step.usage.get("completion_tokens", 0) > 0


@pytest.mark.asyncio
async def test_configured_model_actually_exists_on_together() -> None:
    """Model id trong cấu hình phải là model Together THẬT SỰ phục vụ.

    Tách khỏi test trên để khi đỏ thì biết ngay hỏng ở đâu: hỏng đây = sai model id (cấu hình);
    hỏng test trên = sai key/endpoint/response shape (kết nối).
    """
    _require_key()
    import openai

    from agent.providers.together_client import TogetherClient

    client = TogetherClient()
    try:
        await client.step([{"role": "user", "content": "hi"}], max_tokens=8)
    except openai.NotFoundError as exc:  # pragma: no cover - chỉ chạy khi cấu hình sai
        pytest.fail(
            f"model {client._default_model!r} không tồn tại trên Together — "
            f"kiểm tra TOGETHER_MODEL trong .env. Lỗi gốc: {type(exc).__name__}"
        )
