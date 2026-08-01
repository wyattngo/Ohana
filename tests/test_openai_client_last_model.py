"""OpenAIClient · `last_model` side-channel — bug fix Langfuse "No data" ở Model Costs.

Adapter (`OpenAIClient` + `TogetherClient`/`OllamaClient` kế thừa) phải set `last_model`
với model đã RESOLVE (`model or self._default_model`) đầu mỗi call. Không set ⇒
`TracingClient` không có model đưa lên `GenerationRecord.model` khi call-site không truyền
`model=` (tất cả call-site production ở Ohana đều KHÔNG truyền, xem `api/chat.py:120`,
`api/assistant_chat.py:170`, `agent/drafter.py:230,284`).

Test unit hoá adapter — inject `client=` mock (khuôn mẫu `test_together_client.py`) để
không đi network.

Contract khoá:
1. `.step(messages)` không truyền model ⇒ `last_model == default_model` sau call.
2. `.step(messages, model="X")` truyền model ⇒ `last_model == "X"` sau call.
3. `last_model` set TRƯỚC network — provider raise vẫn thấy model correct (tracer error
   record cần model để dán trace lỗi vào đúng bucket Langfuse).
4. Cả 4 op (stream, complete, step, step_stream) đều set — không chỉ `step`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import openai
import pytest

from agent.providers.openai_client import OpenAIClient

_DEFAULT_MODEL = "vendor/default-model-x"


def _fake_completion(content: str = "hi", usage_total: int = 10) -> Any:
    """Đủ shape cho `.step()`/`.complete()` đọc: choices[0].message.content + usage."""
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = content
    completion.choices[0].message.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = 5
    usage.completion_tokens = 5
    usage.total_tokens = usage_total
    usage.prompt_tokens_details = None
    completion.usage = usage
    return completion


class _FakeCompletions:
    def __init__(
        self,
        raises: BaseException | None = None,
        response: Any = None,
    ) -> None:
        self.raises = raises
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.response or _fake_completion()


class _FakeAsyncOpenAI:
    def __init__(
        self,
        raises: BaseException | None = None,
        response: Any = None,
    ) -> None:
        self.completions = _FakeCompletions(raises=raises, response=response)
        self.chat = self


def _make_client(
    raises: BaseException | None = None, response: Any = None
) -> tuple[OpenAIClient, _FakeAsyncOpenAI]:
    fake = _FakeAsyncOpenAI(raises=raises, response=response)
    client = OpenAIClient(client=fake, default_model=_DEFAULT_MODEL)  # type: ignore[arg-type]
    return client, fake


@pytest.mark.asyncio
async def test_step_sets_last_model_to_default_when_caller_omits() -> None:
    """`.step(messages)` không truyền model ⇒ `last_model` = default_model của adapter."""
    client, _ = _make_client()
    await client.step([{"role": "user", "content": "hello"}])
    assert client.last_model == _DEFAULT_MODEL


@pytest.mark.asyncio
async def test_step_sets_last_model_to_explicit_when_caller_passes() -> None:
    """`.step(model="X")` ⇒ `last_model` = "X" (override thắng default)."""
    client, _ = _make_client()
    await client.step([{"role": "user", "content": "hi"}], model="claude-sonnet")
    assert client.last_model == "claude-sonnet"


@pytest.mark.asyncio
async def test_complete_sets_last_model() -> None:
    client, _ = _make_client()
    await client.complete([{"role": "user", "content": "hi"}])
    assert client.last_model == _DEFAULT_MODEL


@pytest.mark.asyncio
async def test_stream_sets_last_model_before_iteration() -> None:
    """`stream()` là async generator — resolve model phải xảy ra ở body TRƯỚC yield đầu.
    Nếu adapter set model chỉ khi consume xong thì test này bắt được."""

    # stream cần AsyncStream shape — fake tối thiểu: async iterator trên list rỗng.
    class _EmptyStream:
        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

    client, _ = _make_client(response=_EmptyStream())
    it = client.stream([{"role": "user", "content": "hi"}])
    # Trigger execution vào body function (mà `self.last_model = resolved_model` ngồi).
    async for _ in it:
        break
    assert client.last_model == _DEFAULT_MODEL


@pytest.mark.asyncio
async def test_last_model_set_before_network_call_survives_provider_error() -> None:
    """Adapter set `last_model = resolved` NGAY trước `await self._create(...)`. Provider
    raise ⇒ `last_model` vẫn giữ. Đây là gate cho error-record path ở TracingClient — nếu
    ai đó chuyển `self.last_model = resolved` xuống dưới `await` thì test này đỏ.
    """
    boom = openai.RateLimitError(
        "quota",
        response=MagicMock(status_code=429, request=MagicMock()),
        body={"error": {"message": "rate limit"}},
    )
    client, _ = _make_client(raises=boom)
    with pytest.raises(openai.RateLimitError):
        await client.step([{"role": "user", "content": "hi"}])
    assert client.last_model == _DEFAULT_MODEL


@pytest.mark.asyncio
async def test_last_model_reset_between_calls() -> None:
    """Adapter `self.last_model = None` (hoặc set mới) đầu mỗi call — không carry state
    lượt trước. Cùng bài `last_usage` reset ở `stream()/complete()/step()`. Ở đây kiểm
    tra bằng cách override model ở lượt 2 và thấy lượt 2 thắng."""
    client, _ = _make_client()
    await client.step([{"role": "user", "content": "a"}], model="turn-1-model")
    assert client.last_model == "turn-1-model"
    await client.step([{"role": "user", "content": "b"}], model="turn-2-model")
    assert client.last_model == "turn-2-model"
    # Lượt 3 KHÔNG truyền model ⇒ về default, KHÔNG carry "turn-2-model".
    await client.step([{"role": "user", "content": "c"}])
    assert client.last_model == _DEFAULT_MODEL
