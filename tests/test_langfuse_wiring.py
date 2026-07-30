"""Gate wiring Langfuse (I16 + I5b) — trace nằm DƯỚI PII filter, sink không sập đường chính.

I16 nói "Langfuse chỉ nhận Scrubbed". Cơ chế cưỡng chế KHÔNG phải type mà là VỊ TRÍ:
`default_llm_client()` bọc `PIIFilteringClient( TracingClient( provider ) )` — messages
tới sink đã qua `redact()`. Test ở đây khoá đúng ba thứ dễ vỡ khi ai đó refactor stack:

1. Sink không bao giờ thấy PII thô (thứ tự bọc đúng).
2. Sink nổ ⇒ call vẫn trả kết quả (trace là phụ trợ, không phải đường sống).
3. Factory: không key ⇒ KHÔNG có tầng tracing; đủ key ⇒ có (và vẫn PII ngoài cùng).

I5 (import `langfuse` chỉ trong providers) đã có lint-imports canh — không test lại.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.llm_client import (
    AssistantStep,
    ChatMessage,
    GenerationRecord,
    LLMClient,
    TracingClient,
)

_RAW_PHONE = "0912345678"  # khớp pattern `phone` của agent/pii.py — bị thay bằng [SĐT]


class _FakeInner(LLMClient):
    """Provider giả — trả lời cố định, ghi lại messages nó nhận."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[list[ChatMessage]] = []

    async def stream(self, messages, **kw: Any):  # type: ignore[no-untyped-def, override]
        self.seen.append(messages)
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        yield "ok"

    async def complete(self, messages, **kw: Any) -> str:  # type: ignore[no-untyped-def, override]
        self.seen.append(messages)
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        return "ok"

    async def step(self, messages, **kw: Any) -> AssistantStep:  # type: ignore[no-untyped-def, override]
        self.seen.append(messages)
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        return AssistantStep(content="ok", usage=self.last_usage)


class _CollectingSink:
    def __init__(self) -> None:
        self.records: list[GenerationRecord] = []

    def record(self, gen: GenerationRecord) -> None:
        self.records.append(gen)


class _ExplodingSink:
    def record(self, gen: GenerationRecord) -> None:
        raise RuntimeError("sink chết — không được kéo theo đường chính")


def _msgs(text: str) -> list[ChatMessage]:
    return [{"role": "user", "content": text}]


@pytest.mark.asyncio
async def test_sink_never_sees_raw_pii() -> None:
    """I16 qua vị trí stack: PII ngoài, trace trong ⇒ sink chỉ thấy text đã redact.

    Đây là test giữ THỨ TỰ BỌC của `default_llm_client()` — nếu ai đảo
    `TracingClient(PIIFilteringClient(...))` thì đỏ ngay tại đây.
    """
    from agent.pii_client import PIIFilteringClient

    inner = _FakeInner()
    sink = _CollectingSink()
    client = PIIFilteringClient(TracingClient(inner, sink))

    await client.complete(_msgs(f"sđt em là {_RAW_PHONE} nhé"))

    assert len(sink.records) == 1
    recorded = str(sink.records[0].input_messages)
    assert _RAW_PHONE not in recorded, "sink thấy PII thô — thứ tự bọc stack sai (I16)"
    assert "[SĐT]" in recorded, "không thấy vết redact — PII filter không chạy trước sink?"
    # Provider (dưới tracer) cũng chỉ được thấy bản redact — bất biến sẵn có, khoá luôn.
    assert _RAW_PHONE not in str(inner.seen[0])


@pytest.mark.asyncio
async def test_exploding_sink_does_not_break_the_call() -> None:
    client = TracingClient(_FakeInner(), _ExplodingSink())
    assert await client.complete(_msgs("xin chào")) == "ok"


@pytest.mark.asyncio
async def test_tracing_records_all_ops_and_mirrors_usage() -> None:
    inner = _FakeInner()
    sink = _CollectingSink()
    client = TracingClient(inner, sink)

    await client.complete(_msgs("a"))
    async for _ in client.stream(_msgs("b")):
        pass
    await client.step(_msgs("c"))
    async for _ in client.step_stream(_msgs("d")):
        pass

    assert [r.op for r in sink.records] == ["complete", "stream", "step", "step_stream"]
    assert all(r.usage and r.usage["total_tokens"] == 2 for r in sink.records)
    # `last_usage` phải mirror xuyên tầng — orchestrator đọc từ client NGOÀI cùng.
    assert client.last_usage == inner.last_usage


@pytest.mark.asyncio
async def test_provider_error_is_recorded_then_reraised() -> None:
    class _Boom(_FakeInner):
        async def complete(self, messages, **kw: Any) -> str:  # type: ignore[no-untyped-def, override]
            raise ValueError("provider nổ")

    sink = _CollectingSink()
    client = TracingClient(_Boom(), sink)
    with pytest.raises(ValueError, match="provider nổ"):
        await client.complete(_msgs("x"))
    assert sink.records[0].error is not None, "lượt hỏng phải có record kèm error"


def test_factory_without_keys_has_no_tracing_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.llm_client import default_llm_client
    from agent.pii_client import PIIFilteringClient
    from app.config import get_settings

    monkeypatch.setenv("TOGETHER_API_KEY", "sk-test-fake")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    try:
        client = default_llm_client()
        assert isinstance(client, PIIFilteringClient)
        assert not isinstance(client._inner, TracingClient), (  # noqa: SLF001
            "không key mà vẫn có tầng tracing — default_trace_sink phải trả None"
        )
    finally:
        get_settings.cache_clear()


def test_factory_with_keys_wraps_pii_outside_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Đủ key ⇒ stack đúng ba tầng PII → Trace → provider. KHÔNG gọi mạng: chỉ dựng object
    (Langfuse SDK v2 không validate key lúc __init__ — gửi nền theo batch)."""
    from agent.llm_client import default_llm_client
    from agent.pii_client import PIIFilteringClient
    from app.config import get_settings

    monkeypatch.setenv("TOGETHER_API_KEY", "sk-test-fake")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:9")  # cổng chết — không gửi được gì
    get_settings.cache_clear()
    try:
        client = default_llm_client()
        assert isinstance(client, PIIFilteringClient), "PII phải là tầng NGOÀI CÙNG (I16)"
        assert isinstance(client._inner, TracingClient)  # noqa: SLF001
    finally:
        get_settings.cache_clear()
