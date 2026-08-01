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


_DEFAULT_MODEL = "fake-default-model-x"


class _FakeInner(LLMClient):
    """Provider giả — trả lời cố định, ghi lại messages nó nhận. Mô phỏng adapter thật:
    set `last_model = model or default` đầu mỗi call (khớp OpenAIClient contract), để
    TracingClient đọc được model đã RESOLVE thay vì tham số `None` từ wrapper."""

    def __init__(self, default_model: str = _DEFAULT_MODEL) -> None:
        super().__init__()
        self.seen: list[list[ChatMessage]] = []
        self._default_model = default_model

    async def stream(self, messages, *, model=None, **kw: Any):  # type: ignore[no-untyped-def, override]
        self.seen.append(messages)
        self.last_model = model or self._default_model
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        yield "ok"

    async def complete(self, messages, *, model=None, **kw: Any) -> str:  # type: ignore[no-untyped-def, override]
        self.seen.append(messages)
        self.last_model = model or self._default_model
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        return "ok"

    async def step(self, messages, *, model=None, **kw: Any) -> AssistantStep:  # type: ignore[no-untyped-def, override]
        self.seen.append(messages)
        self.last_model = model or self._default_model
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


@pytest.mark.asyncio
async def test_tracing_records_resolved_model_when_caller_omits_it() -> None:
    """Bug đã tồn tại pre-fix: call-site `.step(messages)` KHÔNG truyền `model=`, wrapper
    có `model=None` ở signature, `TracingClient` ghi `GenerationRecord.model=model` (=None).
    Sink Langfuse group "Model Costs" theo tên model — `None` không match ⇒ "No data".

    Fix: adapter set `self.last_model = resolved` đầu mỗi call, `TracingClient` đọc từ
    `self._inner.last_model` thay cho tham số. Test này bắt regress bằng cách gọi cả 4 op
    KHÔNG truyền `model=` và assert record có model non-None và bằng default của inner."""
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
    # Trước fix: model=None ở tất cả record. Sau fix: model = last_model của adapter.
    assert all(r.model == _DEFAULT_MODEL for r in sink.records), (
        f"model=None trong record: Langfuse sẽ hiện 'No data' ở Model Costs. "
        f"Records: {[(r.op, r.model) for r in sink.records]}"
    )
    # `last_model` cũng phải mirror xuyên tầng (cùng bài `last_usage`).
    assert client.last_model == inner.last_model == _DEFAULT_MODEL


@pytest.mark.asyncio
async def test_tracing_records_explicit_model_when_caller_passes_it() -> None:
    """Caller truyền `model="claude-x"` ⇒ adapter set `last_model = "claude-x"`, tracer
    echo. Đảm bảo fix không hardcode default — override thứ nhất vẫn thắng."""
    inner = _FakeInner()
    sink = _CollectingSink()
    client = TracingClient(inner, sink)

    await client.step(_msgs("hi"), model="claude-sonnet-explicit")

    assert sink.records[0].model == "claude-sonnet-explicit"
    assert client.last_model == "claude-sonnet-explicit"


@pytest.mark.asyncio
async def test_provider_error_still_records_resolved_model() -> None:
    """Adapter set `last_model` TRƯỚC await. Provider raise ⇒ record error vẫn ghi model
    thật, không mất traceability cho traces lỗi. Test này khoá đúng vị trí `self.last_model
    = resolved_model` phải NẰM TRƯỚC `await self._create(...)` trong adapter."""

    class _BoomAfterModelSet(_FakeInner):
        async def step(  # type: ignore[override]
            self, messages, *, model=None, **kw: Any
        ) -> AssistantStep:
            # Mô phỏng đúng adapter: set last_model trước, rồi raise ở "network".
            self.last_model = model or self._default_model
            raise RuntimeError("provider timeout")

    sink = _CollectingSink()
    client = TracingClient(_BoomAfterModelSet(), sink)
    with pytest.raises(RuntimeError, match="provider timeout"):
        await client.step(_msgs("x"))
    assert sink.records[0].error is not None
    assert sink.records[0].model == _DEFAULT_MODEL, (
        "record error path phải giữ model — nếu None thì trace lỗi mất context"
    )


@pytest.mark.asyncio
async def test_last_model_mirrored_through_pii_wrapper() -> None:
    """`PIIFilteringClient(TracingClient(inner))` — contract mirror xuyên tầng. Consumer
    bên ngoài (orchestrator, endpoint) đọc `client.last_model` phải nhận value thật, không
    None. Cùng bài `last_usage` mirror sẵn có."""
    from agent.pii_client import PIIFilteringClient

    inner = _FakeInner()
    client = PIIFilteringClient(TracingClient(inner, _CollectingSink()))
    await client.step(_msgs("hello"))
    assert client.last_model == _DEFAULT_MODEL


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


# =====================================================================================
# start_time / end_time on GenerationRecord — fix Langfuse "End Time trống + Latency null".
# =====================================================================================


@pytest.mark.asyncio
async def test_tracing_records_start_and_end_time_around_await() -> None:
    """TracingClient stamp `started_at` TRƯỚC `await inner`, `ended_at` SAU. Record có
    thời điểm THẬT của LLM call, không phải thời điểm gọi `record()` hay `sink.record`.

    Trước fix: `GenerationRecord` không có 2 field này ⇒ Langfuse SDK default cả
    start/end = thời điểm gọi `.generation()` ⇒ span 0-duration ⇒ UI Latency null.
    Test này khoá contract: nếu ai bỏ start/end khỏi dataclass hoặc quên stamp trong
    tracer, đỏ ngay.
    """
    import asyncio
    from datetime import UTC, datetime

    class _SlowInner(_FakeInner):
        async def step(self, messages, *, model=None, **kw: Any):  # type: ignore[no-untyped-def, override]
            self.last_model = model or self._default_model
            await asyncio.sleep(0.05)  # ép span > 0
            self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            return AssistantStep(content="ok", usage=self.last_usage)

    inner = _SlowInner()
    sink = _CollectingSink()
    client = TracingClient(inner, sink)

    before = datetime.now(UTC)
    await client.step(_msgs("hi"))
    after = datetime.now(UTC)

    assert len(sink.records) == 1
    rec = sink.records[0]
    # Cả hai stamp là datetime aware UTC.
    assert rec.started_at.tzinfo is not None
    assert rec.ended_at.tzinfo is not None
    # started_at ∈ [before, after], ended_at ∈ [started_at, after].
    assert before <= rec.started_at <= after
    assert rec.started_at <= rec.ended_at <= after
    # Span thật sự > 0 (sleep 50ms).
    assert (rec.ended_at - rec.started_at).total_seconds() >= 0.04


@pytest.mark.asyncio
async def test_all_ops_record_start_and_end_time() -> None:
    """Cả 4 op (complete/stream/step/step_stream) phải stamp start_time + end_time.

    Không stamp ở 1 op = trace op đó thiếu latency trên UI. Test cover đủ 4 để không
    ai lỡ tay bỏ ở 1 nhánh khi refactor."""
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
    for r in sink.records:
        assert r.started_at is not None, f"op={r.op} thiếu started_at"
        assert r.ended_at is not None, f"op={r.op} thiếu ended_at"
        assert r.started_at <= r.ended_at


@pytest.mark.asyncio
async def test_provider_error_path_still_records_start_and_end_time() -> None:
    """Adapter raise ⇒ tracer vẫn ghi record kèm start/end. Nếu ai đặt ended_at chỉ
    ở success path (quên ở except), record error path sẽ mất latency.
    """
    from datetime import UTC, datetime

    class _Boom(_FakeInner):
        async def complete(self, messages, *, model=None, **kw: Any) -> str:  # type: ignore[no-untyped-def, override]
            self.last_model = model or self._default_model
            raise ValueError("provider timeout")

    sink = _CollectingSink()
    client = TracingClient(_Boom(), sink)
    before = datetime.now(UTC)
    with pytest.raises(ValueError, match="provider timeout"):
        await client.complete(_msgs("x"))
    after = datetime.now(UTC)

    assert sink.records[0].error is not None
    assert before <= sink.records[0].started_at <= after
    assert sink.records[0].started_at <= sink.records[0].ended_at <= after


def test_langfuse_sink_passes_timestamps_to_generation() -> None:
    """`LangfuseSink.record()` phải pass `start_time`/`end_time` vào `trace.generation(...)`.
    Không pass ⇒ SDK v2 default cả hai = now ⇒ span 0-duration.

    Fake Langfuse client — chỉ capture kwargs của trace() + generation() để assert."""
    from datetime import UTC, datetime

    from agent.llm_client import GenerationRecord
    from agent.providers.langfuse_tracer import LangfuseSink

    captured_gen: dict = {}
    captured_trace: dict = {}

    class _FakeGeneration:
        def __init__(self, **kwargs: Any) -> None:
            captured_gen.update(kwargs)

    class _FakeTrace:
        def __init__(self, **kwargs: Any) -> None:
            captured_trace.update(kwargs)

        def generation(self, **kwargs: Any) -> _FakeGeneration:
            return _FakeGeneration(**kwargs)

    class _FakeLangfuse:
        def trace(self, **kwargs: Any) -> _FakeTrace:
            return _FakeTrace(**kwargs)

    sink = LangfuseSink.__new__(LangfuseSink)
    sink._client = _FakeLangfuse()  # type: ignore[attr-defined]  # noqa: SLF001

    t0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 8, 1, 12, 0, 5, tzinfo=UTC)
    rec = GenerationRecord(
        op="step",
        model="fake-model",
        input_messages=[{"role": "user", "content": "hi"}],
        output="hello",
        started_at=t0,
        ended_at=t1,
        usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    )
    sink.record(rec)

    assert captured_trace.get("start_time") == t0
    assert captured_gen.get("start_time") == t0
    assert captured_gen.get("end_time") == t1
    # Sanity: usage vẫn passed đúng shape camelCase Langfuse expect.
    assert captured_gen["usage"]["totalTokens"] == 10
