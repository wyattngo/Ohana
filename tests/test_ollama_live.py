"""Live smoke — server Ollama local THẬT (cùng lý do tồn tại với test_together_live.py:
fake client không quan tâm model id/endpoint có thật hay không; cả lớp lỗi đó chỉ hiện
khi có gói tin thật). `@pytest.mark.live` ⇒ deselect khỏi CI. Chạy tay:

    pytest tests/test_ollama_live.py -m live -q

Server không chạy / model chưa pull ⇒ SKIP, không FAIL — máy không cài Ollama vẫn chạy
được suite mà không thấy đỏ giả.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.live


def _require_server() -> None:
    from app.config import get_settings

    base = get_settings().ollama_base_url.removesuffix("/v1")
    try:
        with urllib.request.urlopen(f"{base}/api/version", timeout=3):  # noqa: S310
            pass
    except (urllib.error.URLError, OSError):
        pytest.skip("server Ollama không chạy — live smoke bỏ qua (không phải lỗi)")


@pytest.mark.asyncio
async def test_ollama_answers_a_real_request() -> None:
    """Một lượt hỏi đáp thật qua wire OpenAI-compat: model đã pull, response có nội dung
    + usage (Ollama báo usage qua /v1 — CostRepo reconcile được token thật)."""
    _require_server()
    from agent.providers.ollama_client import OllamaClient

    client = OllamaClient()
    step = await client.step(
        [
            {"role": "system", "content": "Answer in one short word."},
            {"role": "user", "content": "Say exactly: hello"},
        ],
        max_tokens=32,
    )

    assert step.content, "Ollama trả về nội dung rỗng"
    assert step.usage, "thiếu usage — không đo được cost"
    assert step.usage.get("prompt_tokens", 0) > 0
    assert step.usage.get("completion_tokens", 0) > 0


@pytest.mark.asyncio
async def test_ollama_model_can_call_tools() -> None:
    """Tiêu chí THẬT cho đường drafter: model đang cấu hình phải gọi được tool qua wire
    OpenAI-compat — `LLMDrafter` bắt buộc `emit_reply` là tool call, model không
    tool-capable (codellama...) sẽ trả content trần và drafter fail-loud.

    Test đỏ ⇒ OLLAMA_MODEL đang trỏ model không dùng được cho drafter, đổi model đi
    (qwen2.5/llama3.1+), đừng sửa drafter."""
    _require_server()
    from agent.providers.ollama_client import OllamaClient

    tool = {
        "name": "get_stock",
        "description": "Look up remaining stock for a product. Use this for stock questions.",
        "parameters": {
            "type": "object",
            "properties": {"product": {"type": "string", "description": "product name"}},
            "required": ["product"],
        },
    }
    client = OllamaClient()
    step = await client.step(
        [
            {"role": "system", "content": "Use the provided tools to answer."},
            {"role": "user", "content": "How many 'ao thun trang' are left in stock?"},
        ],
        tools=[tool],
        max_tokens=256,
    )

    assert step.tool_calls, (
        f"model không gọi tool (content={step.content!r}) — OLLAMA_MODEL hiện tại "
        "không dùng được cho drafter, đổi sang model tool-capable"
    )
    assert step.tool_calls[0].name == "get_stock"
    assert "product" in step.tool_calls[0].arguments
