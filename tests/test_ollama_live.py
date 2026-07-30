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
